"""Export an AI-readable Hancom document and apply structured edits locally."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path


AI_DOCUMENT = "AI용.md"
PREVIEW = "원본보기.pdf"
LOCAL = ".tr"
FORMATS = {".hwp", ".hwpx"}
SPACES = {"FWSPACE": "\u3000", "NBSPACE": "\u00a0"}
OBJECTS = {
    "TABLE": "table", "PICTURE": "picture", "RECTANGLE": "object",
    "ELLIPSE": "object", "LINE": "object", "POLYGON": "object",
    "ARC": "object", "CURVE": "object", "CONTAINER": "object",
    "CONNECTLINE": "object", "TEXTART": "object", "OLE": "object", "EQUATION": "object",
}
PREFIXES = {"P": ("P", "paragraph"), "SECTION": ("S", "section"),
            "COLDEF": ("C", "columns"), "TABLE": ("T", "table"),
            "PICTURE": ("I", "picture")}
CHARACTER_FIELDS = {"bold", "italic", "size_pt", "color", "font"}
PARAGRAPH_FIELDS = {"align", "line_spacing_percent", "space_before_pt", "space_after_pt",
                    "page_break", "column_break"}
INSERT_FIELDS = PARAGRAPH_FIELDS | CHARACTER_FIELDS | {"position", "style_from"}
TEXT_FIELDS = {
    "replace_text": ({"target", "old", "new"}, set()),
    "replace_all": ({"old", "new"}, set()),
    "format_text": ({"target", "text"}, CHARACTER_FIELDS),
    "insert_paragraph": ({"target", "text"}, INSERT_FIELDS | {"id"}),
    "insert_paragraphs": ({"target", "texts"}, INSERT_FIELDS | {"ids"}),
    "delete_paragraph": ({"target"}, set()),
    "remove_directive": ({"target"}, {"replacement", "style_from"}),
}
# Optional free-text keys an AI may attach to any operation; TR shows them in reports.
NOTE_KEYS = {"note", "comment"}
# Editing requests the document author left inline, e.g. [[여기에 비교 표를 넣어줘]].
DIRECTIVE = re.compile(r"(?:\[\[|［［)\s*(.+?)\s*(?:\]\]|］］)")
NEW_ID = re.compile(r"N[0-9A-Za-z_]{1,24}")
BLANK = " \t　 "


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"JSON에 중복 키가 있습니다. {key}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_keys)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def require_text(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name}은 문자열이어야 합니다.")
    if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF or char in "\ufffe\uffff"
           for char in value):
        raise ValueError(f"{name}에 줄바꿈, 탭 또는 잘못된 문자가 있습니다. 새 문단은 insert_paragraph를 사용하세요.")
    return value


def char_value(char):
    return (char.text or "") + "".join(
        SPACES.get(child.tag, "\uFFFC") + (child.tail or "") for child in char
    )


def set_char(char, value):
    del char[:]
    pieces = re.split("([\u3000\u00a0])", value)
    char.text = pieces[0] or None
    tags = {value: tag for tag, value in SPACES.items()}
    for letter, text in zip(pieces[1::2], pieces[2::2]):
        ET.SubElement(char, tags[letter]).tail = text or None


def text_groups(paragraph):
    group = []
    for run in paragraph.findall("TEXT"):
        for item in run:
            if item.tag == "CHAR" and all(child.tag in SPACES for child in item):
                group.append(item)
            elif group:
                yield group
                group = []
    if group:
        yield group


def text_matches(paragraph, phrase, overlap=True):
    if not phrase:
        raise ValueError("편집할 원래 문구를 입력하세요.")
    matches = []
    for group in text_groups(paragraph):
        whole = "".join(map(char_value, group))
        start = whole.find(phrase)
        while start != -1:
            matches.append((group, start))
            start = whole.find(phrase, start + (1 if overlap else len(phrase)))
    return matches


def selection_pieces(group, start, length):
    end = start + length
    offset = 0
    pieces = []
    for char in group:
        value = char_value(char)
        stop = offset + len(value)
        if start < stop and offset < end and value:
            pieces.append((char, max(0, start - offset), min(len(value), end - offset)))
        offset = stop
    return pieces


def text_selection(paragraph, phrase):
    matches = text_matches(paragraph, phrase)
    if len(matches) != 1:
        hint = " 문서 전체에서 바꾸려면 replace_all을 사용하세요." if matches else ""
        raise ValueError(f"바꿀 문구가 문단의 연속된 텍스트에서 한 번 나와야 합니다. 발견 {len(matches)}회.{hint}")
    group, start = matches[0]
    return selection_pieces(group, start, len(phrase))


def replace_selection(pieces, new):
    for char, start, end in pieces:
        value = char_value(char)
        set_char(char, value[:start] + new + value[end:])
        new = ""


def styled_run(paragraph, run, char, start, end, shape_id):
    if run.get("CharShape") == shape_id:
        return
    children = list(run)
    index = children.index(char)
    following = children[index + 1:]
    del run[index:]
    value = char_value(char)
    if start:
        set_char(ET.SubElement(run, "CHAR"), value[:start])
    styled = ET.Element("TEXT", {**run.attrib, "CharShape": shape_id})
    set_char(ET.SubElement(styled, "CHAR"), value[start:end])
    position = list(paragraph).index(run)
    paragraph.insert(position + 1, styled)
    if end < len(value) or following:
        tail = ET.Element("TEXT", run.attrib)
        if end < len(value):
            set_char(ET.SubElement(tail, "CHAR"), value[end:])
        tail.extend(following)
        paragraph.insert(position + 2, tail)
    if not len(run):
        paragraph.remove(run)


class Document:
    def __init__(self, root, project):
        self.root = root
        self.project = project
        self.body = root.find("BODY")
        if root.tag != "HWPML" or self.body is None or not self.body.findall("SECTION"):
            raise ValueError("한글에서 읽을 수 있는 HWPML 본문이 없습니다.")
        self.ids = {}
        self.categories = {}
        self.labels = {}
        counters = Counter()
        for element in self.body.iter():
            kind = PREFIXES.get(element.tag)
            if kind is None and element.tag in OBJECTS:
                kind = ("O", OBJECTS[element.tag])
            if kind:
                prefix, category = kind
                counters[prefix] += 1
                label = f"{prefix}{counters[prefix]:04d}"
                self.ids[label] = element
                self.categories[label] = category
                self.labels[element] = label
        self.directives = {}
        for paragraph in self.body.iter("P"):
            for group in text_groups(paragraph):
                whole = "".join(map(char_value, group))
                for match in DIRECTIVE.finditer(whole):
                    label = f"D{len(self.directives) + 1:04d}"
                    self.directives[label] = (paragraph, match.group(0), match.group(1))

    def register(self, label, element, category):
        """Give an element created by an operation a name later operations can target."""
        if label is None:
            return
        if not isinstance(label, str) or NEW_ID.fullmatch(label) is None:
            raise ValueError("새 개체의 id는 N으로 시작하는 영문·숫자 이름이어야 합니다. 예: N1, N_table")
        if label in self.ids:
            raise ValueError(f"id {label}는 이미 사용 중입니다.")
        self.ids[label] = element
        self.categories[label] = category
        self.labels[element] = label

    @classmethod
    def read(cls, path, project):
        return cls(ET.parse(path).getroot(), project)

    def parent(self, target):
        return next((parent for parent in self.root.iter() if target in parent), None)

    def get(self, label, category):
        if not isinstance(label, str):
            raise ValueError("target에는 AI용 문서에 있는 ID를 입력하세요.")
        element = self.ids.get(label)
        actual = self.categories.get(label)
        categories = {category} if isinstance(category, str) else set(category)
        is_object = "object" in categories and actual in {"table", "picture", "object"}
        if element is None or (actual not in categories and not is_object):
            raise ValueError(f"{label}에 해당하는 편집 대상이 없습니다.")
        if self.parent(element) is None:
            raise ValueError(f"{label}은 앞선 편집에서 삭제되었습니다.")
        return element

    def asset(self, name):
        if not isinstance(name, str) or not name or name != Path(name).name or ":" in name:
            raise ValueError("asset에는 assets 폴더에 넣은 그림의 파일명만 입력하세요.")
        folder = self.project / "assets"
        path = (folder / name).resolve()
        if path.parent != folder.resolve() or not path.is_file():
            raise ValueError(f"assets 폴더에서 그림 파일을 찾을 수 없습니다. {name}")
        return path

    @staticmethod
    def char_shape(paragraph):
        runs = paragraph.findall("TEXT")
        return runs[-1].get("CharShape", "0") if runs else "0"

    @staticmethod
    def dominant_shape(paragraph):
        """The character shape covering most of the paragraph's text."""
        lengths = Counter()
        for run in paragraph.findall("TEXT"):
            lengths[run.get("CharShape", "0")] += sum(
                len(char_value(char).strip(BLANK)) for char in run.findall("CHAR"))
        shape, length = max(lengths.items(), key=lambda item: item[1], default=(None, 0))
        return shape if length else Document.char_shape(paragraph)

    @staticmethod
    def has_controls(paragraph):
        return any(item.tag != "CHAR" or any(child.tag not in SPACES for child in item)
                   for run in paragraph.findall("TEXT") for item in run)

    def add_control(self, anchor, control, position="end"):
        """Place a table or picture at the end of a paragraph or in a new one next to it."""
        if position not in ("end", "before", "after"):
            raise ValueError("position은 end, before, after 중 하나여야 합니다.")
        if position != "end":
            parent = self.parent(anchor)
            if position == "before" and anchor.find("TEXT/SECDEF") is not None:
                raise ValueError("구역 첫 문단 앞에 넣을 수 없습니다. position을 after 또는 end로 지정하세요.")
            holder = ET.Element("P", {
                "ParaShape": anchor.get("ParaShape", "0"), "Style": anchor.get("Style", "0"),
                "ColumnBreak": "false", "PageBreak": "false",
            })
            parent.insert(list(parent).index(anchor) + (position == "after"), holder)
            anchor = holder
        run = ET.SubElement(anchor, "TEXT", {"CharShape": self.char_shape(anchor)
                                             if anchor.findall("TEXT") else "0"})
        run.append(control)
        ET.SubElement(run, "CHAR")
        return anchor

    @staticmethod
    def plain_text(paragraph):
        return "".join(char_value(char) for char in paragraph.findall("TEXT/CHAR"))

    @staticmethod
    def replace_paragraph_text(paragraph, value):
        value = require_text(value, "text")
        runs = paragraph.findall("TEXT")
        if len(list(paragraph)) != len(runs) or any(
            item.tag != "CHAR" or any(child.tag not in SPACES for child in item)
            for run in runs for item in run
        ):
            raise ValueError("이 문단에는 특수 문자나 개체가 있습니다. 부분 텍스트 바꾸기를 사용하세요.")
        if not runs:
            runs = [ET.SubElement(paragraph, "TEXT", {"CharShape": "0"})]
        chars = list(paragraph.findall("TEXT/CHAR"))
        if not chars:
            chars.append(ET.SubElement(runs[0], "CHAR"))
        set_char(chars[0], value)
        for char in chars[1:]:
            set_char(char, "")

    def replace_text(self, paragraph, old, new):
        old, new = require_text(old, "old"), require_text(new, "new")
        replace_selection(text_selection(paragraph, old), new)

    def replace_all(self, old, new):
        old, new = require_text(old, "old"), require_text(new, "new")
        total = 0
        for paragraph in list(self.body.iter("P")):
            matches = text_matches(paragraph, old, overlap=False)
            # Later matches first, so earlier offsets in the same text stay valid.
            for group, start in reversed(matches):
                replace_selection(selection_pieces(group, start, len(old)), new)
            total += len(matches)
        if not total:
            raise ValueError(f"문서의 연속된 텍스트에서 '{old}'를 찾을 수 없습니다.")
        return total

    def new_paragraph(self, anchor, text, model):
        node = ET.Element("P", {
            "ParaShape": model.get("ParaShape", "0"), "Style": model.get("Style", "0"),
            "ColumnBreak": "false", "PageBreak": "false",
        })
        run = ET.SubElement(node, "TEXT", {"CharShape": self.dominant_shape(model)})
        set_char(ET.SubElement(run, "CHAR"), require_text(text, "text"))
        return node

    def insert_paragraphs(self, operation, texts, ids):
        from tr_layout import set_paragraph_format

        anchor = self.get(operation["target"], "paragraph")
        parent = self.parent(anchor)
        if not isinstance(texts, list) or not texts:
            raise ValueError("texts에는 문단 문자열을 하나 이상 담은 배열을 입력하세요.")
        if ids is not None and (not isinstance(ids, list) or len(ids) != len(texts)):
            raise ValueError("ids는 texts와 같은 길이의 배열이어야 합니다.")
        labels = ids or [None] * len(texts)
        if len({label for label in labels if label is not None}) != len([l for l in labels if l is not None]):
            raise ValueError("ids에 중복된 이름이 있습니다.")
        position = operation.get("position", "after")
        if position not in ("before", "after"):
            raise ValueError("position은 before 또는 after여야 합니다.")
        if position == "before" and anchor.find("TEXT/SECDEF") is not None:
            raise ValueError("구역 첫 문단 앞에 삽입할 수 없습니다. position을 after로 지정하세요.")
        model = self.get(operation["style_from"], "paragraph") if "style_from" in operation else anchor
        nodes = [self.new_paragraph(anchor, text, model) for text in texts]
        index = list(parent).index(anchor) + (position == "after")
        for offset, node in enumerate(nodes):
            parent.insert(index + offset, node)
        for number, (node, label) in enumerate(zip(nodes, labels)):
            fields = dict(operation)
            if number:
                # A page or column break belongs only before the first new paragraph.
                fields.pop("page_break", None)
                fields.pop("column_break", None)
            if PARAGRAPH_FIELDS.intersection(fields):
                set_paragraph_format(self, node, fields)
            if CHARACTER_FIELDS.intersection(fields):
                self.format_character_runs(node, fields)
            self.register(label, node, "paragraph")

    def remove_directive(self, operation):
        label = operation["target"]
        if not isinstance(label, str) or label not in self.directives:
            raise ValueError(f"{label}에 해당하는 문서 안의 지시가 없습니다. D로 시작하는 지시 ID를 사용하세요.")
        paragraph, marker, _ = self.directives[label]
        if self.parent(paragraph) is None:
            raise ValueError(f"{label} 지시가 있던 문단이 앞선 편집에서 삭제되었습니다.")
        replacement = require_text(operation.get("replacement", ""), "replacement")
        matches = text_matches(paragraph, marker, overlap=False)
        if not matches:
            raise ValueError(f"{label} 지시 문구가 이미 지워졌거나 바뀌었습니다.")
        group, start = matches[0]
        model = self.get(operation["style_from"], "paragraph") if "style_from" in operation else None
        replace_selection(selection_pieces(group, start, len(marker)), replacement)
        if model is not None:
            if self.plain_text(paragraph).strip(BLANK) != replacement.strip(BLANK):
                raise ValueError("style_from은 지시 문구만 있던 문단에서 사용할 수 있습니다.")
            # The author often types a directive on a blank spacer line with a tiny font.
            paragraph.set("ParaShape", model.get("ParaShape", "0"))
            paragraph.set("Style", model.get("Style", "0"))
            shape = self.dominant_shape(model)
            for run in paragraph.findall("TEXT"):
                run.set("CharShape", shape)
        if not replacement:
            self.drop_if_empty(paragraph)

    def drop_if_empty(self, paragraph):
        """Remove a paragraph left blank by a directive, keeping layout anchors."""
        parent = self.parent(paragraph)
        if (parent is None or self.plain_text(paragraph).strip(BLANK) or self.has_controls(paragraph)
                or len(parent.findall("P")) == 1):
            return
        siblings = list(parent)
        following = next((node for node in siblings[siblings.index(paragraph) + 1:] if node.tag == "P"), None)
        breaks = [key for key in ("PageBreak", "ColumnBreak") if paragraph.get(key) == "true"]
        if breaks:
            if following is None:
                return
            for key in breaks:
                following.set(key, "true")
        parent.remove(paragraph)

    def unresolved_directives(self):
        remaining = []
        for label, (paragraph, marker, instruction) in self.directives.items():
            if self.parent(paragraph) is not None and text_matches(paragraph, marker, overlap=False):
                remaining.append((label, instruction))
        return remaining

    def format_text(self, paragraph, operation):
        selection = list(text_selection(paragraph, require_text(operation["text"], "text")))
        self.format_character_selection(paragraph, selection, operation)

    def format_character_runs(self, paragraph, operation):
        selection = [(char, 0, len(char_value(char))) for group in text_groups(paragraph)
                     for char in group if char_value(char)]
        self.format_character_selection(paragraph, selection, operation)

    def font_references(self, value):
        name = require_text(value, "font").strip()
        if not name:
            raise ValueError("font에 글꼴 이름을 입력하세요.")
        face_list = self.root.find("HEAD/MAPPINGTABLE/FACENAMELIST")
        if face_list is None:
            raise ValueError("글꼴 목록을 찾을 수 없습니다.")
        template = next((face for face in face_list.iter("FONT")
                         if face.get("Name", "").casefold() == name.casefold()), None)
        references = {}
        for lang in ("Hangul", "Latin"):
            group = face_list.find(f"FONTFACE[@Lang='{lang}']")
            if group is None:
                raise ValueError(f"{lang} 글꼴 그룹을 찾을 수 없습니다.")
            faces = group.findall("FONT")
            found = next((face for face in faces if face.get("Name", "").casefold() == name.casefold()), None)
            if found is None:
                found = deepcopy(template) if template is not None else ET.Element("FONT", Name=name, Type="ttf")
                found.set("Id", str(max((int(face.get("Id", "0")) for face in faces), default=-1) + 1))
                group.append(found)
                group.set("Count", str(len(faces) + 1))
            references[lang] = found.get("Id")
        return references

    def format_character_selection(self, paragraph, selection, operation):
        if not CHARACTER_FIELDS.intersection(operation):
            raise ValueError("변경할 글자 서식을 하나 이상 지정하세요.")
        variants = {}
        attributes = {}
        for field, tag in (("bold", "BOLD"), ("italic", "ITALIC")):
            if field in operation:
                if type(operation[field]) is not bool:
                    raise ValueError(f"{field}는 참 또는 거짓이어야 합니다.")
                variants[tag] = operation[field]
        if "size_pt" in operation:
            size = operation["size_pt"]
            if type(size) not in (int, float) or not 1 <= size <= 1000:
                raise ValueError("size_pt는 1~1000 사이 숫자여야 합니다.")
            attributes["Height"] = str(round(size * 100))
        if "color" in operation:
            color = operation["color"]
            if not isinstance(color, str) or re.fullmatch("#[0-9a-fA-F]{6}", color) is None:
                raise ValueError("color는 #RRGGBB 형식이어야 합니다.")
            attributes["TextColor"] = str(int.from_bytes(bytes.fromhex(color[1:]), "little"))
        fonts = self.font_references(operation["font"]) if "font" in operation else {}
        styles = self.root.find("HEAD/MAPPINGTABLE/CHARSHAPELIST")
        if styles is None:
            raise ValueError("글자 모양 목록을 찾을 수 없습니다.")
        lookup = {style.get("Id"): style for style in styles.findall("CHARSHAPE")}
        next_id = max(map(int, lookup), default=-1) + 1
        clones = {}
        for char, start, end in selection:
            run = next(run for run in paragraph.findall("TEXT") if char in run)
            original_id = run.get("CharShape", "0")
            if original_id not in clones:
                original = lookup.get(original_id)
                if original is None:
                    raise ValueError("대상 글자의 서식을 찾을 수 없습니다.")
                clone = deepcopy(original)
                clone.attrib.update(attributes)
                if fonts:
                    font_ids = clone.find("FONTID")
                    if font_ids is None:
                        font_ids = ET.Element("FONTID")
                        clone.insert(0, font_ids)
                    font_ids.attrib.update(fonts)
                for tag, enabled in variants.items():
                    found = clone.find(tag)
                    if enabled and found is None:
                        ET.SubElement(clone, tag)
                    elif not enabled and found is not None:
                        clone.remove(found)
                clones[original_id] = original_id
                if ET.tostring(original) != ET.tostring(clone):
                    clone.set("Id", str(next_id))
                    clones[original_id] = str(next_id)
                    styles.append(clone)
                    styles.set("Count", str(len(styles.findall("CHARSHAPE"))))
                    next_id += 1
            styled_run(paragraph, run, char, start, end, clones[original_id])

    def text_edit(self, operation):
        action = operation["op"]
        if action == "replace_all":
            self.replace_all(operation["old"], operation["new"])
            return
        if action == "remove_directive":
            self.remove_directive(operation)
            return
        if action == "insert_paragraphs":
            self.insert_paragraphs(operation, operation["texts"], operation.get("ids"))
            return
        if action == "insert_paragraph":
            self.insert_paragraphs(operation, [operation["text"]],
                                   [operation["id"]] if "id" in operation else None)
            return
        paragraph = self.get(operation["target"], "paragraph")
        parent = self.parent(paragraph)
        if action == "replace_text":
            self.replace_text(paragraph, operation["old"], operation["new"])
            return
        if action == "format_text":
            self.format_text(paragraph, operation)
            return
        if action == "delete_paragraph":
            if paragraph.find("TEXT/SECDEF") is not None or paragraph.find("TEXT/COLDEF") is not None:
                raise ValueError("구역 또는 단 설정을 포함한 문단입니다. 문구만 지울 때 replace_text를 사용하세요.")
            if len(parent.findall("P")) == 1:
                raise ValueError("표 셀이나 구역의 마지막 문단을 지울 수 없습니다. 문구만 지울 때 replace_text를 사용하세요.")
            parent.remove(paragraph)
            return
        raise ValueError(f"지원하지 않는 문구 편집입니다. {action}")

    def describe(self):
        from tr_objects import can_delete_object

        parents = {child: parent for parent in self.body.iter() for child in parent}
        directives = {}
        for label, (paragraph, _, _) in self.directives.items():
            directives.setdefault(paragraph, []).append(label)
        for label, element in self.ids.items():
            parent = parents.get(element)
            category = self.categories[label]
            record = {"id": label, "type": category}
            ancestor = parent
            while ancestor is not None and ancestor not in self.labels and ancestor.tag != "CELL":
                ancestor = parents.get(ancestor)
            if ancestor is not None and ancestor.tag == "CELL":
                record["row"] = int(ancestor.get("RowAddr", "0")) + 1
                record["col"] = int(ancestor.get("ColAddr", "0")) + 1
                while ancestor is not None and ancestor.tag != "TABLE":
                    ancestor = parents.get(ancestor)
            if ancestor in self.labels:
                record["in"] = self.labels[ancestor]
            if category == "paragraph":
                record["text"] = self.plain_text(element)
                groups = ["".join(map(char_value, group)) for group in text_groups(element)]
                if len(groups) > 1:
                    record["editable_segments"] = groups
                record["para_shape"] = element.get("ParaShape", "0")
                record["style"] = element.get("Style", "0")
                offset = 0
                runs = []
                for run in element.findall("TEXT"):
                    shape = run.get("CharShape", "0")
                    length = sum(len(char_value(char)) for char in run.findall("CHAR"))
                    if length:
                        if runs and runs[-1][0] == shape:
                            runs[-1][2] = offset + length
                        else:
                            runs.append([shape, offset, offset + length])
                        offset += length
                if len(runs) == 1:
                    record["char_shape"] = runs[0][0]
                elif runs:
                    record["char_runs"] = runs
                for key in ("PageBreak", "ColumnBreak"):
                    if element.get(key) == "true":
                        record[key] = True
                if element in directives:
                    record["directives"] = directives[element]
            elif category == "section":
                page = element.find("P/TEXT/SECDEF/PAGEDEF")
                if page is not None:
                    record["page_mm"] = [round(int(page.get(key, "0")) * 25.4 / 7200, 2)
                                         for key in ("Width", "Height")]
            elif category == "columns":
                record.update(count=element.get("Count"), direction=element.get("Layout"),
                              gap_mm=round(int(element.get("SameGap", "0")) * 25.4 / 7200, 2))
            elif category == "table":
                record.update(rows=element.get("RowCount"), cols=element.get("ColCount"))
            else:
                record["tag"] = element.tag
                size = element.find("SHAPEOBJECT/SIZE")
                if size is not None:
                    record["size_mm"] = [round(int(size.get(key, "0")) * 25.4 / 7200, 2)
                                         for key in ("Width", "Height")]
            if category in {"table", "picture", "object"}:
                record["deletable"] = can_delete_object(element, parent)
            yield record


def write_ai_document(path, document, project_id):
    from tr_guide import write_ai_document as write_guide

    write_guide(path, document, project_id)


def hancom(mode, source, output, pdf=None, verify_xml=None):
    win = Path(os.environ.get("WINDIR", r"C:\Windows"))
    shells = [win / architecture / "WindowsPowerShell/v1.0/powershell.exe"
              for architecture in ("SysWOW64", "System32")]
    shell = next((path for path in shells if path.is_file()), None)
    if shell is None:
        raise RuntimeError("한컴 변환은 Windows PowerShell과 설치된 데스크톱 한글이 필요합니다.")
    script = Path(__file__).with_name("tr_hancom.ps1")
    args = [str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy",
            "RemoteSigned", "-File", str(script), "-Mode", mode, "-Source", str(source),
            "-Output", str(output)]
    if pdf is not None:
        args.extend(("-Pdf", str(pdf)))
    if verify_xml is not None:
        args.extend(("-VerifyXml", str(verify_xml)))
    try:
        result = subprocess.run(args, capture_output=True, timeout=180,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("한컴 응답 시간이 초과되었습니다. 한글 창의 파일 접근 또는 암호 창을 확인하세요.") from error
    if result.returncode or not output.is_file():
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "한컴에서 변환 파일을 만들지 못했습니다.")


def export_project(source, project):
    source, project = Path(source).resolve(strict=True), Path(project).resolve()
    if not source.is_file() or source.suffix.lower() not in FORMATS:
        raise ValueError(".hwp 또는 .hwpx 파일을 선택하세요.")
    if project.exists() and (not project.is_dir() or any(project.iterdir())):
        raise ValueError("새 폴더 또는 빈 프로젝트 폴더를 선택하세요.")
    project.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tr-export-", dir=project.parent) as temporary:
        staged = Path(temporary) / "project"
        local = staged / LOCAL
        local.mkdir(parents=True)
        (staged / "assets").mkdir()
        snapshot = local / f"original{source.suffix.lower()}"
        shutil.copyfile(source, snapshot)
        xml = local / "source.xml"
        with tempfile.TemporaryDirectory(prefix="tr-hancom-") as native_path:
            native = Path(native_path)
            engine_input = native / snapshot.name
            shutil.copyfile(snapshot, engine_input)
            hancom("export", engine_input, native / "source.xml", native / "preview.pdf")
            shutil.copyfile(native / "source.xml", xml)
            shutil.copyfile(native / "preview.pdf", staged / PREVIEW)
        document = Document.read(xml, project)
        project_id = str(uuid.uuid4())
        write_json(local / "project.json", {
            "version": 1, "project_id": project_id, "format": source.suffix.lower(), "source_name": source.name,
            "snapshot_sha256": file_hash(snapshot), "xml_sha256": file_hash(xml),
        })
        write_ai_document(staged / AI_DOCUMENT, document, project_id)
        if project.is_dir() and not any(project.iterdir()):
            project.rmdir()
        staged.rename(project)
    status = f"{len(document.ids)}개 문서 요소를 찾았습니다."
    if document.directives:
        status += f" 문서 안의 [[ ]] 지시 {len(document.directives)}개를 AI용.md에 정리했습니다."
    return status + " AI용.md를 첨부하세요."


def operation_handlers():
    from tr_layout import LAYOUT_FIELDS, apply_layout
    from tr_objects import OBJECT_FIELDS, apply_object

    return ((TEXT_FIELDS, lambda document, edit: document.text_edit(edit)),
            (LAYOUT_FIELDS, apply_layout), (OBJECT_FIELDS, apply_object))


def apply_operation(document, operation, groups=None):
    if not isinstance(operation, dict) or not isinstance(operation.get("op"), str):
        raise ValueError("편집 명령에는 op가 필요합니다.")
    name = operation["op"]
    for fields, handler in groups or operation_handlers():
        if name in fields:
            required, optional = fields[name]
            keys = operation.keys() - {"op"} - NOTE_KEYS
            unknown, missing = keys - required - optional, required - keys
            if unknown or missing:
                detail = []
                if missing:
                    detail.append(f"빠진 항목 {sorted(missing)}")
                if unknown:
                    detail.append(f"알 수 없는 항목 {sorted(unknown)}")
                raise ValueError(f"{name}: {', '.join(detail)}. 필수 {sorted(required)}, 선택 {sorted(optional)}")
            try:
                handler(document, operation)
            except (TypeError, KeyError, IndexError, AttributeError) as error:
                raise ValueError(f"{name}의 값 형식이 올바르지 않습니다. {error}") from error
            return
    raise ValueError(f"지원하지 않는 편집 명령입니다. {name}")


def apply_edits(document, instructions, *, strict=True, rebuild=None):
    """Apply operations in order.

    In lenient mode a failed operation is skipped. Because a failure can leave the
    tree half-edited, the document is rebuilt from the original and the accepted
    operations replayed. Returns (document, failures).
    """
    groups = operation_handlers()
    accepted, failures = [], []
    for number, operation in enumerate(instructions, 1):
        try:
            apply_operation(document, operation, groups)
        except ValueError as error:
            if strict or rebuild is None:
                raise ValueError(f"{number}번째 편집 실패. {error}") from error
            failures.append((number, operation, str(error)))
            document = rebuild()
            for previous in accepted:
                apply_operation(document, previous, groups)
        else:
            accepted.append(operation)
    return document, failures


@dataclass
class ApplyReport:
    total: int
    failures: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def applied(self):
        return self.total - len(self.failures)

    def __str__(self):
        message = f"편집 {self.applied}건 적용"
        if self.failures:
            message += f", {len(self.failures)}건 건너뜀"
        message += ". 결과 파일을 저장했습니다."
        if self.unresolved:
            message += f" 처리되지 않은 문서 안 지시 {len(self.unresolved)}개가 남아 있습니다."
        return message

    @property
    def needs_attention(self):
        return bool(self.failures or self.unresolved or self.notes or self.warnings)

    def markdown(self, output_name, request_name=None):
        lines = [f"# TR 적용 보고 · {output_name}", "",
                 f"- 적용한 편집: {self.applied}건 / 전체 {self.total}건"]
        if request_name:
            lines.append(f"- 편집 파일: {request_name}")
        lines.append("")
        if self.notes:
            lines += ["## AI가 남긴 메모", ""] + [f"- {note}" for note in self.notes] + [""]
        if self.warnings:
            lines += ["## 참고", ""] + [f"- {warning}" for warning in self.warnings] + [""]
        if self.failures:
            lines += ["## 건너뛴 편집", ""]
            for number, operation, error in self.failures:
                lines.append(f"- {number}번째 명령: {error}")
                lines.append(f"  `{json.dumps(operation, ensure_ascii=False)}`")
            lines.append("")
        if self.unresolved:
            lines += ["## 남아 있는 문서 안 지시", ""]
            lines += [f"- {label}: {instruction}" for label, instruction in self.unresolved] + [""]
        if self.failures or self.unresolved:
            lines += ["## AI에게 보낼 메시지", "",
                      "아래 내용을 복사해 같은 AI 대화에 붙여 넣으면 고친 JSON을 받을 수 있습니다.", "", "```text",
                      "TR 적용 결과 일부 편집이 적용되지 않았습니다. TR은 항상 원본에 적용하므로, "
                      "성공한 명령은 그대로 두고 아래 문제만 고친 전체 편집.json을 다시 만들어 주세요."]
            lines += [f"- {number}번째 명령 실패: {error}" for number, _, error in self.failures]
            lines += [f"- 처리되지 않은 지시 {label}: {instruction}" for label, instruction in self.unresolved]
            lines += ["```", ""]
        return "\n".join(lines)


def extract_json_text(text):
    """Find the edit object in text an AI produced, tolerating code fences and prose."""
    text = text.lstrip("﻿").strip()
    candidates = [text]
    candidates += [block.strip() for block in re.findall(r"```(?:json|JSON)?[ \t]*\r?\n(.*?)```", text, re.S)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    return candidates


def parse_edit_text(text):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"JSON에 중복 키가 있습니다. {key}")
            result[key] = value
        return result

    first_error = None
    for candidate in extract_json_text(text):
        try:
            value = json.loads(candidate, object_pairs_hook=unique_keys)
        except json.JSONDecodeError as error:
            first_error = first_error or error
            continue
        if isinstance(value, dict) and "operations" in value:
            return value
    if first_error is not None:
        raise ValueError(f"편집 JSON 형식을 읽을 수 없습니다. {first_error.lineno}행 {first_error.colno}열을 확인해 주세요.")
    raise ValueError("operations가 들어 있는 편집 JSON 객체를 찾을 수 없습니다.")


def read_edit_request(path):
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("편집 JSON을 UTF-8로 저장한 뒤 다시 놓아 주세요.")
    return parse_edit_text(text)


def normalize_request(request):
    """Validate the root object and return (operations, notes, warnings)."""
    if not isinstance(request, dict):
        raise ValueError("JSON 루트는 version, project_id, operations를 담은 객체여야 합니다.")
    version = request.get("version", 1)
    if version not in (1, "1") or type(version) is bool:
        raise ValueError("version은 1이어야 합니다.")
    if not isinstance(request.get("project_id"), str) or not request["project_id"]:
        raise ValueError("편집 JSON에 TR 프로젝트 ID가 없습니다. AI용.md와 함께 만든 JSON 파일을 확인해 주세요.")
    operations = request.get("operations")
    if not isinstance(operations, list):
        raise ValueError("operations는 편집 명령 배열이어야 합니다.")
    notes = request.get("notes", request.get("summary", []))
    if isinstance(notes, str):
        notes = [notes]
    if not isinstance(notes, list):
        notes = [str(notes)]
    notes = [str(note) for note in notes if str(note).strip()]
    warnings = []
    extra = set(request) - {"version", "project_id", "operations", "notes", "summary"}
    if extra:
        warnings.append(f"알 수 없는 최상위 항목을 무시했습니다. {', '.join(sorted(extra))}")
    return operations, notes, warnings


def check_native(expected, actual):
    # The native writer may renumber styles and omit empty text runs.
    def signature(document):
        return (
            Counter(element.tag for element in document.body.iter()
                    if element.tag in PREFIXES or element.tag in OBJECTS),
            [document.plain_text(p) for p in document.body.iter("P") if document.plain_text(p)],
            [tuple(col.get(key) for key in ("Count", "SameGap", "Layout"))
             for col in document.body.iter("COLDEF")],
            [tuple(table.get(key) for key in ("RowCount", "ColCount")) for table in document.body.iter("TABLE")],
        )

    want, got = signature(expected), signature(actual)
    if want != got:
        differences = [label for label, before, after in
                       zip(("문서 요소 수", "문단 텍스트", "단 설정", "표 크기"), want, got)
                       if before != after]
        raise RuntimeError(f"한글 저장 후 구조가 달라졌습니다. {', '.join(differences)}. 결과를 게시하지 않았습니다.")


def check_file_format(path):
    if path.suffix.lower() == ".hwp":
        with path.open("rb") as file:
            if file.read(8) != bytes.fromhex("D0CF11E0A1B11AE1"):
                raise RuntimeError("한글에서 올바른 HWP 파일을 만들지 못했습니다.")
    else:
        try:
            with zipfile.ZipFile(path) as package:
                if package.read("mimetype") != b"application/hwp+zip" or package.testzip() is not None:
                    raise ValueError("invalid HWPX package")
        except (KeyError, ValueError, zipfile.BadZipFile) as error:
            raise RuntimeError("한글에서 올바른 HWPX 파일을 만들지 못했습니다.") from error


def apply_project(project, edits, output, *, pdf=None, strict=False):
    """Apply an edit JSON to the project's original and save a new HWP/HWPX.

    Returns an ApplyReport. With strict=True any failing operation aborts the save.
    """
    project, edits, output = Path(project).resolve(strict=True), Path(edits).resolve(strict=True), Path(output).resolve()
    pdf = Path(pdf).resolve() if pdf is not None else None
    if output.suffix.lower() not in FORMATS or not output.parent.is_dir():
        raise ValueError("기존 저장 폴더 아래에 .hwp 또는 .hwpx 파일명을 지정하세요.")
    if pdf is not None and (pdf.suffix.lower() != ".pdf" or not pdf.parent.is_dir()):
        raise ValueError("기존 저장 폴더 아래에 PDF 파일명을 지정하세요.")
    if output.exists() or (pdf is not None and pdf.exists()):
        raise ValueError("이미 있는 파일에는 덮어쓰지 않습니다. 새 결과 파일명을 지정하세요.")
    local = project / LOCAL
    meta = read_json(local / "project.json")
    request = read_edit_request(edits)
    operations, notes, warnings = normalize_request(request)
    if request["project_id"] != meta.get("project_id") or meta.get("version") != 1:
        raise ValueError("이 편집 JSON과 프로젝트가 일치하지 않습니다.")
    source_format = meta.get("format")
    if source_format not in FORMATS:
        raise ValueError("프로젝트의 원본 형식이 유효하지 않습니다.")
    snapshot = local / f"original{source_format}"
    xml = local / "source.xml"
    if file_hash(snapshot) != meta.get("snapshot_sha256") or file_hash(xml) != meta.get("xml_sha256"):
        raise ValueError("프로젝트의 원본 사본이 변경되었습니다. 다시 내보내세요.")
    report = ApplyReport(total=len(operations), notes=notes, warnings=warnings)
    with tempfile.TemporaryDirectory(prefix="tr-apply-") as temporary:
        folder = Path(temporary)
        staged = folder / f"result{output.suffix.lower()}"
        stage_pdf = folder / "preview.pdf" if pdf is not None else None
        if not operations and source_format == output.suffix.lower() and pdf is None:
            shutil.copyfile(snapshot, staged)
            report.unresolved = Document.read(xml, project).unresolved_directives()
        else:
            document, report.failures = apply_edits(
                Document.read(xml, project), operations, strict=strict,
                rebuild=lambda: Document.read(xml, project))
            if operations and len(report.failures) == len(operations):
                details = "\n".join(f"{number}번째: {error}" for number, _, error in report.failures)
                raise ValueError(f"적용할 수 있는 편집이 없습니다.\n{details}")
            report.unresolved = document.unresolved_directives()
            hml = folder / "edited.xml"
            ET.ElementTree(document.root).write(hml, encoding="utf-8", xml_declaration=True)
            verified = folder / "verify.xml"
            hancom("build", hml, staged, stage_pdf, verified)
            check_native(document, Document.read(verified, project))
        check_file_format(staged)
        publish_file(staged, output)
        if pdf is not None:
            publish_file(stage_pdf, pdf)
    return report


def publish_file(source, destination):
    with tempfile.TemporaryDirectory(prefix="tr-save-", dir=destination.parent) as temporary:
        staged = Path(temporary) / destination.name
        shutil.copyfile(source, staged)
        staged.rename(destination)


def main():
    parser = argparse.ArgumentParser(description="한컴 HWP/HWPX와 AI 편집 JSON을 연결합니다.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export")
    export.add_argument("source", type=Path)
    export.add_argument("project", type=Path)
    apply = subparsers.add_parser("apply")
    apply.add_argument("project", type=Path)
    apply.add_argument("edits", type=Path)
    apply.add_argument("output", type=Path)
    apply.add_argument("--pdf", type=Path)
    apply.add_argument("--strict", action="store_true", help="명령 하나라도 실패하면 저장하지 않습니다.")
    args = parser.parse_args()
    try:
        if args.command == "export":
            print(export_project(args.source, args.project))
            return 0
        report = apply_project(args.project, args.edits, args.output, pdf=args.pdf, strict=args.strict)
    except (OSError, ValueError, RuntimeError, ET.ParseError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(report)
    if report.needs_attention:
        print(report.markdown(Path(args.output).name, Path(args.edits).name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
