"""Where each body paragraph's lines break in the PDF that Hangul printed.

Hangul 2018 cannot number lines itself. TR reads the printed preview to learn how many lines
each body paragraph takes and at which character each line starts, so margin line numbers
(insert_line_numbers) can be placed and cited ("8~9줄") without guessing the layout.

    py -3 tr_lines.py <프로젝트 폴더> [P0029 P0030 ...]   문단별 줄 나눔을 보여 줌
    py -3 tr_lines.py <프로젝트 폴더> --update            AI용.md에 줄 배치(line_starts)를 넣어 다시 씀
"""

import sys
from difflib import SequenceMatcher
from pathlib import Path


def normalized(text):
    """Letters and digits of text (spaces, punctuation and symbols dropped) with their offsets."""
    chars, offsets = [], []
    for index, char in enumerate(text):
        if char.isalnum():
            chars.append(char)
            offsets.append(index)
    return "".join(chars), offsets


def pdf_lines(pdf):
    """Visual text lines in reading order: page, then left/right half, then top to bottom."""
    from tr_pdf import load_pymupdf

    pymupdf = load_pymupdf()
    lines = []
    with pymupdf.open(str(pdf)) as document:
        for number, page in enumerate(document, 1):
            middle = page.rect.width / 2
            pieces = []
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    text = "".join(span["text"] for span in line["spans"])
                    if not normalized(text)[0]:
                        continue
                    x0, y0, x1, y1 = line["bbox"]
                    size = max((span["size"] for span in line["spans"] if span["text"].strip()), default=0)
                    pieces.append(dict(page=number, side=int(x0 >= middle), x0=x0, y0=y0, x1=x1, y1=y1,
                                       size=size, text=text))
            pieces.sort(key=lambda item: (item["side"], (item["y0"] + item["y1"]) / 2, item["x0"]))
            merged = []
            for piece in pieces:
                last = merged[-1] if merged else None
                centre = (piece["y0"] + piece["y1"]) / 2
                if (last is not None and last["side"] == piece["side"]
                        and abs(centre - (last["y0"] + last["y1"]) / 2) < 0.35 * max(last["size"], piece["size"], 1)):
                    # Pieces on one baseline (a margin label, a size change) form one visual line.
                    parts = sorted(last["parts"] + [piece], key=lambda item: item["x0"])
                    last.update(parts=parts, text="".join(item["text"] for item in parts),
                                x0=min(last["x0"], piece["x0"]), x1=max(last["x1"], piece["x1"]),
                                y0=min(last["y0"], piece["y0"]), y1=max(last["y1"], piece["y1"]),
                                size=max(last["size"], piece["size"]))
                else:
                    merged.append(dict(piece, parts=[piece]))
            for item in merged:
                item["key"] = normalized(item["text"])[0]
                del item["parts"]
                lines.append(item)
    return lines


def _advance(target, position, key):
    """Where target continues after this printed line, or None when the line is not its text."""
    for junk in range(min(4, len(key))):
        # A margin label ([A]) or a printed line number may precede the passage text.
        piece = key[junk:]
        if piece and target.startswith(piece, position):
            return position + len(piece)
    window = target[position:position + len(key)]
    if len(key) >= 8 and SequenceMatcher(None, key, window, autojunk=False).ratio() >= 0.85:
        return min(position + len(key), len(target))
    return None


def _line_start(text, offset, printed):
    """Step back over a symbol the printed line starts with (‘, ⓐ, 󰡔) that has no letter key."""
    first = printed.lstrip()[:1]
    if first and not first.isalnum():
        for back in range(1, 4):
            if offset - back >= 0 and text[offset - back] == first:
                return offset - back
    return offset


def paragraph_lines(text, lines, start=0, search=200):
    """Find the paragraph's printed lines from lines[start:]; return (line_starts, line indexes)."""
    target, offsets = normalized(text)
    if len(target) < 4:
        return None
    need = min(len(target), 8)
    for first in range(start, min(len(lines), start + search)):
        position = _advance(target, 0, lines[first]["key"])
        if position is None or position < need:
            continue
        starts, used, index, skipped = [0], [first], first + 1, 0
        while position < len(target) and index < len(lines) and skipped <= 2:
            following = _advance(target, position, lines[index]["key"])
            if following is not None and following > position:
                starts.append(_line_start(text, offsets[position], lines[index]["text"]))
                used.append(index)
                position, skipped = following, 0
            else:
                skipped += 1
            index += 1
        if position >= len(target) - 1:
            return starts, used
    return None


def body_paragraphs(document):
    """Top-level body paragraphs (not in tables or text boxes) with their TR labels."""
    for section in document.body.findall("SECTION"):
        for paragraph in section.findall("P"):
            label = document.labels.get(paragraph)
            if label:
                yield label, paragraph


def line_layout(document, pdf, labels=None):
    """{paragraph label: [character offset where each printed line starts]} for multi-line paragraphs."""
    lines = pdf_lines(pdf)
    layout, cursor = {}, 0
    wanted = set(labels) if labels else None
    for label, paragraph in body_paragraphs(document):
        if wanted is not None and label not in wanted:
            continue
        text = document.plain_text(paragraph)
        found = paragraph_lines(text, lines, cursor)
        if found is None and wanted is not None:
            found = paragraph_lines(text, lines, 0, search=len(lines))
        if found is None:
            continue
        starts, used = found
        cursor = used[-1] + 1
        if len(starts) > 1 or wanted is not None:
            layout[label] = starts
    return layout


def line_of(starts, offset):
    """1-based line (within the paragraph) holding the character at offset."""
    return sum(1 for start in starts if start <= offset)


def main():
    from tr_core import AI_DOCUMENT, LOCAL, PREVIEW, Document, read_json, write_ai_document

    args = [arg for arg in sys.argv[1:] if arg != "--update"]
    if not args:
        print(__doc__.strip())
        return 2
    project = Path(args[0]).resolve()
    document = Document.read(project / LOCAL / "source.xml", project)
    if "--update" in sys.argv[1:]:
        # Projects exported before TR read line layouts: add them to AI용.md now.
        meta = read_json(project / LOCAL / "project.json")
        document.line_layout = line_layout(document, project / PREVIEW)
        write_ai_document(project / AI_DOCUMENT, document, meta["project_id"])
        print(f"AI용.md에 문단 {len(document.line_layout)}개의 줄 배치(line_starts)를 넣었습니다.")
        return 0
    layout = line_layout(document, project / PREVIEW, args[1:] or None)
    for label, starts in layout.items():
        text = document.plain_text(document.ids[label])
        print(f"{label}  {len(starts)}줄")
        for number, (start, stop) in enumerate(zip(starts, starts[1:] + [len(text)]), 1):
            print(f"  {number:>3}  {text[start:stop]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
