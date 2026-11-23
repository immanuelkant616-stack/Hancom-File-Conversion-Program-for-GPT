"""The words of a Hancom document without its layout, as a plain Markdown file.

AI용.md carries IDs, shapes and styles so an AI can edit the document. This file
holds only the text in reading order, for reading, summarising or quoting.
"""

import re
import xml.etree.ElementTree as ET
from collections import Counter


# Page furniture and layout settings: repeated on every page or not text at all.
SKIPPED = {"SECDEF", "COLDEF", "HEADER", "FOOTER", "AUTONUM", "NEWNUM", "PAGENUM", "PAGENUMCTRL",
           "PAGEHIDING", "SHAPECOMMENT", "SCRIPT", "FIELDBEGIN", "FIELDEND", "BOOKMARK",
           "INDEXMARK", "HIDDENCOMMENT"}
NOTES = {"FOOTNOTE", "ENDNOTE"}
INLINE = {"FWSPACE": " ", "NBSPACE": " ", "TAB": " ", "LINEBREAK": "\n"}
# A margin mark such as [A] that a PDF conversion keeps as a picture with this description.
LABEL = re.compile(r"\[[^\[\]\s]{1,4}\]")
SPACES = re.compile(r"[ \t　 ]+")


def char_text(char):
    return (char.text or "") + "".join(INLINE.get(child.tag, "") + (child.tail or "") for child in char)


def clean(text):
    lines = (SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


class TextWriter:
    def __init__(self, notes=None):
        self.blocks = []
        self.notes = [] if notes is None else notes   # shared, so numbers run through tables

    def child(self):
        return TextWriter(self.notes)

    def walk(self, element):
        for item in element:
            if item.tag == "P":
                self.paragraph(item)
            elif item.tag not in SKIPPED:
                self.control(item)

    def paragraph(self, paragraph):
        pieces = []
        controls = []
        for run in paragraph.findall("TEXT"):
            for item in run:
                if item.tag == "CHAR":
                    pieces.append(char_text(item))
                elif item.tag in NOTES:
                    note = self.child()
                    note.walk(item)
                    self.notes.append("\n    ".join(" ".join(note.blocks).splitlines()))
                    pieces.append(f"[^{len(self.notes)}]")
                elif item.tag not in SKIPPED:
                    controls.append(item)
        self.add("".join(pieces))
        # Tables, text boxes and marks follow the line that holds them.
        for control in controls:
            self.control(control)

    def add(self, text):
        text = clean(text)
        if text:
            self.blocks.append(text)

    def control(self, element):
        if element.tag == "TABLE":
            self.table(element)
            return
        comment = (element.findtext("SHAPEOBJECT/SHAPECOMMENT") or "").strip()
        if LABEL.fullmatch(comment):
            self.add(comment)
        self.walk(element)   # text boxes, captions and grouped shapes

    def table(self, table):
        cells = []
        nested = {cell for inner in table.iter("TABLE") if inner is not table for cell in inner.iter("CELL")}
        for cell in table.iter("CELL"):
            if cell in nested:
                continue
            writer = self.child()
            writer.walk(cell)
            cells.append((int(cell.get("RowAddr", "0")), int(cell.get("ColAddr", "0")), writer.blocks))
        columns = 1 + max((column for _, column, _ in cells), default=0)
        rows = 1 + max((row for row, _, _ in cells), default=0)
        filled = Counter(row for row, _, blocks in cells if blocks)
        if columns == 1 or max(filled.values(), default=0) <= 1 or rows * columns > 400:
            # A table used as a frame (<보기>, a [A] bracket) has at most one filled cell per
            # row; it reads as ordinary paragraphs.
            for _, _, blocks in sorted(cells, key=lambda cell: cell[:2]):
                self.blocks.extend(blocks)
            return
        grid = [[""] * columns for _ in range(rows)]
        for row, column, blocks in cells:
            grid[row][column] = "<br>".join(block.replace("\n", "<br>") for block in blocks).replace("|", "\\|")
        if not any(any(row) for row in grid):
            return
        lines = ["| " + " | ".join(grid[0]) + " |", "|" + " --- |" * columns]
        lines += ["| " + " | ".join(row) + " |" for row in grid[1:]]
        self.blocks.append("\n".join(lines))


def document_text(root):
    body = root.find("BODY")
    writer = TextWriter()
    for section in body.findall("SECTION") if body is not None else ():
        writer.walk(section)
    text = "\n\n".join(writer.blocks)
    if writer.notes:
        text += "\n\n" + "\n".join(f"[^{number}]: {note}" for number, note in enumerate(writer.notes, 1))
    return text + "\n"


def write_text_document(path, root):
    path.write_text(document_text(root), encoding="utf-8")


def write_text_from_xml(path, xml):
    write_text_document(path, ET.parse(xml).getroot())
