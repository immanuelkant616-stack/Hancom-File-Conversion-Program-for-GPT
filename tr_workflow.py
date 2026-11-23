"""Find Windows TR projects and route dropped documents."""

import ctypes
import json
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count
from pathlib import Path

from tr_core import (AI_DOCUMENT, FORMATS, LOCAL, TEXT_DOCUMENT, apply_project, export_project, normalize_request,
                     parse_edit_text, read_edit_request, read_json, write_json)


EDIT_TYPES = {".json", ".txt", ".zip"}
IMAGE_TYPES = {".png", ".jpg", ".jpeg"}
ZIP_LIMIT = 64 * 1024 * 1024
IMAGE_LIMIT = 12 * 1024 * 1024


# GUID fields use Windows byte order in memory.
DESKTOP_ID = uuid.UUID("B4BFCC3A-DB2C-424C-B029-7FE99A87C641")
PROJECT_SUFFIX = re.compile(r"_TR(?: \(\d+\))?$", re.IGNORECASE)
RESULT_SUFFIX = re.compile(r"_편집본(?: \(\d+\))?$", re.IGNORECASE)
PDF_SUFFIX = "_PDF변환"
ACTIVITY_LABELS = {"export": "AI용.md 생성", "apply": "편집본 저장", "pdf": "PDF 변환본 저장"}


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


def projects_folder():
    return desktop_folder() / "gpt"


def windows_filename(stem, suffix):
    # Windows filename lengths are measured in UTF-16 code units.
    remaining = 510 - len(suffix.encode("utf-16-le"))
    visible_stem = stem.encode("utf-16-le")[:remaining].decode("utf-16-le", errors="ignore")
    return visible_stem + suffix


def candidate_paths(folder, stem, marker, extension=""):
    for index in count(1):
        number = "" if index == 1 else f" ({index})"
        suffix = f"{marker}{number}{extension}"
        path = folder / windows_filename(stem, suffix)
        if not path.exists() and not path.is_symlink():
            yield path


def export_dropped(source, progress=None):
    source = Path(source).resolve()
    if source.is_file() and source.suffix.lower() == ".pdf":
        return export_pdf(source, progress)
    if not source.is_file() or source.suffix.lower() not in FORMATS:
        raise ValueError("한글 문서(.hwp 또는 .hwpx) 또는 PDF 파일을 놓아 주세요.")
    return create_project(source)


def create_project(source, *, pdf_name=None):
    stem = Path(pdf_name).stem if pdf_name else source.stem
    converted_name = windows_filename(stem, PDF_SUFFIX + ".hwp") if pdf_name else None
    pdf_origin = (pdf_name, converted_name) if converted_name else None
    folder = projects_folder()
    folder.mkdir(parents=True, exist_ok=True)
    for project in candidate_paths(folder, stem, "_TR"):
        try:
            status = export_project(source, project, pdf_origin=pdf_origin)
        except (FileExistsError, ValueError):
            if not project.exists():
                raise
        else:
            if converted_name:
                record_activity(project, "pdf", converted_name)
            record_activity(project, "export", AI_DOCUMENT)
            return project, status


def export_pdf(source, progress=None):
    """Rebuild a PDF as a Hangul document, keep that HWP in the project, and export it."""
    from tr_pdf import convert_pdf

    with tempfile.TemporaryDirectory(prefix="tr-pdf-") as temporary:
        converted = Path(temporary) / windows_filename(source.stem, ".hwp")
        conversion = convert_pdf(source, converted, progress=progress)
        project, status = create_project(converted, pdf_name=source.name)
    return project, f"{conversion} {status}"


def project_metadata(folder):
    try:
        meta = read_json(folder / LOCAL / "project.json")
    except (OSError, ValueError):
        return None
    if (not isinstance(meta, dict) or type(meta.get("version")) is not int
            or meta["version"] != 1 or not isinstance(meta.get("project_id"), str)
            or not meta["project_id"] or meta.get("format") not in (".hwp", ".hwpx")):
        return None
    return meta


def discover_projects():
    """List projects in Desktop/gpt and older projects stored directly on Desktop."""
    group = projects_folder()
    discovered = {}
    for root in (group, group.parent):
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if child == group or not child.is_dir() or not project_metadata(child):
                    continue
                project = child.resolve()
                discovered[project] = project.stat().st_mtime_ns
            except OSError:
                continue
    return sorted(discovered, key=lambda project: (-discovered[project], str(project).casefold()))


def source_stem(project, meta):
    source_name = meta.get("source_name")
    if isinstance(source_name, str) and Path(source_name).name == source_name:
        original = Path(source_name)
        if original.suffix.lower() == meta["format"]:
            return original.stem
    return PROJECT_SUFFIX.sub("", project.name) or project.name


def project_outputs(project):
    """List saved TR results for the project, most recently written first."""
    project = Path(project)
    meta = project_metadata(project)
    if meta is None:
        return []
    stem = source_stem(project, meta)
    outputs = {}
    try:
        children = list(project.iterdir())
    except OSError:
        return []
    for path in children:
        marker = RESULT_SUFFIX.search(path.stem)
        if marker is None or path.suffix.lower() not in FORMATS:
            continue
        expected = windows_filename(stem, marker.group() + path.suffix)
        if expected.casefold() != path.name.casefold():
            continue
        try:
            if path.is_file():
                outputs[path] = path.stat().st_mtime_ns
        except OSError:
            continue
    return sorted(outputs, key=lambda path: (-outputs[path], path.name.casefold()))


def pdf_conversion(project):
    """Find the generated HWP using the PDF source saved in project metadata."""
    meta = project_metadata(project) or {}
    name = meta.get("pdf_name")
    if not isinstance(name, str) or Path(name).name != name or Path(name).suffix.lower() != ".pdf":
        return None
    path = Path(project) / windows_filename(Path(name).stem, PDF_SUFFIX + ".hwp")
    return path if path.is_file() else None


def text_document(project):
    """The project's text-only Markdown; projects made before it existed get one from source.xml."""
    path = Path(project) / TEXT_DOCUMENT
    if path.is_file():
        return path
    source = Path(project) / LOCAL / "source.xml"
    if not source.is_file():
        return None
    from tr_text import write_text_from_xml

    try:
        write_text_from_xml(path, source)
    except (OSError, ValueError, SyntaxError):   # ParseError is a SyntaxError
        return None
    return path


def record_activity(project, kind, filename):
    """Keep a project event without affecting the already-saved output."""
    folder = project / LOCAL / "activity"
    timestamp = time.time_ns()
    try:
        folder.mkdir(exist_ok=True)
        write_json(folder / f"{timestamp}-{uuid.uuid4().hex}.json", {
            "timestamp_ns": timestamp, "kind": kind, "filename": filename,
        })
    except OSError:
        pass


def project_activity(project):
    """List recorded activity, including files since moved out of the project."""
    project = Path(project)
    folder = project / LOCAL / "activity"
    converted = pdf_conversion(project)
    live = {path.name.casefold(): path for path in [project / AI_DOCUMENT, converted, *project_outputs(project)]
            if path is not None and path.is_file()}
    try:
        records = list(folder.glob("*.json"))
    except OSError:
        records = []
    events = []
    for record in records:
        try:
            event = read_json(record)
            if not isinstance(event, dict):
                continue
            timestamp = event.get("timestamp_ns")
            name = event.get("filename")
            kind = event.get("kind")
            if (type(timestamp) is not int or timestamp < 0 or not isinstance(name, str)
                    or not name or Path(name).name != name or not isinstance(kind, str)
                    or kind not in ACTIVITY_LABELS):
                continue
            date = datetime.fromtimestamp(timestamp / 1_000_000_000)
        except (OSError, ValueError, OverflowError):
            continue
        events.append((date, kind, name))

    result = []
    for date, kind, name in sorted(events, reverse=True):
        path = live.pop(name.casefold(), None)
        label = ACTIVITY_LABELS[kind] if kind == "export" else f"{ACTIVITY_LABELS[kind]} · {name}"
        result.append((date, label, path))
    for path in live.values():
        try:
            date = datetime.fromtimestamp(path.stat().st_mtime)
        except (OSError, ValueError, OverflowError):
            continue
        kind = "pdf" if path == converted else "apply"
        label = ACTIVITY_LABELS["export"] if path.name == AI_DOCUMENT else f"{ACTIVITY_LABELS[kind]} · {path.name}"
        result.append((date, label, path))
    return sorted(result, key=lambda event: event[0], reverse=True)


def matching_project(project_id, active_project):
    if active_project is not None:
        active_project = Path(active_project).resolve()
        meta = project_metadata(active_project)
        if meta and meta.get("project_id") == project_id:
            return active_project, meta

    matches = []
    for folder in discover_projects():
        if folder == active_project:
            continue
        meta = project_metadata(folder)
        if meta and meta.get("project_id") == project_id:
            matches.append((folder, meta))

    if not matches:
        raise ValueError("이 JSON과 일치하는 TR 프로젝트를 찾을 수 없습니다. "
                         "다른 위치에 보관한 프로젝트는 폴더를 TR 창에 놓아 열어 주세요.")
    if len(matches) > 1:
        raise ValueError("같은 프로젝트의 복사본이 여러 개 있습니다. "
                         "기록에서 편집할 프로젝트를 선택한 뒤 JSON을 다시 놓아 주세요.")
    return matches[0]


@dataclass
class PendingEdit:
    """An edit request matched to its project, waiting for any missing pictures."""
    project: Path
    meta: dict
    request: dict
    request_path: Path
    source_label: str
    images: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    mapping: dict = field(default_factory=dict)
    auto_mapped: list = field(default_factory=list)


def _safe_image_name(name):
    name = Path(name.replace("\\", "/")).name
    if not name or ":" in name or Path(name).suffix.lower() not in IMAGE_TYPES:
        return None
    return name


def _read_zip(path):
    """Return (request, [(image name, bytes)]) from an 편집.zip an AI produced."""
    try:
        package = zipfile.ZipFile(path)
    except zipfile.BadZipFile as error:
        raise ValueError("ZIP 파일을 열 수 없습니다.") from error
    with package:
        members = [item for item in package.infolist()
                   if not item.is_dir() and not item.filename.startswith("__MACOSX/")]
        if sum(item.file_size for item in members) > ZIP_LIMIT:
            raise ValueError("ZIP 안의 파일이 너무 큽니다. 64MB 이하로 묶어 주세요.")
        requests = [item for item in members if item.filename.lower().endswith(".json")]
        if len(requests) != 1:
            raise ValueError(f"ZIP 안에는 편집 JSON이 하나 있어야 합니다. 발견 {len(requests)}개")
        data = package.read(requests[0])
        try:
            request = parse_edit_text(data.decode("utf-8-sig"))
        except UnicodeDecodeError as error:
            raise ValueError("ZIP 안의 편집 JSON을 UTF-8로 읽을 수 없습니다.") from error
        images = []
        for item in members:
            name = _safe_image_name(item.filename)
            if name and item.file_size <= IMAGE_LIMIT:
                images.append((name, package.read(item)))
        return request, images


def _referenced_assets(operations):
    assets = {}
    for operation in operations:
        if isinstance(operation, dict) and operation.get("op") in ("insert_image", "replace_image"):
            name = operation.get("asset")
            if isinstance(name, str) and name:
                note = operation.get("note") or operation.get("comment") or ""
                assets.setdefault(name, str(note))
    return assets


def prepare_edit(path=None, *, text=None, images=(), active_project=None):
    """Read an edit request from a file, ZIP or pasted text and match it to a project."""
    zip_images = []
    if text is not None:
        request, label = parse_edit_text(text), "붙여 넣은 JSON"
    else:
        path = Path(path).resolve()
        if not path.is_file() or path.suffix.lower() not in EDIT_TYPES:
            raise ValueError("AI에서 받은 편집 JSON(.json, .txt) 또는 편집.zip을 놓아 주세요.")
        if path.suffix.lower() == ".zip":
            request, zip_images = _read_zip(path)
        else:
            request = read_edit_request(path)
        label = path.name
    operations, _, _ = normalize_request(request)
    project, meta = matching_project(request["project_id"], active_project)

    received = project / LOCAL / "received" / f"{time.time_ns()}"
    received.mkdir(parents=True)
    request_path = received / "편집.json"
    write_json(request_path, request)
    provided = []
    for name, data in zip_images:
        target = received / name
        target.write_bytes(data)
        provided.append(target)
    for image in images:
        image = Path(image)
        if image.is_file() and image.suffix.lower() in IMAGE_TYPES:
            provided.append(image.resolve())

    assets_folder = project / "assets"
    existing = set()
    if assets_folder.is_dir():
        existing = {item.name.casefold() for item in assets_folder.iterdir() if item.is_file()}
    offered = {image.name.casefold() for image in provided}
    referenced = _referenced_assets(operations)
    missing = [(name, note) for name, note in referenced.items()
               if name.casefold() not in existing and name.casefold() not in offered]
    wanted = {name.casefold() for name in referenced}
    unused = [image for image in provided if image.name.casefold() not in wanted]
    pending = PendingEdit(project, meta, request, request_path, label, provided, missing)
    # ChatGPT and Claude name downloaded pictures themselves; one spare picture fills one gap.
    if len(missing) == 1 and len(unused) == 1:
        pending.mapping[missing[0][0]] = unused[0]
        pending.auto_mapped.append((missing[0][0], unused[0].name))
        pending.missing = []
    return pending


def add_images(paths, project):
    """Copy pictures into the project's assets folder and return their names."""
    folder = Path(project) / "assets"
    folder.mkdir(exist_ok=True)
    names = []
    for path in map(Path, paths):
        if not path.is_file() or path.suffix.lower() not in IMAGE_TYPES:
            raise ValueError(f"PNG 또는 JPEG 그림만 추가할 수 있습니다. {path.name}")
        if path.stat().st_size > IMAGE_LIMIT:
            raise ValueError(f"그림은 12MB 이하여야 합니다. {path.name}")
        target = folder / path.name
        if path.resolve() != target.resolve():
            shutil.copyfile(path, target)
        names.append(path.name)
    return names


def finish_edit(pending):
    """Bring pictures into assets, apply the edit, and write a report when needed."""
    project = pending.project
    add_images(pending.images, project)
    request = pending.request
    if pending.mapping:
        renamed = {}
        folder = project / "assets"
        for name, source in pending.mapping.items():
            source = Path(source)
            # Keep the extension honest: TR checks that it matches the picture data.
            target_name = Path(name).stem + source.suffix.lower()
            shutil.copyfile(source, folder / target_name)
            renamed[name] = target_name
        for operation in request.get("operations", []):
            if isinstance(operation, dict) and operation.get("asset") in renamed:
                operation["asset"] = renamed[operation["asset"]]
        write_json(pending.request_path, request)

    stem = source_stem(project, pending.meta)
    for output in candidate_paths(project, stem, "_편집본", pending.meta["format"]):
        try:
            report = apply_project(project, pending.request_path, output)
        except (FileExistsError, ValueError):
            if not output.exists():
                raise
        else:
            break
    record_activity(project, "apply", output.name)
    for name, picture in pending.auto_mapped:
        report.warnings.append(f"{name} 자리에 함께 놓은 그림 {picture}를 사용했습니다.")
    report_path = None
    if report.failures or report.unresolved:
        report_path = output.with_name(f"{output.stem}_적용보고.md")
        try:
            report_path.write_text(report.markdown(output.name, pending.source_label), encoding="utf-8")
        except OSError:
            report_path = None
    return project, output, report, report_path


def apply_dropped(edits, active_project=None, images=()):
    """Apply an edit file without interactive picture selection."""
    pending = prepare_edit(edits, images=images, active_project=active_project)
    return finish_edit(pending)
