"""Read scanned PDF pages: Windows OCR for the text, the page image for rules, boxes and figures.

A scanned page is one picture. Windows' own OCR engine (Windows.Media.Ocr, Korean included with
the Korean language pack) gives words with their positions; this module turns them into the same
characters, ruled lines and pictures that tr_pdf reads from a text PDF, so the rest of the
conversion (columns, paragraphs, boxes, page fitting) works the same way.
"""

import json
import math
import os
import re
import statistics
import subprocess
import tempfile
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

OCR_DPI = 300          # Windows OCR reads 10-11pt print reliably from 300dpi
LINE_DPI = 150         # page image for finding ruled lines (0.3pt frame lines stay dark enough)
LINE_DARK = 200        # grey level below which a pixel counts as ink for lines
INK_DPI = 50           # coarse page image for finding figures (thin diagram lines still show)
INK_DARK = 225
SCAN_FONT = "함초롬바탕"  # the scan does not say which font it used
SIZE_FROM_HEIGHT = 1.015   # OCR word box height (Hangul ink) to font size
BASELINE_FROM_BOTTOM = 0.115  # Hangul ink sits this far (times the size) below the baseline
LABEL_MISREAD = {"시": "A", "니": "B"}
DESKEW_SIGN = int(os.environ.get("TR_DESKEW_SIGN", "1"))
SKEW_LIMIT = 0.12      # degrees; straighter pages are read as they are
PLACEHOLDER = re.compile(r"[0Oo]{2,3}")


@dataclass
class ScanPage:
    rows: list = field(default_factory=list)          # tr_pdf.Row
    horizontal: list = field(default_factory=list)    # (x0, x1, y) in points
    vertical: list = field(default_factory=list)      # (x, y0, y1)
    figures: list = field(default_factory=list)       # (x0, y0, x1, y1)
    angle: float = 0.0
    page: object = None        # the straightened page, when the scan was slanted
    document: object = None    # keeps that page's document open


def is_scanned(page):
    """No selectable text, but an image covering most of the page."""
    if page.get_text("text").strip():
        return False
    area = page.rect.width * page.rect.height
    for block in page.get_text("dict")["blocks"]:
        if block["type"] == 1:
            x0, y0, x1, y1 = block["bbox"]
            if (x1 - x0) * (y1 - y0) > 0.5 * area:
                return True
    return False


def run_ocr(images, language="ko"):
    """OCR a list of PNG files with Windows' engine; returns one result per image."""
    script = Path(__file__).with_name("tr_ocr.ps1")
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    shell = windir / "System32/WindowsPowerShell/v1.0/powershell.exe"
    with tempfile.TemporaryDirectory(prefix="tr-ocr-") as temporary:
        listing = Path(temporary) / "list.txt"
        output = Path(temporary) / "ocr.json"
        listing.write_text("\n".join(str(path) for path in images), encoding="utf-8")
        completed = subprocess.run(
            [str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-List", str(listing), "-Output", str(output), "-Language", language],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=60 + 30 * len(images))
        if completed.returncode != 0 or not output.is_file():
            detail = (completed.stderr or completed.stdout or "").strip()
            if "NO_LANGUAGE" in detail:
                raise RuntimeError("Windows의 한국어 OCR을 쓸 수 없습니다. Windows 설정 > 시간 및 언어 > 언어에서 "
                                   "한국어를 추가하고 '광학 문자 인식' 기능을 설치한 뒤 다시 시도하세요.")
            raise RuntimeError(f"스캔한 쪽의 글자를 읽지 못했습니다(Windows OCR). {detail}")
        data = json.loads(output.read_text(encoding="utf-8-sig"))
    return data if isinstance(data, list) else [data]


# ---------------------------------------------------------------- text

def _weight(char):
    """Relative width of a character inside an OCR word box."""
    if "가" <= char <= "힣" or "\u3130" <= char <= "\u318f" or "\u2460" <= char <= "\u24ff" \
            or "\u3200" <= char <= "\u32ff" or "\u4e00" <= char <= "\u9fff":
        return 1.0
    if char in ".,'’‘`:;!|·":
        return 0.32
    if char in "\"“”()[]{}<>〈〉《》「」『』":
        return 0.45
    if char.isascii():
        return 0.55
    return 0.9


def ocr_rows(result, page_width, char_class, row_class, image=None):
    """Characters and rows from one page of OCR output, in PDF points."""
    scale = page_width / result["width"]
    heights = [word["h"] * scale for line in result["lines"] for word in line["words"]
               if any("가" <= c <= "힣" for c in word["text"])]
    typical = statistics.median(heights) if heights else 10.0
    rows = []
    for line in result["lines"]:
        if not line["words"]:
            continue
        hangul = [word["h"] * scale for word in line["words"] if any("가" <= c <= "힣" for c in word["text"])]
        ink = statistics.median(hangul) if hangul else typical
        size = round(ink * SIZE_FROM_HEIGHT * 10) / 10
        bottoms = [(word["y"] + word["h"]) * scale for word in line["words"]
                   if any("가" <= c <= "힣" for c in word["text"])]
        bottom = statistics.median(bottoms) if bottoms else max((w["y"] + w["h"]) * scale for w in line["words"])
        baseline = bottom - BASELINE_FROM_BOTTOM * size
        current, last_x1 = [], None
        ordered = sorted(line["words"], key=lambda w: w["x"])
        # A bracket label in the margin ([A], often read as "[시") opens the OCR line but is not
        # part of the text: it becomes a row of its own, which tr_pdf sets aside as a margin mark.
        label = (len(ordered) > 1 and ordered[0]["text"].startswith("[") and len(ordered[0]["text"]) <= 3
                 and (ordered[0]["text"].endswith("]") or len(ordered[0]["text"]) <= 2))
        if label and len(ordered[0]["text"]) >= 2:
            # Windows OCR reads the A of [A] as 시.
            letter = LABEL_MISREAD.get(ordered[0]["text"][1], ordered[0]["text"][1])
            ordered[0] = dict(ordered[0], text="[" + letter + ordered[0]["text"][2:])
        for index, word in enumerate(ordered):
            x, width = word["x"] * scale, word["w"] * scale
            # A wide gap inside one OCR line is a column gap: start another row.
            if last_x1 is not None and current and (x - last_x1 > 3 * size or (label and index == 1)):
                if label and index == 1 and current[-1].c != "]":
                    closing = deepcopy(current[-1])
                    closing.c, closing.x0 = "]", closing.x1
                    current.append(closing)
                rows.append(row_class(current, baseline))
                current = []
            text = correct_placeholder(word, image)
            total = sum(_weight(c) for c in text) or 1
            unit = width / total
            cursor = x
            for c in text:
                advance = _weight(c) * unit
                current.append(char_class(c, cursor, baseline - size * 0.88, cursor + advance,
                                          baseline + size * 0.12, baseline, SCAN_FONT, size, False, 0))
                cursor += advance
            last_x1 = x + width
        if current:
            rows.append(row_class(current, baseline))
    return rows


def skew_degrees(result):
    """How far the scan is turned, from the slope of word bottoms along each OCR line.

    Positive means the lines run downhill to the right (the page was turned clockwise)."""
    slopes = []
    for line in result["lines"]:
        points = [(w["x"] + w["w"] / 2, w["y"] + w["h"]) for w in line["words"]
                  if any("가" <= c <= "힣" for c in w["text"])]
        if len(points) < 4 or points[-1][0] - points[0][0] < 20 * statistics.median(w["h"] for w in line["words"]):
            continue
        mean_x = statistics.mean(x for x, _ in points)
        mean_y = statistics.mean(y for _, y in points)
        spread = sum((x - mean_x) ** 2 for x, _ in points)
        if spread:
            slopes.append(sum((x - mean_x) * (y - mean_y) for x, y in points) / spread)
    if len(slopes) < 5:
        return 0.0
    return math.degrees(math.atan(statistics.median(slopes)))


def straightened(pymupdf, document, number, degrees):
    """The page redrawn turned back by `degrees`, at the same scale, in a new one-page document."""
    page = document[number]
    width, height = page.rect.width, page.rect.height
    turn = math.radians(abs(degrees))
    wide = width * math.cos(turn) + height * math.sin(turn)
    tall = width * math.sin(turn) + height * math.cos(turn)
    out = pymupdf.open()
    target = out.new_page(width=width, height=height)
    # show_pdf_page fits the turned page's bounding box into the rectangle: give it room to keep 1:1.
    target.show_pdf_page(pymupdf.Rect(width / 2 - wide / 2, height / 2 - tall / 2,
                                      width / 2 + wide / 2, height / 2 + tall / 2),
                         document, number, rotate=DESKEW_SIGN * degrees)
    return out


# ---------------------------------------------------------------- answer choices ①-⑤

CIRCLED = "①②③④⑤"
# What Windows OCR makes of a circled digit at the start of a choice: "㉦", a bare digit, a ring.
MISREAD_FIRST = set("㉦0123456789Oo○◎®©@ⓞ")
MISREAD_INNER = set("㉦○◎®©")
QUESTION = re.compile(r"^\d{1,2}\.")
DEBUG_MARKS = bool(os.environ.get("TR_DEBUG_MARKS"))


def _ring_left_of(image, word):
    """A circled mark (ring about one letter high) just left of a word the OCR did not report."""
    size = word["h"]
    return _ring_in(image, word["x"] - 1.8 * size, word["y"] - 0.2 * size,
                    word["x"] - 0.15 * size, word["y"] + 1.15 * size, size)


def _ring_in(image, left, top, right, bottom, size):
    """The ring of a circled character inside the given pixel area, or None."""
    width, height, stride, data = image
    x0, x1, y0, y1 = int(left), int(right), int(top), int(bottom)
    if x0 < 0 or y0 < 0 or y1 >= height or x1 > width or x1 - x0 < 4:
        return None
    xs, ys = [], []
    for y in range(y0, y1):
        line = data[y * stride + x0:y * stride + x1]
        for offset, value in enumerate(line):
            if value < 150:
                xs.append(x0 + offset)
                ys.append(y)
    if len(xs) < size:          # a few specks, not a glyph
        return None
    bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
    w, h = bx1 - bx0, by1 - by0
    if not (0.7 * size <= w <= 1.25 * size and 0.7 * size <= h <= 1.2 * size and 0.75 <= w / h <= 1.33):
        return None
    # A ring has ink along all four sides of its box, near the middle of each side; a bracket
    # corner (└) or a short rule does not.
    band = max(2, int(0.15 * min(w, h)))
    mid_x = range(bx0 + w // 3, bx1 - w // 3 + 1)
    mid_y = range(by0 + h // 3, by1 - h // 3 + 1)
    points = set(zip(xs, ys))
    sides = [
        any((x, y) in points for x in mid_x for y in range(by0, by0 + band)),
        any((x, y) in points for x in mid_x for y in range(by1 - band, by1 + 1)),
        any((x, y) in points for y in mid_y for x in range(bx0, bx0 + band)),
        any((x, y) in points for y in mid_y for x in range(bx1 - band, bx1 + 1)),
    ]
    if not all(sides):
        return None
    # ... and leaves the corners of that box empty, where a Hangul syllable usually has ink.
    corner = max(2, int(0.1 * min(w, h)))     # a circle passes 0.15 of its width from each corner
    in_corners = sum(1 for x, y in points
                     if (x < bx0 + corner or x > bx1 - corner) and (y < by0 + corner or y > by1 - corner))
    if in_corners > 0.03 * len(points):
        return None
    return {"text": "?", "x": bx0, "y": by0, "w": w, "h": h}


def correct_placeholder(word, image):
    """Correct suspected circles only if every glyph is round in the scan."""
    text = word["text"]
    if image is None or not PLACEHOLDER.fullmatch(text):
        return text
    x, y, size = word["x"], word["y"], word["h"]
    step = word["w"] / len(text)
    for index in range(len(text)):
        ring = _ring_in(image, x + step * index, y - 1, x + step * (index + 1) + 1, y + size + 2, size)
        if ring is None or ring["w"] < 0.85 * ring["h"]:
            return text
    return "○" * len(text)


def _letter_sized(word, words):
    """A circled digit is as big as a letter; a bullet (◦ read as 0) is smaller."""
    heights = [other["h"] for other in words if other is not word and any("가" <= c <= "힣" for c in other["text"])]
    if not heights:
        return True
    height = statistics.median(heights)
    return word["h"] >= 0.8 * height and word["w"] >= 0.7 * height


def restore_choice_marks(result, image, split):
    """Put ①-⑤ back where the OCR lost or misread them.

    Answer choices run ① to ⑤ under each question, so the marks are numbered in reading order
    (left column, then right), starting again at every question number."""
    def column(line):
        return 0 if split is None or line["words"][0]["x"] < split else 1

    lines = [line for line in result["lines"] if line["words"]]
    for line in lines:
        line["words"].sort(key=lambda word: word["x"])
    lines.sort(key=lambda line: (column(line), line["words"][0]["y"]))
    # Pass 1: find where marks are, without numbering yet. Each entry: (line, kind, payload, x).
    found = []
    for line in lines:
        words = line["words"]
        first = words[0]["text"]
        if QUESTION.match(first) or (first.isdigit() and len(words) > 1 and words[1]["text"].startswith(".")):
            found.append((line, "question", None, None))
            continue
        for index, word in enumerate(words):
            text = word["text"]
            following = words[index + 1] if index + 1 < len(words) else None
            # A choice number stands apart from its text; a circled letter in a passage (ⓐ, read
            # the same way) is set right against its word.
            apart = following is not None and following["x"] - (word["x"] + word["w"]) > 0.3 * word["h"]
            if text and text[0] in CIRCLED:
                found.append((line, "known", word, word["x"]))
            elif (index == 0 and len(text) > 1 and text[0] == "㉦"
                  and ("㉠" <= text[1] <= "㉭" or "ⓐ" <= text[1] <= "ⓩ")):
                found.append((line, "joined", word, word["x"]))     # "① ㉠은 …" read as "㉦㉠은"
            elif apart and (len(following["text"]) > 1 or len(words) >= 3) and _letter_sized(word, words) and (
                    (len(text) == 1 and ((index == 0 and text == "㉦") or (index > 0 and text in MISREAD_INNER)))
                    or (index == 0 and len(text) <= 2 and not any("가" <= c <= "힣" for c in text)
                        and _ring_in(image, word["x"] - 0.2 * word["h"], word["y"] - 0.2 * word["h"],
                                     word["x"] + word["w"] + 0.2 * word["h"], word["y"] + 1.2 * word["h"],
                                     word["h"]) is not None)):
                found.append((line, "token", word, word["x"]))
            elif index == 0:
                ring = _ring_left_of(image, word)
                if ring is not None:
                    found.append((line, "ring", ring, ring["x"]))

    # Pass 2: a ring the OCR missed counts only where marks the OCR did see stand, at the same
    # indent in the same column; elsewhere it is likely a syllable the OCR dropped (흰, 원 …).
    anchors = {}
    for line, kind, _, x in found:
        if kind in ("known", "joined", "token"):
            anchors.setdefault(column(line), []).append(x)

    def aligned(line, ring):
        return any(abs(ring["x"] - x) <= 0.15 * ring["h"] for x in anchors.get(column(line), []))

    # Pass 3: number them in reading order.
    count = 0
    for line, kind, payload, _ in found:
        if kind == "question":
            count = 0
        elif kind == "known":
            count = CIRCLED.index(payload["text"][0]) + 1
        elif kind == "ring" and not aligned(line, payload):
            continue
        else:
            count = count % 5 + 1
            mark = CIRCLED[count - 1]
            if kind == "joined":
                payload["text"] = mark + " " + payload["text"][1:]
            elif kind == "token":
                payload["text"] = mark
            else:
                payload["text"] = mark
                line["words"].insert(0, payload)
            if DEBUG_MARKS:
                print("mark", kind, mark, line["text"][:30])
    for line in lines:
        line["text"] = " ".join(word["text"] for word in line["words"])


# ---------------------------------------------------------------- rules and figures

def _dark_table(threshold):
    return bytes(1 if value < threshold else 0 for value in range(256))


def ruled_lines(pymupdf, page, text_boxes):
    """Horizontal and vertical lines drawn on the scanned page (box frames, column rule, underlines)."""
    pix = page.get_pixmap(dpi=LINE_DPI, colorspace=pymupdf.csGRAY, alpha=False)
    width, height, stride = pix.width, pix.height, pix.stride
    ink = pix.samples.translate(_dark_table(LINE_DARK))
    scale = 72 / LINE_DPI
    shortest = int(20 / scale)         # 20pt: longer than any stroke of body text
    runs = []
    for y in range(height):
        row = ink[y * stride:y * stride + width]
        for match in re.finditer(rb"\x01{%d,}" % shortest, row):
            runs.append((match.start(), match.end(), y))
    horizontal = _join_runs(runs)
    runs = []
    for x in range(width):
        column = ink[x:x + stride * height:stride]
        for match in re.finditer(rb"\x01{%d,}" % shortest, column):
            runs.append((match.start(), match.end(), x))
    vertical = _join_runs(runs)

    def inside_text(x0, y0, x1, y1):
        return any(bx0 <= x0 and x1 <= bx1 and by0 <= y0 and y1 <= by1 for bx0, by0, bx1, by1 in text_boxes)

    h_out = [(x0 * scale, x1 * scale, y * scale) for x0, x1, y in horizontal
             if not inside_text(x0 * scale, y * scale - 1, x1 * scale, y * scale + 1)]
    v_out = [(x * scale, y0 * scale, y1 * scale) for y0, y1, x in vertical
             if not inside_text(x * scale - 1, y0 * scale, x * scale + 1, y1 * scale)]
    return h_out, v_out


def _join_runs(runs):
    """Merge runs on neighbouring scan lines (a line 2-3 pixels thick) into one segment."""
    runs.sort(key=lambda r: (r[2], r[0]))
    open_segments, done = [], []
    for start, end, position in runs:
        match = next((s for s in open_segments if s[3] >= position - 1 and min(s[1], end) - max(s[0], start)
                      > 0.6 * min(s[1] - s[0], end - start)), None)
        if match:
            match[0], match[1] = min(match[0], start), max(match[1], end)
            match[3] = position
            match[4].append(position)
        else:
            open_segments.append([start, end, position, position, [position]])
        still = []
        for segment in open_segments:
            (still if segment[3] >= position - 1 else done).append(segment)
        open_segments = still
    done.extend(open_segments)
    # Thicker than 5 pixels is a filled area (a picture or a black label), not a line.
    return [(s[0], s[1], statistics.mean(s[4])) for s in done if s[3] - s[2] <= 5]


def frame_lines(page, horizontal, vertical):
    """The lines that belong to the page's layout: column rule, head rules, passage frames.

    Other lines (chart axes, table grids inside a figure) are left as ink of the figure."""
    from tr_pdf import _find_boxes   # tr_pdf imports this module; import late to avoid a cycle

    width, height = page.rect.width, page.rect.height
    keep_h = [h for h in horizontal if h[1] - h[0] > 0.6 * width]
    keep_v = [v for v in vertical if v[2] - v[1] > 0.45 * height]
    for box in _find_boxes(horizontal, vertical):
        if box.y1 - box.y0 < 28 or box.x1 - box.x0 < 0.25 * width:
            continue    # small boxes (a boxed word) and narrow ones stay with whatever holds them
        keep_h += [h for h in horizontal if abs(h[2] - box.y0) < 2 or abs(h[2] - box.y1) < 2]
        keep_v += [v for v in vertical if abs(v[0] - box.x0) < 2 or abs(v[0] - box.x1) < 2]
    keep_h += [h for h in horizontal if h[1] - h[0] < 0.4 * width]   # underlines and short rules
    return keep_h, keep_v


def body_text_boxes(rows):
    """Boxes of OCR lines that read like running text; labels inside a chart are left out."""
    sizes = [row.chars[0].size for row in rows if row.chars]
    typical = statistics.median(sizes) if sizes else 10
    boxes = []
    for row in rows:
        text = "".join(char.c for char in row.chars).strip()
        size = row.chars[0].size
        if len(text.replace(" ", "")) >= 8 and abs(size - typical) < 0.25 * typical:
            boxes.append((min(c.x0 for c in row.chars), min(c.y0 for c in row.chars),
                          max(c.x1 for c in row.chars), max(c.y1 for c in row.chars)))
    return boxes


def figure_regions(pymupdf, page, text_boxes, horizontal, vertical):
    """Inked areas that are neither running text nor layout lines: diagrams, charts, photos."""
    pix = page.get_pixmap(dpi=INK_DPI, colorspace=pymupdf.csGRAY, alpha=False)
    width, height, stride = pix.width, pix.height, pix.stride
    data = pix.samples
    cell = 72 / INK_DPI
    covered = bytearray(width * height)

    def cover(x0, y0, x1, y1, pad=1):
        for cy in range(max(0, int(y0 / cell) - pad), min(height, int(y1 / cell) + pad + 1)):
            start, end = max(0, int(x0 / cell) - pad), min(width, int(x1 / cell) + pad + 1)
            covered[cy * width + start:cy * width + end] = b"\x01" * (end - start)

    for box in text_boxes:
        cover(*box, pad=2)
    for x0, x1, y in horizontal:
        cover(x0, y, x1, y, 0)
    for x, y0, y1 in vertical:
        cover(x, y0, x, y1, 0)
    ink = [data[y * stride + x] < INK_DARK and not covered[y * width + x] for y in range(height) for x in range(width)]
    seen = bytearray(width * height)
    regions = []
    for start in range(width * height):
        if not ink[start] or seen[start]:
            continue
        stack, cells = [start], []
        seen[start] = 1
        while stack:
            index = stack.pop()
            cells.append(index)
            y, x = divmod(index, width)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < height and 0 <= nx < width:
                        other = ny * width + nx
                        if ink[other] and not seen[other]:
                            seen[other] = 1
                            stack.append(other)
        ys = [index // width for index in cells]
        xs = [index % width for index in cells]
        x0, y0, x1, y1 = min(xs) * cell, min(ys) * cell, (max(xs) + 1) * cell, (max(ys) + 1) * cell
        if len(cells) >= 60 and x1 - x0 > 24 and y1 - y0 > 24:
            # Leftover specks around running text join up into a large sparse area: a region that
            # is mostly lines of body text is not a figure.
            text_area = sum(max(0, min(x1, bx1) - max(x0, bx0)) * max(0, min(y1, by1) - max(y0, by0))
                            for bx0, by0, bx1, by1 in text_boxes)
            if text_area < 0.25 * (x1 - x0) * (y1 - y0):
                regions.append([x0, y0, x1, y1])
    # Join pieces of one figure that sit close together.
    merged = True
    while merged:
        merged = False
        for a in regions:
            for b in regions:
                if a is not b and a[0] - 6 < b[2] and b[0] - 6 < a[2] and a[1] - 6 < b[3] and b[1] - 6 < a[3]:
                    a[:] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    regions.remove(b)
                    merged = True
                    break
            if merged:
                break
    return [tuple(region) for region in regions]


def read_scanned_pages(pymupdf, document, numbers, char_class, row_class, progress=None):
    """ScanPage for every scanned page among `numbers` (0-based), keyed by page number."""
    scanned = [number for number in numbers if is_scanned(document[number])]
    if not scanned:
        return {}
    pages = {}
    with tempfile.TemporaryDirectory(prefix="tr-scan-") as temporary:
        images = []
        for number in scanned:
            path = Path(temporary) / f"page{number:04d}.png"
            document[number].get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csGRAY, alpha=False).save(path)
            images.append(path)
        if progress:
            progress(f"스캔한 {len(scanned)}쪽의 글자를 읽는 중(OCR)")
        results = run_ocr(images)
        # A page fed in at a slant is turned back straight and read again.
        sources = {number: document[number] for number in scanned}
        keep_alive = []
        tilted = [(index, number, skew_degrees(result)) for index, (number, result)
                  in enumerate(zip(scanned, results))]
        tilted = [(index, number, degrees) for index, number, degrees in tilted if abs(degrees) > SKEW_LIMIT]
        if tilted:
            if progress:
                progress(f"비뚤게 스캔된 {len(tilted)}쪽을 바로 세워 다시 읽는 중")
            again = []
            for index, number, degrees in tilted:
                fixed = straightened(pymupdf, document, number, degrees)
                keep_alive.append(fixed)
                sources[number] = fixed[0]
                fixed[0].get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csGRAY, alpha=False).save(images[index])
                again.append(images[index])
            for (index, _, _), result in zip(tilted, run_ocr(again)):
                results[index] = result
        for number, result, path in zip(scanned, results, images):
            page = sources[number]
            if progress:
                progress(f"스캔한 쪽 {number + 1}쪽의 선과 그림 찾는 중")
            scale = page.rect.width / result["width"]
            word_boxes = [(w["x"] * scale, w["y"] * scale, (w["x"] + w["w"]) * scale, (w["y"] + w["h"]) * scale)
                          for line in result["lines"] for w in line["words"]]
            horizontal, vertical = ruled_lines(pymupdf, page, word_boxes)
            rules = [v for v in vertical if v[2] - v[1] > 0.45 * page.rect.height
                     and 0.3 * page.rect.width < v[0] < 0.7 * page.rect.width]
            split = max(rules, key=lambda v: v[2] - v[1])[0] / scale if rules else None
            pix = pymupdf.Pixmap(str(path))
            image = (pix.width, pix.height, pix.stride, pix.samples)
            restore_choice_marks(result, image, split)
            rows = ocr_rows(result, page.rect.width, char_class, row_class, image)
            layout_h, layout_v = frame_lines(page, horizontal, vertical)
            figures = figure_regions(pymupdf, page, body_text_boxes(rows), layout_h, layout_v)
            scan = ScanPage(rows, horizontal, vertical, figures, skew_degrees(result))
            if page.parent is not document:
                scan.page, scan.document = page, page.parent
            pages[number] = scan
    return pages
