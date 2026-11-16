"""Resolve the native Windows desktop and route dropped TR documents."""

import ctypes
import json
import re
import uuid
from itertools import count
from pathlib import Path

from tr_core import FORMATS, LOCAL, apply_project, export_project, read_json


# GUID fields use Windows byte order in memory.
DESKTOP_ID = uuid.UUID("B4BFCC3A-DB2C-424C-B029-7FE99A87C641")
PROJECT_SUFFIX = re.compile(r"_TR(?: \(\d+\))?$", re.IGNORECASE)


def desktop_folder():
    """Return the configured desktop, including a redirected Windows desktop."""
    try:
        get_folder = ctypes.windll.shell32.SHGetKnownFolderPath
        free = ctypes.windll.ole32.CoTaskMemFree
    except (AttributeError, OSError):
        return Path.home() / "Desktop"
    get_folder.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                           ctypes.POINTER(ctypes.c_void_p))
    get_folder.restype = ctypes.c_long
    free.argtypes = (ctypes.c_void_p,)
    free.restype = None
    guid = (ctypes.c_ubyte * 16).from_buffer_copy(DESKTOP_ID.bytes_le)
    result = ctypes.c_void_p()
    try:
        status = get_folder(ctypes.byref(guid), 0, None, ctypes.byref(result))
        if status >= 0 and result.value:
            return Path(ctypes.wstring_at(result))
    finally:
        if result.value:
            free(result)
    return Path.home() / "Desktop"


def candidate_paths(folder, stem, marker, extension=""):
    for index in count(1):
        number = "" if index == 1 else f" ({index})"
        suffix = f"{marker}{number}{extension}"
        # Windows filename lengths are measured in UTF-16 code units.
        remaining = 510 - len(suffix.encode("utf-16-le"))
        visible_stem = stem.encode("utf-16-le")[:remaining].decode("utf-16-le", errors="ignore")
        path = folder / (visible_stem + suffix)
        if not path.exists() and not path.is_symlink():
            yield path


def export_dropped(source):
    source = Path(source).resolve()
    if not source.is_file() or source.suffix.lower() not in FORMATS:
        raise ValueError("한글 문서(.hwp 또는 .hwpx) 파일을 놓아 주세요.")
    desktop = desktop_folder()
    for project in candidate_paths(desktop, source.stem, "_TR"):
        try:
            status = export_project(source, project)
        except (FileExistsError, ValueError):
            if not project.exists():
                raise
        else:
            return project, status


def project_metadata(folder):
    try:
        meta = read_json(folder / LOCAL / "project.json")
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def matching_project(project_id, active_project):
    if active_project is not None:
        active_project = Path(active_project).resolve()
        meta = project_metadata(active_project)
        if meta and meta.get("project_id") == project_id:
            return active_project, meta

    matches = []
    try:
        for folder in desktop_folder().iterdir():
            if folder == active_project or not folder.is_dir():
                continue
            meta = project_metadata(folder)
            if meta and meta.get("project_id") == project_id:
                matches.append((folder, meta))
    except OSError as error:
        raise ValueError("바탕화면의 TR 프로젝트를 확인할 수 없습니다.") from error

    if not matches:
        raise ValueError("이 JSON과 일치하는 TR 프로젝트가 없습니다. "
                         "AI용.md를 내보낸 프로젝트가 바탕화면에 있는지 확인해 주세요.")
    if len(matches) > 1:
        raise ValueError("바탕화면에 같은 프로젝트의 복사본이 여러 개 있습니다. "
                         "편집에 사용할 프로젝트만 바탕화면에 남겨 주세요.")
    return matches[0]


def apply_dropped(edits, active_project=None):
    edits = Path(edits).resolve()
    if not edits.is_file() or edits.suffix.lower() != ".json":
        raise ValueError("AI에서 받은 편집 JSON 파일을 놓아 주세요.")
    try:
        request = read_json(edits)
    except json.JSONDecodeError as error:
        raise ValueError(f"편집 JSON 형식을 읽을 수 없습니다. {error.lineno}행 {error.colno}열을 확인해 주세요.") from error
    except UnicodeDecodeError as error:
        raise ValueError("편집 JSON을 UTF-8로 저장한 뒤 다시 놓아 주세요.") from error
    if not isinstance(request, dict) or not isinstance(request.get("project_id"), str) or not request["project_id"]:
        raise ValueError("편집 JSON에 TR 프로젝트 ID가 없습니다. AI용.md와 함께 만든 JSON 파일을 확인해 주세요.")

    project, meta = matching_project(request["project_id"], active_project)
    extension = meta.get("format")
    if not isinstance(extension, str) or extension not in FORMATS:
        raise ValueError("프로젝트에서 원본 한글 파일 형식을 확인할 수 없습니다.")
    source_name = meta.get("source_name")
    stem = PROJECT_SUFFIX.sub("", project.name) or project.name
    if isinstance(source_name, str) and Path(source_name).name == source_name:
        if Path(source_name).suffix.lower() == extension:
            stem = Path(source_name).stem

    for output in candidate_paths(project, stem, "_편집본", extension):
        try:
            status = apply_project(project, edits, output)
        except (FileExistsError, ValueError):
            if not output.exists():
                raise
        else:
            return project, output, status
