"""Windows desktop workflow for importing and exporting TR files."""

import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font, ttk

from tr_core import AI_DOCUMENT, FORMATS
from tr_drag import DropReceiver, drag_file
from tr_workflow import apply_dropped, export_dropped, project_metadata


BACKGROUND = "#F3EFFB"
PAPER = "#FBF9FF"
WHITE = "#FFFFFF"
TEXT = "#251D3A"
MUTED = "#5E5473"
LAVENDER = "#EDE5FC"
BORDER = "#D9CDEB"
ACCENT = "#6844AF"
ACCENT_DARK = "#543390"
HOVER = "#F7F1FF"
ERROR = "#982854"
SUCCESS = "#326346"


def rounded(canvas, x0, y0, x1, y1, radius, **options):
    r = radius
    points = (x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
              x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0)
    return canvas.create_polygon(points, smooth=True, splinesteps=20, **options)


class FileCard(tk.Canvas):
    def __init__(self, parent, title, kind, click, get_path, drag, *, wide=False):
        super().__init__(parent, background=BACKGROUND, height=116 if wide else 177,
                         highlightthickness=0, borderwidth=0, takefocus=True)
        self.title = title
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
        self.detail_font = font.Font(self, family="Malgun Gothic", size=10)
        self.bind("<Configure>", self.draw)
        self.bind("<Enter>", lambda event: self.set_hover(True))
        self.bind("<Leave>", lambda event: self.set_hover(False))
        self.bind("<FocusIn>", self.draw)
        self.bind("<FocusOut>", self.draw)
        self.bind("<ButtonPress-1>", self.mouse_down)
        self.bind("<B1-Motion>", self.mouse_motion)
        self.bind("<ButtonRelease-1>", self.mouse_up)
        self.bind("<Return>", self.activate)
        self.bind("<space>", self.activate)

    def update_card(self, detail, hint, enabled):
        self.detail, self.hint, self.enabled = detail, hint, enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self.draw()

    def set_hover(self, state):
        self.hover = state
        self.draw()

    def mouse_down(self, event):
        if self.enabled:
            self.focus_set()
            self.press = (event.x_root, event.y_root)

    def mouse_motion(self, event):
        if self.press is None or self.drag is None:
            return
        x, y = self.press
        if max(abs(event.x_root - x), abs(event.y_root - y)) < 7:
            return
        self.press = None
        path = self.get_path()
        if path is not None:
            self.drag(path)

    def mouse_up(self, event):
        pressed = self.press is not None
        self.press = None
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
        tail = "…" + (suffix if suffix.lower() in FORMATS | {".md", ".json"} else "")
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
        fill = HOVER if active else WHITE if ready else PAPER
        outline = ACCENT if (active or focused) and self.enabled else BORDER
        rounded(self, 3, 4, width - 2, height - 1, 16, fill="#E7DFF3", outline="")
        rounded(self, 2, 2, width - 3, height - 4, 16, fill=fill, outline=outline,
                width=2 if focused and self.enabled else 1)
        if self.wide:
            x = 63
            y = height // 2
            self.draw_icon(x, y, ready)
            self.create_text(105, y - 24, text=self.title, font=("Malgun Gothic", 12, "bold"),
                             fill=TEXT, anchor="w")
            self.create_text(105, y, text=self.fit_detail(width - 135), font=self.detail_font,
                             fill=TEXT if ready else MUTED, anchor="w")
            self.create_text(105, y + 26, text=self.hint, font=("Malgun Gothic", 9),
                             fill=ACCENT_DARK if ready else MUTED, anchor="w")
            return
        x = width // 2
        self.draw_icon(x, 42, ready)
        self.create_text(x, 81, text=self.title, font=("Malgun Gothic", 12, "bold"), fill=TEXT)
        self.create_text(x, 113, text=self.fit_detail(width - 34), font=self.detail_font,
                         fill=TEXT if ready else MUTED)
        self.create_text(x, 144, text=self.hint, font=("Malgun Gothic", 9), fill=ACCENT_DARK)

    def draw_icon(self, x, y, ready):
        ink = WHITE if ready else ACCENT
        rounded(self, x - 21, y - 21, x + 21, y + 21, 11,
                fill=ACCENT if ready else LAVENDER, outline="")
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
        self.output = None
        self.source_name = None
        self.job = None
        self.results = queue.SimpleQueue()
        self.receiver = None

        root.title("TR · 한글 문서 편집")
        root.configure(background=BACKGROUND)
        root.minsize(790, 576)
        root.geometry("980x634")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)

        self.frame = tk.Frame(root, background=BACKGROUND, padx=28, pady=23)
        self.frame.grid(sticky="nsew")
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(3, weight=1)
        self.build_header()

        cards = tk.Frame(self.frame, background=BACKGROUND)
        cards.grid(row=1, column=0, sticky="ew", pady=(19, 17))
        for index in range(3):
            cards.columnconfigure(index, weight=1, uniform="workflow")
        self.source_card = FileCard(cards, "원본 한글", "source", self.choose_source, lambda: None, None)
        self.md_card = FileCard(cards, "AI에 보낼 파일", "markdown",
                                lambda: self.open_file(self.markdown),
                                lambda: self.markdown if self.job != "export" else None, self.send_markdown)
        self.json_card = FileCard(cards, "AI의 편집 결과", "json", self.choose_json, lambda: None, None)
        self.source_card.grid(row=0, column=0, sticky="ew", padx=(0, 7))
        self.md_card.grid(row=0, column=1, sticky="ew", padx=7)
        self.json_card.grid(row=0, column=2, sticky="ew", padx=(7, 0))

        self.result_card = FileCard(self.frame, "편집된 한글 파일", "result",
                                    lambda: self.open_file(self.output),
                                    lambda: self.output if self.job is None else None,
                                    self.send_result, wide=True)
        self.result_card.configure(width=455)
        self.result_card.grid(row=2, column=0, pady=(0, 18))
        self.build_activity()
        self.refresh()
        self.set_status("원본 한글 파일을 놓아 시작하세요.")
        root.update_idletasks()
        try:
            self.receiver = DropReceiver(root, self.on_drop, accepts=lambda _x, _y: self.job is None)
        except (OSError, RuntimeError) as error:
            self.set_status("끌어놓기를 시작할 수 없습니다. 카드를 클릭해 파일을 선택하세요.", "error")
            self.add_log(str(error), "error")

    def build_header(self):
        bar = tk.Frame(self.frame, background=BACKGROUND)
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)
        mark = tk.Canvas(bar, width=49, height=46, background=BACKGROUND, highlightthickness=0)
        mark.grid(row=0, column=0, padx=(0, 11))
        rounded(mark, 2, 2, 47, 44, 11, fill=ACCENT, outline="")
        mark.create_text(24, 22, text="TR", font=("Malgun Gothic", 14, "bold"), fill=WHITE)
        tk.Label(bar, text="한글 문서 편집", bg=BACKGROUND, fg=TEXT,
                 font=("Malgun Gothic", 22, "bold"), anchor="w").grid(row=0, column=1, sticky="ew")
        self.project_button = tk.Button(
            bar, text="프로젝트 폴더", command=lambda: self.open_file(self.project),
            font=("Malgun Gothic", 10, "bold"), background=LAVENDER, foreground=ACCENT_DARK,
            activebackground="#DAC9F5", activeforeground=TEXT, disabledforeground="#756989",
            borderwidth=0, padx=14, pady=7, cursor="hand2", highlightthickness=0,
        )
        self.project_button.grid(row=0, column=2, sticky="e")

    def build_activity(self):
        panel = tk.Frame(self.frame, background=PAPER, highlightthickness=1, highlightbackground=BORDER,
                         padx=14, pady=9)
        panel.grid(row=3, column=0, sticky="nsew")
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(3, weight=1)
        tk.Label(panel, text="작업 기록", background=PAPER, foreground=TEXT,
                 font=("Malgun Gothic", 11, "bold")).grid(row=0, column=0, sticky="w")
        self.status = tk.Label(panel, background=PAPER, foreground=MUTED, font=("Malgun Gothic", 9),
                               anchor="w", justify="left")
        self.status.grid(row=1, column=0, sticky="ew", pady=(1, 6))
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TR.Horizontal.TProgressbar", background="#9A77D7",
                        troughcolor=LAVENDER, bordercolor=LAVENDER, lightcolor="#9A77D7", darkcolor="#9A77D7")
        self.progress = ttk.Progressbar(panel, mode="determinate", style="TR.Horizontal.TProgressbar")
        self.progress.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 7))
        self.log = tk.Text(panel, height=2, width=1, wrap="word", state="disabled",
                           background=PAPER, foreground=MUTED, font=("Malgun Gothic", 9),
                           selectbackground=LAVENDER, selectforeground=TEXT, borderwidth=0,
                           highlightthickness=0, padx=2, pady=1)
        self.log.grid(row=3, column=0, sticky="nsew")
        self.log.tag_configure("time", foreground="#716486")
        self.log.tag_configure("error", foreground=ERROR)
        self.log.tag_configure("ok", foreground=SUCCESS)
        style.configure("TR.Vertical.TScrollbar", background=LAVENDER, troughcolor=PAPER,
                        bordercolor=PAPER, arrowsize=12, arrowcolor=MUTED)
        scroll = ttk.Scrollbar(panel, orient="vertical", command=self.log.yview,
                               style="TR.Vertical.TScrollbar")
        self.log.configure(yscrollcommand=scroll.set)
        scroll.grid(row=3, column=1, sticky="ns")
        panel.bind("<Configure>", lambda event: self.status.configure(wraplength=max(100, event.width - 38)))

    def refresh(self):
        idle = self.job is None
        markdown = self.md_card.get_path()
        result = self.result_card.get_path()
        self.source_card.update_card(self.source_name or "HWP 또는 HWPX", "여기에 끌어놓기 · 클릭 선택", idle)
        md_placeholder = "AI용.md를 만드는 중입니다" if self.job == "export" else "원본을 놓으면 생성됩니다"
        self.md_card.update_card(markdown.name if markdown else md_placeholder,
                                 "끌어서 AI에 첨부" if markdown else "Markdown", markdown is not None)
        self.json_card.update_card("다운로드한 편집 JSON", "여기에 끌어놓기 · 클릭 선택", idle)
        result_placeholder = "편집본을 저장하는 중입니다" if self.job == "apply" else "편집 JSON을 놓으면 저장됩니다"
        self.result_card.update_card(result.name if result else result_placeholder,
                                     "클릭하여 열기 · 끌어서 다른 폴더로" if result else "HWP 또는 HWPX",
                                     result is not None)
        self.project_button.configure(state="normal" if self.project else "disabled")

    def choose_source(self):
        selected = filedialog.askopenfilename(parent=self.root, title="원본 한글 파일",
                                               filetypes=[("한글 문서", "*.hwp *.hwpx")])
        if selected:
            self.on_drop((Path(selected),))

    def choose_json(self):
        selected = filedialog.askopenfilename(parent=self.root, title="AI의 편집 결과",
                                               filetypes=[("편집 지시", "*.json")])
        if selected:
            self.on_drop((Path(selected),))

    def on_drop(self, paths, _x=None, _y=None):
        if self.job is not None:
            self.add_log("진행 중인 변환이 끝나면 다음 파일을 놓아 주세요.")
            return
        if len(paths) != 1:
            self.report_error("한 번에 한 파일을 놓아 주세요.")
            return
        path = Path(paths[0])
        if path.is_dir() and project_metadata(path):
            self.select_project(path.resolve())
            self.output = None
            self.refresh()
            self.set_status("기존 TR 프로젝트를 연결했습니다.", "ok")
            self.add_log(str(self.project), "ok")
            return
        file_type = path.suffix.lower()
        if file_type in FORMATS:
            self.start("export", "원본을 읽고 바탕화면에 프로젝트를 만드는 중입니다.", export_dropped, path)
        elif file_type == ".json":
            self.start("apply", "편집 JSON을 적용하고 한글 파일을 저장하는 중입니다.", apply_dropped,
                       path, self.project)
        else:
            self.report_error("원본 HWP/HWPX 또는 AI가 반환한 JSON 파일을 놓아 주세요.")

    def start(self, kind, message, action, *args):
        self.job = kind
        self.refresh()
        self.set_status(message, "busy")
        self.add_log(f"{message}  {Path(args[0]).name}")
        self.progress.configure(mode="indeterminate")
        self.progress.start(14)

        def work():
            try:
                self.results.put((action(*args), None))
            except Exception as error:
                self.results.put((None, error))

        threading.Thread(target=work, name="tr-converter").start()
        self.root.after(60, self.check_result)

    def check_result(self):
        try:
            result, error = self.results.get_nowait()
        except queue.Empty:
            self.root.after(60, self.check_result)
            return
        kind, self.job = self.job, None
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        if error is not None:
            self.refresh()
            self.report_error(str(error) or type(error).__name__)
            return
        if kind == "export":
            project, detail = result
            self.select_project(project)
            self.output = None
            self.set_status("AI에 보낼 파일이 준비되었습니다.", "ok")
            self.add_log(f"{detail}  {project}", "ok")
        else:
            project, self.output, detail = result
            self.select_project(project)
            self.set_status("편집된 한글 파일을 저장했습니다.", "ok")
            self.add_log(f"{detail}  {self.output}", "ok")
        self.refresh()

    def select_project(self, project):
        self.project = project
        self.markdown = project / AI_DOCUMENT
        if not self.markdown.is_file():
            self.markdown = None
        meta = project_metadata(project) or {}
        self.source_name = meta.get("source_name") or project.name

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
            self.refresh()
            self.set_status("편집본을 프로젝트 밖으로 이동했습니다.", "ok")
            self.add_log(f"이동 후 프로젝트 폴더에서 빠진 파일 · {path.name}", "ok")

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
        if self.receiver is not None:
            self.receiver.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    ConverterWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
