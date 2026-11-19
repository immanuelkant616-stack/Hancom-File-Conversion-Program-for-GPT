"""Modeless window for browsing saved TR project activity."""

import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk

from tr_core import PREVIEW
from tr_workflow import discover_projects, project_activity, project_metadata


DATE_FORMAT = "%Y.%m.%d  %H:%M:%S"


class ProjectHistory(tk.Toplevel):
    def __init__(self, root, fonts, colors, active_project, load_project, open_file):
        super().__init__(root, background=colors["background"])
        self.root = root
        self.fonts = fonts
        self.colors = colors
        self.active_project = None
        self.load_project = load_project
        self.open_file = open_file
        self.project_rows = {}
        self.row_data = {}
        self.title("TR · 프로젝트 기록")
        self.geometry("776x480")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        frame = tk.Frame(self, background=colors["background"], padx=22, pady=18)
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        header = tk.Frame(frame, background=colors["background"])
        header.grid(row=0, column=0, sticky="ew", pady=(0, 13))
        header.columnconfigure(0, weight=1)
        tk.Label(header, text="프로젝트 기록", font=fonts.get("heading", fonts["label"]),
                 background=colors["background"], foreground=colors["text"], padx=0,
                 pady=0).grid(sticky="w")
        self.make_button(header, "새로고침", self.refresh).grid(row=0, column=1)

        panel = tk.Frame(frame, background=colors["white"],
                         highlightthickness=1, highlightbackground=colors["border"])
        panel.grid(row=1, column=0, sticky="nsew")
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(0, weight=1)
        body = tk.Frame(panel, background=colors["white"])
        body.grid(row=0, column=0, sticky="nsew", padx=9, pady=(6, 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        row_height = max(31, fonts["body"].metrics("linespace") + 10)
        style = ttk.Style(self)
        style.configure("TRHistory.Treeview", background=colors["white"],
                        fieldbackground=colors["white"], foreground=colors["text"],
                        borderwidth=0, relief="flat", font=fonts["body"],
                        rowheight=row_height)
        style.configure("TRHistory.Treeview.Heading", font=fonts["label"], padding=(5, 6),
                        foreground=colors["muted"], background=colors["paper"],
                        lightcolor=colors["paper"], darkcolor=colors["paper"],
                        bordercolor=colors["border"], relief="flat")
        style.map("TRHistory.Treeview", background=[("selected", colors["lavender"])],
                  foreground=[("selected", colors["accent_dark"])])
        self.tree = ttk.Treeview(body, columns=("state", "when"), selectmode="browse",
                                 style="TRHistory.Treeview")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.heading("#0", text="프로젝트와 작업", anchor="w")
        self.tree.heading("state", text="상태", anchor="w")
        self.tree.heading("when", text="작업 시간", anchor="w")
        self.tree.column("#0", width=358, minwidth=195)
        state_width = max(105, fonts["label"].measure("현재 프로젝트") + 16)
        date_sample = "8888.88.88  88:88:88"
        when_width = max(148, fonts["label"].measure(date_sample) + 16,
                         fonts["body"].measure(date_sample) + 16)
        self.tree.column("state", width=state_width, minwidth=state_width, stretch=False)
        self.tree.column("when", width=when_width, minwidth=when_width, stretch=False)
        self.tree.tag_configure("project", background=colors["paper"], font=fonts["label"])
        self.tree.tag_configure("active", foreground=colors["accent_dark"])
        self.tree.tag_configure("missing", foreground=colors["muted"])
        style.configure("TRHistory.Vertical.TScrollbar", background=colors["paper"],
                        troughcolor=colors["white"], bordercolor=colors["white"],
                        lightcolor=colors["paper"], darkcolor=colors["paper"],
                        arrowcolor=colors["muted"], arrowsize=12)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview,
                               style="TRHistory.Vertical.TScrollbar")
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.empty = tk.Label(body, text="저장된 TR 프로젝트가 없습니다.",
                              background=colors["white"], foreground=colors["muted"],
                              font=fonts["body"], padx=12, pady=7)

        tk.Frame(panel, background=colors["border"], height=1).grid(row=1, column=0, sticky="ew")
        self.detail = tk.Label(panel, anchor="w", justify="left", background=colors["paper"],
                               foreground=colors["muted"], font=fonts["hint"], wraplength=770,
                               padx=10, pady=6)
        self.detail.grid(row=2, column=0, sticky="ew")
        panel.bind("<Configure>", self.resize_detail)
        footer = tk.Frame(frame, background=colors["background"])
        footer.grid(row=2, column=0, sticky="ew", pady=(11, 0))
        footer.columnconfigure(3, weight=1)
        self.load_button = self.make_button(footer, "프로젝트 불러오기", self.load_selection, primary=True)
        self.load_button.grid(row=0, column=0, padx=(0, 8))
        self.open_button = self.make_button(footer, "선택 항목 열기", self.open_selection)
        self.open_button.grid(row=0, column=1, padx=(0, 8))
        self.preview_button = self.make_button(footer, "원본 미리보기", self.open_preview)
        self.preview_button.grid(row=0, column=2)
        close_button = self.make_button(footer, "닫기", self.destroy)
        close_button.grid(row=0, column=4, sticky="e")
        footer_width = sum(button.winfo_reqwidth() for button in (
            self.load_button, self.open_button, self.preview_button, close_button
        ))
        minimum_width = max(610, state_width + when_width + 282, footer_width + 68)
        heading_height = fonts.get("heading", fonts["label"]).metrics("linespace")
        self.minsize(minimum_width, max(360, heading_height + 4 * row_height + 142))
        self.tree.bind("<<TreeviewSelect>>", self.selection_changed)
        self.tree.bind("<Double-1>", self.open_event)
        self.tree.bind("<Return>", self.open_event)
        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<F5>", lambda _event: self.refresh())
        self.refresh_history(active_project)

    def resize_detail(self, event):
        self.detail.configure(wraplength=max(120, event.width - 24))

    def make_button(self, parent, label, command, *, primary=False):
        c = self.colors
        background = c["accent"] if primary else c["white"]
        hover = c["accent_dark"] if primary else c["lavender"]
        button = tk.Button(
            parent, text=label, command=command, font=self.fonts["body"],
            background=background,
            foreground=c["white"] if primary else c["text"],
            activebackground=hover,
            activeforeground=c["white"] if primary else c["text"],
            disabledforeground=c["muted"], relief="flat", borderwidth=0, padx=11, pady=5,
            highlightthickness=1, highlightbackground=c["border"],
            highlightcolor=c["accent_dark"],
        )
        button.bind("<Enter>", lambda _event: self.hover_button(button, hover, True))
        button.bind("<Leave>", lambda _event: self.hover_button(button, background, False))
        return button

    @staticmethod
    def hover_button(button, background, hovered):
        if button.cget("state") != "disabled":
            button.configure(background=background, cursor="hand2" if hovered else "")

    def refresh(self):
        self.refresh_history(self.active_project)

    def refresh_history(self, active_project):
        self.active_project = Path(active_project) if active_project is not None else None
        selected = self.row_data.get(self.current_row())
        expanded = {project for project, row in self.project_rows.items() if self.tree.item(row, "open")}
        was_empty = not self.project_rows

        paths = discover_projects()
        if self.active_project is not None and self.active_project not in paths:
            if project_metadata(self.active_project) is not None:
                paths.append(self.active_project)
        projects = []
        for project in paths:
            try:
                activity = project_activity(project)
                latest = activity[0][0] if activity else datetime.fromtimestamp(project.stat().st_mtime)
            except OSError:
                continue
            projects.append((latest, project, activity))
        projects.sort(key=lambda group: (-group[0].timestamp(), str(group[1]).casefold()))

        self.tree.delete(*self.tree.get_children())
        self.project_rows.clear()
        self.row_data.clear()
        if not projects:
            self.empty.place(relx=0.5, rely=0.5, anchor="center")
            self.selection_changed()
            return
        self.empty.place_forget()

        selection = None
        for latest, project, activity in projects:
            active = project == self.active_project
            row = self.tree.insert(
                "", "end", text=project.name, tags=("project", "active") if active else ("project",),
                values=("현재 프로젝트" if active else "", self.format_date(latest)),
                open=project in expanded or (was_empty and active),
            )
            self.project_rows[project] = row
            self.row_data[row] = (project, project, None)
            if self.row_data[row] == selected:
                selection = row
            for timestamp, label, path in activity:
                path = Path(path) if path is not None else None
                missing = path is None or not self.path_exists(path)
                item = self.tree.insert(
                    row, "end", text=label,
                    values=("파일 없음" if missing else "", self.format_date(timestamp)),
                    tags=("missing",) if missing else (),
                )
                self.row_data[item] = (project, path, (timestamp, label))
                if self.row_data[item] == selected:
                    selection = item
        if selection is None:
            selection = self.project_rows.get(self.active_project) or next(iter(self.project_rows.values()))
            if was_empty:
                self.tree.item(selection, open=True)
        self.tree.selection_set(selection)
        self.tree.focus(selection)
        self.tree.see(selection)
        self.selection_changed()

    @staticmethod
    def path_exists(path):
        try:
            return path is not None and path.exists()
        except OSError:
            return False

    @staticmethod
    def format_date(value):
        if value.tzinfo is not None:
            value = value.astimezone()
        return value.strftime(DATE_FORMAT)

    def current_row(self):
        return next(iter(self.tree.selection()), None)

    def selection_changed(self, _event=None):
        project, path, _activity = self.row_data.get(self.current_row(), (None, None, None))
        preview = project / PREVIEW if project is not None else None
        for button, target in (
            (self.load_button, project), (self.open_button, path), (self.preview_button, preview)
        ):
            available = self.path_exists(target)
            emphasized = available and button is self.load_button
            button.configure(
                state="normal" if available else "disabled",
                background=self.colors["accent"] if emphasized else (
                    self.colors["white"] if available else self.colors["paper"]
                ),
                cursor="hand2" if available else "",
            )
        self.detail.configure(text=str(path or project) if project is not None else "")

    def load_selection(self):
        data = self.row_data.get(self.current_row())
        if data is None:
            return
        project = data[0]
        if project_metadata(project) is None:
            self.refresh()
            self.detail.configure(text="프로젝트 폴더를 찾을 수 없습니다.")
        elif self.load_project(project) is not False:
            self.refresh_history(project)
            self.root.lift()
            self.root.focus_set()

    def open_path(self, path):
        if self.path_exists(path):
            self.open_file(path)
        else:
            self.selection_changed()
            self.detail.configure(text="선택한 파일 또는 폴더를 찾을 수 없습니다.")

    def open_selection(self):
        data = self.row_data.get(self.current_row())
        if data is not None:
            self.open_path(data[1])

    def open_preview(self):
        data = self.row_data.get(self.current_row())
        if data is not None:
            self.open_path(data[0] / PREVIEW)

    def open_event(self, event):
        row = self.tree.identify_row(event.y) if event.type == tk.EventType.ButtonPress else self.current_row()
        if row is None or not self.tree.parent(row):
            return
        self.open_path(self.row_data[row][1])
        return "break"


def show_project_history(root, *, fonts, colors, active_project, load_project, open_file):
    return ProjectHistory(root, fonts, colors, active_project, load_project, open_file)
