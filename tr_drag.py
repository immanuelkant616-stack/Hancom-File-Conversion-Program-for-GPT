"""Windows file drag and drop for the Tk interface."""

import ctypes as ct
from ctypes import wintypes as wt
from pathlib import Path
from queue import Empty, SimpleQueue
from uuid import UUID


_ole = ct.WinDLL("ole32")
_shell = ct.WinDLL("shell32")
_user = ct.WinDLL("user32")
_kernel = ct.WinDLL("kernel32")

_HRESULT = ct.c_long
_PVOID = ct.c_void_p
_S_OK = 0
_E_NOINTERFACE = _HRESULT(0x80004002).value
_E_POINTER = _HRESULT(0x80004003).value
_DRAGDROP_S_DROP = 0x00040100
_DRAGDROP_S_CANCEL = 0x00040101
_DRAGDROP_S_USEDEFAULTCURSORS = 0x00040102
_MK_LBUTTON = 1
_CF_HDROP = 15
_DVASPECT_CONTENT = 1
_TYMED_HGLOBAL = 1
_DROPEFFECT_NONE = 0
_DROPEFFECT_COPY = 1
_DROPEFFECT_MOVE = 2
_GA_ROOT = 2
_ALL_DROP_FILES = 0xFFFFFFFF
_GMEM_MOVEABLE = 2
_DROP_POLL_MS = 35


class _GUID(ct.Structure):
    _fields_ = (("data1", wt.DWORD), ("data2", wt.WORD), ("data3", wt.WORD),
                ("data4", ct.c_ubyte * 8))

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(UUID(value).bytes_le)


_IID_UNKNOWN = _GUID.parse("00000000-0000-0000-c000-000000000046")
_IID_DROP_TARGET = _GUID.parse("00000122-0000-0000-c000-000000000046")
_IID_DROP_SOURCE = _GUID.parse("00000121-0000-0000-c000-000000000046")
_IID_DATA_OBJECT = _GUID.parse("0000010e-0000-0000-c000-000000000046")
_IID_SHELL_ITEM = _GUID.parse("43826d1e-e718-42ee-bc55-a1e261c37bfe")
_BHID_DATA_OBJECT = _GUID.parse("b8c0bd9f-ed24-455c-83e6-d5390c4fe8c4")


class _FORMATETC(ct.Structure):
    _fields_ = (("cf_format", wt.WORD), ("target_device", _PVOID),
                ("aspect", wt.DWORD), ("index", wt.LONG), ("tymed", wt.DWORD))


class _STGMEDIUM(ct.Structure):
    # Every member of the native union is a pointer or handle.
    _fields_ = (("tymed", wt.DWORD), ("data", _PVOID), ("release", _PVOID))


class _POINTL(ct.Structure):
    _fields_ = (("x", wt.LONG), ("y", wt.LONG))


# Windows passes an eight-byte aggregate the same way as an integer on
# its 64-bit calling convention. Keep the native structure on 32-bit.
_POINTL_ARGUMENT = ct.c_uint64 if ct.sizeof(_PVOID) == 8 else _POINTL


def _point_coordinates(point):
    if isinstance(point, int):
        return wt.LONG(point & 0xFFFFFFFF).value, wt.LONG(point >> 32).value
    return point.x, point.y


def _api(library, name, returns, *arguments):
    function = getattr(library, name)
    function.restype = returns
    function.argtypes = arguments
    return function


_ole_initialize = _api(_ole, "OleInitialize", _HRESULT, _PVOID)
_ole_uninitialize = _api(_ole, "OleUninitialize", None)
_register_drop = _api(_ole, "RegisterDragDrop", _HRESULT, wt.HWND, _PVOID)
_revoke_drop = _api(_ole, "RevokeDragDrop", _HRESULT, wt.HWND)
_do_drag_drop = _api(_ole, "DoDragDrop", _HRESULT, _PVOID, _PVOID,
                     wt.DWORD, ct.POINTER(wt.DWORD))
_release_medium = _api(_ole, "ReleaseStgMedium", None, ct.POINTER(_STGMEDIUM))
_get_ancestor = _api(_user, "GetAncestor", wt.HWND, wt.HWND, wt.UINT)
_register_format = _api(_user, "RegisterClipboardFormatW", wt.UINT, wt.LPCWSTR)
_drag_query_file = _api(_shell, "DragQueryFileW", wt.UINT, wt.HANDLE, wt.UINT,
                        wt.LPWSTR, wt.UINT)
_create_shell_item = _api(_shell, "SHCreateItemFromParsingName", _HRESULT, wt.LPCWSTR,
                          _PVOID, ct.POINTER(_GUID), ct.POINTER(_PVOID))
_global_alloc = _api(_kernel, "GlobalAlloc", wt.HGLOBAL, wt.UINT, ct.c_size_t)
_global_lock = _api(_kernel, "GlobalLock", _PVOID, wt.HGLOBAL)
_global_unlock = _api(_kernel, "GlobalUnlock", wt.BOOL, wt.HGLOBAL)
_global_free = _api(_kernel, "GlobalFree", wt.HGLOBAL, wt.HGLOBAL)


def _check(result, action):
    if result < 0:
        raise RuntimeError(f"{action} (Windows 오류 0x{result & 0xFFFFFFFF:08X})")


def _method(interface, index, returns, *arguments):
    table = ct.cast(interface, ct.POINTER(ct.POINTER(_PVOID))).contents
    return ct.WINFUNCTYPE(returns, _PVOID, *arguments)(table[index])


def _hdrop_format():
    return _FORMATETC(_CF_HDROP, None, _DVASPECT_CONTENT, -1, _TYMED_HGLOBAL)


def _has_files(data_object):
    if not data_object:
        return False
    query = _method(data_object, 5, _HRESULT, ct.POINTER(_FORMATETC))  # QueryGetData
    return query(data_object, ct.byref(_hdrop_format())) == _S_OK


def _dropped_files(data_object):
    get_data = _method(data_object, 3, _HRESULT, ct.POINTER(_FORMATETC),
                       ct.POINTER(_STGMEDIUM))
    medium = _STGMEDIUM()
    if get_data(data_object, ct.byref(_hdrop_format()), ct.byref(medium)) != _S_OK:
        return ()
    try:
        if medium.tymed != _TYMED_HGLOBAL or not medium.data:
            return ()
        count = _drag_query_file(medium.data, _ALL_DROP_FILES, None, 0)
        paths = []
        for index in range(count):
            length = _drag_query_file(medium.data, index, None, 0)
            if length:
                path = ct.create_unicode_buffer(length + 1)
                if _drag_query_file(medium.data, index, path, length + 1):
                    paths.append(Path(path.value))
        return tuple(paths)
    finally:
        _release_medium(ct.byref(medium))


class _ComCallbacks:
    """Keep the native vtable and Python callbacks alive together."""

    _live = {}

    def __init__(self, iid, methods):
        self._iid = bytes(iid)
        self._refs = 1
        functions = (
            (_HRESULT, (ct.POINTER(_GUID), ct.POINTER(_PVOID)), self._query_interface),
            (wt.ULONG, (), self._add_ref),
            (wt.ULONG, (), self._release),
            *methods,
        )
        self._functions = [ct.WINFUNCTYPE(returns, _PVOID, *params)(handler)
                           for returns, params, handler in functions]
        self._vtable = (_PVOID * len(self._functions))(
            *(ct.cast(function, _PVOID).value for function in self._functions)
        )
        self._object = ct.pointer(self._vtable)
        self.pointer = ct.cast(ct.pointer(self._object), _PVOID)
        self._live[self.pointer.value] = self

    def _query_interface(self, this, iid, out):
        if not out:
            return _E_POINTER
        out[0] = None
        if not iid or bytes(iid.contents) not in (bytes(_IID_UNKNOWN), self._iid):
            return _E_NOINTERFACE
        out[0] = this
        self._add_ref(this)
        return _S_OK

    def _add_ref(self, _):
        self._refs += 1
        return self._refs

    def _release(self, _):
        self._refs -= 1
        return self._refs

    def close(self):
        self._release(self.pointer.value)
        self.collect_released()

    @classmethod
    def collect_released(cls):
        # Defer collection until back in Python code outside the released
        # COM callback, since its native trampoline is still returning.
        for address, callback in tuple(cls._live.items()):
            if callback._refs == 0:
                del cls._live[address]


_outgoing_drag = False


class DropReceiver:
    """Listen for physical files dropped on a Tk top-level window.

    callback receives (tuple[Path, ...], x_screen, y_screen) on the Tk thread.
    accepts is an optional screen-coordinate filter run on the Tk thread
    when delivering a queued drop. Set enabled=False to deny native drops.
    """

    def __init__(self, root, callback, accepts=None):
        self._root = root
        self._callback = callback
        self._accepts = accepts
        self._can_read = False
        self._closed = False
        self.enabled = True
        self._pending = SimpleQueue()
        self._timer = None
        root.update_idletasks()
        hwnd = root.winfo_id()
        self._hwnd = _get_ancestor(hwnd, _GA_ROOT) or hwnd
        effect = ct.POINTER(wt.DWORD)
        self._native = _ComCallbacks(_IID_DROP_TARGET, (
            (_HRESULT, (_PVOID, wt.DWORD, _POINTL_ARGUMENT, effect), self._enter),
            (_HRESULT, (wt.DWORD, _POINTL_ARGUMENT, effect), self._over),
            (_HRESULT, (), self._leave),
            (_HRESULT, (_PVOID, wt.DWORD, _POINTL_ARGUMENT, effect), self._drop),
        ))
        try:
            _check(_ole_initialize(None), "Windows 파일 끌어놓기를 초기화할 수 없습니다")
            try:
                _check(_register_drop(self._hwnd, self._native.pointer),
                       "Windows 파일 끌어놓기를 등록할 수 없습니다")
            except BaseException:
                _ole_uninitialize()
                raise
        except BaseException:
            self._native.close()
            raise
        try:
            root.bind("<Destroy>", self._destroy, add="+")
            self._timer = root.after(_DROP_POLL_MS, self._dispatch)
        except BaseException:
            self.close()
            raise

    def _effect(self, offered):
        if self._closed or not self.enabled or _outgoing_drag or not self._can_read:
            return _DROPEFFECT_NONE
        return offered & _DROPEFFECT_COPY

    def _enter(self, _, data_object, keys, point, effect):
        self._can_read = False
        try:
            self._can_read = not _outgoing_drag and _has_files(data_object)
        except Exception as error:
            self._pending.put(error)
        return self._over(None, keys, point, effect)

    def _over(self, _, _keys, _point, effect):
        effect[0] = self._effect(effect[0])
        return _S_OK

    def _leave(self, _):
        self._can_read = False
        return _S_OK

    def _drop(self, _, data_object, _keys, point, effect):
        offered = effect[0]
        effect[0] = _DROPEFFECT_NONE
        try:
            if self._effect(offered):
                paths = _dropped_files(data_object)
                if paths:
                    # OLE may call us during Tcl's Windows message dispatch.
                    # Even scheduling after_idle here would reenter Tcl.
                    self._pending.put((paths, *_point_coordinates(point)))
                    effect[0] = _DROPEFFECT_COPY
        except Exception as error:
            self._pending.put(error)
        finally:
            self._can_read = False
        return _S_OK

    def _dispatch(self):
        self._timer = None
        _ComCallbacks.collect_released()
        try:
            while not self._closed:
                try:
                    item = self._pending.get_nowait()
                except Empty:
                    break
                if isinstance(item, Exception):
                    self._root.report_callback_exception(type(item), item, item.__traceback__)
                    continue
                paths, x, y = item
                if self.enabled and (self._accepts is None or self._accepts(x, y)):
                    self._callback(paths, x, y)
        finally:
            if not self._closed:
                self._timer = self._root.after(_DROP_POLL_MS, self._dispatch)

    def _destroy(self, event):
        if event.widget is self._root:
            self.close()

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                if self._timer is not None:
                    self._root.after_cancel(self._timer)
                    self._timer = None
            finally:
                _revoke_drop(self._hwnd)
                _ole_uninitialize()
                self._native.close()


def _file_object(path):
    shell_item = _PVOID()
    _check(_create_shell_item(str(path), None, ct.byref(_IID_SHELL_ITEM), ct.byref(shell_item)),
           "끌어낼 파일을 Windows에서 찾을 수 없습니다")
    data_object = _PVOID()
    try:
        bind = _method(shell_item, 3, _HRESULT, _PVOID, ct.POINTER(_GUID),
                       ct.POINTER(_GUID), ct.POINTER(_PVOID))  # BindToHandler
        _check(bind(shell_item, None, ct.byref(_BHID_DATA_OBJECT),
                    ct.byref(_IID_DATA_OBJECT), ct.byref(data_object)),
               "끌어낼 파일을 Windows 데이터 개체로 준비할 수 없습니다")
        return data_object
    finally:
        _method(shell_item, 2, wt.ULONG)(shell_item)  # Release


def _prefer_copy(data_object):
    clipboard_format = _register_format("Preferred DropEffect")
    if not clipboard_format:
        return False
    handle = _global_alloc(_GMEM_MOVEABLE, ct.sizeof(wt.DWORD))
    if not handle:
        return False
    transferred = False
    try:
        address = _global_lock(handle)
        if not address:
            return False
        try:
            ct.cast(address, ct.POINTER(wt.DWORD))[0] = _DROPEFFECT_COPY
        finally:
            _global_unlock(handle)
        description = _FORMATETC(clipboard_format, None, _DVASPECT_CONTENT, -1, _TYMED_HGLOBAL)
        medium = _STGMEDIUM(_TYMED_HGLOBAL, handle, None)
        set_data = _method(data_object, 7, _HRESULT, ct.POINTER(_FORMATETC),
                           ct.POINTER(_STGMEDIUM), wt.BOOL)
        transferred = set_data(data_object, ct.byref(description), ct.byref(medium), True) == _S_OK
        return transferred
    finally:
        if not transferred:
            _global_free(handle)


def _continue_drag(_, escape_pressed, keys):
    if escape_pressed:
        return _DRAGDROP_S_CANCEL
    if not keys & _MK_LBUTTON:
        return _DRAGDROP_S_DROP
    return _S_OK


def _default_cursor(_, _effect):
    return _DRAGDROP_S_USEDEFAULTCURSORS


def drag_file(path, *, allow_move=False):
    """Drag a file out. Call this on the Tk thread while left is held."""
    global _outgoing_drag
    if _outgoing_drag:
        return "none"
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("끌어낼 파일이 없습니다.")
    _check(_ole_initialize(None), "Windows 파일 끌기를 초기화할 수 없습니다")
    _outgoing_drag = True
    data_object = None
    source = None
    try:
        data_object = _file_object(path)
        source = _ComCallbacks(_IID_DROP_SOURCE, (
            (_HRESULT, (wt.BOOL, wt.DWORD), _continue_drag),
            (_HRESULT, (wt.DWORD,), _default_cursor),
        ))
        effect = wt.DWORD()
        allowed = _DROPEFFECT_COPY
        if allow_move and _prefer_copy(data_object):
            allowed |= _DROPEFFECT_MOVE
        result = _do_drag_drop(data_object, source.pointer, allowed, ct.byref(effect))
        _check(result, "파일을 끌어놓는 동안 오류가 발생했습니다")
        if result != _DRAGDROP_S_DROP:
            return "none"
        # The shell may move the file itself. TR never removes the source
        # on the basis of the target's reported drop effect.
        if allow_move and not path.exists():
            return "move"
        return "copy" if effect.value & allowed else "none"
    finally:
        if data_object:
            _method(data_object, 2, wt.ULONG)(data_object)
        if source is not None:
            source.close()
        _outgoing_drag = False
        _ole_uninitialize()
