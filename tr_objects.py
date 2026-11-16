"""Native table and picture edits in Hancom HWPML 2.x."""

import base64
import zlib
from xml.etree import ElementTree as ET


OBJECT_FIELDS = {
    "set_table_cell": ({"target", "row", "col", "text"}, set()),
    "insert_table": ({"target", "cells"}, {"width_mm"}),
    "delete_object": ({"target"}, set()),
    "replace_image": ({"target", "asset"}, set()),
    "insert_image": ({"target", "asset"}, {"width_mm", "height_mm"}),
}

_REMOVABLE_OBJECTS = {
    "TABLE", "PICTURE", "RECTANGLE", "ELLIPSE", "LINE", "ARC", "POLYGON",
    "CURVE", "CONNECTLINE", "CONTAINER", "TEXTART", "EQUATION",
}
_JPEG_FRAME_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                       0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_ZERO_MARGIN = dict(Left="0", Right="0", Top="0", Bottom="0")


def apply_object(doc, operation):
    """Apply one field-validated operation to the in-memory native document."""
    _HANDLERS[operation["op"]](doc, operation)


def _edit_cell(doc, operation):
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


def _cell_text(value):
    if not isinstance(value, str):
        raise ValueError("셀 내용은 문자열이어야 합니다.")
    if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF or char in "\ufffe\uffff"
           for char in value):
        raise ValueError("셀 내용은 줄바꿈과 제어 문자가 없는 한 줄이어야 합니다.")
    return value


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


def _table_border(root):
    borders = root.find("./HEAD/MAPPINGTABLE/BORDERFILLLIST")
    if borders is None:
        raise ValueError("문서의 테두리 서식 목록을 찾을 수 없습니다.")
    existing = borders.findall("./BORDERFILL")
    border_id = str(max((int(item.get("Id", "0")) for item in existing), default=0) + 1)
    border = ET.SubElement(borders, "BORDERFILL", Id=border_id, BackSlash="0", Slash="0",
                           Shadow="false", ThreeD="false")
    for kind in ("LEFTBORDER", "RIGHTBORDER", "TOPBORDER", "BOTTOMBORDER"):
        ET.SubElement(border, kind, Type="Solid", Width="0.12mm", Color="0")
    ET.SubElement(border, "DIAGONAL", Type="None", Width="0.12mm", Color="0")
    borders.set("Count", str(len(existing) + 1))
    return border_id


def _insert_table(doc, operation):
    anchor = doc.get(operation["target"], "paragraph")
    cells = operation["cells"]
    if not isinstance(cells, list) or not cells:
        raise ValueError("새 표의 행을 하나 이상 입력하세요.")
    if not isinstance(cells[0], list) or not cells[0]:
        raise ValueError("새 표의 열을 하나 이상 입력하세요.")
    rows, cols = len(cells), len(cells[0])
    if rows * cols > 16384:
        raise ValueError("한글 표의 셀 수는 최대 16,384개입니다.")
    for row in cells:
        if not isinstance(row, list) or len(row) != cols:
            raise ValueError("새 표의 모든 행은 열 수가 같아야 합니다.")
        for value in row:
            _cell_text(value)

    width = _dimension_units(operation.get("width_mm", 55), "width_mm")
    if width < cols:
        raise ValueError("열당 폭이 너무 좁습니다. 열 수를 줄이거나 표 폭을 늘리세요.")
    row_height = 1800
    border = _table_border(doc.root)
    table = ET.Element("TABLE", BorderFill=border, CellSpacing="0", ColCount=str(cols),
                       PageBreak="Cell", RepeatHeader="false", RowCount=str(rows))
    _new_shape(doc, table, "Table", width, rows * row_height)
    ET.SubElement(table, "INSIDEMARGIN", _ZERO_MARGIN)
    char_shape = doc.char_shape(anchor)
    for row_number, values in enumerate(cells):
        row = ET.SubElement(table, "ROW")
        for col_number, value in enumerate(values):
            cell_width = (col_number + 1) * width // cols - col_number * width // cols
            cell = ET.SubElement(row, "CELL", BorderFill=border, ColAddr=str(col_number), ColSpan="1",
                                 Dirty="false", Editable="false", HasMargin="false", Header="false",
                                 Height=str(row_height), Protect="false", RowAddr=str(row_number),
                                 RowSpan="1", Width=str(cell_width))
            ET.SubElement(cell, "CELLMARGIN", Left="141", Right="141", Top="141", Bottom="141")
            plist = ET.SubElement(cell, "PARALIST", LineWrap="Break", LinkListID="0", LinkListIDNext="0",
                                  TextDirection="0", VertAlign="Center")
            paragraph = ET.SubElement(plist, "P", ParaShape="0", Style="0")
            ET.SubElement(paragraph, "TEXT", CharShape=char_shape)
            if value:
                doc.replace_paragraph_text(paragraph, value)
    doc.add_control(anchor, table)


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
    doc.add_control(anchor, picture)


_HANDLERS = {
    "set_table_cell": _edit_cell,
    "insert_table": _insert_table,
    "delete_object": _delete_object,
    "replace_image": _replace_image,
    "insert_image": _insert_image,
}
