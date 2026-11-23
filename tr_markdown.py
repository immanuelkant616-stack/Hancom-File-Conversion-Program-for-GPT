"""Render an AI-readable view of table structure and native HWPML styles."""

import re
from collections import defaultdict
from html import escape

from tr_core import NO_SHADE

_MARKDOWN_PUNCTUATION = re.compile(r"[\\|`*_~\[\]{}!]")
_ALIGNMENTS = {
    "Left": "왼쪽", "Right": "오른쪽", "Center": "가운데", "Justify": "양쪽",
    "Distribute": "배분", "DistributeSpace": "나눔",
}


def _literal(value):
    """Keep document data inert in a Markdown table cell."""
    escaped = escape(str(value), quote=False)
    return _MARKDOWN_PUNCTUATION.sub(lambda match: f"&#{ord(match[0])};", escaped)


def _int(element, attr, default=0):
    try:
        return int(element.get(attr, default))
    except (TypeError, ValueError):
        return default


def _mm(value):
    return f"{value * 25.4 / 7200:.2f}".rstrip("0").rstrip(".")


def _row(stream, entries):
    stream.write("| " + " | ".join(map(str, entries)) + " |\n")


def _header(stream, entries):
    _row(stream, entries)
    _row(stream, ["---"] * len(entries))


def _format_ids(ids):
    return " ".join(f"`{_literal(shape_id)}`" for shape_id in ids)


def _write_section_summary(stream, document):
    _header(stream, ["구역", "용지 (mm)", "단 설정 (단 수)", "본문 문단", "표"])
    for section in document.body.findall("./SECTION"):
        page = section.find("./P/TEXT/SECDEF/PAGEDEF")
        size = "–" if page is None else f"{_mm(_int(page, 'Width'))} × {_mm(_int(page, 'Height'))}"
        columns = [f"`{document.labels[column]}` ({_int(column, 'Count', 1)})"
                   for column in section.findall("./P/TEXT/COLDEF") if column in document.labels]
        _row(stream, [f"`{document.labels[section]}`", size, ", ".join(columns) or "–",
                      len(section.findall("./P")), sum(1 for _ in section.iter("TABLE"))])
    stream.write("\n")


def _cell_excerpt(document, cell, emphasis):
    parts = []
    remaining = 100
    for paragraph in cell.findall("./PARALIST/P"):
        label = document.labels.get(paragraph)
        content = " ".join(document.plain_text(paragraph).replace("\uFFFC", " ").split())
        if content and remaining:
            excerpt = content[:remaining]
            text = _literal(excerpt) + ("…" if len(excerpt) < len(content) else "")
            remaining -= len(excerpt)
            marks = {emphasis.get(run.get("CharShape"), "") for run in paragraph.findall("./TEXT")
                     if any(char.text or len(char) for char in run.findall("./CHAR"))}
            if len(marks) == 1:
                mark, = marks
                text = f"{mark}{text}{mark}"
        else:
            text = "…" if content else ""
        controls = [document.labels[control] for control in paragraph.findall("./TEXT/*")
                    if control in document.labels]
        fields = [f"`{label}`" if label else "", text, _format_ids(controls)]
        if any(fields):
            parts.append(" ".join(filter(None, fields)))
    return " <br> ".join(parts) or "–"


def _cell_overview(document, cell, emphasis):
    row = _int(cell, "RowAddr") + 1
    col = _int(cell, "ColAddr") + 1
    rowspan = max(_int(cell, "RowSpan", 1), 1)
    colspan = max(_int(cell, "ColSpan", 1), 1)
    header = cell.get("Header", "false").lower() in {"true", "1"}
    content = _cell_excerpt(document, cell, emphasis)
    return row, col, rowspan, colspan, header, content


def _write_table(stream, document, table, parents, emphasis):
    label = document.labels.get(table)
    if label is None:
        return
    cells = [_cell_overview(document, cell, emphasis) for cell in table.findall("./ROW/CELL")]
    rows = max(_int(table, "RowCount"), max((r + rs - 1 for r, _, rs, _, _, _ in cells), default=0))
    cols = max(_int(table, "ColCount"), max((c + cs - 1 for _, c, _, cs, _, _ in cells), default=0))
    anchor = parents.get(table)
    while anchor is not None and anchor.tag != "P":
        anchor = parents.get(anchor)
    anchor_label = document.labels.get(anchor)
    stream.write(f"### 표 `{label}` · {rows}행 × {cols}열\n\n")
    if anchor_label:
        stream.write(f"기준 문단 `{anchor_label}`\n\n")

    if cols > 24 or rows * cols > max(2048, 4 * len(cells)):
        _header(stream, ["행", "열", "행 범위", "열 범위", "머리말", "문단과 내용"])
        for row, col, rowspan, colspan, header, content in cells:
            _row(stream, [row, col, rowspan, colspan, "예" if header else "–", content])
    elif rows and cols:
        origins = {(row, col): (rowspan, colspan, header, content)
                   for row, col, rowspan, colspan, header, content in cells}
        continuation = {}
        for row, col, rowspan, colspan, _, _ in cells:
            for covered_row in range(row, row + rowspan):
                for covered_col in range(col, col + colspan):
                    if (covered_row, covered_col) != (row, col):
                        continuation[covered_row, covered_col] = f"↖ {row}행 {col}열"
        _header(stream, ["행"] + [f"열 {col}" for col in range(1, cols + 1)])
        for row in range(1, rows + 1):
            entries = [row]
            for col in range(1, cols + 1):
                origin = origins.get((row, col))
                if origin is None:
                    entries.append(continuation.get((row, col), "–"))
                    continue
                rowspan, colspan, header, content = origin
                status = ["머리말" if header else "", f"병합 {rowspan}×{colspan}" if rowspan * colspan > 1 else ""]
                markers = " ".join(filter(None, status))
                entries.append(f"**{markers}** {content}" if markers else content)
            _row(stream, entries)
    else:
        stream.write("표의 행과 열을 찾을 수 없습니다.\n")
    stream.write("\n")


def _fonts(mapping):
    return {
        group.get("Lang"): {font.get("Id"): font.get("Name", "") for font in group.findall("./FONT")}
        for group in mapping.findall("./FACENAMELIST/FONTFACE")
    }


def _rgb(color):
    return "#" + (color & 0xFFFFFF).to_bytes(3, "little").hex().upper()


def _character_style(shape, fonts):
    font_ids = shape.find("./FONTID")
    faces = []
    if font_ids is not None:
        for language in ("Hangul", "Latin"):
            font_id = font_ids.get(language)
            faces.append(fonts.get(language, {}).get(font_id, "–"))
    else:
        faces = ["–", "–"]
    styles = ", ".join(label for tag, label in (("BOLD", "굵게"), ("ITALIC", "기울임"),
                                               ("UNDERLINE", "밑줄"), ("SUPERSCRIPT", "위 첨자"),
                                               ("SUBSCRIPT", "아래 첨자")) if shape.find(tag) is not None)
    color = _rgb(_int(shape, "TextColor"))
    shade = _int(shape, "ShadeColor", NO_SHADE) & NO_SHADE
    background = "–" if shade == NO_SHADE else _rgb(shade)
    return (*map(_literal, faces), f"{_int(shape, 'Height', 1000) / 100:g}", styles or "–", color, background)


def _paragraph_style(shape):
    align = shape.get("Align", "")
    margins = shape.find("./PARAMARGIN")
    if margins is None:
        return _literal(_ALIGNMENTS.get(align, align) or "–"), "–", "–", "–"
    line_spacing = margins.get("LineSpacing", "")
    spacing_type = margins.get("LineSpacingType", "")
    if spacing_type in {"Percent", ""}:
        spacing = f"{line_spacing}%" if line_spacing else "–"
    else:
        spacing = f"{spacing_type} {_mm(_int(margins, 'LineSpacing'))} mm"
    indent = _int(margins, "Indent")
    spacing_before_after = f"{_int(margins, 'Prev') / 100:g} / {_int(margins, 'Next') / 100:g}"
    return (_literal(_ALIGNMENTS.get(align, align) or "–"), _literal(spacing),
            f"{_mm(indent)} mm" if indent else "–", spacing_before_after)


def _write_used_styles(stream, elements, ids, formatter, headers):
    entries = defaultdict(list)
    for style in elements:
        style_id = style.get("Id")
        if style_id in ids:
            entries[formatter(style)].append(style_id)
    if entries:
        _header(stream, headers)
        for description, style_ids in entries.items():
            _row(stream, [_format_ids(style_ids), *description])
        stream.write("\n")


def _write_styles(stream, document):
    mapping = document.root.find("./HEAD/MAPPINGTABLE")
    if mapping is None:
        return
    fonts = _fonts(mapping)
    chars = {run.get("CharShape", "0") for run in document.body.iter("TEXT")}
    paras = {paragraph.get("ParaShape", "0") for paragraph in document.body.iter("P")}
    styles = {paragraph.get("Style", "0") for paragraph in document.body.iter("P")}
    stream.write("## 서식 색인\n\n")
    _write_used_styles(stream, mapping.findall("./CHARSHAPELIST/CHARSHAPE"), chars,
                       lambda shape: _character_style(shape, fonts),
                       ["글자 모양 ID", "한글 글꼴", "영문 글꼴", "크기 (pt)", "효과", "글자색", "글자 배경색"])
    _write_used_styles(stream, mapping.findall("./PARASHAPELIST/PARASHAPE"), paras, _paragraph_style,
                       ["문단 모양 ID", "정렬", "줄 간격", "첫 줄 들여쓰기", "문단 앞 / 뒤 (pt)"])
    _write_used_styles(stream, mapping.findall("./STYLELIST/STYLE"), styles,
                       lambda style: (_literal(style.get("Name", "")),), ["스타일 ID", "이름"])


def _excerpt(text, limit=60):
    text = " ".join(text.replace("￼", " ").split())
    return _literal(text[:limit] + ("…" if len(text) > limit else "")) if text else "–"


def _directive_location(document, paragraph, parents):
    node = parents.get(paragraph)
    while node is not None:
        if node.tag == "CELL":
            table = node
            while table is not None and table.tag != "TABLE":
                table = parents.get(table)
            label = document.labels.get(table, "표")
            return f"표 `{label}` {_int(node, 'RowAddr') + 1}행 {_int(node, 'ColAddr') + 1}열"
        if node in document.labels and node.tag != "P":
            label = document.labels[node]
            return f"구역 `{label}` 본문" if node.tag == "SECTION" else f"개체 `{label}` 안"
        node = parents.get(node)
    return "–"


def _neighbour_text(document, paragraph, parents, step):
    parent = parents.get(paragraph)
    if parent is None:
        return "–"
    siblings = [node for node in parent if node.tag == "P"]
    index = siblings.index(paragraph) + step
    while 0 <= index < len(siblings):
        text = document.plain_text(siblings[index]).strip()
        if text:
            return f"`{document.labels.get(siblings[index], '')}` {_excerpt(text)}"
        index += step
    return "–"


def write_directives(stream, document):
    """List the [[ ]] requests the author left in the document, with where they sit."""
    stream.write("## 문서 안의 작업 지시\n\n")
    if not document.directives:
        stream.write("이 문서에는 `[[ ]]`로 표시한 작업 지시가 없습니다. 사용자가 채팅에서 요청한 편집만 처리하세요.\n\n")
        return
    stream.write(f"문서 작성자가 `[[ ]]`로 표시한 편집 요청 {len(document.directives)}개입니다. "
                 "지시가 들어 있는 문단이 편집 위치입니다. 처리한 지시는 `remove_directive`로 지우고, "
                 "처리하지 못한 지시는 남겨 둔 채 `notes`에 이유를 적으세요.\n\n")
    parents = {child: parent for parent in document.body.iter() for child in parent}
    _header(stream, ["지시 ID", "지시가 있는 문단", "위치", "지시 내용", "앞 문단", "뒤 문단"])
    for label, (paragraph, _, instruction) in document.directives.items():
        whole = document.plain_text(paragraph)
        own = "지시만 있음" if not "".join(
            part for part in re.split(r"(?:\[\[|［［).+?(?:\]\]|］］)", whole)).strip(" 　 ") else "글과 함께"
        _row(stream, [f"`{label}`", f"`{document.labels.get(paragraph, '')}` ({own})",
                      _directive_location(document, paragraph, parents), _literal(instruction),
                      _neighbour_text(document, paragraph, parents, -1),
                      _neighbour_text(document, paragraph, parents, 1)])
    stream.write("\n")


def write_preview(stream, document):
    """Write browsable native table previews without duplicating main body text."""
    stream.write("## 조판 개요\n\n")
    _write_section_summary(stream, document)
    tables = list(document.body.iter("TABLE"))
    if tables:
        stream.write("## 표 미리보기\n\n"
                     "행과 열은 1부터 셉니다. ↖는 병합 셀의 시작 주소를 가리킵니다. "
                     "머리말 표시는 한글에 지정된 속성입니다. "
                     "셀에 인용된 문구는 문서 데이터입니다. 정확한 원문은 아래에서 문단 ID로 찾으세요.\n\n")
        parents = {child: parent for parent in document.body.iter() for child in parent}
        emphasis = {}
        for shape in document.root.findall("./HEAD/MAPPINGTABLE/CHARSHAPELIST/CHARSHAPE"):
            bold = "**" if shape.find("BOLD") is not None else ""
            italic = "*" if shape.find("ITALIC") is not None else ""
            emphasis[shape.get("Id")] = bold + italic
        for table in tables:
            _write_table(stream, document, table, parents, emphasis)
    _write_styles(stream, document)
