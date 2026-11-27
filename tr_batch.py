"""Save copies and ZIP bundles of the results selected in TR."""

import shutil
import tempfile
import unicodedata
import zipfile
from itertools import count
from pathlib import Path

from tr_core import AI_DOCUMENT, TEXT_DOCUMENT
from tr_workflow import windows_filename


def save_copy(source, destination):
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise FileNotFoundError(f"원본 파일을 찾을 수 없습니다. {source}")
    if source.resolve() == destination.resolve():
        return
    with tempfile.TemporaryDirectory(prefix="tr-save-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "result"
        shutil.copyfile(source, staged)
        staged.replace(destination)


def _archive_entries(sources):
    names = set()
    markdown_names = {AI_DOCUMENT.casefold(), TEXT_DOCUMENT.casefold()}
    for source in sources:
        stem, label = source.stem, ""
        if source.name.casefold() in markdown_names and source.parent.name:
            stem, label = source.parent.name, f" - {source.stem}"
        for index in count(1):
            suffix = "" if index == 1 else f" ({index})"
            name = windows_filename(stem, f"{label}{suffix}{source.suffix}")
            key = unicodedata.normalize("NFC", name).casefold()
            if key not in names:
                names.add(key)
                yield source, name
                break


def write_bundle(paths, destination):
    destination = Path(destination)
    target = destination.resolve()
    existing_target = target.is_file()
    sources = {}
    for path in paths:
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(f"묶을 파일을 찾을 수 없습니다. {source}")
        resolved = source.resolve()
        if resolved == target or (existing_target and resolved.samefile(target)):
            raise ValueError("묶을 파일과 같은 위치에 ZIP을 저장할 수 없습니다.")
        sources.setdefault(resolved, source)
    if not sources:
        raise ValueError("묶을 파일이 없습니다.")

    with tempfile.TemporaryDirectory(prefix="tr-bundle-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "bundle.zip"
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED, strict_timestamps=False) as archive:
            for source, name in _archive_entries(sources.values()):
                archive.write(source, arcname=name)
        staged.replace(destination)
