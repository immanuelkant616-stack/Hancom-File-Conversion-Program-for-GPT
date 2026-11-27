"""Browse, save, and drag results from a TR batch."""

import tkinter as tk
from collections import Counter
from pathlib import Path
from tkinter import ttk


class ResultPanel(tk.Frame):
    def __init__(self, parent, title, empty_text, *, fonts, colors, open_file, drag_files,
                 save_file, save_bundle, drag_bundle, height=4):
        super().__init__(parent, background=colors["paper"], padx=9, pady=7,
                         highlightthickness=1, highlightbackground=colors["border"])
        self.fonts, self.colors = fonts, colors
        self.open_file, self.drag_files = open_file, drag_files
        self.save_file, self.save_bundle, self.drag_bundle = save_file, save_bundle, drag_bundle
        self.paths = ()
        self.rows = {}
        self.drag_start = None
        self.zip_start = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        header = tk.Frame(self, background=colors["paper"])
        header.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        header.columnconfigure(0, weight=1)
        tk.Label(header, text=title, font=fonts["label"], anchor="w", background=colors["paper"],
                 foreground=colors["text"], padx=0, pady=0).grid(row=0, column=0, sticky="ew")
        self.count = tk.Label(header, font=fonts["hint"], background=colors["paper"],
                              foreground=colors["muted"], padx=0, pady=0)
        self.count.grid(row=0, column=1, padx=(5, 0))

        body = tk.Frame(self, background=colors["white"])
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        style = ttk.Style(self)
        style.configure("TRResults.Treeview", background=colors["white"],
                        fieldbackground=colors["white"], foreground=colors["text"],
                        font=fonts["body"], rowheight=fonts["body"].metrics("linespace") + 6,
                        borderwidth=0, relief="flat")
        style.map("TRResults.Treeview", background=[("selected", colors["lavender"])],
                  foreground=[("selected", colors["accent_dark"])])
        self.tree = ttk.Treeview(body, show="tree", selectmode="extended",
                                 height=height, style="TRResults.Treeview")
        self.tree.column("#0", width=158, minwidth=0)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.empty = tk.Label(body, text=empty_text, background=colors["white"],
                              foreground=colors["muted"], font=fonts["hint"])
        body.bind("<Configure>", lambda event: self.empty.configure(wraplength=max(100, event.width - 24)))

        footer = tk.Frame(self, background=colors["paper"])
        footer.grid(row=2, column=0, sticky="ew", pady=(5, 0))
        footer.columnconfigure(1, weight=1)
        self.save_button = self.make_button(footer, "선택 저장", self.save_selected)
        self.save_button.grid(row=0, column=0, sticky="w")
        self.zip_button = self.make_button(footer, "전체 ZIP", self.save_all, primary=True)
        self.zip_button.grid(row=0, column=2, sticky="e", padx=(5, 0))
        self.zip_button.bind("<ButtonPress-1>", self.zip_press)
        self.zip_button.bind("<B1-Motion>", self.zip_motion)
        self.zip_button.bind("<ButtonRelease-1>", self.zip_release)

        self.tree.bind("<<TreeviewSelect>>", self.update_buttons)
        self.tree.bind("<ButtonPress-1>", self.row_press)
        self.tree.bind("<B1-Motion>", self.row_motion)
        self.tree.bind("<ButtonRelease-1>", self.row_release)
        self.tree.bind("<Double-1>", self.open_row)
        self.tree.bind("<Return>", self.open_row)
        self.tree.bind("<Control-a>", self.select_all)
        self.tree.bind("<Control-s>", self.save_selected)
        self.set_paths(())

    def make_button(self, parent, text, command, *, primary=False):
        c = self.colors
        background = c["accent"] if primary else c["white"]
        foreground = c["white"] if primary else c["text"]
        button = tk.Button(parent, text=text, command=command, font=self.fonts["hint"],
                           background=background, foreground=foreground, disabledforeground=c["muted"],
                           activebackground=c["accent_dark"] if primary else c["lavender"],
                           activeforeground=foreground, relief="flat", borderwidth=0, padx=7, pady=2,
                           highlightthickness=1, highlightbackground=c["border"],
                           highlightcolor=c["accent_dark"])
        button.bind("<Return>", lambda _event: button.invoke())
        return button

    def set_paths(self, paths):
        paths = tuple(dict.fromkeys(map(Path, paths)))
        if paths != self.paths:
            selected = set(self.selected_paths())
            focus = self.rows.get(self.tree.focus())
            top = self.tree.yview()[0]
            self.tree.delete(*self.tree.get_children())
            self.paths = paths
            self.rows.clear()
            names = Counter(path.name.casefold() for path in paths)
            for path in paths:
                label = path.name
                if path.suffix.lower() == ".md" or names[path.name.casefold()] > 1:
                    label += f"  ·  {path.parent.name}"
                row = self.tree.insert("", "end", text=label)
                self.rows[row] = path
                if path == focus:
                    self.tree.focus(row)
            rows = [row for row, path in self.rows.items() if path in selected]
            if not rows and self.rows:
                rows = [next(iter(self.rows))]
            if rows:
                self.tree.selection_set(rows)
                if not self.tree.focus():
                    self.tree.focus(rows[0])
                self.tree.yview_moveto(top)
        if self.paths:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.5, anchor="center")
        self.count.configure(text=f"{len(self.paths)}개")
        self.update_buttons()

    def selected_paths(self):
        selected = set(self.tree.selection())
        return tuple(path for row, path in self.rows.items() if row in selected)

    def update_buttons(self, _event=None):
        for button, enabled in ((self.save_button, self.tree.selection()), (self.zip_button, self.paths)):
            button.configure(state="normal" if enabled else "disabled", cursor="hand2" if enabled else "")

    def select_all(self, _event):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def open_row(self, event):
        row = self.tree.identify_row(event.y) if event.type == tk.EventType.ButtonPress else self.tree.focus()
        path = self.rows.get(row)
        if path is not None:
            self.open_file(path)
        return "break"

    def save_selected(self, _event=None):
        selected = self.selected_paths()
        if len(selected) == 1:
            self.save_file(selected[0])
        elif selected:
            self.save_bundle(selected)
        return "break"

    def save_all(self):
        if self.paths:
            self.save_bundle(self.paths)

    def row_press(self, event):
        row = self.tree.identify_row(event.y)
        path = self.rows.get(row)
        if path is None:
            self.drag_start = None
            return
        modified = event.state & (0x0001 | 0x0004)  # Shift or Ctrl selects rows.
        chosen = self.selected_paths() if row in self.tree.selection() and not modified else (path,)
        self.drag_start = event.x_root, event.y_root, chosen

    def row_motion(self, event):
        if self.drag_start is None:
            return "break"
        x, y, paths = self.drag_start
        if max(abs(event.x_root - x), abs(event.y_root - y)) >= 7:
            self.drag_start = None
            self.tree.selection_set([row for row, path in self.rows.items() if path in paths])
            self.drag_files(paths)
        return "break"

    def row_release(self, _event):
        self.drag_start = None

    def zip_press(self, event):
        if self.paths:
            self.zip_button.focus_set()
            self.zip_start = event.x_root, event.y_root
            self.zip_button.configure(relief="sunken")
        return "break"

    def zip_motion(self, event):
        if self.zip_start is not None:
            x, y = self.zip_start
            if max(abs(event.x_root - x), abs(event.y_root - y)) >= 7:
                self.zip_start = None
                self.zip_button.configure(relief="flat")
                if self.paths:
                    self.drag_bundle(self.paths)
        return "break"

    def zip_release(self, event):
        pressed = self.zip_start is not None
        self.zip_start = None
        self.zip_button.configure(relief="flat")
        if pressed and 0 <= event.x < self.zip_button.winfo_width() and 0 <= event.y < self.zip_button.winfo_height():
            self.save_all()
        return "break"
