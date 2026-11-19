"""Native layout edits against a Hancom HWPML document."""

from copy import deepcopy
from xml.etree import ElementTree as ET

from tr_core import CHARACTER_FIELDS, PARAGRAPH_FIELDS


LAYOUT_FIELDS = {
    "set_columns": ({"target", "count"}, {"gap_mm", "direction"}),
    "set_page_layout": ({"target"}, {"width_mm", "height_mm", "margins_mm"}),
    "insert_section": ({"target"}, {"columns", "gap_mm"}),
    "format_paragraph": ({"target"}, PARAGRAPH_FIELDS | CHARACTER_FIELDS),
}

ALIGNMENTS = {
    "left": "Left",
    "right": "Right",
    "center": "Center",
    "justify": "Justify",
    "distribute": "Distribute",
    "distribute_space": "DistributeSpace",
}
COLUMN_DIRECTIONS = {"left": "Left", "right": "Right", "mirror": "Mirror"}
MARGIN_DEFAULTS = {
    "Top": "5668",
    "Bottom": "4252",
    "Left": "8504",
    "Right": "8504",
    "Header": "4252",
    "Footer": "4252",
    "Gutter": "0",
}
HWPUNITS_PER_MM = 7200 / 25.4


def bounded_integer(value, name, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{name} 값은 {lower}~{upper} 범위의 정수여야 합니다.")
    return value


def units(value, name, lower=0, upper=1188):
    if type(value) not in (int, float) or not lower <= value <= upper:
        raise ValueError(f"{name} 값은 {lower}~{upper} mm 범위의 숫자여야 합니다.")
    return round(value * HWPUNITS_PER_MM)


def choice(value, name, choices):
    if type(value) is not str or value not in choices:
        raise ValueError(f"{name} 값은 다음 중 하나여야 합니다. {', '.join(choices)}")
    return choices[value]


def child_or_error(element, path):
    child = element.find(path)
    if child is None:
        raise ValueError(f"한글 문서에서 {path} 구조를 찾을 수 없습니다.")
    return child


def dimensions(page, margins):
    width = int(page.get("Width", "59528"))
    height = int(page.get("Height", "84188"))
    if page.get("Landscape") == "1":
        width, height = height, width
    values = {name: int(margins.get(name, default)) for name, default in MARGIN_DEFAULTS.items()}
    body_width = width - values["Left"] - values["Right"]
    body_height = height - values["Top"] - values["Bottom"]
    if page.get("GutterType") == "TopBottom":
        body_height -= values["Gutter"]
    else:
        body_width -= values["Gutter"]
    return body_width, body_height, height, values


def section_page(section):
    return child_or_error(section, "P/TEXT/SECDEF/PAGEDEF")


def column_area(doc, columns):
    element = doc.parent(columns)
    while element is not None:
        if element.tag == "SECTION":
            page = section_page(element)
            return dimensions(page, child_or_error(page, "PAGEMARGIN"))[0]
        if element.tag == "CELL":
            margins = element.find("CELLMARGIN")
            horizontal = 0
            if margins is not None:
                horizontal = int(margins.get("Left", "0")) + int(margins.get("Right", "0"))
            return int(element.get("Width", "0")) - horizontal
        if element.tag == "PARALIST":
            # A text box, note, or master page has its own width.
            owner = doc.parent(element)
            if owner is None or owner.tag != "CELL":
                return None
        element = doc.parent(element)
    return None


def configure_columns(columns, operation, area, count_field="count"):
    current_count = int(columns.get("Count", "1"))
    count = current_count
    if count_field in operation:
        count = bounded_integer(operation[count_field], count_field, 1, 255)
    if "direction" in operation:
        columns.set("Layout", choice(operation["direction"], "direction", COLUMN_DIRECTIONS))
    columns.set("Count", str(count))
    if count != current_count or "gap_mm" in operation:
        gap = (
            units(operation["gap_mm"], "gap_mm")
            if "gap_mm" in operation
            else int(columns.get("SameGap", "0"))
        )
        if count == 1:
            gap = 0
        if area is not None and area > 0 and gap * (count - 1) >= area:
            raise ValueError("단 사이 간격이 본문 너비에 비해 너무 큽니다.")
        columns.set("SameSize", "true")
        columns.set("SameGap", str(gap))
        for widths in columns.findall("COLUMNTABLE"):
            columns.remove(widths)


def set_columns(doc, operation):
    columns = doc.get(operation["target"], "columns")
    configure_columns(columns, operation, column_area(doc, columns))


def set_page_layout(doc, operation):
    section = doc.get(operation["target"], "section")
    page = section_page(section)
    margins = child_or_error(page, "PAGEMARGIN")
    if not LAYOUT_FIELDS["set_page_layout"][1].intersection(operation):
        raise ValueError("set_page_layout에는 용지 크기 또는 여백을 지정해야 합니다.")

    width_field, height_field = "Width", "Height"
    if page.get("Landscape") == "1":
        width_field, height_field = height_field, width_field
    for field, attribute in (("width_mm", width_field), ("height_mm", height_field)):
        if field in operation:
            page.set(attribute, str(units(operation[field], field, 20)))
    if "margins_mm" in operation:
        requested = operation["margins_mm"]
        if type(requested) is not dict or not requested:
            raise ValueError("margins_mm에는 한 개 이상의 여백 값을 지정해야 합니다.")
        for name, value in requested.items():
            if name not in {candidate.lower() for candidate in MARGIN_DEFAULTS}:
                raise ValueError(f"지원하지 않는 용지 여백 이름입니다. {name}")
            margins.set(name.capitalize(), str(units(value, f"margins_mm.{name}")))

    body_width, body_height, page_height, values = dimensions(page, margins)
    if min(body_width, body_height) <= 0 or values["Header"] + values["Footer"] >= page_height:
        raise ValueError("용지에 본문과 머리말·꼬리말을 배치할 공간이 부족합니다.")
    for paragraph in section.findall("P"):
        for columns in paragraph.findall("TEXT/COLDEF"):
            count = int(columns.get("Count", "1"))
            gap = int(columns.get("SameGap", "0"))
            if columns.get("SameSize") == "true" and gap * (count - 1) >= body_width:
                raise ValueError("기존 단 사이 간격을 유지하기에 용지의 본문 너비가 부족합니다.")


def clone_section_definition(doc, source):
    cloned = deepcopy(source)
    for master in cloned.findall("EXT_MASTERPAGE"):
        cloned.remove(master)
    start = cloned.find("STARTNUMBER")
    if start is not None:
        for name in ("Page", "Figure", "Table", "Equation"):
            start.set(name, "0")
        start.set("PageStartsOn", "Both")
    hiding = cloned.find("HIDE")
    if hiding is not None:
        for name in hiding.attrib:
            hiding.set(name, "false")

    used = {
        int(value)
        for element in doc.root.iter()
        for name, value in element.attrib.items()
        if name.lower() in {"instid", "linklistid", "linklistidnext"} and value.isdecimal()
    }
    next_id = 1 << 30

    def allocate():
        nonlocal next_id
        while next_id in used:
            next_id += 1
        result = str(next_id)
        next_id += 1
        return result

    links = {"0": "0"}
    for element in cloned.iter():
        for name, value in element.attrib.items():
            if name.lower() == "instid":
                element.set(name, allocate())
            if name == "LinkListID" and value not in links:
                links[value] = allocate()
    for element in cloned.iter("PARALIST"):
        for name in ("LinkListID", "LinkListIDNext"):
            if name in element.attrib:
                element.set(name, links.get(element.get(name), "0"))
    return cloned


def insert_section(doc, operation):
    anchor = doc.get(operation["target"], "paragraph")
    section = doc.parent(anchor)
    if section is None or section.tag != "SECTION":
        raise ValueError("새 구역을 시작하려면 본문 문단을 지정해 주세요.")
    index = list(section).index(anchor)
    if index == 0 or anchor.find("TEXT/SECDEF") is not None:
        raise ValueError("이미 구역 처음에 있는 문단입니다.")

    body = child_or_error(doc.root, "BODY")
    head = child_or_error(doc.root, "HEAD")
    definition = clone_section_definition(doc, child_or_error(section, "P/TEXT/SECDEF"))
    page = child_or_error(definition, "PAGEDEF")
    body_width = dimensions(page, child_or_error(page, "PAGEMARGIN"))[0]
    active_columns = [
        columns
        for paragraph in list(section)[:index]
        for columns in paragraph.findall("TEXT/COLDEF")
    ]
    # A leading column definition already belongs to the first line of the new section.
    first_run = anchor.find("TEXT")
    leading_columns = None
    if first_run is not None and len(first_run) and first_run[0].tag == "COLDEF":
        leading_columns = first_run[0]
    if leading_columns is not None:
        columns = leading_columns
    elif active_columns:
        columns = deepcopy(active_columns[-1])
    else:
        columns = ET.Element(
            "COLDEF", Count="1", Layout="Left", SameGap="0", SameSize="true", Type="Newspaper"
        )
    configure_columns(columns, operation, body_width, "columns")

    if first_run is None:
        first_run = ET.Element("TEXT", CharShape="0")
        anchor.insert(0, first_run)
    first_run.insert(0, definition)
    if leading_columns is None:
        first_run.insert(1, columns)
    # A section already starts a new page, so an explicit break would insert a blank page.
    anchor.set("PageBreak", "false")
    anchor.set("ColumnBreak", "false")
    new_section = ET.Element("SECTION")
    for paragraph in list(section)[index:]:
        section.remove(paragraph)
        new_section.append(paragraph)
    body.insert(list(body).index(section) + 1, new_section)
    sections = body.findall("SECTION")
    for identifier, item in enumerate(sections):
        item.set("Id", str(identifier))
    head.set("SecCnt", str(len(sections)))


def format_paragraph(doc, operation):
    paragraph = doc.get(operation["target"], "paragraph")
    if not LAYOUT_FIELDS["format_paragraph"][1].intersection(operation):
        raise ValueError("format_paragraph에는 변경할 문단 서식을 지정해야 합니다.")
    if PARAGRAPH_FIELDS.intersection(operation):
        set_paragraph_format(doc, paragraph, operation)
    if CHARACTER_FIELDS.intersection(operation):
        doc.format_character_runs(paragraph, operation)


def set_paragraph_format(doc, paragraph, operation):
    breaks = {}
    for field, attribute in (("page_break", "PageBreak"), ("column_break", "ColumnBreak")):
        if field in operation:
            flag = operation[field]
            if type(flag) is not bool:
                raise ValueError(f"{field} 값은 true 또는 false여야 합니다.")
            breaks[attribute] = str(flag).lower()
    if all(breaks.get(name, paragraph.get(name)) == "true" for name in ("PageBreak", "ColumnBreak")):
        raise ValueError("한 문단에는 쪽 나눔과 단 나눔을 동시에 지정할 수 없습니다.")
    paragraph.attrib.update(breaks)
    paragraph_spacing = {"line_spacing_percent", "space_before_pt", "space_after_pt"}
    if "align" not in operation and not paragraph_spacing.intersection(operation):
        return

    shapes = child_or_error(doc.root, "HEAD/MAPPINGTABLE/PARASHAPELIST")
    native_shapes = {shape.get("Id"): shape for shape in shapes.findall("PARASHAPE")}
    shape = native_shapes.get(paragraph.get("ParaShape", "0"))
    if shape is None:
        raise ValueError("문단이 참조하는 한글 문단 모양을 찾을 수 없습니다.")
    updated = deepcopy(shape)
    if "align" in operation:
        updated.set("Align", choice(operation["align"], "align", ALIGNMENTS))
    if paragraph_spacing.intersection(operation):
        margin = updated.find("PARAMARGIN")
        if margin is None:
            margin = ET.Element("PARAMARGIN")
            updated.insert(0, margin)
        if "line_spacing_percent" in operation:
            value = bounded_integer(operation["line_spacing_percent"], "line_spacing_percent", 0, 500)
            margin.set("LineSpacingType", "Percent")
            margin.set("LineSpacing", str(value))
        for name, attribute in (("space_before_pt", "Prev"), ("space_after_pt", "Next")):
            if name in operation:
                value = operation[name]
                if type(value) not in (int, float) or not 0 <= value <= 1000:
                    raise ValueError(f"{name} 값은 0~1000 pt 범위의 숫자여야 합니다.")
                margin.set(attribute, str(round(value * 100)))
    for identifier, existing in native_shapes.items():
        updated.set("Id", identifier)
        if ET.tostring(updated) == ET.tostring(existing):
            paragraph.set("ParaShape", identifier)
            return
    identifier = str(max(map(int, native_shapes), default=-1) + 1)
    updated.set("Id", identifier)
    shapes.append(updated)
    shapes.set("Count", str(len(native_shapes) + 1))
    paragraph.set("ParaShape", identifier)


def apply_layout(doc, operation):
    actions = {
        "set_columns": set_columns,
        "set_page_layout": set_page_layout,
        "insert_section": insert_section,
        "format_paragraph": format_paragraph,
    }
    actions[operation["op"]](doc, operation)
