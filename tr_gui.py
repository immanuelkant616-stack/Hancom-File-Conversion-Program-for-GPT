"""Windows desktop workflow for importing and exporting TR files."""

import os
import queue
import threading
import tkinter as tk
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font, ttk

from tr_batch import save_copy, write_bundle
from tr_core import AI_DOCUMENT, FORMATS
from tr_drag import DropReceiver, drag_file
from tr_history import show_project_history
from tr_results import ResultPanel
from tr_surface import clay_photo
from tr_workflow import (EDIT_TYPES, IMAGE_TYPES, add_images, candidate_paths, export_dropped, finish_edit,
                         pdf_conversion, prepare_edit, project_metadata, project_outputs, projects_folder,
                         text_document, windows_filename)


BACKGROUND = "#ECE4F0"
PAPER = "#F8F0FA"
WHITE = "#FFFAFE"
TEXT = "#34263F"
MUTED = "#64516E"
LAVENDER = "#EBDCF4"
BORDER = "#D3C0DC"
CARD_OUTLINE = "#8D71A6"
ACCENT = "#7753A9"
ACCENT_DARK = "#543480"
ERROR = "#9B3353"
SUCCESS = "#2B6A54"

DEFAULT_WIDTH = 1180
DEFAULT_HEIGHT = 760
MIN_WIDTH = 950
RESULT_COLORS = {
    "background": BACKGROUND, "paper": PAPER, "white": WHITE, "text": TEXT, "muted": MUTED,
    "lavender": LAVENDER, "border": BORDER, "accent": ACCENT, "accent_dark": ACCENT_DARK,
}
CARD_COLORS = {
    "source": "#F4E4F0",
    "markdown": "#EEE1F7",
    "json": "#E9E5F8",
    "result": "#F2E6F6",
}
# The small '텍스트 파일' button inside the md card: a cool blue so it reads apart from the card.
EXTRA_FILL = "#E2E8FA"
EXTRA_HOVER = "#D2DCF7"
EXTRA_OUTLINE = "#8F9DD0"
EXTRA_TEXT = "#3B4A86"


def screen_fonts(root):
    families = set(font.families(root))
    family = next((name for name in ("Pretendard", "Noto Sans KR", "나눔고딕", "맑은 고딕")
                   if name in families), font.nametofont("TkDefaultFont", root=root).actual("family"))
    semibold = "Pretendard SemiBold" if family == "Pretendard" and "Pretendard SemiBold" in families else None
    roles = {
        "heading": (22, True), "card": (11, True), "label": (10, True),
        "body": (10, False), "hint": (9, False), "mark": (14, True),
    }
    return {role: font.Font(root, family=semibold if strong and semibold else family, size=size,
                            weight="bold" if strong and not semibold else "normal")
            for role, (size, strong) in roles.items()}


def rounded(canvas, x0, y0, x1, y1, radius, **options):
    r = radius
    points = (x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
              x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0)
    return canvas.create_polygon(points, smooth=True, splinesteps=20, **options)


class FileCard(tk.Canvas):
    def __init__(self, parent, title, kind, click, get_path, drag, *, fonts, wide=False, save=None,
                 extra=None):
        title_line = fonts["card"].metrics("linespace")
        height = max(84, 3 * title_line + 21) if wide else max(153, 101 + 4 * title_line)
        super().__init__(parent, background=BACKGROUND, width=165, height=height,
                         highlightthickness=0, borderwidth=0, takefocus=True)
        self.title = title
        self.save = save             # wide card: a second button that saves the file elsewhere
        self.save_area = None
        # Small card: a corner button for a second file, (label, get_path, click, drag).
        self.extra = extra
        self.extra_area = None
        self.extra_hover = False
        self.press_extra = False
        self.open_label = "편집본 열기"
        self.kind = kind
        self.click = click
        self.get_path = get_path
        self.drag = drag
        self.wide = wide
        self.detail = ""
        self.hint = ""
        self.enabled = False
        self.hover = False
        self.press = None
        self.fonts = fonts
        self.detail_font = fonts["body"]
        self.surface = None
        self.surface_key = None
        self.resize_timer = None
        self.bind("<Configure>", self.schedule_draw)
        self.bind("<Destroy>", self.cancel_draw)
        self.bind("<Enter>", lambda event: self.set_hover(True))
        self.bind("<Leave>", lambda event: self.set_hover(False))
        self.bind("<FocusIn>", self.draw)
        self.bind("<FocusOut>", self.draw)
        self.bind("<ButtonPress-1>", self.mouse_down)
        self.bind("<B1-Motion>", self.mouse_motion)
        self.bind("<ButtonRelease-1>", self.mouse_up)
        self.bind("<Motion>", self.mouse_move)
        self.bind("<Return>", self.activate)
        self.bind("<space>", self.activate)

    def schedule_draw(self, _event):
        if self.resize_timer is None:
            self.resize_timer = self.after_idle(self.draw_after_resize)

    def draw_after_resize(self):
        self.resize_timer = None
        self.draw()

    def cancel_draw(self, _event):
        if self.resize_timer is not None:
            self.after_cancel(self.resize_timer)
            self.resize_timer = None

    def update_card(self, detail, hint, enabled):
        self.detail, self.hint, self.enabled = detail, hint, enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self.draw()

    def set_hover(self, state):
        self.hover = state
        if not state:
            self.extra_hover = False
        self.draw()

    @staticmethod
    def inside(area, event):
        return area is not None and area[0] <= event.x <= area[2] and area[1] <= event.y <= area[3]

    def mouse_move(self, event):
        over = self.inside(self.extra_area, event)
        if over != self.extra_hover:
            self.extra_hover = over
            self.draw()

    def mouse_down(self, event):
        self.press_extra = self.inside(self.extra_area, event)
        if self.enabled or self.press_extra:
            self.focus_set()
            self.press = (event.x_root, event.y_root)

    def mouse_motion(self, event):
        drag = self.extra[3] if self.press_extra else self.drag
        if self.press is None or drag is None:
            return
        x, y = self.press
        if max(abs(event.x_root - x), abs(event.y_root - y)) < 7:
            return
        self.press = None
        path = self.extra[1]() if self.press_extra else self.get_path()
        if path is not None:
            drag(path)

    def mouse_up(self, event):
        pressed = self.press is not None
        self.press = None
        if pressed and self.press_extra:
            if self.inside(self.extra_area, event):
                self.extra[2]()
            return
        if pressed and self.save_area is not None:
            left, top, right, bottom = self.save_area
            if left <= event.x <= right and top <= event.y <= bottom:
                self.save()
                return
        if pressed and 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height():
            self.activate()

    def activate(self, _event=None):
        if self.enabled:
            self.click()
        return "break"

    def fit_detail(self, width):
        if self.detail_font.measure(self.detail) <= width:
            return self.detail
        # Keep the recognizable file extension when filenames are long.
        suffix = Path(self.detail).suffix
        tail = "…" + (suffix if suffix.lower() in FORMATS | {".md", ".json", ".pdf"} else "")
        for length in range(len(self.detail) - 1, 0, -1):
            clipped = self.detail[:length].rstrip() + tail
            if self.detail_font.measure(clipped) <= width:
                return clipped
        return tail

    def draw(self, _event=None):
        width, height = self.winfo_width(), self.winfo_height()
        if width < 40 or height < 40:
            return
        self.delete("all")
        focused = self.focus_get() == self
        ready = self.get_path() is not None
        active = self.hover and self.enabled
        color = "#EBD9F5" if self.wide and ready else CARD_COLORS[self.kind]
        outline = CARD_OUTLINE
        if self.enabled:
            outline = ACCENT_DARK if focused else ACCENT if active else CARD_OUTLINE
        surface_key = (width, height, color, outline)
        if self.surface_key != surface_key:
            self.surface = clay_photo(self, width, height, color, outline=outline, radius=20)
            self.surface_key = surface_key
        self.create_image(0, 0, anchor="nw", image=self.surface)
        if self.wide:
            self.draw_result(width, height, ready)
            return
        left = 21
        self.draw_icon(left + 15, 28, ready)
        title = self.create_text(left, 49, anchor="nw", text=self.title, font=self.fonts["card"],
                                 fill=TEXT, width=width - 2 * left, tags=("title",))
        title_bottom = self.bbox(title)[3]
        detail_line = self.detail_font.metrics("linespace")
        detail_y = max(108, title_bottom + detail_line // 2 + 8)
        detail = self.fit_detail(width - 2 * left - (16 if ready else 0))
        if ready:
            chip_width = self.detail_font.measure(detail) + 16
            half_height = (detail_line + 9) // 2
            rounded(self, left, detail_y - half_height, left + chip_width, detail_y + half_height,
                    8, fill="#F8EFFC", outline="#DFCAE9")
        self.create_text(left + (8 if ready else 0), detail_y, anchor="w", text=detail,
                         font=self.detail_font, fill=ACCENT_DARK if ready else MUTED, tags=("detail",))
        hint_y = detail_y + (detail_line + self.fonts["hint"].metrics("linespace")) // 2 + 6
        self.create_text(left, hint_y, anchor="w", text=self.hint, font=self.fonts["hint"],
                         fill=MUTED, tags=("hint",))
        self.draw_extra(width, height)

    def draw_extra(self, width, height):
        self.extra_area = None
        if self.extra is None or self.extra[1]() is None:
            self.extra_hover = False
            return
        label = self.extra[0]
        label_font = self.fonts["label"]
        button_width = label_font.measure(label) + 26
        button_height = label_font.metrics("linespace") + 10
        right, bottom = width - 17, height - 15
        area = (right - button_width, bottom - button_height, right, bottom)
        rounded(self, *area, 10, fill=EXTRA_HOVER if self.extra_hover else EXTRA_FILL,
                outline=EXTRA_TEXT if self.extra_hover else EXTRA_OUTLINE, tags=("extra",))
        self.create_text(right - button_width / 2, bottom - button_height / 2, text=label,
                         font=label_font, fill=EXTRA_TEXT, tags=("extra",))
        self.extra_area = area

    def draw_result(self, width, height, ready):
        y = height // 2
        self.draw_icon(35, y, ready)
        title_line = self.fonts["card"].metrics("linespace")
        text_lines = self.detail_font.metrics("linespace") + self.fonts["hint"].metrics("linespace")
        title_y = y - (title_line + text_lines + 7) // 2
        self.create_text(62, title_y, text=self.title, font=self.fonts["card"],
                         fill=TEXT, anchor="nw", tags=("title",))
        detail_y = title_y + title_line + 4
        button_text = self.open_label
        save_text = "한글 파일 저장"
        open_width = self.fonts["label"].measure(button_text) + 36
        save_width = self.fonts["label"].measure(save_text) + 36 if self.save is not None else 0
        reserved = open_width + (save_width + 8 if save_width else 0) + 24 if ready else 24
        self.create_text(62, detail_y, text=self.fit_detail(width - 62 - reserved),
                         font=self.detail_font, fill=TEXT if ready else MUTED, anchor="nw", tags=("detail",))
        self.save_area = None
        if ready:
            self.create_text(62, detail_y + self.detail_font.metrics("linespace") + 3,
                             text=self.hint, font=self.fonts["hint"], fill=MUTED, anchor="nw", tags=("hint",))
            half_height = self.fonts["label"].metrics("linespace") // 2 + 7
            right = width - 23
            for text, size, primary in ((button_text, open_width, True), (save_text, save_width, False)):
                if not size:
                    continue
                rounded(self, right - size, y - half_height, right, y + half_height,
                        11, fill=WHITE if primary else "#F8EFFC", outline=CARD_OUTLINE)
                self.create_text(right - size // 2, y, text=text, font=self.fonts["label"], fill=ACCENT_DARK)
                if not primary:
                    self.save_area = (right - size, y - half_height, right, y + half_height)
                right -= size + 8

    def draw_icon(self, x, y, ready):
        ink = ACCENT_DARK if ready else ACCENT
        rounded(self, x - 15, y - 15, x + 15, y + 15, 10,
                fill="#F4E7FC" if ready else "#F9EFFC", outline=BORDER)
        if self.kind == "source":
            self.create_line(x, y - 11, x, y + 4, fill=ink, width=2)
            self.create_line(x - 5, y, x, y + 5, x + 5, y, fill=ink, width=2)
            self.create_line(x - 9, y + 5, x - 9, y + 10, x + 9, y + 10, x + 9, y + 5,
                             fill=ink, width=2)
        elif self.kind == "json":
            for sign in (-1, 1):
                self.create_line(x + sign * 9, y - 9, x + sign * 5, y - 9, x + sign * 5, y - 3,
                                 x + sign * 2, y, x + sign * 5, y + 3, x + sign * 5, y + 9,
                                 x + sign * 9, y + 9, fill=ink, width=2)
        elif self.kind == "result" and ready:
            self.create_line(x - 9, y, x - 3, y + 6, x + 9, y - 7, fill=ink, width=2)
        else:
            self.create_line(x - 7, y + 11, x - 7, y - 10, x + 3, y - 10, x + 8, y - 5,
                             x + 8, y + 11, x - 7, y + 11, fill=ink, width=2)
            self.create_line(x + 3, y - 10, x + 3, y - 5, x + 8, y - 5, fill=ink, width=2)
            self.create_line(x - 3, y + 1, x + 4, y + 1, fill=ink, width=1)
            self.create_line(x - 3, y + 5, x + 4, y + 5, fill=ink, width=1)


class ConverterWindow:
    def __init__(self, root):
        self.root = root
        self.project = None
        self.markdown = None
        self.text_markdown = None    # 텍스트.md: the words only, without layout
        self.output = None
        self.converted_pdf = None
        self.source_name = None
        self.job = None
        self.markdown_results = {}
        self.hancom_results = {}
        self.results = queue.SimpleQueue()
        self.progress_notes = queue.SimpleQueue()   # messages from the worker thread
        self.receiver = None
        self.history_window = None
        self.fonts = screen_fonts(root)
        ttk.Style(root).theme_use("clam")

        root.title("TR · 문서 편집")
        root.configure(background=BACKGROUND)
        root.minsize(MIN_WIDTH, 440)
        root.geometry(f"{DEFAULT_WIDTH}x{DEFAULT_HEIGHT}")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)

        self.frame = tk.Frame(root, background=BACKGROUND, padx=19, pady=15)
        self.frame.grid(sticky="nsew")
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(4, weight=1)
        self.build_header()

        cards = tk.Frame(self.frame, background=BACKGROUND)
        cards.grid(row=1, column=0, sticky="ew", pady=(10, 6))
        for index, weight in enumerate((5, 5, 8, 6)):
            cards.columnconfigure(index, weight=weight, uniform="workflow")
        self.source_card = FileCard(cards, "파일을 이곳에 첨부하세요", "source",
                                   self.choose_source, lambda: None, None, fonts=self.fonts)
        self.md_card = FileCard(cards, "AI 업로드용 md 파일", "markdown",
                                lambda: self.open_file(self.markdown),
                                lambda: self.markdown if self.job != "export" else None,
                                self.send_markdown, fonts=self.fonts,
                                extra=("텍스트 파일",
                                       lambda: self.text_markdown if self.job != "export" else None,
                                       lambda: self.open_file(self.text_markdown), self.send_markdown))
        self.json_card = FileCard(cards, "AI가 편집한 JSON·ZIP 파일은 이곳에 업로드하세요", "json",
                                 self.choose_json, lambda: None, None, fonts=self.fonts)
        self.markdown_panel = self.make_result_panel(cards, "Markdown 결과", "원본을 넣으면 여기에 표시됩니다.")
        self.source_card.grid(row=0, column=0, sticky="nsew", padx=(0, 3))
        self.md_card.grid(row=0, column=1, sticky="nsew", padx=3)
        self.markdown_panel.grid(row=0, column=2, sticky="nsew", padx=5, pady=4)
        self.json_card.grid(row=0, column=3, sticky="nsew", padx=(3, 0))

        self.result_card = FileCard(self.frame, "편집된 한글 파일", "result",
                                    lambda: self.open_file(self.output),
                                    lambda: self.output if self.job is None else None,
                                    self.send_result, fonts=self.fonts, wide=True, save=self.save_result)
        self.result_card.grid(row=2, column=0, sticky="ew", pady=(0, 3))
        self.hancom_panel = self.make_result_panel(self.frame, "한글 파일 결과", "변환하거나 편집한 한글 파일이 여기에 표시됩니다.")
        self.hancom_panel.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 9))
        self.build_activity()
        root.report_callback_exception = self.callback_error
        root.bind_all("<Control-v>", self.paste_json)
        root.bind_all("<Control-V>", self.paste_json)
        self.refresh()
        self.set_status("한글·PDF 또는 편집 파일을 여러 개 놓을 수 있습니다. 글로 받은 JSON은 Ctrl+V로 붙여 넣으세요.")
        root.update_idletasks()
        minimum_height = max(630, self.frame.winfo_reqheight())
        root.minsize(MIN_WIDTH, minimum_height)
        root.geometry(f"{max(MIN_WIDTH, min(DEFAULT_WIDTH, root.winfo_screenwidth() - 36))}x"
                      f"{max(minimum_height, min(DEFAULT_HEIGHT, root.winfo_screenheight() - 80))}")
        try:
            self.receiver = DropReceiver(root, self.on_drop)
        except (OSError, RuntimeError) as error:
            self.set_status("끌어놓기를 시작할 수 없습니다. 카드를 클릭해 파일을 선택하세요.", "error")
            self.add_log(str(error), "error")

    def make_result_panel(self, parent, title, empty_text):
        return ResultPanel(parent, title, empty_text, fonts=self.fonts, colors=RESULT_COLORS,
                           open_file=self.open_file, drag_files=self.drag_selected, save_file=self.save_copy_of,
                           save_bundle=self.save_bundle, drag_bundle=self.drag_bundle, height=3)

    def build_header(self):
        bar = tk.Frame(self.frame, background=BACKGROUND)
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)
        mark_width = max(46, self.fonts["mark"].measure("TR") + 27)
        mark = tk.Canvas(bar, width=mark_width, height=42, background=BACKGROUND, highlightthickness=0)
        mark.grid(row=0, column=0, padx=(0, 8))
        self.mark_surface = clay_photo(mark, mark_width, 42, "#E5D5F3", outline=CARD_OUTLINE, radius=11)
        mark.create_image(0, 0, image=self.mark_surface, anchor="nw")
        mark.create_text(mark_width // 2, 21, text="TR", font=self.fonts["mark"], fill=ACCENT_DARK)
        tk.Label(bar, text="문서 편집", bg=BACKGROUND, fg=TEXT,
                 font=self.fonts["heading"], anchor="w").grid(row=0, column=1, sticky="ew")
        for column, (label, action) in enumerate(
            (("JSON 붙여넣기", self.paste_json), ("작업 내역", self.open_history),
             ("프로젝트 폴더", self.open_projects_folder)), start=2
        ):
            button = tk.Button(
                bar, text=label, command=action, font=self.fonts["label"],
                background=PAPER, foreground=TEXT, activebackground=LAVENDER, activeforeground=ACCENT_DARK,
                borderwidth=0, padx=11, pady=4, cursor="hand2", highlightthickness=1,
                highlightbackground=BORDER, highlightcolor=ACCENT, relief="flat",
            )
            button.grid(row=0, column=column, sticky="e", padx=(8, 0))

    def build_activity(self):
        panel = tk.Frame(self.frame, background=PAPER, highlightthickness=1, highlightbackground=BORDER,
                         padx=12, pady=7)
        panel.grid(row=4, column=0, sticky="nsew", padx=8)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(3, weight=1)
        tk.Label(panel, text="작업 기록", background=PAPER, foreground=TEXT,
                 font=self.fonts["label"]).grid(row=0, column=0, sticky="w")
        self.status = tk.Label(panel, background=PAPER, foreground=MUTED, font=self.fonts["body"],
                               anchor="nw", justify="left", height=2)
        self.status.grid(row=1, column=0, sticky="ew", pady=(2, 5))
        style = ttk.Style(self.root)
        style.configure("TR.Horizontal.TProgressbar", background=ACCENT, thickness=4,
                        troughcolor=LAVENDER, bordercolor=LAVENDER, lightcolor=ACCENT, darkcolor=ACCENT)
        track = tk.Frame(panel, background=LAVENDER, height=5)
        track.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        track.grid_propagate(False)
        track.rowconfigure(0, weight=1)
        track.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(track, mode="determinate", style="TR.Horizontal.TProgressbar")
        self.progress.grid(sticky="nsew")
        self.log = tk.Text(panel, height=2, width=1, wrap="word", state="disabled",
                           background=PAPER, foreground=MUTED, font=self.fonts["hint"], spacing1=2, spacing3=3,
                           selectbackground=LAVENDER, selectforeground=TEXT, borderwidth=0,
                           highlightthickness=0, padx=2, pady=1)
        self.log.grid(row=3, column=0, sticky="nsew")
        self.log.tag_configure("time", foreground="#716486")
        self.log.tag_configure("error", foreground=ERROR)
        self.log.tag_configure("ok", foreground=SUCCESS)
        style.configure("TR.Vertical.TScrollbar", background=LAVENDER, troughcolor=PAPER,
                        bordercolor=PAPER, lightcolor=LAVENDER, darkcolor=LAVENDER,
                        arrowsize=12, arrowcolor=MUTED)
        scroll = ttk.Scrollbar(panel, orient="vertical", command=self.log.yview,
                               style="TR.Vertical.TScrollbar")
        self.log.configure(yscrollcommand=scroll.set)
        scroll.grid(row=3, column=1, sticky="ns")
        panel.bind("<Configure>", lambda event: self.status.configure(wraplength=max(100, event.width - 38)))

    def refresh(self):
        idle = self.job is None
        markdown = self.md_card.get_path()
        result = self.result_card.get_path()
        self.source_card.update_card(self.source_name or "HWP · HWPX · PDF", "끌어놓기 · 클릭 선택", idle)
        md_placeholder = "AI용.md를 만드는 중입니다" if self.job == "export" else "원본을 놓으면 생성됩니다"
        self.md_card.update_card(markdown.name if markdown else md_placeholder,
                                 "끌어서 AI에 첨부" if markdown else "", markdown is not None)
        self.json_card.update_card("편집.json · 편집.zip", "끌어놓기 · 클릭 · Ctrl+V", idle)
        result_placeholder = "편집본을 저장하는 중입니다" if self.job == "apply" else "편집 JSON을 놓으면 저장됩니다"
        converted = result is not None and result == self.converted_pdf
        self.result_card.title = "PDF에서 변환한 한글 파일" if converted else "편집된 한글 파일"
        self.result_card.open_label = "한글에서 열기" if converted else "편집본 열기"
        self.result_card.update_card(result.name if result else result_placeholder,
                                     "끌어서 다른 폴더로 · AI로 고치려면 위의 AI용.md를 첨부" if converted
                                     else "끌어서 다른 폴더로" if result else "",
                                     result is not None)
        self.markdown_panel.set_paths(path for path in self.markdown_results if path.is_file())
        self.hancom_panel.set_paths(path for path in self.hancom_results if path.is_file())

    def choose_source(self):
        selected = filedialog.askopenfilenames(parent=self.root, title="원본 파일 첨부",
                                                filetypes=[("한글 문서 또는 PDF", "*.hwp *.hwpx *.pdf")])
        if selected:
            self.on_drop(tuple(map(Path, selected)))

    def choose_json(self):
        selected = filedialog.askopenfilenames(
            parent=self.root, title="AI가 편집한 JSON 파일 (그림도 함께 고를 수 있습니다)",
            filetypes=[("편집 지시와 그림", "*.json *.zip *.txt *.png *.jpg *.jpeg"), ("모든 파일", "*.*")])
        if selected:
            self.on_drop(tuple(Path(item) for item in selected))

    def on_drop(self, paths, _x=None, _y=None):
        if self.job is not None:
            self.add_log("진행 중인 변환이 끝나면 다음 파일을 놓아 주세요.")
            return
        paths = list(dict.fromkeys(map(Path, paths)))
        if not paths:
            return
        if len(paths) == 1 and paths[0].is_dir():
            if project_metadata(paths[0]):
                self.load_project(paths[0].resolve())
            else:
                self.report_error("TR 프로젝트 폴더가 아닙니다. 한컴 파일이나 편집 JSON을 놓아 주세요.")
            return
        documents = [path for path in paths if path.suffix.lower() in FORMATS | {".pdf"}]
        edits = [path for path in paths if path.suffix.lower() in EDIT_TYPES]
        images = [path for path in paths if path.suffix.lower() in IMAGE_TYPES]
        others = [path for path in paths if path not in documents + edits + images]
        if others:
            self.report_error(f"TR이 다룰 수 없는 파일입니다. {others[0].name}  "
                              "한컴 파일·PDF, 편집 JSON·ZIP, PNG·JPEG 그림을 놓아 주세요.")
        elif documents:
            if edits or images:
                self.report_error("원본 파일과 편집 파일·그림은 따로 넣어 주세요.")
            else:
                self.start_batch("export", documents)
        elif edits:
            if len(edits) > 1 and images:
                self.report_error("편집 파일을 여러 개 넣을 때는 JSON과 해당 그림을 파일별로 ZIP에 묶어 주세요.")
            else:
                self.start_batch("apply", edits, images=images)
        elif images:
            self.add_project_images(images)

    def add_project_images(self, images):
        if self.project is None:
            self.report_error("그림을 넣을 프로젝트가 없습니다. 먼저 한컴 파일을 놓거나 작업 내역에서 프로젝트를 여세요.")
            return
        try:
            names = add_images(images, self.project)
        except (OSError, ValueError) as error:
            self.report_error(str(error))
            return
        self.set_status("그림을 프로젝트의 assets 폴더에 넣었습니다. AI에게 파일명을 알려 주세요.", "ok")
        self.add_log(f"assets에 추가 · {', '.join(names)}", "ok")

    def paste_json(self, _event=None):
        if self.job is not None:
            self.add_log("진행 중인 변환이 끝나면 붙여 넣어 주세요.")
            return "break"
        focus = self.root.focus_get()
        if isinstance(focus, (tk.Entry, tk.Text)) and focus is not self.log:
            return None
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            self.report_error("클립보드에 글이 없습니다. AI가 보여 준 JSON을 복사한 뒤 다시 붙여 넣어 주세요.")
            return "break"
        candidate = Path(text.strip().strip('"')) if len(text) < 1000 and "\n" not in text.strip() else None
        try:
            if candidate is not None and candidate.is_file():
                self.on_drop((candidate,))
                return "break"
        except OSError:
            pass
        self.start_batch("apply", (None,), text=text)
        return "break"

    def choose_missing_images(self, pending):
        for name, note in pending.missing:
            description = f" · {note}" if note else ""
            self.set_status(f"편집에 필요한 그림 {name}이 없습니다. 이 자리에 넣을 그림을 골라 주세요.", "busy")
            self.add_log(f"필요한 그림 {name}{description}")
            selected = filedialog.askopenfilename(
                parent=self.root, title=f"'{name}' 자리에 넣을 그림 선택{description}"[:200],
                filetypes=[("그림", "*.png *.jpg *.jpeg")])
            if selected:
                pending.mapping[name] = Path(selected)
                self.add_log(f"{name} ← {Path(selected).name}", "ok")
            else:
                self.add_log(f"{name}을 고르지 않았습니다. 이 그림 편집은 건너뜁니다.", "error")

    def start_batch(self, kind, paths, *, text=None, images=()):
        self.job = kind
        self.batch_paths = deque(paths)
        self.batch_total = len(paths)
        self.batch_index = 0
        self.batch_saved = 0
        self.batch_partial = 0
        self.batch_project = self.project
        self.batch_text = text
        self.batch_images = images
        self.hancom_results.clear()
        if kind == "export":
            self.markdown_results.clear()
        self.output = None
        self.progress.configure(mode="determinate", maximum=self.batch_total, value=0)
        self.refresh()
        self.advance_batch()

    def advance_batch(self):
        self.progress.configure(value=self.batch_index)
        if not self.batch_paths:
            failed = self.batch_total - self.batch_saved
            summary = f"{self.batch_total}개 중 {self.batch_saved}개 저장"
            if failed:
                summary += f" · {failed}개 실패"
            if self.batch_partial:
                summary += f" · {self.batch_partial}개는 일부 편집 확인 필요"
            level = "error" if not self.batch_saved else "busy" if failed or self.batch_partial else "ok"
            self.job = None
            self.batch_text = None
            self.batch_images = ()
            self.refresh()
            self.set_status(summary, level)
            self.add_log(summary, level)
            if self.history_window is not None and self.history_window.winfo_exists():
                self.history_window.refresh_history(self.project)
            return

        path = self.batch_paths.popleft()
        self.batch_index += 1
        self.batch_label = f"{self.batch_index}/{self.batch_total} · {path.name if path else '붙여 넣은 JSON'}"
        self.set_status(self.batch_label, "busy")
        self.add_log(f"처리 중 · {self.batch_label}")
        if self.job == "export":
            self.run_job(export_dropped, path, self.progress_notes.put)
        else:
            try:
                pending = prepare_edit(path, text=self.batch_text, images=self.batch_images,
                                       active_project=self.batch_project)
                self.choose_missing_images(pending)
            except Exception as error:
                detail = str(error) or type(error).__name__
                self.add_log(f"실패 · {self.batch_label} · {detail}", "error")
                self.root.after_idle(self.advance_batch)
                return
            self.run_job(finish_edit, pending)

    def run_job(self, action, *args):
        def work():
            try:
                self.results.put((action(*args), None))
            except Exception as error:
                self.results.put((None, str(error) or type(error).__name__))

        threading.Thread(target=work, name="tr-converter").start()
        self.root.after(60, self.check_result)

    def check_result(self):
        note = None
        while True:
            try:
                note = self.progress_notes.get_nowait()
            except queue.Empty:
                break
        if note:
            self.set_status(f"{self.batch_label}\n{note}", "busy")
        try:
            result, error = self.results.get_nowait()
        except queue.Empty:
            self.root.after(60, self.check_result)
            return
        if error is not None:
            self.add_log(f"실패 · {self.batch_label} · {error}", "error")
        elif self.job == "export":
            project, detail = result
            self.select_project(project)
            self.add_markdown_results()
            if self.converted_pdf is not None:
                self.hancom_results[self.converted_pdf] = None
            self.add_log(f"{detail}  {project}", "ok")
        else:
            project, output, report, report_path = result
            self.select_project(project)
            self.output = output
            self.hancom_results[output] = None
            if report.failures or report.unresolved:
                self.batch_partial += 1
            self.add_log(f"{report}  {self.output}", "ok")
            for note in report.notes:
                self.add_log(f"AI 메모 · {note}")
            for warning in report.warnings:
                self.add_log(f"참고 · {warning}")
            for number, _, error in report.failures:
                self.add_log(f"{number}번째 편집 건너뜀 · {error}", "error")
            for label, instruction in report.unresolved:
                self.add_log(f"남은 지시 {label} · {instruction}", "error")
            if report_path is not None:
                self.add_log(f"적용 보고를 저장했습니다. AI에게 다시 보낼 메시지가 들어 있습니다.  {report_path}")
        if error is None:
            self.batch_saved += 1
        self.refresh()
        self.root.after_idle(self.advance_batch)

    def add_markdown_results(self):
        self.markdown_results.update(dict.fromkeys(path for path in (self.markdown, self.text_markdown) if path))

    def select_project(self, project):
        self.project = project
        self.markdown = project / AI_DOCUMENT
        if not self.markdown.is_file():
            self.markdown = None
        self.text_markdown = text_document(project)
        meta = project_metadata(project) or {}
        source_name = meta.get("pdf_name") or meta.get("source_name")
        self.source_name = source_name if isinstance(source_name, str) and source_name else project.name
        self.converted_pdf = pdf_conversion(project)
        self.output = next(iter(project_outputs(project)), None) or self.converted_pdf

    def load_project(self, project):
        if self.job is not None:
            self.add_log("파일 처리가 끝나면 이전 프로젝트를 불러올 수 있습니다.")
            return False
        if not project_metadata(project):
            self.report_error(f"프로젝트를 찾을 수 없습니다. {project}")
            return False
        self.select_project(project)
        self.markdown_results.clear()
        self.add_markdown_results()
        self.hancom_results = dict.fromkeys(project_outputs(project))
        if self.converted_pdf is not None:
            self.hancom_results[self.converted_pdf] = None
        self.refresh()
        self.set_status("이전 프로젝트를 불러왔습니다.", "ok")
        self.add_log(str(project), "ok")
        return True

    def open_projects_folder(self):
        directory = projects_folder()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self.report_error(str(error))
            return
        self.open_file(directory)

    def open_history(self):
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window.refresh_history(self.project)
            self.history_window.lift()
            self.history_window.focus_set()
            return
        self.history_window = show_project_history(
            self.root, fonts=self.fonts, colors=RESULT_COLORS, active_project=self.project,
            load_project=self.load_project, open_file=self.open_file,
        )

    def set_status(self, text, level="idle"):
        colors = {"idle": MUTED, "busy": ACCENT_DARK, "ok": SUCCESS, "error": ERROR}
        self.status.configure(text=text, foreground=colors[level])

    def add_log(self, message, level="idle"):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", stamp + "   ", "time")
        self.log.insert("end", message + "\n", level)
        self.log.see("end")
        self.log.configure(state="disabled")

    def report_error(self, message):
        self.set_status("작업을 완료하지 못했습니다. 아래 기록을 확인하세요.", "error")
        self.add_log(message, "error")

    def callback_error(self, error_type, error, stack):
        traceback.print_exception(error_type, error, stack)
        self.report_error(str(error) or error_type.__name__)

    def open_file(self, path):
        if path is None:
            return
        try:
            if not path.exists():
                raise FileNotFoundError(f"파일 또는 폴더를 찾을 수 없습니다. {path}")
            try:
                os.startfile(path)
            except OSError as error:
                if path.suffix.lower() != ".md" or getattr(error, "winerror", None) != 1155:
                    raise
                os.startfile("notepad.exe", arguments=f'"{path}"')
        except OSError as error:
            self.report_error(str(error))

    def send_markdown(self, path):
        self.send_file(path, allow_move=False)

    def send_result(self, path):
        if self.send_file(path, allow_move=True) == "move":
            self.output = None
            self.hancom_results.pop(path, None)
            self.refresh()
            self.set_status("결과 파일을 프로젝트 밖으로 이동했습니다.", "ok")
            self.add_log(f"이동 후 프로젝트 폴더에서 빠진 파일 · {path.name}", "ok")

    def save_result(self):
        if self.output is None or self.job is not None:
            return
        name = self.output.name
        if self.output == self.converted_pdf:
            name = Path(self.source_name).with_suffix(".hwp").name
        self.save_copy_of(self.output, name=name)

    def ask_destination(self, title, name, kind):
        extension = Path(name).suffix.lower()
        downloads = Path.home() / "Downloads"
        path = filedialog.asksaveasfilename(parent=self.root, title=title, initialfile=name,
                                            defaultextension=extension,
                                            initialdir=str(downloads) if downloads.is_dir() else None,
                                            filetypes=[(kind, f"*{extension}")])
        return Path(path) if path else None

    def save_copy_of(self, source, *, name=None):
        markdown = source.suffix.lower() == ".md"
        if name is None:
            name = windows_filename(source.parent.name, f" - {source.name}") if markdown else source.name
        kind = "Markdown" if markdown else "한글 파일"
        target = self.ask_destination(f"{kind} 저장", name, kind)
        if target is None:
            return
        try:
            save_copy(source, target)
        except (OSError, ValueError) as error:
            self.report_error(str(error))
            return
        self.add_log(f"저장 · {target}", "ok")

    @staticmethod
    def bundle_title(paths):
        return "TR Markdown 묶음" if paths[0].suffix.lower() == ".md" else "TR 한글 파일 묶음"

    def save_bundle(self, paths):
        target = self.ask_destination("ZIP 묶음 저장", self.bundle_title(paths) + ".zip", "ZIP 묶음")
        if target is None:
            return
        try:
            write_bundle(paths, target)
        except (OSError, ValueError) as error:
            self.report_error(str(error))
            return
        self.add_log(f"ZIP {len(paths)}개 묶음 저장 · {target}", "ok")

    def drag_selected(self, paths):
        if len(paths) == 1:
            self.send_file(paths[0], allow_move=False)
        else:
            self.drag_bundle(paths)

    def drag_bundle(self, paths):
        # Keep the archive available for targets that read the file after the drop.
        folder = projects_folder() / "묶음"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = next(candidate_paths(folder, self.bundle_title(paths), "", ".zip"))
            write_bundle(paths, target)
        except (OSError, ValueError) as error:
            self.report_error(str(error))
            return
        self.send_file(target, allow_move=False)

    def send_file(self, path, *, allow_move):
        try:
            effect = drag_file(path, allow_move=allow_move)
        except (OSError, RuntimeError, ValueError) as error:
            self.report_error(str(error))
            return None
        if effect == "copy":
            self.add_log(f"파일을 드래그하여 전달했습니다.  {path.name}", "ok")
        return effect

    def close(self):
        if self.job is not None:
            self.add_log("파일 처리가 끝나면 창을 닫을 수 있습니다.")
            return
        self.progress.stop()
        if self.receiver is not None:
            self.receiver.close()
        self.root.destroy()


# Kept beside the program, not in the repository (it holds other companies' logos). Windows caches
# icons by path, so a redesigned icon gets a new file name rather than overwriting this one.
ICON_FILE = "TR 아이콘.ico"


def use_tr_icon(root):
    """Show the TR icon on the window and taskbar."""
    icon = Path(__file__).with_name(ICON_FILE)
    if not icon.is_file():
        return
    try:
        root.iconbitmap(default=str(icon))   # default=: the history window uses it too
    except tk.TclError:
        pass


def main():
    try:
        # Group TR under its own taskbar button instead of Python's, so the TR icon shows.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TR.DocumentEditor")
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    use_tr_icon(root)
    ConverterWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
