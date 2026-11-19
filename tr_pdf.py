"""Turn a text-based PDF (such as an exam printed from Hangul) into an editable HWP draft.

The PDF's text layer gives every character with its font, size and position. This
module rebuilds paragraphs, two-column flow, boxed passages, underlines and pictures
as native HWPML, then lets desktop Hangul save the HWP, the same way TR saves edits.
"""

import argparse
import math
import os
import re
import statistics
import sys
import tempfile
import xml.etree.ElementTree as ET
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

import tr_objects
from tr_core import Document, check_file_format, hancom, publish_file, set_char

PT = 100                 # HWPUNIT per point
MARGIN_UNIT = 200        # PARAMARGIN values are stored at twice the HWPUNIT scale
LANGS = ("Hangul", "Latin", "Hanja", "Japanese", "Other", "Symbol", "User")
MARGIN_LABEL = re.compile(r"^\s*\[[A-Z가-힣]\]\s*$")
STARTER = re.compile(r"^(?:[가-힣A-Za-z]{1,6}\s?\d{0,2}\s?:\s|\d{1,2}\s?\.\s|[①②③④⑤]|\[\s?\d|[<〈]\s?보|\((?:가|나|다|라|마|A|B|C)\)|"
                     r"[㉠-㉭ⓐ-ⓩ]\s?:|※|\*|○|•|-\s)")
KNOWN_FONTS = {
    "HaansoftBatang": "한컴바탕", "HaansoftDotum": "한컴돋움", "Batang": "바탕", "Dotum": "돋움",
    "Gulim": "굴림", "Gungsuh": "궁서", "MalgunGothic": "맑은 고딕", "HCRBatang": "함초롬바탕",
    "HCRDotum": "함초롬돋움", "TimesNewRoman": "Times New Roman", "Arial": "Arial",
    "GulimChe": "굴림체", "BatangChe": "바탕체", "DotumChe": "돋움체",
}


def load_pymupdf():
    # TR keeps PyMuPDF in its own tr_lib folder so the system Python stays untouched.
    local = Path(__file__).with_name("tr_lib")
    if local.is_dir() and str(local) not in sys.path:
        sys.path.insert(0, str(local))
    try:
        import pymupdf
    except ImportError as error:
        raise RuntimeError("PDF 변환에는 PyMuPDF가 필요합니다. 명령 프롬프트에서 "
                           f'py -3 -m pip install --target "{local}" pymupdf '
                           "를 실행한 뒤 다시 시도하세요.") from error
    return pymupdf


def font_name(raw):
    name = raw.split("+", 1)[1] if re.match(r"^[A-Z]{6}\+", raw) else raw
    name = re.sub(r"-Identity-[HV]$", "", name.lstrip("*"))
    try:
        name = name.encode("latin-1").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    name = re.split(r"[,]", name)[0].strip()
    base = re.sub(r"[-,]?(Bold|Regular|Italic|Medium|Light)$", "", name)
    if name.startswith("신명-"):
        # Hangul prints its built-in 신명 fonts as "신명-중명조"; the font itself is "신명 중명조".
        name = name.replace("-", " ", 1)
    return KNOWN_FONTS.get(base, name) or "함초롬바탕"


def font_size(size):
    """Sizes kept to 0.1pt: a scaled exam prints 10pt as 11.2pt, and 11.0 would rewrap every line."""
    return round(size * 10) / 10


HFT_SINGLE = {"명조", "고딕", "샘물", "필기", "옛한글", "그래픽", "궁서", "디나루", "휴먼명조", "휴먼고딕"}


def font_type(name):
    """Hangul's own fonts (HFT) must be declared as such, or Hangul substitutes another face."""
    if name.startswith(("신명 ", "한양", "#", "양재 ")) or name in HFT_SINGLE:
        return "hft"
    return "ttf"


# ---------------------------------------------------------------- page analysis

@dataclass
class Char:
    c: str
    x0: float
    y0: float
    x1: float
    y1: float
    baseline: float
    font: str
    size: float
    bold: bool
    color: int
    underline: bool = False
    boxed: bool = False
    spacing: int = 0          # 자간 in percent

    def style(self):
        return (self.font, font_size(self.size), self.bold, self.underline, self.boxed, self.color, self.spacing)


@dataclass
class Row:
    chars: list
    baseline: float
    x0: float = 0
    x1: float = 0
    y0: float = 0
    y1: float = 0
    size: float = 10

    height: float = 10      # largest glyph size: Hangul sizes the line box by it
    floats: list = field(default_factory=list)   # margin marks ([A] with its bracket) beside this line
    checked: bool = False   # full-line protection already decided

    def finish(self):
        # The PDF often positions words apart without emitting a space character.
        spaced = []
        for char in self.chars:
            if spaced and not char.c.isspace() and not spaced[-1].c.isspace():
                gap = char.x0 - spaced[-1].x1
                # A boxed word has padding inside its frame, which is not a space.
                factor = 0.6 if char.boxed != spaced[-1].boxed else 0.22
                if gap > min(char.size, spaced[-1].size) * factor:
                    filler = deepcopy(spaced[-1])
                    filler.c, filler.underline, filler.boxed = " ", False, False
                    spaced.append(filler)
            spaced.append(char)
        self.chars = spaced
        visible = [char for char in self.chars if not char.c.isspace()] or self.chars
        self.x0 = min(char.x0 for char in visible)
        self.x1 = max(char.x1 for char in visible)
        self.y0 = min(char.y0 for char in visible)
        self.y1 = max(char.y1 for char in visible)
        self.size = statistics.median(char.size for char in visible)
        self.height = max(char.size for char in visible)
        return self

    @property
    def text(self):
        return "".join(char.c for char in self.chars)


@dataclass
class Box:
    x0: float
    y0: float
    x1: float
    y1: float
    rows: list = field(default_factory=list)
    open: bool = False      # drawn without top/bottom edge: a passage running on from or into another column


@dataclass
class Picture:
    x0: float
    y0: float
    x1: float
    y1: float
    png: bytes = b""
    note: str = ""            # text the picture stands for, kept as the object's description


@dataclass
class Page:
    width: float
    height: float
    split: float | None
    body_top: float
    body_bottom: float
    columns: list           # per column: list of items (Row | Box | Picture) in reading order
    column_edges: list      # per column: (left, right)
    head_top: float | None = None     # top of the running head / title block above the body
    foot_top: float | None = None     # page number, copyright line under the body
    foot_bottom: float | None = None


def _segments(page):
    horizontal, vertical = [], []

    def add(x0, y0, x1, y1):
        if abs(y0 - y1) < 1.0 and abs(x1 - x0) > 1.0:
            horizontal.append((min(x0, x1), max(x0, x1), (y0 + y1) / 2))
        elif abs(x0 - x1) < 1.0 and abs(y1 - y0) > 1.0:
            vertical.append(((x0 + x1) / 2, min(y0, y1), max(y0, y1)))

    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "l":
                add(item[1].x, item[1].y, item[2].x, item[2].y)
            elif item[0] in ("re", "qu"):
                rect = item[1].rect if item[0] == "qu" else item[1]
                if rect.height < 1.6:
                    add(rect.x0, rect.y0 + rect.height / 2, rect.x1, rect.y0 + rect.height / 2)
                elif rect.width < 1.6:
                    add(rect.x0 + rect.width / 2, rect.y0, rect.x0 + rect.width / 2, rect.y1)
                else:
                    add(rect.x0, rect.y0, rect.x1, rect.y0)
                    add(rect.x0, rect.y1, rect.x1, rect.y1)
                    add(rect.x0, rect.y0, rect.x0, rect.y1)
                    add(rect.x1, rect.y0, rect.x1, rect.y1)
    return horizontal, vertical


def _merge_horizontal(segments, gap):
    merged = []
    for x0, x1, y in sorted(segments, key=lambda s: (round(s[2] * 2), s[0])):
        if merged and abs(merged[-1][2] - y) < 0.8 and x0 - merged[-1][1] <= gap:
            last = merged[-1]
            merged[-1] = (last[0], max(last[1], x1), last[2])
        else:
            merged.append((x0, x1, y))
    return merged


def _merge_vertical(segments):
    merged = []
    for x, y0, y1 in sorted(segments, key=lambda s: (round(s[0] * 2), s[1])):
        if merged and abs(merged[-1][0] - x) < 0.8 and y0 - merged[-1][2] <= 3:
            last = merged[-1]
            merged[-1] = (last[0], last[1], max(last[2], y1))
        else:
            merged.append((x, y0, y1))
    return merged


def _find_boxes(horizontal, vertical):
    """Rectangles drawn as four lines; a label may interrupt the top edge (<보기>)."""
    loose = _merge_horizontal(horizontal, 90)
    upright = _merge_vertical(vertical)

    def has_side(x, y0, y1):
        return any(abs(vx - x) < 2.5 and vy0 <= y0 + 3 and vy1 >= y1 - 3 for vx, vy0, vy1 in upright)

    boxes = []
    for top in loose:
        if top[1] - top[0] < 12:
            continue
        for bottom in sorted(loose, key=lambda s: s[2]):
            if bottom[2] <= top[2] + 6 or abs(bottom[0] - top[0]) > 3 or abs(bottom[1] - top[1]) > 3:
                continue
            if has_side(top[0], top[2], bottom[2]) and has_side(top[1], top[2], bottom[2]):
                boxes.append(Box(top[0], top[2], top[1], bottom[2]))
                break
    # A passage that continues into the next column is drawn open at the top or bottom:
    # two matching sides without one of the horizontal edges.
    for index, left in enumerate(upright):
        for right in upright[index + 1:]:
            if (right[0] - left[0] > 100 and abs(left[1] - right[1]) < 3 and abs(left[2] - right[2]) < 3
                    and left[2] - left[1] > 40
                    and not any(box.x0 - 3 < left[0] < box.x0 + 3 and box.x1 - 3 < right[0] < box.x1 + 3
                                and box.y0 - 3 < left[1] < box.y1 for box in boxes)):
                boxes.append(Box(left[0], max(left[1], right[1]), right[0], min(left[2], right[2]), open=True))
    boxes.sort(key=lambda b: (b.x1 - b.x0) * (b.y1 - b.y0), reverse=True)
    return boxes


def _inside(x, y, box, pad=1.5):
    return box.x0 - pad <= x <= box.x1 + pad and box.y0 - pad <= y <= box.y1 + pad


def analyse_page(pymupdf, page, number, dpi=220):
    width, height = page.rect.width, page.rect.height
    horizontal, vertical = _segments(page)
    rules = [v for v in _merge_vertical(vertical) if v[2] - v[1] > height * 0.45
             and width * 0.3 < v[0] < width * 0.7]
    # A tall passage box has long sides too; the column rule is the longest, most central line.
    rules.sort(key=lambda v: (-(v[2] - v[1]), abs(v[0] - width / 2)))
    split = rules[0][0] if rules else None
    body_top, body_bottom = (rules[0][1], rules[0][2]) if rules else (0.0, height)
    # The title rule: the lowest wide line near the top. On a first page the column rule may
    # start higher, hidden under a white title area.
    wide = [h for h in _merge_horizontal(horizontal, 2) if h[1] - h[0] > width * 0.6
            and body_top - 6 <= h[2] < height * 0.3]
    header_line = max((h[2] for h in wide), default=body_top)
    body_top = max(body_top, header_line)

    boxes = _find_boxes(horizontal, vertical)
    big = [box for box in boxes if box.y1 - box.y0 >= 28 and box.y0 > header_line - 1
           and box.y1 < body_bottom + 1]
    small = [box for box in boxes if box.y1 - box.y0 < 28 and box.x1 - box.x0 < 260]
    edges = {(round(b.y0), round(b.y1)) for b in boxes}
    underlines = [h for h in _merge_horizontal(horizontal, 1.2)
                  if h[1] - h[0] < (width * 0.4) and not any(
                      abs(h[2] - b.y0) < 1.2 or abs(h[2] - b.y1) < 1.2 for b in boxes
                      if b.x0 - 2 <= h[0] and h[1] <= b.x1 + 2)]
    del edges

    flags = pymupdf.TEXTFLAGS_RAWDICT | pymupdf.TEXT_PRESERVE_IMAGES
    raw = page.get_text("rawdict", flags=flags)
    pictures = []
    for block in raw["blocks"]:
        if block["type"] == 1:
            x0, y0, x1, y1 = block["bbox"]
            if x1 - x0 > 8 and y1 - y0 > 8:
                pictures.append(Picture(x0, y0, x1, y1))

    rows = []
    for block in raw["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            chars = []
            for span in line["spans"]:
                font = font_name(span["font"])
                bold = bool(span["flags"] & 16) or "Bold" in span["font"]
                for item in span["chars"]:
                    x0, y0, x1, y1 = item["bbox"]
                    chars.append(Char(item["c"], x0, y0, x1, y1, item["origin"][1], font,
                                      span["size"], bold, span["color"]))
            if chars and any(not char.c.isspace() for char in chars):
                rows.append(Row(chars, statistics.median(char.baseline for char in chars)))

    for char in (char for row in rows for char in row.chars):
        center = (char.x0 + char.x1) / 2
        for x0, x1, y in underlines:
            if x0 - 0.5 <= center <= x1 + 0.5 and -1.5 <= y - char.baseline <= 4.5:
                char.underline = True
                break
        for box in small:
            if _inside(center, (char.y0 + char.y1) / 2, box, 0.5):
                char.boxed = True
                break

    # Lines that share a baseline in one column become one row ("[A]" next to its text).
    def column_of(x):
        return 0 if split is None or x < split else 1

    rows.sort(key=lambda r: (column_of(min(c.x0 for c in r.chars)), round(r.baseline), min(c.x0 for c in r.chars)))
    merged = []
    for row in rows:
        row.chars.sort(key=lambda c: c.x0)
        previous = merged[-1] if merged else None
        label = any(MARGIN_LABEL.match("".join(char.c for char in item.chars))
                    for item in (row, previous) if item is not None)
        if (previous and not label and abs(previous.baseline - row.baseline) < 2.2
                and column_of(previous.chars[0].x0) == column_of(row.chars[0].x0)):
            gap = row.chars[0].x0 - previous.chars[-1].x1
            if gap > previous.chars[-1].size * 0.25 and not previous.chars[-1].c.isspace():
                filler = deepcopy(previous.chars[-1])
                filler.c, filler.underline, filler.boxed = " ", False, False
                previous.chars.append(filler)
            previous.chars.extend(row.chars)
        else:
            merged.append(row)
    rows = [row.finish() for row in merged]
    # A bracket label such as [A] sits in the margin beside a block of lines, often on a bracket
    # line. Inside the text it would take room and rewrap the paragraph, so the label and its
    # bracket are kept as a picture at the same place on the page, attached to the nearest line.
    upright = _merge_vertical(vertical)
    for label in [row for row in rows if MARGIN_LABEL.match(row.text)]:
        others = [row for row in rows if row is not label and not MARGIN_LABEL.match(row.text)
                  and column_of(row.x0) == column_of(label.x0)]
        if not others:
            continue
        nearest = min(others, key=lambda row: abs(row.baseline - label.baseline))
        if abs(nearest.baseline - label.baseline) > nearest.size * 2.5:
            continue
        x0, y0, x1, y1 = label.x0 - 1, label.y0 - 1, label.x1 + 1, label.y1 + 1
        for vx, vy0, vy1 in upright:
            if label.x0 - 6 < vx < label.x1 + 14 and vy0 <= label.y1 + 4 and vy1 >= label.y0 - 4:
                x0, x1, y0, y1 = min(x0, vx - 1), max(x1, vx + 1), min(y0, vy0 - 1), max(y1, vy1 + 1)
        for hx0, hx1, hy in horizontal:
            if hx0 >= x0 - 2 and hx1 - hx0 < 20 and (abs(hy - y0 - 1) < 1.5 or abs(hy - y1 + 1) < 1.5):
                x1 = max(x1, hx1 + 1)
        # Stop short of the text the bracket stands beside.
        starts = [row.x0 for row in others if row.y1 > y0 and row.y0 < y1 and row.x0 > label.x1]
        if starts:
            x1 = min(x1, min(starts) - 0.5)
        clip = pymupdf.Rect(x0, y0, x1, y1)
        nearest.floats.append(Picture(x0, y0, x1, y1, page.get_pixmap(clip=clip, dpi=dpi).tobytes("png"),
                                      note=label.text.strip()))
        rows.remove(label)

    if not rules and rows:
        # No column rule: the body is simply where the text is.
        body_top = header_line = min(row.y0 for row in rows) - 2
        body_bottom = max(row.y1 for row in rows) + 2
    # The running head (above the top rule) and the footer (page number, copyright line under
    # the column rule) are page furniture; they become the page's 머리말/꼬리말.
    marks = [(row.y0, row.y1) for row in rows] + [(pic.y0, pic.y1) for pic in pictures]
    for drawing in page.get_drawings():
        rect = drawing["rect"]
        marks.append((rect.y0, rect.y1))
    above = [y0 for y0, y1 in marks if y1 <= header_line + 1.5]
    below = [(y0, y1) for y0, y1 in marks if y0 >= body_bottom + 1]
    head_top = min(above) if above else None
    foot_top = min(y0 for y0, _ in below) if below else None
    foot_bottom = max(y1 for _, y1 in below) if below else None
    body_rows = [row for row in rows if row.y1 > header_line + 1 and row.y0 < body_bottom - 1]
    body_pictures = [pic for pic in pictures if pic.y1 > header_line + 1 and pic.y0 < body_bottom - 1]
    # A framed figure (a table of pictures with captions) is kept whole as one picture.
    for box in [box for box in big if any(
            _inside((pic.x0 + pic.x1) / 2, (pic.y0 + pic.y1) / 2, box, 2) for pic in body_pictures)]:
        big.remove(box)
        body_pictures = [pic for pic in body_pictures
                         if not _inside((pic.x0 + pic.x1) / 2, (pic.y0 + pic.y1) / 2, box, 2)]
        body_pictures.append(Picture(box.x0 - 1.5, box.y0 - 1.5, box.x1 + 1.5, box.y1 + 1.5))
    # Pictures side by side in one column become one picture, or they would be stacked.
    merged_pictures = []
    for pic in sorted(body_pictures, key=lambda p: p.y0):
        same = next((m for m in merged_pictures if column_of(m.x0) == column_of(pic.x0)
                     and min(m.y1, pic.y1) - max(m.y0, pic.y0) > 0.3 * min(m.y1 - m.y0, pic.y1 - pic.y0)), None)
        if same:
            same.x0, same.y0 = min(same.x0, pic.x0), min(same.y0, pic.y0)
            same.x1, same.y1 = max(same.x1, pic.x1), max(same.y1, pic.y1)
        else:
            merged_pictures.append(pic)
    body_pictures = merged_pictures
    for pic in body_pictures:
        clip = pymupdf.Rect(pic.x0, pic.y0, pic.x1, pic.y1)
        pic.png = page.get_pixmap(clip=clip, dpi=dpi).tobytes("png")
    # Text drawn over a picture is already in the rendered picture.
    body_rows = [row for row in body_rows if not any(
        _inside((row.x0 + row.x1) / 2, (row.y0 + row.y1) / 2, pic, 0) for pic in body_pictures)]
    big = [box for box in big if not any(_inside((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2, pic, 0)
                                         for pic in body_pictures)]

    column_count = 1 if split is None else 2
    columns, column_edges = [], []
    for index in range(column_count):
        mine = [row for row in body_rows if column_of(row.x0) == index]
        boxes_here = [box for box in big if column_of((box.x0 + box.x1) / 2) == index]
        pics_here = [pic for pic in body_pictures if column_of((pic.x0 + pic.x1) / 2) == index]
        loose = []
        for row in mine:
            owner = next((box for box in boxes_here if _inside((row.x0 + row.x1) / 2, row.baseline, box, 4)), None)
            (owner.rows if owner else loose).append(row)
        items = loose + [box for box in boxes_here if box.rows] + pics_here
        items.sort(key=lambda item: (item.y0, item.x0))
        columns.append(items)
        xs = [row.x0 for row in mine] + [box.x0 for box in boxes_here]
        ends = [row.x1 for row in mine] + [box.x1 for box in boxes_here]
        column_edges.append((min(xs) if xs else 0, max(ends) if ends else width))
    return Page(width, height, split, header_line, body_bottom, columns, column_edges,
                head_top, foot_top, foot_bottom)


# ---------------------------------------------------------------- paragraphs

@dataclass
class Paragraph:
    runs: list                 # [(text, style)]
    align: str = "Justify"
    left: float = 0            # points
    indent: float = 0          # points, negative = hanging
    spacing: int = 160
    size: float = 10
    page_break: bool = False
    column_break: bool = False
    picture: Picture | None = None
    right: float = 0           # points
    border: int | None = None  # paragraphs sharing a number are drawn inside one box
    space_before: float = 0    # points
    space_after: float = 0
    top: float = 0             # where the first line box starts on the PDF page
    lines: int = 1
    line_size: float = 10      # font size Hangul sizes these line boxes by
    spacer: bool = False       # an empty 1pt paragraph that only carries vertical space
    keep_words: bool = False   # wrap lines between words (어절) instead of between characters
    floats: list = field(default_factory=list)   # pictures placed at fixed page positions

    def height(self):
        if self.picture is not None:
            return self.picture.y1 - self.picture.y0 + PICTURE_PAD
        if self.spacer:
            return SPACER
        return self.lines * self.line_size * self.spacing / 100


# Hangul places a line's baseline this far (times the font size) below the top of its line box,
# and a line box is font size x line spacing tall. Measured on Hangul 2018.
ASCENT = 0.835
SPACER = 1.0
SPACER_STYLE = ("함초롬바탕", 1.0, False, False, False, 0, 0)
PICTURE_PAD = 0.2
CONDENSE = int(os.environ.get("TR_CONDENSE", "20"))   # 최소 공백: exam papers squeeze spaces up to ~20%
FIT_ROUNDS = int(os.environ.get("TR_FIT_ROUNDS", "3"))   # rebuilds to pull back text pushed onto the next page
SPACING_SHIFT = int(os.environ.get("TR_SPACING_SHIFT", "0"))
GLYPH_EM = 1.001   # a Hangul syllable's advance at 0% spacing, in em, as Hangul prints it
SPACE_EM = 0.54    # a word space at 0% spacing; justified lines only ever widen it
NO_LINE_START = set(".,?!:;)]}」』”’>〉》…·%")


@dataclass
class Layout:
    """Page geometry shared by the whole document, in PDF points from the top-left corner."""
    width: float
    height: float
    left: float           # body left / right edge
    right: float
    top: float            # where the 머리말 starts (= body_start when there is none)
    body_start: float     # where the columns start on an ordinary page
    body_end: float
    bottom: float         # where the 꼬리말 ends (= body_end when there is none)
    foot_top: float | None = None


PARTICLE_END = set("은는이가을를의에서로와과도만며고면다요죠께게지나야서인데한된될던")
CLOSING = set(".,?!:;)]}」』”’>〉》…")
VOCABULARY = set()


def build_vocabulary(pages):
    """Words that appear whole inside lines, used to tell wraps between words from wraps inside one."""
    words = set()
    for page in pages:
        for items in page.columns:
            for item in items:
                for row in ([item] if isinstance(item, Row) else getattr(item, "rows", [])):
                    tokens = row.text.split()
                    for token in tokens[1:-1]:
                        words.add(token.strip("‘’“”\"'()[]<>.,?!:;"))
    words.discard("")
    return words


def wrap_needs_space(before, after):
    """Hangul drops the space at a line end, so the PDF cannot tell '문서에|비해' from '고려|해야'."""
    if not before or not after:
        return False
    if before[-1] in CLOSING:
        return True
    tail = before.split()[-1].strip("‘’“”\"'()[]<>") if before.split() else ""
    head = after.split()[0].strip("‘’“”\"'()[]<>.,?!:;") if after.split() else ""
    if not tail or not head:
        return False
    if (tail + head) in VOCABULARY:
        return False
    if tail.strip(".,?!") in VOCABULARY and head in VOCABULARY:
        return True
    return len(tail) >= 2 and tail[-1] in PARTICLE_END


def _runs(rows):
    chars = []
    for index, row in enumerate(rows):
        row_chars = list(row.chars)
        while row_chars and row_chars[-1].c.isspace() and index == len(rows) - 1:
            row_chars.pop()
        while row_chars and row_chars[0].c.isspace():
            row_chars.pop(0)
        if chars and row_chars and chars[-1].c == " " and row_chars[0].c == " ":
            row_chars.pop(0)
        if chars and row_chars and not chars[-1].c.isspace():
            before = "".join(char.c for char in chars[-24:])
            after = "".join(char.c for char in row_chars[:24])
            if wrap_needs_space(before, after):
                filler = deepcopy(chars[-1])
                filler.c, filler.underline, filler.boxed = " ", False, False
                chars.append(filler)
        chars.extend(row_chars)
    runs = []
    for char in chars:
        text = " " if char.c in "\t\r\n" else char.c
        if runs and runs[-1][1] == char.style():
            runs[-1][0] += text
        else:
            runs.append([text, char.style()])
    for run in runs:
        run[0] = re.sub(r" {2,}", " ", run[0])
    return [tuple(run) for run in runs if run[0]]


def _hangul(char):
    return "가" <= char.c <= "힣"


def keeps_words(rows):
    """Whether Hangul wrapped this paragraph by whole words (어절) rather than by characters.

    Word spaces are 0.54em plus the line's spacing and only grow when a line is justified,
    so the growth on a line is the room it had left. With room for the next line's first
    character, a character-wrapping paragraph would have taken it."""
    for row, following in zip(rows, rows[1:]):
        chars = row.chars
        room = 0.0
        for a, space, b in zip(chars, chars[1:], chars[2:]):
            if space.c == " " and _hangul(a) and _hangul(b):
                natural = (GLYPH_EM + SPACE_EM + 2 * a.spacing / 100) * a.size
                room += max(0.0, b.x0 - a.x0 - natural)
        head = [char for char in following.chars if not char.c.isspace()][:3]
        if not head or head[0].c in NO_LINE_START or head[0].c.isascii():
            continue
        need = 0.0
        for index, char in enumerate(head):
            if index and char.c not in NO_LINE_START:
                break
            need += ((GLYPH_EM + char.spacing / 100) * char.size if _hangul(char) else char.x1 - char.x0)
        if room > need + 0.5:
            return True
    return False


def _advance(char):
    """A character's natural advance in Hangul at its spacing, in points."""
    if _hangul(char):
        return (GLYPH_EM + char.spacing / 100) * char.size
    if char.c == " ":
        return (SPACE_EM + char.spacing / 100) * char.size
    return char.x1 - char.x0


def protect_full_lines(rows, right, keep_words):
    """Tighten lines that fill the measure by 1% so Hangul cannot push their last character over.

    Whole-percent spacing can leave a line a point wider in Hangul than in the PDF; a full line
    then loses its last character to a new line, which in a full column pushes the page over.
    The line is tightened only where that cannot pull the next line's first character up."""
    for index, row in enumerate(rows):
        visible = [char for char in row.chars if not char.c.isspace()]
        if row.checked or not visible or right - row.x1 > row.size * 0.6:
            continue
        row.checked = True     # decided once, however often the page is rebuilt
        gain = 0.01 * sum(char.size for char in row.chars)
        if index + 1 < len(rows):
            room = right - row.x1
            for a, space, b in zip(row.chars, row.chars[1:], row.chars[2:]):
                if space.c == " " and not a.c.isspace() and not b.c.isspace():
                    room += (b.x0 - a.x0) - _advance(a) - _advance(space)
            head = [char for char in rows[index + 1].chars if not char.c.isspace()]
            if keep_words:
                word = []
                for char in rows[index + 1].chars:
                    if char.c.isspace() and word:
                        break
                    if not char.c.isspace():
                        word.append(char)
                head = word
            need = sum(_advance(char) for char in head[:1 if not keep_words else None])
            if not head or room > 1.5 or room + gain >= need - 1:
                continue
        for char in row.chars:
            char.spacing -= 1


def build_paragraphs(rows, left, right, pitch_hint=None):
    rows = sorted(rows, key=lambda r: r.baseline)
    if not rows:
        return []
    pitches = [b.baseline - a.baseline for a, b in zip(rows, rows[1:])
               if 0.9 * a.size < b.baseline - a.baseline < 2.6 * a.size]
    pitch = statistics.median(pitches) if pitches else (pitch_hint or rows[0].size * 1.6)
    groups, current, previous = [], None, None
    for row in rows:
        text = row.text.lstrip()
        start = current is None
        if not start:
            gap = row.baseline - previous.baseline
            base = current["cont"] if current["cont"] is not None else left
            if gap > pitch * 1.45:
                start = True
            elif STARTER.match(text):
                start = True
            elif previous.x1 < right - previous.size * 1.6:
                start = True
            elif current["marker"]:
                start = row.x0 < current["first"] - 1.5
            elif row.x0 > previous.x0 + row.size * 0.45 and row.x0 > base + row.size * 0.45:
                # Indented against the line above: a new paragraph's first line. A block that is
                # indented as a whole (a [A] bracket) keeps its lines at one x and stays together.
                start = True
        if start:
            current = {"rows": [row], "first": row.x0, "cont": None,
                       "marker": bool(STARTER.match(text))}
            groups.append(current)
        else:
            current["rows"].append(row)
            if current["cont"] is None:
                current["cont"] = row.x0
        previous = row

    paragraphs = []
    for group in groups:
        members = group["rows"]
        size = statistics.median(row.size for row in members)
        first = group["first"]
        cont = statistics.median(row.x0 for row in members[1:]) if len(members) > 1 else first
        # Hangul: Left is where the outermost line starts; a positive Indent moves the first
        # line in, a negative one moves the following lines in (hanging indent).
        margin = max(0.0, min(first, cont) - left)
        indent = first - cont
        if abs(indent) < 0.8:
            indent = 0
        align = "Justify"
        if len(members) == 1:
            row = members[0]
            middle, span = (left + right) / 2, right - left
            if abs((row.x0 + row.x1) / 2 - middle) < span * 0.03 and row.x1 - row.x0 < span * 0.85 and row.x0 - left > size:
                align, margin, indent = "Center", 0, 0
            elif right - row.x1 < size * 0.6 and row.x0 - left > span * 0.3:
                align, margin, indent = "Right", 0, 0
        keep_words = keeps_words(members)
        if align == "Justify":
            protect_full_lines(members, right, keep_words)
        local = [b.baseline - a.baseline for a, b in zip(members, members[1:])]
        line_pitch = statistics.median(local) if local else pitch
        # Character shapes are written in half points, so Hangul sizes the lines by those values.
        line_size = font_size(statistics.median(row.height for row in members))
        # Whole percents: a 5% step is half a point per line and adds up over a column.
        spacing = int(min(400, max(80, round(line_pitch / line_size * 100))))
        paragraphs.append(Paragraph(_runs(members), align, margin, indent, spacing, size,
                                    top=members[0].baseline - ASCENT * font_size(members[0].height),
                                    lines=len(members), line_size=line_size,
                                    keep_words=keep_words,
                                    floats=[pic for row in members for pic in row.floats]))
    return paragraphs


def text_left(rows):
    """The block's text edge: its leftmost line start, ignoring one stray line far to the left.

    (Margin labels such as [A] are already set aside as pictures.) Dialogue passages indent
    most lines, so a typical start would sit at the indent rather than at the edge."""
    starts = sorted(row.x0 for row in rows)
    if len(starts) > 2 and starts[0] < starts[1] - 2 * rows[0].size:
        return starts[1]
    return starts[0]


def measure_spacing(pages):
    """Give every character the letter spacing (자간) of its line.

    Exam papers are tightened line by line: most lines at one value (say -8%), some a few
    percent tighter or looser so that a word fits. A Hangul glyph advances 1em plus the
    spacing, so each line's value can be read back from its glyph advances; lines too short
    to measure take the value most lines of that font and size use."""
    measured, usual = [], {}
    for page in pages:
        for items in page.columns:
            for item in items:
                for row in ([item] if isinstance(item, Row) else getattr(item, "rows", [])):
                    samples = {}
                    for a, b in zip(row.chars, row.chars[1:]):
                        if ("가" <= a.c <= "힣" and "가" <= b.c <= "힣"
                                and a.font == b.font and abs(a.size - b.size) < 0.1):
                            advance = (b.x0 - a.x0) / a.size
                            if 0.6 < advance < 1.3:
                                samples.setdefault((a.font, font_size(a.size)), []).append(advance)
                    value = None
                    if samples:
                        key, advances = max(samples.items(), key=lambda item: len(item[1]))
                        if len(advances) >= 4:
                            value = max(-50, min(50, round((statistics.median(advances) - GLYPH_EM) * 100)
                                                 + SPACING_SHIFT))
                            usual.setdefault(key, []).append(value)
                    measured.append((row, value))
    usual = {key: statistics.mode(values) for key, values in usual.items()}
    for row, value in measured:
        if value is None:
            # Too short to measure: the value most lines in this font use.
            fonts = [(char.font, font_size(char.size)) for char in row.chars if not char.c.isspace()]
            main = statistics.mode(fonts) if fonts else None
            value = usual.get(main, 0)
        # The spacing is set on the whole line, numerals, brackets and spaces included.
        for char in row.chars:
            char.spacing = value


def _squeeze(column, amount):
    """Take `amount` points out of a column: from the widest gaps first, then from line spacing."""
    for paragraph in sorted(column[1:], key=lambda p: p.space_before, reverse=True):
        if amount <= 0:
            return
        take = min(amount, max(0.0, paragraph.space_before - 3))
        paragraph.space_before = round((paragraph.space_before - take) * 2) / 2
        amount -= take
    if amount > 0:
        lines = sum(p.lines for p in column if not p.spacer and p.picture is None) or 1
        for paragraph in column:
            if not paragraph.spacer and paragraph.picture is None:
                cut = math.ceil(amount / lines / paragraph.line_size * 100)
                paragraph.spacing = max(80, paragraph.spacing - cut)


def page_paragraphs(page, start, squeeze=0.0):
    """Paragraphs of one page, each given the space above it that puts it where the PDF has it.

    `start` is where the columns begin in the Hangul layout (under the running head or title).
    `squeeze` points come out of each column's largest gaps when Hangul needed more lines."""
    rows = [item for items in page.columns for item in items if isinstance(item, Row)]
    sizes = [row.size for row in rows] or [10]
    pitch = statistics.median(sizes) * 1.65
    paragraphs = []
    for index, items in enumerate(page.columns):
        left, right = page.column_edges[index]
        blocks, group = [], []

        def flush():
            nonlocal group
            if group:
                blocks.extend(("text", paragraph) for paragraph in build_paragraphs(group, left, right, pitch))
                group = []

        for item in items:
            if isinstance(item, Row):
                group.append(item)
                continue
            flush()
            if isinstance(item, Picture):
                blocks.append(("text", Paragraph([], "Left", max(0.0, item.x0 - left), spacing=100,
                                                 picture=item, top=item.y0)))
            else:
                # A boxed passage is a run of paragraphs with a connected paragraph border, as
                # Hangul exam papers are made; it flows across columns and pages like text.
                inner_left, inner_right = text_left(item.rows), max(row.x1 for row in item.rows)
                inner = build_paragraphs(item.rows, inner_left, inner_right, pitch)
                for paragraph in inner:
                    paragraph.left += max(0.0, inner_left - left)
                    paragraph.right = max(0.0, right - inner_right)
                    paragraph.border = id(item)
                if inner:
                    blocks.append(("box", item, inner))
        flush()

        # Walk down the column as Hangul will lay it out and add the missing space above each block.
        def room(target):
            # Paragraph spacing is stored in half points; the cursor follows the rounded value
            # so that rounding errors do not add up down the column.
            return max(0.0, round((target - cursor) * 2) / 2)

        column, cursor, framed = [], start, False
        for block in blocks:
            if block[0] == "text":
                paragraph = block[1]
                paragraph.space_before = room(paragraph.top)
                cursor += paragraph.space_before + paragraph.height()
                column.append(paragraph)
                framed = False
                continue
            _, box, inner = block
            # A passage running on from the previous column keeps one frame with it: no spacer, so
            # Hangul connects the borders across the column break and leaves the ends open.
            runs_on = box.open and not column
            if (box.y0 - cursor > SPACER + 0.2 or framed) and not runs_on:
                # The space above a box must stay outside its frame, so it goes on a 1pt spacer;
                # the spacer also keeps two boxes in a row from merging into one frame.
                spacer = Paragraph([], spacing=100, spacer=True, space_before=room(box.y0 - SPACER))
                column.append(spacer)
                cursor += spacer.space_before + SPACER
            for paragraph in inner:
                paragraph.space_before = room(paragraph.top)
                cursor += paragraph.space_before + paragraph.height()
            # The frame closes where the PDF draws it; a box left open at the column end stops short
            # of the bottom so that rounding cannot push an empty frame into the next column.
            inner[-1].space_after = room(min(box.y1, page.body_bottom - 4))
            cursor += inner[-1].space_after
            column.extend(inner)
            framed = True
        if squeeze > 0:
            _squeeze(column, squeeze)
        if column:
            if index == 0:
                column[0].page_break = True
            else:
                column[0].column_break = True
        paragraphs.extend(column)
    return paragraphs


# ---------------------------------------------------------------- HWPML output

class Builder:
    def __init__(self, template):
        self.root = ET.parse(template).getroot()
        self.mapping = self.root.find("HEAD/MAPPINGTABLE")
        self.section = self.root.find("BODY/SECTION")
        self.first = self.section.find("P")
        self.char_base = self.mapping.find("CHARSHAPELIST/CHARSHAPE")
        self.para_base = self.mapping.find("PARASHAPELIST/PARASHAPE")
        self.char_shapes, self.para_shapes, self.fonts = {}, {}, {}
        self.box_border = None
        self.pictures = []     # (paragraph, Picture) placed in the line
        self.floats = []       # (paragraph, Picture) placed at the picture's own spot on the page
        self.boxes = []        # (paragraph, box, inner paragraphs)

    def font_ids(self, name):
        if name not in self.fonts:
            ids = {}
            for group in self.mapping.findall("FACENAMELIST/FONTFACE"):
                faces = group.findall("FONT")
                found = next((face for face in faces if face.get("Name") == name), None)
                if found is None:
                    found = ET.SubElement(group, "FONT", Id=str(len(faces)), Name=name, Type=font_type(name))
                    group.set("Count", str(len(faces) + 1))
                ids[group.get("Lang")] = found.get("Id")
            self.fonts[name] = ids
        return self.fonts[name]

    def border(self):
        if self.box_border is None:
            borders = self.mapping.find("BORDERFILLLIST")
            border = ET.Element("BORDERFILL", BackSlash="0", Slash="0", Shadow="false", ThreeD="false")
            for kind in ("LEFTBORDER", "RIGHTBORDER", "TOPBORDER", "BOTTOMBORDER"):
                ET.SubElement(border, kind, Type="Solid", Width="0.12mm", Color="0")
            ET.SubElement(border, "DIAGONAL", Type="None", Width="0.12mm", Color="0")
            self.box_border = tr_objects._append_border(borders, border)
        return self.box_border

    def char_shape(self, style):
        if style not in self.char_shapes:
            font, size, bold, underline, boxed, color, spacing = style
            shapes = self.mapping.find("CHARSHAPELIST")
            shape = deepcopy(self.char_base)
            shape.set("Id", str(len(shapes.findall("CHARSHAPE"))))
            shape.set("Height", str(max(100, round(size * PT))))
            rgb = color & 0xFFFFFF
            shape.set("TextColor", str(((rgb & 0xFF) << 16) | (rgb & 0xFF00) | (rgb >> 16)))
            ids = self.font_ids(font)
            font_ids = shape.find("FONTID")
            for lang in LANGS:
                if lang in ids:
                    font_ids.set(lang, ids[lang])
            if spacing:
                # Every language slot, Latin included: spaces count as Latin, and the PDF's word
                # gaps carry the same tightening as its letters.
                letter = shape.find("CHARSPACING")
                for lang in LANGS:
                    letter.set(lang, str(spacing))
            if bold:
                ET.SubElement(shape, "BOLD")
            if underline:
                ET.SubElement(shape, "UNDERLINE", Color="0", Shape="Solid", Type="Bottom")
            if boxed:
                shape.set("BorderFillId", self.border())
            shapes.append(shape)
            shapes.set("Count", str(len(shapes.findall("CHARSHAPE"))))
            self.char_shapes[style] = shape.get("Id")
        return self.char_shapes[style]

    def para_shape(self, align, left, indent, spacing, right=0, border=False, before=0, after=0,
                   keep_words=False):
        key = (align, round(left * 2) / 2, round(indent * 2) / 2, spacing, round(right * 2) / 2,
               bool(border), round(before * 2) / 2, round(after * 2) / 2, bool(keep_words))
        if key not in self.para_shapes:
            shapes = self.mapping.find("PARASHAPELIST")
            shape = deepcopy(self.para_base)
            shape.set("Id", str(len(shapes.findall("PARASHAPE"))))
            shape.set("Align", align)
            shape.set("SnapToGrid", "false")
            # 줄 나눔 기준 (한글): 글자 or 어절, read from the PDF's line ends; 최소 공백 as printed.
            shape.set("BreakNonLatinWord", "false" if keep_words else "true")
            shape.set("Condense", str(CONDENSE))
            margin = shape.find("PARAMARGIN")
            margin.set("Left", str(round(key[1] * MARGIN_UNIT)))
            margin.set("Indent", str(round(key[2] * MARGIN_UNIT)))
            margin.set("Right", str(round(key[4] * MARGIN_UNIT)))
            margin.set("Prev", str(round(key[6] * MARGIN_UNIT)))
            margin.set("Next", str(round(key[7] * MARGIN_UNIT)))
            margin.set("LineSpacing", str(spacing))
            margin.set("LineSpacingType", "Percent")
            if border:
                # Consecutive paragraphs with the same connected border share one frame.
                frame = shape.find("PARABORDER")
                if frame is None:
                    frame = ET.SubElement(shape, "PARABORDER")
                frame.attrib.update(BorderFill=self.border(), Connect="true", IgnoreMargin="false")
            shapes.append(shape)
            shapes.set("Count", str(len(shapes.findall("PARASHAPE"))))
            self.para_shapes[key] = shape.get("Id")
        return self.para_shapes[key]

    def paragraph(self, parent, paragraph, default_style):
        node = ET.SubElement(parent, "P", ParaShape=self.para_shape(
            paragraph.align, paragraph.left, paragraph.indent, paragraph.spacing, paragraph.right,
            paragraph.border is not None, paragraph.space_before, paragraph.space_after,
            paragraph.keep_words), Style="0",
            PageBreak=str(paragraph.page_break).lower(), ColumnBreak=str(paragraph.column_break).lower())
        runs = paragraph.runs or [("", SPACER_STYLE if paragraph.spacer else default_style)]
        for text, style in runs:
            run = ET.SubElement(node, "TEXT", CharShape=self.char_shape(style))
            set_char(ET.SubElement(run, "CHAR"), text.replace(" ", " "))
        return node

    def furniture(self, kind, series, width, height, align):
        """A 머리말 or 꼬리말 holding one paragraph; the page's picture is added to it later."""
        control = ET.Element(kind, ApplyPageType="Both", SeriesNum=str(series))
        holder = ET.SubElement(control, "PARALIST", HasNumRef="false", HasTextRef="false",
                               LineWrap="Break", LinkListID="0", LinkListIDNext="0", TextDirection="0",
                               TextHeight=str(round(height * PT)), TextWidth=str(round(width * PT)),
                               VertAlign=align)
        node = ET.SubElement(holder, "P", ParaShape=self.para_shape("Left", 0, 0, 100), Style="0")
        set_char(ET.SubElement(ET.SubElement(node, "TEXT", CharShape=self.char_shape(SPACER_STYLE)), "CHAR"), "")
        return control, node

    def setup_page(self, layout):
        # Hangul adds this gap where the column layout changes on a page (under a title block).
        self.first.find("TEXT/SECDEF").set("SpaceColumns", "0")
        page_def = self.first.find("TEXT/SECDEF/PAGEDEF")
        page_def.set("Width", str(round(layout.width * PT)))
        page_def.set("Height", str(round(layout.height * PT)))
        page_def.set("Landscape", "0")
        page_def.find("PAGEMARGIN").attrib.update(
            {key: str(round(value * PT)) for key, value in (
                ("Left", layout.left), ("Right", layout.width - layout.right),
                ("Top", layout.top), ("Header", layout.body_start - layout.top),
                ("Bottom", layout.height - layout.bottom), ("Footer", layout.bottom - layout.body_end),
                ("Gutter", 0))})

    def columns(self, page, left, right):
        if page.split is None:
            return None
        gap = 0
        (l0, l1), (r0, r1) = page.column_edges
        if l1 and r0:
            gap = max(r0 - l1, 4)
        definition = ET.Element("COLDEF", Count="2", Layout="Left", SameGap=str(round(gap * PT)),
                                SameSize="true", Type="Newspaper")
        ET.SubElement(definition, "COLUMNLINE", Color="0", Type="Solid", Width="0.12mm")
        return definition


def convert_pdf(source, output, *, template=None, pages=None, progress=None):
    """Convert a PDF to a new .hwp/.hwpx file and return a short status message."""
    pymupdf = load_pymupdf()
    source, output = Path(source).resolve(strict=True), Path(output).resolve()
    if output.suffix.lower() not in (".hwp", ".hwpx"):
        raise ValueError("결과 파일은 .hwp 또는 .hwpx여야 합니다.")
    if output.exists():
        raise ValueError("이미 있는 파일에는 덮어쓰지 않습니다.")
    document = pymupdf.open(source)
    if document.needs_pass:
        raise ValueError("암호가 걸린 PDF는 변환할 수 없습니다.")
    numbers = list(range(document.page_count)) if pages is None else [n for n in pages if 0 <= n < document.page_count]
    if not numbers:
        raise ValueError("변환할 PDF 쪽이 없습니다. 쪽 범위를 확인하세요.")
    analysed = []
    characters = 0
    for position, number in enumerate(numbers):
        if progress:
            progress(f"PDF {number + 1}/{document.page_count}쪽 분석 중")
        page = analyse_page(pymupdf, document[number], position)
        analysed.append(page)
        characters += sum(len(row.chars) for items in page.columns for item in items
                          for row in ([item] if isinstance(item, Row) else getattr(item, "rows", [])))
    if not characters:
        raise ValueError("PDF에서 선택할 수 있는 글자를 찾지 못했습니다. 스캔한 PDF는 OCR 처리가 필요합니다.")
    VOCABULARY.clear()
    VOCABULARY.update(build_vocabulary(analysed))
    measure_spacing(analysed)

    layout = page_layout(analysed)
    usual_top = statistics.median(page.body_top for page in analysed)
    rendered = {}

    def render(position, x0, y0, x1, y1):
        key = (position, round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2))
        if key not in rendered:
            clip = pymupdf.Rect(x0, y0, x1, y1)
            rendered[key] = document[numbers[position]].get_pixmap(clip=clip, dpi=220).tobytes("png")
        return Picture(x0, y0, x1, y1, rendered[key])

    default_style = ("함초롬바탕", 10.0, False, False, False, 0, 0)
    body_sizes = [row.size for page in analysed for items in page.columns for item in items
                  if isinstance(item, Row) for row in [item]]
    if body_sizes:
        default_style = ("함초롬바탕", font_size(statistics.median(body_sizes)), False, False, False, 0, 0)

    def compose(template_path, folder, squeeze):
        """Build the HWPML for all pages; `squeeze` takes points of space out of given pages."""
        builder = Builder(template_path)
        builder.setup_page(layout)
        two_columns = builder.columns(analysed[0], layout.left, layout.right)
        section = builder.section
        first_node = None
        series = 0

        def emit(paragraph):
            node = builder.paragraph(section, paragraph, default_style)
            if paragraph.picture is not None:
                builder.pictures.append((node, paragraph.picture))
            builder.floats.extend((node, picture) for picture in paragraph.floats)
            return node

        for position, page in enumerate(analysed):
            # A section opening page (첫 쪽, 화법과 작문 …) has a title block across the page under the
            # running head: one column for its picture, then two columns again.
            title = two_columns is not None and page.body_top > usual_top + 20
            start = page.body_top + 1.0 + PICTURE_PAD if title else layout.body_start
            paragraphs = page_paragraphs(page, start, squeeze.get(position, 0))
            lead = None
            if title:
                block = render(position, layout.left, layout.body_start, layout.right, page.body_top + 1.0)
                lead = emit(Paragraph([], "Left", spacing=100, picture=block, page_break=position > 0))
                if position > 0:
                    lead.find("TEXT").insert(0, ET.Element("COLDEF", Count="1", Layout="Left", SameGap="0",
                                                           SameSize="true", Type="Newspaper"))
                if paragraphs:
                    paragraphs[0].page_break = False
            elif position > 0 and paragraphs:
                paragraphs[0].page_break = True
            if not paragraphs and lead is None:
                paragraphs = [Paragraph([], spacing=100, spacer=True, page_break=position > 0)]
            for index, paragraph in enumerate(paragraphs):
                if position == 0:
                    paragraph.page_break = False
                node = emit(paragraph)
                if title and index == 0:
                    node.find("TEXT").insert(0, deepcopy(two_columns))
                lead = lead if lead is not None else node
            first_node = first_node if first_node is not None else lead

            # This page's running head and footer, as Hangul 머리말/꼬리말 from this page on.
            controls = []
            if layout.top < layout.body_start - 2:
                series += 1
                control, holder = builder.furniture("HEADER", series, layout.right - layout.left,
                                                    layout.body_start - layout.top, "Top")
                builder.pictures.append((holder, render(position, layout.left, layout.top, layout.right,
                                                        layout.body_start - 0.4)))
                controls.append(control)
            if layout.foot_top is not None:
                series += 1
                control, holder = builder.furniture("FOOTER", series, layout.right - layout.left,
                                                    layout.bottom - layout.body_end, "Bottom")
                builder.pictures.append((holder, render(position, layout.left, layout.foot_top, layout.right,
                                                        layout.bottom - PICTURE_PAD)))
                controls.append(control)
            for control in reversed(controls):
                lead.find("TEXT").insert(0, control)

        # The template's first paragraph only carries the section and column settings; move them
        # into the first real paragraph so that nothing empty sits above the page's content.
        settings = [child for child in builder.first.find("TEXT") if child.tag != "CHAR"]
        target = first_node.find("TEXT")
        for index, child in enumerate(settings):
            if child.tag == "COLDEF" and two_columns is not None and not (
                    analysed[0].body_top > usual_top + 20):
                child = deepcopy(two_columns)
            target.insert(index, child)
        section.remove(builder.first)

        project = folder / "project"
        (project / "assets").mkdir(parents=True)
        doc = Document(builder.root, project)
        placed = [(node, picture, False) for node, picture in builder.pictures]
        placed += [(node, picture, True) for node, picture in builder.floats]
        for index, (node, picture, floating) in enumerate(placed):
            name = f"pdf_{index:03d}.png"
            (project / "assets" / name).write_bytes(picture.png)
            label = f"N_pic{index}"
            doc.register(label, node, "paragraph")
            tr_objects._insert_image(doc, {"target": label, "asset": name, "id": f"N_img{index}",
                                           "width_mm": (picture.x1 - picture.x0) * 25.4 / 72,
                                           "height_mm": (picture.y1 - picture.y0) * 25.4 / 72})
            shape = doc.ids[f"N_img{index}"].find("SHAPEOBJECT")
            if floating:
                # Fixed on the page in front of the text, taking no room in the lines.
                shape.set("TextWrap", "InFrontOfText")
                shape.find("POSITION").attrib.update(
                    TreatAsChar="false", AllowOverlap="true", FlowWithText="false",
                    HorzRelTo="Paper", HorzAlign="Left", HorzOffset=str(round(picture.x0 * PT)),
                    VertRelTo="Paper", VertAlign="Top", VertOffset=str(round(picture.y0 * PT)))
            if picture.note:
                ET.SubElement(shape, "SHAPECOMMENT").text = picture.note
        hml = folder / "converted.xml"
        ET.ElementTree(builder.root).write(hml, encoding="utf-8", xml_declaration=True)
        return hml, series

    probes = [page_probe(page) for page in analysed]
    line_pitches = [row.baseline - previous.baseline for page in analysed for items in page.columns
                    for previous, row in zip(items, items[1:])
                    if isinstance(previous, Row) and isinstance(row, Row)
                    and 0.9 * row.size < row.baseline - previous.baseline < 2.6 * row.size]
    pitch = statistics.median(line_pitches) if line_pitches else 18.0
    squeeze = {}
    with tempfile.TemporaryDirectory(prefix="tr-pdf-") as temporary:
        template_path = Path(template) if template else blank_template()
        for attempt in range(FIT_ROUNDS + 1):
            folder = Path(temporary) / f"round{attempt}"
            folder.mkdir()
            if progress:
                progress("한글 문서를 만드는 중" if attempt == 0 else
                         f"넘친 쪽 {len(overflow)}곳의 여백을 줄여 다시 맞추는 중")
            hml, series = compose(template_path, folder, squeeze)
            staged = folder / f"converted{output.suffix.lower()}"
            check = folder / "check.pdf"
            _build_with_retry(hml, staged, check)
            # Hangul may wrap a line differently from the PDF; a column that grows by a line pushes
            # everything after it to the next page. Find such pages and take the room from their gaps.
            overflow = overflowing_pages(pymupdf, check, probes)
            if not overflow or attempt == FIT_ROUNDS:
                break
            for position in overflow:
                squeeze[position] = squeeze.get(position, 0) + pitch
        check_file_format(staged)
        publish_file(staged, output)
    note = " 머리말·꼬리말(쪽 번호)은 쪽마다 원본 모양 그대로 그림으로 넣었습니다." if series else ""
    if overflow:
        note += f" {len(overflow)}쪽은 원본보다 길어져 다음 쪽으로 넘쳤습니다."
    return f"PDF {len(analysed)}쪽을 한글 문서로 옮겼습니다.{note}"


def _build_with_retry(hml, staged, check):
    """Hangul's automation server now and then drops a long build (RPC failure); try once more."""
    for attempt in range(2):
        try:
            hancom("build", hml, staged, check)
            return
        except RuntimeError:
            if attempt:
                raise
            for leftover in (staged, check):
                leftover.unlink(missing_ok=True)


def _hangul_text(text):
    return "".join(char for char in text if "가" <= char <= "힣")


def page_probe(page):
    """A stretch of the page's first lines, used to find where the page landed in Hangul."""
    rows = [row for items in page.columns for item in items
            for row in ([item] if isinstance(item, Row) else getattr(item, "rows", []))]
    return _hangul_text("".join(row.text for row in rows[:4]))[:30]


def overflowing_pages(pymupdf, pdf, probes):
    """Pages whose text ran over into the next page of the Hangul result (0-based positions)."""
    texts = [_hangul_text(page.get_text()) for page in pymupdf.open(pdf)]
    overflow, shift = [], 0
    for position, probe in enumerate(probes):
        if len(probe) < 8:
            continue
        found = [index for index, text in enumerate(texts) if probe in text and index >= position + shift]
        if not found:
            continue
        moved = found[0] - position
        if moved > shift and position > 0:
            overflow.append(position - 1)
        shift = max(shift, moved)
    if len(texts) > len(probes) + shift:
        overflow.append(len(probes) - 1)
    return sorted(set(overflow))


def page_layout(pages):
    """One page geometry for the whole document, taken from what most pages share."""
    first = pages[0]
    lefts = [min(edge[0] for edge in page.column_edges) for page in pages if page.column_edges]
    rights = [max(edge[1] for edge in page.column_edges) for page in pages if page.column_edges]
    left = max(statistics.median(lefts), 10) if lefts else 56
    # One point of slack on the right so a line that exactly fits the PDF column still fits.
    right = min(statistics.median(rights) + 1, first.width - 10) if rights else first.width - 56
    usual_top = statistics.median(page.body_top for page in pages)
    heads = [page.head_top for page in pages if page.head_top is not None and page.head_top < usual_top]
    if heads:
        body_start = usual_top + 1.0      # just under the rule that closes the running head
        top = max(min(heads) - 1.5, 0)
    else:
        body_start = top = max(usual_top, 10)
    body_end = min(max(page.body_bottom for page in pages) + 6, first.height - 10)
    feet = [page for page in pages if page.foot_top is not None]
    foot_top = None
    if feet:
        foot_top = min(page.foot_top for page in feet) - 1.5
        body_end = min(body_end, foot_top)
        bottom = min(max(page.foot_bottom for page in feet) + 1.5, first.height)
    else:
        bottom = body_end
    return Layout(first.width, first.height, left, right, top, body_start, body_end, bottom, foot_top)


def blank_template():
    """A blank Hangul document as HWPML, cached per user."""
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "TR"
    path = base / "blank_hwpml.xml"
    if not path.is_file():
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="tr-blank-") as temporary:
            staged = Path(temporary) / "blank.xml"
            hancom("blank", Path(temporary) / "none.hwp", staged)
            staged.replace(path)
    return path


def main():
    parser = argparse.ArgumentParser(description="텍스트가 들어 있는 PDF를 편집 가능한 한글 문서로 옮깁니다.")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pages", help="예: 1-3,5 (1부터)")
    parser.add_argument("--template", type=Path)
    args = parser.parse_args()
    pages = None
    if args.pages:
        pages = []
        for part in args.pages.split(","):
            start, _, end = part.partition("-")
            pages.extend(range(int(start) - 1, int(end or start)))
    try:
        print(convert_pdf(args.source, args.output, template=args.template, pages=pages, progress=print))
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
