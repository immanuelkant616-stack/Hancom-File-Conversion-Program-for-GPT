"""Native table and picture edits in Hancom HWPML 2.x."""

import base64
import re
import zlib
from copy import deepcopy
from xml.etree import ElementTree as ET

import tr_layout


CELL_CHARACTER_FIELDS = {"bold", "italic", "size_pt", "color", "font"}
OBJECT_FIELDS = {
    "set_table_cell": ({"target", "row", "col", "text"}, set()),
    "format_table_cell": ({"target", "row", "col"}, {"background", "align", "vertical_align"}
                          | CELL_CHARACTER_FIELDS),
    "insert_table": ({"target", "cells"}, {"width_mm", "col_widths", "header_row", "header_background",
                                           "header_color", "position", "id", "border", "border_color",
                                           "font", "size_pt", "color", "bold", "col_align", "stripe_background",
                                           "padding_mm", "line_spacing_percent", "markup"}),
    "insert_line_numbers": ({"target", "lines"}, {"start", "every", "size_pt", "color", "font", "gap_mm",
                                                  "width_mm", "pitch_pt", "shift_pt", "side", "id"}),
    "delete_object": ({"target"}, set()),
    "replace_image": ({"target", "asset"}, set()),
    "insert_image": ({"target", "asset"}, {"width_mm", "height_mm", "position", "id"}),
}

_REMOVABLE_OBJECTS = {
    "TABLE", "PICTURE", "RECTANGLE", "ELLIPSE", "LINE", "ARC", "POLYGON",
    "CURVE", "CONNECTLINE", "CONTAINER", "TEXTART", "EQUATION",
}
_JPEG_FRAME_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                       0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_ZERO_MARGIN = dict(Left="0", Right="0", Top="0", Bottom="0")
_VERTICAL_ALIGNMENTS = {"top": "Top", "center": "Center", "bottom": "Bottom"}
_CELL_KEYS = {"text", "bold", "italic", "color", "size_pt", "background", "align"}
_BORDER_STYLES = {"all", "none", "horizontal"}
# Margin line numbers: small grey digits beside each laid-out line of a passage paragraph.
_LINE_NUMBER_DEFAULTS = {"size_pt": 7, "color": "#8C8C8C", "gap_mm": 1.5, "width_mm": 6.0}


def apply_object(doc, operation):
    """Apply one field-validated operation to the in-memory native document."""
    _HANDLERS[operation["op"]](doc, operation)


def _find_cell(doc, operation):
    table = doc.get(operation["target"], "table")
    row, col = operation["row"], operation["col"]
    if type(row) is not int or type(col) is not int or row < 1 or col < 1:
        raise ValueError("표의 행과 열은 1부터 시작하는 정수여야 합니다.")
    cell = next((node for node in table.findall("./ROW/CELL")
                 if int(node.get("RowAddr", "-1")) == row - 1
                 and int(node.get("ColAddr", "-1")) == col - 1), None)
    if cell is None:
        raise ValueError(f"{row}행 {col}열에 독립된 셀이 없습니다. 병합 셀은 시작 주소를 쓰세요.")
    if cell.get("Protect", "false").lower() == "true":
        raise ValueError("보호된 표 셀은 수정할 수 없습니다.")
    return cell


def _edit_cell(doc, operation):
    cell = _find_cell(doc, operation)
    paragraphs = cell.findall("./PARALIST/P")
    if len(paragraphs) != 1:
        raise ValueError("이 명령은 문단이 한 개인 셀에 적용할 수 있습니다. 문단 ID를 사용하세요.")
    paragraph = paragraphs[0]
    value = _cell_text(operation["text"])
    if doc.plain_text(paragraph) == value:
        return
    styles = {run.get("CharShape", "0") for run in paragraph.findall("./TEXT")
              if run.find("./CHAR") is not None}
    if len(styles) > 1:
        raise ValueError("글자 모양이 섞인 셀입니다. 문단 ID로 부분 텍스트를 바꾸세요.")
    doc.replace_paragraph_text(paragraph, value)


def _format_cell(doc, operation):
    if not OBJECT_FIELDS["format_table_cell"][1].intersection(operation):
        raise ValueError("바꿀 셀 서식을 하나 이상 지정하세요.")
    characters = {key: operation[key] for key in CELL_CHARACTER_FIELDS if key in operation}
    cell = _find_cell(doc, operation)
    contents = cell.find("PARALIST")
    if contents is None:
        raise ValueError("셀의 문단 목록을 찾을 수 없습니다.")

    vertical = None
    if "vertical_align" in operation:
        vertical = tr_layout.choice(operation["vertical_align"], "vertical_align", _VERTICAL_ALIGNMENTS)
    background = _color_ref(operation["background"], "background") if "background" in operation else None
    if "align" in operation:
        alignment = {"align": operation["align"]}
        for paragraph in contents.findall("P"):
            tr_layout.set_paragraph_format(doc, paragraph, alignment)
    if vertical is not None:
        contents.set("VertAlign", vertical)
    if background is not None:
        cell.set("BorderFill", _filled_border(doc.root, cell.get("BorderFill"), background))
    if characters:
        for paragraph in contents.findall("P"):
            if doc.plain_text(paragraph):
                doc.format_character_runs(paragraph, characters)


def _cell_text(value):
    if not isinstance(value, str):
        raise ValueError("셀 내용은 문자열이어야 합니다.")
    if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF or char in "\ufffe\uffff"
           for char in value):
        raise ValueError("셀 내용은 줄바꿈과 제어 문자가 없는 한 줄이어야 합니다.")
    return value


def _cell_lines(value):
    """New table cells may hold several paragraphs separated by newlines."""
    if not isinstance(value, str):
        raise ValueError("셀 내용은 문자열이어야 합니다.")
    return [_cell_text(line) for line in value.replace("\r\n", "\n").split("\n")]


def _paragraph_margins(doc, paragraph):
    shapes = doc.root.find("HEAD/MAPPINGTABLE/PARASHAPELIST")
    shape = None if shapes is None else next(
        (item for item in shapes.findall("PARASHAPE") if item.get("Id") == paragraph.get("ParaShape", "0")), None)
    margin = None if shape is None else shape.find("PARAMARGIN")
    if margin is None:
        return 0
    return max(int(margin.get("Left", "0")), 0) + max(int(margin.get("Right", "0")), 0)


def _available_width(doc, anchor):
    """Width a new inline object can use at the anchor paragraph, in HWP units."""
    area = _area_width(doc, anchor)
    if area is None:
        return None
    return max(area - _paragraph_margins(doc, anchor), 0) or None


def _area_width(doc, anchor):
    node = anchor
    while True:
        parent = doc.parent(node)
        if parent is None:
            return None
        if parent.tag == "CELL":
            margins = parent.find("CELLMARGIN")
            inset = 0
            if margins is not None and parent.get("HasMargin", "false").lower() == "true":
                inset = int(margins.get("Left", "0")) + int(margins.get("Right", "0"))
            else:
                table = doc.parent(doc.parent(parent))
                inner = table.find("INSIDEMARGIN") if table is not None else None
                inset = 0 if inner is None else int(inner.get("Left", "0")) + int(inner.get("Right", "0"))
            return max(int(parent.get("Width", "0")) - inset, 0) or None
        if parent.tag == "SECTION":
            page = tr_layout.section_page(parent)
            body = tr_layout.dimensions(page, tr_layout.child_or_error(page, "PAGEMARGIN"))[0]
            columns = None
            for paragraph in parent.findall("P"):
                for definition in paragraph.findall("TEXT/COLDEF"):
                    columns = definition
                if paragraph is node:
                    break
            if columns is None:
                return body
            count = max(int(columns.get("Count", "1")), 1)
            gap = int(columns.get("SameGap", "0"))
            widths = [int(item.get("Width", "0")) for item in columns.iter("COLUMN")]
            if columns.get("SameSize", "true") != "true" and widths and min(widths) > 0:
                return min(widths)
            return max((body - gap * (count - 1)) // count, 0) or None
        if parent.tag == "PARALIST":
            owner = doc.parent(parent)
            if owner is None or owner.tag != "CELL":
                return None
        node = parent


def _column_widths(operation, cols, width):
    if "col_widths" not in operation:
        return [(index + 1) * width // cols - index * width // cols for index in range(cols)]
    ratios = operation["col_widths"]
    if (not isinstance(ratios, list) or len(ratios) != cols
            or any(type(value) not in (int, float) or value <= 0 for value in ratios)):
        raise ValueError("col_widths는 열 수와 같은 길이의 양수 배열이어야 합니다.")
    total = sum(ratios)
    edges = [round(width * sum(ratios[:index]) / total) for index in range(cols + 1)]
    widths = [right - left for left, right in zip(edges, edges[1:])]
    if min(widths) < 1:
        raise ValueError("열 너비가 너무 좁습니다.")
    return widths


def _dimension_units(value, key):
    if type(value) not in (int, float) or not 0 < value <= 1188:
        raise ValueError(f"{key} 값은 0보다 크고 1,188mm 이하여야 합니다.")
    result = round(value * 7200 / 25.4)
    if not result:
        raise ValueError(f"{key} 값이 한글 문서의 길이 단위보다 작습니다.")
    return result


def _next_instance_id(root):
    ids = {int(value) for element in root.iter()
           for key in ("InstId", "InstID") if (value := element.get(key, "")).isdecimal()}
    candidate = 10_000
    while candidate in ids or candidate + 1 in ids:
        candidate += 1
    return candidate


def _new_shape(doc, owner, numbering, width, height):
    order = max((int(shape.get("ZOrder", "0")) for shape in doc.root.iter("SHAPEOBJECT")),
                default=-1) + 1
    instance = _next_instance_id(doc.root)
    shape = ET.SubElement(owner, "SHAPEOBJECT", InstId=str(instance), Lock="false",
                          NumberingType=numbering, TextWrap="TopAndBottom", ZOrder=str(order))
    ET.SubElement(shape, "SIZE", Height=str(height), HeightRelTo="Absolute", Protect="false",
                  Width=str(width), WidthRelTo="Absolute")
    ET.SubElement(shape, "POSITION", AffectLSpacing="false", AllowOverlap="false",
                  FlowWithText="true", HoldAnchorAndSO="false", HorzAlign="Left",
                  HorzOffset="0", HorzRelTo="Para", TreatAsChar="true", VertAlign="Top",
                  VertOffset="0", VertRelTo="Para")
    ET.SubElement(shape, "OUTSIDEMARGIN", _ZERO_MARGIN)
    return instance


def _border_list(root):
    borders = root.find("./HEAD/MAPPINGTABLE/BORDERFILLLIST")
    if borders is None:
        raise ValueError("문서의 테두리 서식 목록을 찾을 수 없습니다.")
    return borders


def _append_border(borders, border):
    existing = borders.findall("BORDERFILL")
    identifier = str(max((int(item.get("Id", "0")) for item in existing), default=0) + 1)
    border.set("Id", identifier)
    borders.append(border)
    borders.set("Count", str(len(existing) + 1))
    return identifier


def _table_border(root, style="all", color="0"):
    border = ET.Element("BORDERFILL", BackSlash="0", Slash="0", Shadow="false", ThreeD="false")
    drawn = {"all": {"LEFTBORDER", "RIGHTBORDER", "TOPBORDER", "BOTTOMBORDER"},
             "horizontal": {"TOPBORDER", "BOTTOMBORDER"}, "none": set()}[style]
    for kind in ("LEFTBORDER", "RIGHTBORDER", "TOPBORDER", "BOTTOMBORDER"):
        if kind in drawn:
            ET.SubElement(border, kind, Type="Solid", Width="0.12mm", Color=color)
        else:
            ET.SubElement(border, kind, Type="None", Width="0.1mm", Color="0")
    ET.SubElement(border, "DIAGONAL", Type="None", Width="0.12mm", Color="0")
    return _append_border(_border_list(root), border)


def _color_ref(value, key):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError(f"{key}에 #RRGGBB 색상 값을 입력하세요.")
    return str(int.from_bytes(bytes.fromhex(value[1:]), "little"))


def _filled_border(root, identifier, color):
    borders = _border_list(root)
    base = next((item for item in borders.findall("BORDERFILL") if item.get("Id") == identifier), None)
    if base is None:
        raise ValueError("셀의 원래 테두리 서식을 찾을 수 없습니다.")
    clone = deepcopy(base)
    brush = clone.find("FILLBRUSH")
    if brush is not None:
        clone.remove(brush)
    brush = ET.SubElement(clone, "FILLBRUSH")
    ET.SubElement(brush, "WINDOWBRUSH", Alpha="0", FaceColor=color, HatchColor="0")
    return _append_border(borders, clone)


def _cell_spec(value):
    """A cell is a string or an object {text, bold, italic, color, size_pt, background, align}."""
    if isinstance(value, str):
        return {"text": value}
    if not isinstance(value, dict) or "text" not in value:
        raise ValueError("셀은 문자열 또는 text를 담은 객체여야 합니다.")
    unknown = set(value) - _CELL_KEYS
    if unknown:
        raise ValueError(f"셀 객체에 알 수 없는 항목이 있습니다. {sorted(unknown)}. 사용 가능 {sorted(_CELL_KEYS)}")
    return value


def _padding(value):
    if type(value) in (int, float):
        value = [value, value]
    if (not isinstance(value, list) or len(value) != 2
            or any(type(item) not in (int, float) or not 0 <= item <= 20 for item in value)):
        raise ValueError("padding_mm는 0~20 사이 숫자 또는 [좌우, 위아래] 배열이어야 합니다.")
    side, edge = (round(item * 7200 / 25.4) for item in value)
    return dict(Left=str(side), Right=str(side), Top=str(edge), Bottom=str(edge))


def _insert_table(doc, operation):
    from tr_core import parse_markup

    anchor = doc.get(operation["target"], "paragraph")
    cells = operation["cells"]
    if not isinstance(cells, list) or not cells:
        raise ValueError("새 표의 행을 하나 이상 입력하세요.")
    if not isinstance(cells[0], list) or not cells[0]:
        raise ValueError("새 표의 열을 하나 이상 입력하세요.")
    rows, cols = len(cells), len(cells[0])
    if rows * cols > 16384:
        raise ValueError("한글 표의 셀 수는 최대 16,384개입니다.")
    specs = []
    for row in cells:
        if not isinstance(row, list) or len(row) != cols:
            raise ValueError("새 표의 모든 행은 열 수가 같아야 합니다.")
        specs.append([_cell_spec(value) for value in row])
        for spec in specs[-1]:
            _cell_lines(spec["text"])
    position = operation.get("position", "end")
    markup = operation.get("markup", False)
    if type(markup) is not bool:
        raise ValueError("markup에는 true 또는 false를 입력하세요.")

    header_row = operation.get("header_row", "header_background" in operation or "header_color" in operation)
    if type(header_row) is not bool:
        raise ValueError("header_row에 true 또는 false를 입력하세요.")
    header_fill = None
    if "header_background" in operation or "header_color" in operation:
        if not header_row:
            raise ValueError("header_background·header_color를 지정하려면 첫 행을 제목 행으로 설정하세요.")
    if "header_background" in operation:
        header_fill = _color_ref(operation["header_background"], "header_background")
    stripe = (_color_ref(operation["stripe_background"], "stripe_background")
              if "stripe_background" in operation else None)
    style = operation.get("border", "all")
    if style not in _BORDER_STYLES:
        raise ValueError(f"border는 {', '.join(sorted(_BORDER_STYLES))} 중 하나여야 합니다.")
    line_color = _color_ref(operation["border_color"], "border_color") if "border_color" in operation else "0"
    alignments = operation.get("col_align")
    if alignments is not None and (not isinstance(alignments, list) or len(alignments) != cols):
        raise ValueError("col_align은 열 수와 같은 길이의 정렬 배열이어야 합니다.")
    for alignment in alignments or []:
        tr_layout.choice(alignment, "col_align", tr_layout.ALIGNMENTS)
    if "width_mm" in operation:
        width = _dimension_units(operation["width_mm"], "width_mm")
    else:
        # Fill the column or cell the table lands in, like a table drawn in Hangul.
        available = _available_width(doc, anchor)
        width = available - 100 if available and available > 2000 else _dimension_units(55, "width_mm")
    if width < cols * 300:
        raise ValueError("열당 폭이 너무 좁습니다. 열 수를 줄이거나 표 폭을 늘리세요.")
    column_widths = _column_widths(operation, cols, width)
    row_height = 1800
    border = _table_border(doc.root, style, line_color)
    fills = {}

    def filled(color):
        if color is None:
            return border
        if color not in fills:
            fills[color] = _filled_border(doc.root, border, color)
        return fills[color]

    table = ET.Element("TABLE", BorderFill=border, CellSpacing="0", ColCount=str(cols),
                       PageBreak="Cell", RepeatHeader=str(header_row).lower(), RowCount=str(rows))
    _new_shape(doc, table, "Table", width, rows * row_height)
    # Hangul's own default cell padding: 1.8mm left/right, 0.5mm top/bottom.
    inside = (_padding(operation["padding_mm"]) if "padding_mm" in operation
              else dict(Left="510", Right="510", Top="141", Bottom="141"))
    ET.SubElement(table, "INSIDEMARGIN", inside)
    base_shape = doc.char_shape(anchor)
    table_characters = {key: operation[key] for key in ("font", "size_pt", "color", "bold") if key in operation}
    if table_characters:
        base_shape = doc.character_variant(base_shape, doc.character_changes(table_characters))
    header_characters = {"bold": True}
    if "header_color" in operation:
        header_characters["color"] = operation["header_color"]
    header_shape = doc.character_variant(base_shape, doc.character_changes(header_characters))
    paragraph_fields = {}
    if "line_spacing_percent" in operation:
        paragraph_fields["line_spacing_percent"] = operation["line_spacing_percent"]
    for row_number, values in enumerate(specs):
        header = header_row and row_number == 0
        body_index = row_number - (1 if header_row else 0)
        row = ET.SubElement(table, "ROW")
        for col_number, spec in enumerate(values):
            if "background" in spec:
                fill = filled(_color_ref(spec["background"], "background"))
            elif header:
                fill = filled(header_fill)
            elif stripe is not None and body_index % 2 == 1:
                fill = filled(stripe)
            else:
                fill = border
            cell = ET.SubElement(row, "CELL", BorderFill=fill,
                                 ColAddr=str(col_number), ColSpan="1",
                                 Dirty="false", Editable="false", HasMargin="false", Header=str(header).lower(),
                                 Height=str(row_height), Protect="false", RowAddr=str(row_number),
                                 RowSpan="1", Width=str(column_widths[col_number]))
            ET.SubElement(cell, "CELLMARGIN", Left="141", Right="141", Top="141", Bottom="141")
            plist = ET.SubElement(cell, "PARALIST", LineWrap="Break", LinkListID="0", LinkListIDNext="0",
                                  TextDirection="0", VertAlign="Center")
            alignment = spec.get("align") or ("center" if header else
                                              (alignments[col_number] if alignments else None))
            characters = {key: spec[key] for key in ("bold", "italic", "color", "size_pt") if key in spec}
            for line in _cell_lines(spec["text"]):
                text, spans = parse_markup(line) if markup else (line, [])
                paragraph = ET.SubElement(plist, "P", ParaShape="0", Style="0")
                ET.SubElement(paragraph, "TEXT", CharShape=header_shape if header else base_shape)
                if text:
                    doc.replace_paragraph_text(paragraph, text)
                fields = dict(paragraph_fields)
                if alignment is not None:
                    fields["align"] = alignment
                if fields:
                    tr_layout.set_paragraph_format(doc, paragraph, fields)
                if text and characters:
                    doc.format_character_runs(paragraph, characters)
                if text:
                    doc.bold_spans(paragraph, spans)
    doc.register(operation.get("id"), table, "table")
    doc.add_control(anchor, table, position)


def _positive(value, name, lower, upper, integer=False):
    kinds = (int,) if integer else (int, float)
    if type(value) not in kinds or not lower <= value <= upper:
        kind = "정수" if integer else "숫자"
        raise ValueError(f"{name} 값은 {lower}~{upper} 범위의 {kind}여야 합니다.")
    return value


def line_pitch(doc, paragraph, *, pitch=None):
    """Glyph height and line pitch in HWP units. Infer pitch from percentage spacing when omitted."""
    characters = doc.root.find("HEAD/MAPPINGTABLE/CHARSHAPELIST")
    char = None if characters is None else next(
        (item for item in characters.findall("CHARSHAPE") if item.get("Id") == doc.dominant_shape(paragraph)), None)
    height = int(char.get("Height", "1000")) if char is not None else 1000
    if pitch is not None:
        return height, pitch
    shapes = doc.root.find("HEAD/MAPPINGTABLE/PARASHAPELIST")
    shape = None if shapes is None else next(
        (item for item in shapes.findall("PARASHAPE") if item.get("Id") == paragraph.get("ParaShape", "0")), None)
    margin = None if shape is None else shape.find("PARAMARGIN")
    if margin is not None and margin.get("LineSpacingType", "Percent") != "Percent":
        raise ValueError("이 문단은 줄 간격이 비율(%)이 아니므로 pitch_pt(줄 사이 거리)를 지정하세요.")
    spacing = int(margin.get("LineSpacing", "160")) if margin is not None else 160
    return height, round(height * spacing / 100)


def _insert_line_numbers(doc, operation):
    """Float a borderless one-column table of line numbers beside a passage paragraph.

    Hangul 2018 has no native line numbering, so the digits are drawn as a table that stays
    in front of the text, anchored to the paragraph, one row per laid-out line.
    """
    anchor = doc.get(operation["target"], "paragraph")
    if doc.parent(anchor) is None or doc.parent(anchor).tag != "SECTION":
        raise ValueError("줄 번호는 본문(표 밖) 문단에만 넣을 수 있습니다.")
    count = _positive(operation["lines"], "lines", 1, 500, integer=True)
    start = operation.get("start", getattr(doc, "next_line_number", 1))
    start = _positive(start, "start", 0, 99999, integer=True)
    every = _positive(operation.get("every", 1), "every", 1, 100, integer=True)
    settings = {**_LINE_NUMBER_DEFAULTS, **{key: operation[key] for key in _LINE_NUMBER_DEFAULTS
                                            if key in operation}}
    size = _positive(settings["size_pt"], "size_pt", 3, 30)
    gap = round(_positive(settings["gap_mm"], "gap_mm", 0, 60) * 7200 / 25.4)
    width = round(_positive(settings["width_mm"], "width_mm", 2, 60) * 7200 / 25.4)
    side = operation.get("side", "left")
    if side not in ("left", "right"):
        raise ValueError("side는 left 또는 right여야 합니다.")
    pitch_override = (round(_positive(operation["pitch_pt"], "pitch_pt", 1, 200) * 100)
                      if "pitch_pt" in operation else None)
    font_height, pitch = line_pitch(doc, anchor, pitch=pitch_override)
    # Centre the small digits on the passage glyphs. Hangul sets each line's text at the top of
    # its line box; measured on Hangul 2018 PDFs the glyph middles line up when the number
    # column starts 0.32 x (passage size - number size) below the first line's top.
    shift = round((font_height - size * 100) * 0.32)
    if "shift_pt" in operation:
        shift += round(_positive(operation["shift_pt"], "shift_pt", -100, 100) * 100)

    blank = _table_border(doc.root, "none")
    table = ET.Element("TABLE", BorderFill=blank, CellSpacing="0", ColCount="1", PageBreak="None",
                       RepeatHeader="false", RowCount=str(count))
    _new_shape(doc, table, "Table", width, count * pitch)
    shape = table.find("SHAPEOBJECT")
    shape.set("TextWrap", "InFrontOfText")
    shape.set("TextFlow", "BothSides")
    # Measured outwards from the column edge on the chosen side, so the digits sit in the margin.
    shape.find("POSITION").attrib.update(
        AllowOverlap="true", FlowWithText="false", HorzAlign="Left" if side == "left" else "Right",
        HorzOffset=str(-(width + gap)), HorzRelTo="Column", TreatAsChar="false",
        VertAlign="Top", VertOffset=str(shift), VertRelTo="Para")
    ET.SubElement(table, "INSIDEMARGIN", _ZERO_MARGIN)
    char_shape = doc.character_variant(doc.dominant_shape(anchor), doc.character_changes(
        {"size_pt": size, "color": settings["color"], "bold": False, "italic": False,
         **({"font": operation["font"]} if "font" in operation else {})}))
    number_style = {"align": "right" if side == "left" else "left", "line_spacing_percent": 100,
                    "left_margin_pt": 0, "right_margin_pt": 0, "indent_pt": 0,
                    "space_before_pt": 0, "space_after_pt": 0}
    for index in range(count):
        number = start + index
        row = ET.SubElement(table, "ROW")
        cell = ET.SubElement(row, "CELL", BorderFill=blank, ColAddr="0", ColSpan="1", Dirty="false",
                             Editable="false", HasMargin="true", Header="false", Height=str(pitch),
                             Protect="false", RowAddr=str(index), RowSpan="1", Width=str(width))
        ET.SubElement(cell, "CELLMARGIN", _ZERO_MARGIN)
        plist = ET.SubElement(cell, "PARALIST", LineWrap="Break", LinkListID="0", LinkListIDNext="0",
                              TextDirection="0", VertAlign="Top")
        paragraph = ET.SubElement(plist, "P", ParaShape="0", Style="0")
        ET.SubElement(paragraph, "TEXT", CharShape=char_shape)
        if number % every == 0 or every == 1:
            doc.replace_paragraph_text(paragraph, str(number))
        tr_layout.set_paragraph_format(doc, paragraph, number_style)
    doc.next_line_number = start + count
    doc.register(operation.get("id"), table, "table")
    # A paragraph-relative object is placed from the line holding its anchor, so anchor the
    # numbers at the start of the paragraph (after any section or column definition).
    runs = list(anchor)
    index = 0
    while index < len(runs) and runs[index].tag == "TEXT" and (
            runs[index].find("SECDEF") is not None or runs[index].find("COLDEF") is not None):
        index += 1
    first = next((run for run in anchor.findall("TEXT")), None)
    holder = ET.Element("TEXT", CharShape=first.get("CharShape", "0") if first is not None else "0")
    holder.append(table)
    ET.SubElement(holder, "CHAR")
    anchor.insert(index, holder)


def _delete_object(doc, operation):
    target_id = operation["target"]
    if not isinstance(target_id, str):
        raise ValueError("삭제할 개체의 ID를 입력하세요.")
    category = {"I": "picture", "T": "table"}.get(target_id[:1], "object")
    node = doc.get(target_id, category)
    parent = doc.parent(node)
    if not can_delete_object(node, parent):
        raise ValueError("이 개체는 독립된 또는 그룹 안의 본문 개체로 삭제할 수 없습니다.")
    if doc.parent(parent) is None:
        raise ValueError("이 개체의 상위 구조가 이미 삭제되었습니다.")
    parent.remove(node)


def can_delete_object(node, parent):
    return node.tag in _REMOVABLE_OBJECTS and parent is not None and parent.tag in {"TEXT", "CONTAINER"}


def _png_dimensions(data):
    offset = 8
    has_image_data = False
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        end = offset + 8 + length
        if end + 4 > len(data):
            break
        expected_crc = int.from_bytes(data[end:end + 4], "big")
        if zlib.crc32(data[offset + 4:end]) != expected_crc:
            break
        if kind == b"IDAT":
            has_image_data = True
        if kind == b"IEND" and length == 0 and has_image_data:
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        offset = end + 4
    raise ValueError("PNG 그림이 잘렸거나 손상되었습니다.")


def _jpeg_dimensions(data):
    offset = 2
    while offset < len(data):
        if data[offset] != 0xFF:
            break
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset == len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in (0xDA, 0xD9) or offset + 2 > len(data):
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > len(data):
            break
        if marker in _JPEG_FRAME_MARKERS and length >= 8:
            height = int.from_bytes(data[offset + 3:offset + 5], "big")
            width = int.from_bytes(data[offset + 5:offset + 7], "big")
            return width, height
        offset += length
    raise ValueError("JPEG 그림의 크기 정보를 읽을 수 없습니다.")


def _read_image(doc, asset):
    if not isinstance(asset, str) or not asset:
        raise ValueError("assets 폴더의 그림 파일명을 입력하세요.")
    path = doc.asset(asset)
    limit = 12 * 1024 * 1024
    try:
        with path.open("rb") as image_file:
            data = image_file.read(limit + 1)
    except OSError as error:
        raise ValueError(f"assets 폴더에서 그림을 읽을 수 없습니다. {asset}") from error
    if len(data) > limit:
        raise ValueError("그림은 12MB 이하여야 합니다.")
    suffix = path.suffix.lower()
    if suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR") and len(data) >= 45:
        width, height = _png_dimensions(data)
        image_format = "png"
    elif suffix in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8\xff") and b"\xff\xd9" in data:
        width, height = _jpeg_dimensions(data)
        image_format = "jpg"
    else:
        raise ValueError("파일 확장자와 데이터가 일치하는 PNG 또는 JPEG 그림을 사용하세요.")
    if not width or not height or width > 20000 or height > 20000 or width * height > 50_000_000:
        raise ValueError("그림 해상도가 0이거나 지원 범위를 초과합니다.")
    return data, image_format, width, height


def _embed_image(root, data, image_format):
    mapping = root.find("./HEAD/MAPPINGTABLE")
    if mapping is None:
        raise ValueError("문서의 바이너리 목록을 찾을 수 없습니다.")
    items = mapping.find("./BINDATALIST")
    if items is None:
        items = ET.Element("BINDATALIST", Count="0")
        mapping.insert(0, items)
    tail = root.find("./TAIL")
    if tail is None:
        tail = ET.SubElement(root, "TAIL")
    storage = tail.find("./BINDATASTORAGE")
    if storage is None:
        storage = ET.Element("BINDATASTORAGE")
        tail.insert(0, storage)
    data_id = str(max((int(element.get("Id", "0")) for element in storage.findall("./BINDATA")),
                      default=0) + 1)
    item_number = len(items.findall("./BINITEM")) + 1
    items.set("Count", str(item_number))
    ET.SubElement(items, "BINITEM", BinData=data_id, Format=image_format, Type="Embedding")
    encoded = base64.b64encode(data).decode("ascii")
    ET.SubElement(storage, "BINDATA", Encoding="Base64", Id=data_id, Size=str(len(encoded))).text = encoded
    return str(item_number)


def _replace_image(doc, operation):
    picture = doc.get(operation["target"], "picture")
    image = picture.find("./IMAGE")
    if image is None:
        raise ValueError("선택한 그림 안에서 이미지 참조를 찾을 수 없습니다.")
    data, image_format, _, _ = _read_image(doc, operation["asset"])
    image.set("BinItem", _embed_image(doc.root, data, image_format))


def _insert_image(doc, operation):
    anchor = doc.get(operation["target"], "paragraph")
    data, image_format, pixels_wide, pixels_high = _read_image(doc, operation["asset"])
    width_mm, height_mm = operation.get("width_mm"), operation.get("height_mm")
    if "width_mm" in operation:
        _dimension_units(width_mm, "width_mm")
    if "height_mm" in operation:
        _dimension_units(height_mm, "height_mm")
    aspect = pixels_wide / pixels_high
    if width_mm is None and height_mm is None:
        width_mm = min(45, 45 * aspect)
        available = _available_width(doc, anchor)
        if available:
            width_mm = min(width_mm, available * 25.4 / 7200)
    if width_mm is None:
        width_mm = height_mm * aspect
    if height_mm is None:
        height_mm = width_mm / aspect
    width = _dimension_units(width_mm, "width_mm")
    height = _dimension_units(height_mm, "height_mm")

    picture = ET.Element("PICTURE", Reverse="false")
    instance = _new_shape(doc, picture, "Figure", width, height)
    component = ET.SubElement(picture, "SHAPECOMPONENT", CurHeight=str(height), CurWidth=str(width),
                              GroupLevel="0", HorzFlip="false", InstID=str(instance + 1),
                              OriHeight=str(height), OriWidth=str(width), VertFlip="false", XPos="0", YPos="0")
    ET.SubElement(component, "ROTATIONINFO", Angle="0", CenterX=str(width // 2), CenterY=str(height // 2), Rotate="1")
    rendering = ET.SubElement(component, "RENDERINGINFO")
    for tag in ("TRANSMATRIX", "SCAMATRIX", "ROTMATRIX"):
        ET.SubElement(rendering, tag, E1="1.00000", E2="0.00000", E3="0.00000", E4="0.00000",
                      E5="1.00000", E6="0.00000")
    ET.SubElement(picture, "IMAGERECT", X0="0", X1=str(width), X2=str(width), X3="0",
                  Y0="0", Y1="0", Y2=str(height), Y3=str(height))
    ET.SubElement(picture, "IMAGECLIP", Bottom=str(height), Left="0", Right=str(width), Top="0")
    ET.SubElement(picture, "INSIDEMARGIN", _ZERO_MARGIN)
    ET.SubElement(picture, "IMAGEDIM", Height=str(height), Width=str(width))
    ET.SubElement(picture, "IMAGE", Alpha="0", BinItem=_embed_image(doc.root, data, image_format),
                  Bright="0", Contrast="0", Effect="RealPic")
    ET.SubElement(picture, "EFFECTS")
    doc.register(operation.get("id"), picture, "picture")
    doc.add_control(anchor, picture, operation.get("position", "end"))


_HANDLERS = {
    "set_table_cell": _edit_cell,
    "format_table_cell": _format_cell,
    "insert_table": _insert_table,
    "insert_line_numbers": _insert_line_numbers,
    "delete_object": _delete_object,
    "replace_image": _replace_image,
    "insert_image": _insert_image,
}
