"""RAR Studio: a single-file macOS Apple silicon GUI.

Install: python3 -m pip install customtkinter tkinterdnd2
RAR is downloaded from RARLAB on request and kept in Application Support.
Use the supplied .icns directly in PyInstaller without modifying it.
"""
from __future__ import annotations

import os
import platform
import queue
import re
import shutil
import subprocess
import tarfile
import threading
import urllib.request
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox
from urllib.parse import urlparse

try:
    import customtkinter as ctk
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError as exc:
    raise SystemExit("Install dependencies: python3 -m pip install customtkinter tkinterdnd2") from exc

BG = "#F4F5F9"
CARD = "#FFFFFF"
INK = "#202438"
MUTED = "#697187"
LINE = "#E6E8F0"
ACCENT = "#6754DE"
HOVER = "#5643CD"
TINT = "#F0EDFF"
GREEN = "#278364"
RAR_DOWNLOAD_URL = "https://www.rarlab.com/rar/rarmacos-arm-723.tar.gz"
COMPRESSION = ["0 · Store", "1 · Fastest", "2 · Fast", "3 · Normal", "4 · Good", "5 · Best"]
COMPRESSION_HINTS = [
    "Keep files uncompressed. Fastest to create.",
    "Prioritize speed with light compression.",
    "Compress quickly with a modest file size.",
    "A balance of speed and archive size.",
    "More compression with a longer creation time.",
    "Smallest archive. Takes more time to create.",
]


class StudioWindow(ctk.CTk, TkinterDnD.DnDWrapper):
    """Rounded CustomTkinter controls plus native Finder drag-and-drop."""
    def __init__(self):
        super().__init__()
        self.TkdndVersion = TkinterDnD._require(self)


class RarDropApp:
    def __init__(self, root):
        self.root = root
        root.title("RAR Studio")
        root.configure(fg_color=BG)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.ui_scale = min(1.18, max(1.0, sw / 1700))
        root.geometry(f"{min(round(1120 * self.ui_scale), sw - 64)}x{min(round(790 * self.ui_scale), sh - 96)}")
        root.minsize(min(440, sw - 48), min(520, sh - 72))
        self.paths = []
        self.rar_path = ctk.StringVar(value=self.find_rar())
        self.archive_name = ctk.StringVar(value="Archive")
        self.split_size = ctk.StringVar(value="0")
        self.split_unit = ctk.StringVar(value="MB")
        self.compression = ctk.IntVar(value=5)
        self.compression_choice = ctk.StringVar(value=COMPRESSION[5])
        self.status = ctk.StringVar(value="Add files to get started.")
        self.events = queue.Queue()
        self._busy = False
        self._alive = True
        self._layout_mode = None
        self._resize_job = None
        self._resume_after_download = False
        self._file_rows = []
        self._controls = []
        self._build()
        self.refresh()
        self._update_engine()
        root.bind("<Configure>", self.on_resize, add="+")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(60, self._poll_events)
        root.after(100, self._reflow)

    def font(self, size=15, weight="normal"):
        return ctk.CTkFont(family="Helvetica Neue", size=round(size * self.ui_scale), weight=weight)

    def label(self, parent, text, size=15, color=INK, weight="normal", **kwargs):
        return ctk.CTkLabel(parent, text=text, text_color=color, font=self.font(size, weight), **kwargs)

    def button(self, parent, text, command, primary=False, width=110, **kwargs):
        options = dict(text=text, command=command, width=width, height=40, corner_radius=11,
                       font=self.font(14, "bold"), fg_color=ACCENT if primary else TINT,
                       hover_color=HOVER if primary else "#E5DFFF",
                       text_color="white" if primary else ACCENT,
                       text_color_disabled="#AAA7BC", cursor="hand2")
        options.update(kwargs)
        widget = ctk.CTkButton(parent, **options)
        self._controls.append(widget)
        return widget

    def entry(self, parent, variable, **kwargs):
        widget = ctk.CTkEntry(parent, textvariable=variable, height=44, corner_radius=10,
                             border_width=1, border_color=LINE, fg_color="#FAFBFD",
                             text_color=INK, font=self.font(15), **kwargs)
        self._controls.append(widget)
        return widget

    def _build(self):
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=1)
        header = ctk.CTkFrame(self.root, fg_color=BG, corner_radius=0)
        header.grid(row=0, column=0, sticky="ew", padx=26, pady=(22, 16))
        header.grid_columnconfigure(1, weight=1)
        brand = ctk.CTkFrame(header, fg_color=ACCENT, width=52, height=52, corner_radius=16)
        brand.grid(row=0, column=0, rowspan=2, padx=(0, 14))
        brand.grid_propagate(False)
        self.label(brand, "RAR", size=14, weight="bold", color="white").place(relx=.5, rely=.5, anchor="center")
        self.label(header, "RAR Studio", size=29, weight="bold", anchor="w").grid(row=0, column=1, sticky="w")
        self.label(header, "A little space for your files.", size=14, color=MUTED, anchor="w").grid(row=1, column=1, sticky="w")
        self.header_badge = self.label(header, "Apple silicon", size=13, color=ACCENT,
                                       fg_color=TINT, corner_radius=14, width=118, height=30)
        self.header_badge.grid(row=0, column=2, rowspan=2, padx=(12, 0))

        # The body scrolls on short displays; the create button stays visible.
        self.content = ctk.CTkScrollableFrame(self.root, fg_color=BG, corner_radius=0,
                         scrollbar_fg_color=BG, scrollbar_button_color="#D3D7E3",
                         scrollbar_button_hover_color="#BDC4D5")
        self.content.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 12))
        self.content.grid_columnconfigure(0, weight=3)
        self.content.grid_columnconfigure(1, weight=2)
        self.files_card = ctk.CTkFrame(self.content, fg_color=CARD, border_width=1,
                                      border_color=LINE, corner_radius=20, width=1)
        self.files_card.grid(row=0, column=0, sticky="nsew", padx=(2, 10), pady=2)
        self.files_card.grid_columnconfigure(0, weight=1)
        heading = ctk.CTkFrame(self.files_card, fg_color="transparent")
        heading.grid(row=0, column=0, sticky="ew", padx=22, pady=(20, 12))
        heading.grid_columnconfigure(0, weight=1)
        self.label(heading, "Your files", size=19, weight="bold", anchor="w").grid(row=0, column=0, sticky="w")
        self.count_label = self.label(heading, "0 items", size=13, color=MUTED)
        self.count_label.grid(row=0, column=1, padx=(8, 0))
        self.drop = ctk.CTkFrame(self.files_card, fg_color="#F7F5FF", border_width=1,
                                border_color="#DCD5FA", corner_radius=16)
        self.drop.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 16))
        self.drop.grid_columnconfigure(0, weight=1)
        self.label(self.drop, "↓", size=34, color=ACCENT, height=38).grid(row=0, column=0, pady=(16, 0))
        self.drop_title = self.label(self.drop, "Drop files or folders here", size=18, weight="bold")
        self.drop_title.grid(row=1, column=0, padx=12, pady=(2, 0))
        self.label(self.drop, "Everything you add goes into one archive.", size=13, color=MUTED,
                   wraplength=310).grid(row=2, column=0, padx=12, pady=(3, 14))
        browse_row = ctk.CTkFrame(self.drop, fg_color="transparent")
        browse_row.grid(row=3, column=0, pady=(0, 18))
        self.button(browse_row, "Add files", self.choose_files, width=106).pack(side="left", padx=(0, 6))
        self.button(browse_row, "Add folder", self.choose_folder, width=106, fg_color=CARD,
                    border_width=1, border_color="#DCD5FA").pack(side="left", padx=(6, 0))
        for widget in (self.drop, self.root):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self.on_drop)
            widget.dnd_bind("<<DropEnter>>", self.on_drag_enter)
            widget.dnd_bind("<<DropLeave>>", self.on_drag_leave)
        self.files_scroll = ctk.CTkScrollableFrame(self.files_card, height=250, fg_color=CARD,
                              corner_radius=0, scrollbar_button_color="#E1E4EC",
                              scrollbar_button_hover_color="#CBD1DF")
        self.files_scroll.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 6))
        self.files_scroll.grid_columnconfigure(0, weight=1)
        self.empty_label = self.label(self.files_scroll, "No files added yet.\nDrop a file above to begin.",
                                     size=14, color=MUTED, justify="center", height=180)
        self.empty_label.grid(row=0, column=0, sticky="ew", pady=12)
        list_footer = ctk.CTkFrame(self.files_card, fg_color="transparent")
        list_footer.grid(row=3, column=0, sticky="ew", padx=20, pady=(6, 16))
        self.list_summary = self.label(list_footer, "Files stay on your Mac.", size=13, color=MUTED, anchor="w")
        self.list_summary.pack(side="left", fill="x", expand=True)
        self.button(list_footer, "Clear", self.clear, width=66, height=32, fg_color="transparent",
                    hover_color=BG, font=self.font(13)).pack(side="right")

        self.settings_card = ctk.CTkFrame(self.content, fg_color=CARD, border_width=1,
                                         border_color=LINE, corner_radius=20, width=1)
        self.settings_card.grid(row=0, column=1, sticky="nsew", padx=(10, 2), pady=2)
        self.settings_card.grid_columnconfigure(0, weight=1)
        self.label(self.settings_card, "Make it yours", size=19, weight="bold", anchor="w").grid(
            row=0, column=0, sticky="ew", padx=22, pady=(20, 0))
        self.label(self.settings_card, "Choose how your archive is packed.", size=13,
                   color=MUTED, anchor="w").grid(row=1, column=0, sticky="ew", padx=22, pady=(3, 18))
        self.label(self.settings_card, "Archive name", size=14, weight="bold", anchor="w").grid(
            row=2, column=0, sticky="w", padx=22)
        name_row = ctk.CTkFrame(self.settings_card, fg_color="transparent")
        name_row.grid(row=3, column=0, sticky="ew", padx=22, pady=(7, 20))
        name_row.grid_columnconfigure(0, weight=1)
        self.entry(name_row, self.archive_name, width=1).grid(row=0, column=0, sticky="ew")
        self.label(name_row, ".rar", size=14, color=MUTED, width=38).grid(row=0, column=1, padx=(8, 0))
        self.label(self.settings_card, "Compression", size=14, weight="bold", anchor="w").grid(
            row=4, column=0, sticky="w", padx=22)
        self.compression_menu = ctk.CTkOptionMenu(self.settings_card, values=COMPRESSION,
            variable=self.compression_choice, command=self.update_compression, height=44,
            corner_radius=10, fg_color=TINT, button_color=TINT, button_hover_color="#E3DCFF",
            text_color=INK, dropdown_fg_color=CARD, dropdown_text_color=INK,
            dropdown_hover_color=TINT, font=self.font(15), dropdown_font=self.font(14))
        self._controls.append(self.compression_menu)
        self.compression_menu.grid(row=5, column=0, sticky="ew", padx=22, pady=(7, 6))
        self.comp_hint = self.label(self.settings_card, COMPRESSION_HINTS[5], size=13,
                                    color=MUTED, anchor="w", justify="left", wraplength=280)
        self.comp_hint.grid(row=6, column=0, sticky="ew", padx=22, pady=(0, 20))
        self.label(self.settings_card, "Split into parts", size=14, weight="bold", anchor="w").grid(
            row=7, column=0, sticky="w", padx=22)
        split_row = ctk.CTkFrame(self.settings_card, fg_color="transparent")
        split_row.grid(row=8, column=0, sticky="ew", padx=22, pady=(7, 6))
        split_row.grid_columnconfigure(0, weight=1)
        self.entry(split_row, self.split_size, width=1).grid(row=0, column=0, sticky="ew")
        self.split_unit_menu = ctk.CTkOptionMenu(
            split_row, values=["KB", "MB"], variable=self.split_unit,
            width=78, height=44, corner_radius=10, fg_color=TINT,
            button_color=TINT, button_hover_color="#E3DCFF", text_color=INK,
            dropdown_fg_color=CARD, dropdown_text_color=INK,
            dropdown_hover_color=TINT, font=self.font(14), dropdown_font=self.font(14),
        )
        self._controls.append(self.split_unit_menu)
        self.split_unit_menu.grid(row=0, column=1, padx=(8, 0))
        self.label(self.settings_card, "0 creates a single archive.", size=13, color=MUTED,
                   anchor="w").grid(row=9, column=0, sticky="ew", padx=22, pady=(0, 22))
        ctk.CTkFrame(self.settings_card, fg_color=LINE, height=1, corner_radius=0).grid(
            row=10, column=0, sticky="ew", padx=22)
        self.engine_label = self.label(self.settings_card, "○  RAR setup required", size=13,
                                       color=MUTED, anchor="w", wraplength=280)
        self.engine_label.grid(row=11, column=0, sticky="ew", padx=22, pady=(16, 4))
        self.engine_hint = self.label(self.settings_card, "Download once. Ready for next time.", size=13,
                                      color=MUTED, anchor="w", wraplength=280, justify="left")
        self.engine_hint.grid(row=12, column=0, sticky="ew", padx=22, pady=(0, 12))
        engine_buttons = ctk.CTkFrame(self.settings_card, fg_color="transparent")
        engine_buttons.grid(row=13, column=0, sticky="ew", padx=22, pady=(0, 20))
        engine_buttons.grid_columnconfigure((0, 1), weight=1)
        self.download_button = self.button(engine_buttons, "Download RAR", self.download_rar, width=1,
                                            height=36, font=self.font(13, "bold"))
        self.download_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.button(engine_buttons, "Choose…", self.choose_rar, width=1, height=36,
                    fg_color=BG, text_color=MUTED, hover_color=LINE,
                    font=self.font(13)).grid(row=0, column=1, sticky="ew", padx=(5, 0))

        self.footer = ctk.CTkFrame(self.root, fg_color=CARD, corner_radius=18,
                                  border_width=1, border_color=LINE)
        self.footer.grid(row=2, column=0, sticky="ew", padx=26, pady=(0, 22))
        self.footer.grid_columnconfigure(0, weight=1)
        self.status_box = ctk.CTkFrame(self.footer, fg_color="transparent")
        self.status_box.grid(row=0, column=0, sticky="ew", padx=20, pady=16)
        self.status_box.grid_columnconfigure(0, weight=1)
        self.status_label = self.label(self.status_box, "", size=14, color=MUTED, anchor="w",
                                       textvariable=self.status, wraplength=400, justify="left")
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.progress_label = self.label(self.status_box, "", size=13, color=ACCENT, weight="bold", width=45)
        self.progress_label.grid(row=0, column=1, padx=(8, 0))
        self.progress = ctk.CTkProgressBar(self.status_box, height=6, corner_radius=3,
                                          fg_color=LINE, progress_color=ACCENT, mode="determinate")
        self.progress.set(0)
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.progress.grid_remove()
        self.create_button = self.button(self.footer, "Create archive  →", self.create_archive, primary=True,
                                          width=200, height=48, corner_radius=13, font=self.font(15, "bold"))
        self.create_button.grid(row=0, column=1, padx=(12, 20), pady=16)

    def on_resize(self, event):
        if event.widget is self.root:
            if self._resize_job:
                self.root.after_cancel(self._resize_job)
            self._resize_job = self.root.after(80, self._reflow)

    def _reflow(self):
        self._resize_job = None
        width = self.root.winfo_width() / self.root._get_window_scaling()
        mode = "wide" if width >= 900 else "narrow"
        if mode != self._layout_mode:
            self._layout_mode = mode
            if mode == "wide":
                self.content.grid_columnconfigure(0, weight=3)
                self.content.grid_columnconfigure(1, weight=2)
                self.files_card.grid(row=0, column=0, sticky="nsew", padx=(2, 10), pady=2)
                self.settings_card.grid(row=0, column=1, sticky="nsew", padx=(10, 2), pady=2)
            else:
                self.content.grid_columnconfigure(0, weight=1)
                self.content.grid_columnconfigure(1, weight=0)
                self.files_card.grid(row=0, column=0, sticky="ew", padx=2, pady=(2, 12))
                self.settings_card.grid(row=1, column=0, sticky="ew", padx=2, pady=(0, 2))
        if width < 600:
            self.header_badge.grid_remove()
            self.status_box.grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 0))
            self.create_button.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18, pady=(12, 16))
        else:
            self.header_badge.grid()
            self.status_box.grid(row=0, column=0, columnspan=1, sticky="ew", padx=20, pady=16)
            self.create_button.grid(row=0, column=1, columnspan=1, sticky="", padx=(12, 20), pady=16)
        self.root.update_idletasks()
        settings_width = max(200, self.settings_card.winfo_width() - 48)
        for label in (self.comp_hint, self.engine_hint, self.engine_label):
            label.configure(wraplength=settings_width)
        self.status_label.configure(wraplength=max(170, self.status_box.winfo_width() - 60))
        self._fit_file_labels()

    @staticmethod
    def elide(text, font, width):
        if font.measure(text) <= width:
            return text
        while text and font.measure(text + "…") > width:
            text = text[:-1]
        return text + "…"

    def _fit_file_labels(self):
        width = max(140, self.files_scroll.winfo_width() - 115)
        for name_label, path_label, path in self._file_rows:
            name_label.configure(text=self.elide(path.name, name_label.cget("font"), width))
            path_label.configure(text=self.elide(str(path.parent), path_label.cget("font"), width))

    def refresh(self):
        for name_label, _, _ in self._file_rows:
            name_label.master.destroy()
        self._file_rows.clear()
        count = len(self.paths)
        self.count_label.configure(text=f"{count} item" + ("s" if count != 1 else ""))
        if count:
            self.empty_label.grid_remove()
        else:
            self.empty_label.grid()
        total = 0
        for index, path in enumerate(self.paths):
            row = ctk.CTkFrame(self.files_scroll, fg_color="#FAFBFD", corner_radius=12, width=1)
            row.grid(row=index, column=0, sticky="ew", pady=(0, 6))
            row.grid_columnconfigure(1, weight=1)
            self.label(row, "DIR" if path.is_dir() else "FILE", size=10, color=ACCENT, weight="bold",
                       fg_color=TINT, width=42, height=38, corner_radius=9).grid(
                           row=0, column=0, rowspan=2, padx=(10, 12), pady=10)
            name_label = self.label(row, path.name, size=14, weight="bold", anchor="w", width=1)
            name_label.grid(row=0, column=1, sticky="ew", pady=(9, 0))
            path_label = self.label(row, str(path.parent), size=12, color=MUTED, anchor="w", width=1)
            path_label.grid(row=1, column=1, sticky="ew", pady=(0, 9))
            remove = ctk.CTkButton(row, text="×", width=28, height=28, corner_radius=8,
                fg_color="transparent", hover_color=LINE, text_color=MUTED, font=self.font(19),
                command=lambda p=path: self.remove_path(p))
            remove.grid(row=0, column=2, rowspan=2, padx=(5, 9))
            self._file_rows.append((name_label, path_label, path))
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                pass
        summary = (f"{self.format_size(total)} in selected files" if total else "Folders include their contents.") if count else "Files stay on your Mac."
        self.list_summary.configure(text=summary)
        if not self._busy:
            self.create_button.configure(state="normal" if count else "disabled")
            self.status.set("Choose your settings, then create." if count else "Add files to get started.")
        self.root.after_idle(self._fit_file_labels)

    @staticmethod
    def format_size(size):
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
            size /= 1024

    def add_paths(self, values):
        if self._busy:
            return
        for value in values:
            path = Path(value).expanduser().absolute()
            if path.exists() and path not in self.paths:
                self.paths.append(path)
        self.refresh()

    def on_drag_enter(self, event):
        if not self._busy:
            self.drop.configure(fg_color="#EAE4FF", border_color=ACCENT)
        return event.action

    def on_drag_leave(self, _event):
        self.drop.configure(fg_color="#F7F5FF", border_color="#DCD5FA")

    def on_drop(self, event):
        self.on_drag_leave(event)
        self.add_paths(self.root.tk.splitlist(event.data))
        return event.action

    def choose_files(self):
        self.add_paths(filedialog.askopenfilenames(parent=self.root, title="Add files"))

    def choose_folder(self):
        path = filedialog.askdirectory(parent=self.root, title="Add a folder")
        if path:
            self.add_paths([path])

    def clear(self):
        if not self._busy:
            self.paths.clear()
            self.refresh()

    def remove_path(self, path):
        if not self._busy and path in self.paths:
            self.paths.remove(path)
            self.refresh()

    def update_compression(self, value):
        level = int(value.split(" · ", 1)[0])
        self.compression.set(level)
        self.comp_hint.configure(text=COMPRESSION_HINTS[level])

    @staticmethod
    def app_support_dir():
        return Path.home() / "Library" / "Application Support" / "RAR Studio"

    @classmethod
    def find_rar(cls):
        candidates = [str(cls.app_support_dir() / "rar"), shutil.which("rar"),
                      "/opt/homebrew/bin/rar", "/usr/local/bin/rar"]
        return next((p for p in candidates if p and Path(p).is_file() and os.access(p, os.X_OK)), "")

    def _update_engine(self):
        path = Path(self.rar_path.get()).expanduser()
        ready = path.is_file() and os.access(path, os.X_OK)
        self.engine_label.configure(text="●  RAR is ready" if ready else "○  RAR setup required",
                                    text_color=GREEN if ready else MUTED)
        self.engine_hint.configure(text="Your engine is available on this Mac." if ready else "Download once. Ready for next time.")

    def choose_rar(self):
        path = filedialog.askopenfilename(parent=self.root, title="Choose the rar executable")
        if path:
            self.rar_path.set(path)
            self._update_engine()

    def _check_platform(self):
        if platform.system() == "Darwin" and platform.machine() == "arm64":
            return True
        messagebox.showerror("Unsupported device", "Use RAR Studio on a Mac with Apple silicon.", parent=self.root)
        return False

    def _set_busy(self, busy):
        self._busy = busy
        for control in self._controls:
            control.configure(state="disabled" if busy else "normal")
        if not busy:
            self.create_button.configure(state="normal" if self.paths else "disabled", text="Create archive  →")

    def _begin_progress(self, status):
        self.status.set(status)
        self.progress_label.configure(text="", text_color=ACCENT)
        self.progress.set(0)
        self.progress.configure(mode="indeterminate")
        self.progress.grid()
        self.progress.start()

    def _emit(self, method, *args):
        self.events.put((method, args))

    def _poll_events(self):
        if not self._alive:
            return
        for _ in range(100):
            try:
                method, args = self.events.get_nowait()
            except queue.Empty:
                break
            getattr(self, method)(*args)
        if self._alive:
            self.root.after(60, self._poll_events)

    def download_rar(self):
        if self._busy or not self._check_platform():
            return
        if not messagebox.askyesno("Download RAR?",
            "Download the official macOS ARM package from RARLAB?\n\n"
            "It is saved on this Mac for future launches. RAR has a 40-day trial; "
            "its license terms apply to use.", parent=self.root):
            self._resume_after_download = False
            return
        self._set_busy(True)
        self.download_button.configure(text="Downloading…")
        self._begin_progress("Downloading RAR from RARLAB…")
        threading.Thread(target=self._download_rar_worker, daemon=True).start()

    def _download_rar_worker(self):
        try:
            support = self.app_support_dir()
            support.mkdir(parents=True, exist_ok=True)
            package = support / "rar-macos-arm.tar.gz"
            request = urllib.request.Request(RAR_DOWNLOAD_URL, headers={"User-Agent": "RAR-Studio/1.0"})
            with urllib.request.urlopen(request, timeout=60) as response, package.open("wb") as output:
                if urlparse(response.geturl()).hostname not in {"www.rarlab.com", "rarlab.com"}:
                    raise RuntimeError("RAR was not returned by the official RARLAB website.")
                shutil.copyfileobj(response, output)
            with tarfile.open(package, "r:gz") as archive:
                member = next((m for m in archive.getmembers() if Path(m.name).name == "rar" and m.isfile()), None)
                if member is None:
                    raise RuntimeError("The package does not contain the rar executable.")
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError("The rar executable could not be read.")
                temporary = support / "rar.download"
                with source, temporary.open("wb") as output:
                    shutil.copyfileobj(source, output)
                temporary.chmod(0o755)
                installed = support / "rar"
                temporary.replace(installed)
            self._emit("_download_finished", str(installed), None)
        except Exception as exc:
            self._emit("_download_finished", None, str(exc))

    def _download_finished(self, installed, error):
        self.progress.stop()
        self.progress.grid_remove()
        self._set_busy(False)
        self.download_button.configure(text="Download RAR")
        if error:
            self._resume_after_download = False
            self.status.set("RAR could not be downloaded.")
            messagebox.showerror("Download failed", error, parent=self.root)
            return
        self.rar_path.set(installed)
        self._update_engine()
        self.status.set("RAR is ready. You can create an archive.")
        if self._resume_after_download:
            self._resume_after_download = False
            self.root.after_idle(self.create_archive)

    def create_archive(self):
        if self._busy or not self._check_platform() or not self.paths:
            return
        rar = Path(self.rar_path.get()).expanduser()
        if not rar.is_file() or not os.access(rar, os.X_OK):
            self._resume_after_download = True
            self.download_rar()
            return
        name = self.archive_name.get().strip()
        if name.lower().endswith(".rar"):
            name = name[:-4]
        if not name or "/" in name or name in {".", ".."}:
            messagebox.showerror("Archive name", "Enter a file name for your archive.", parent=self.root)
            return
        try:
            split = int(self.split_size.get().strip() or "0")
            if split < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Split size", "Use 0 for one archive, or a positive whole number of KB or MB.", parent=self.root)
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title="Save your archive",
            initialfile=f"{name}.rar", defaultextension=".rar", filetypes=[("RAR archive", "*.rar")])
        if not destination:
            return
        args = [str(rar), "a", f"-m{self.compression.get()}", "-ep1"]
        if split:
            suffix = {"KB": "k", "MB": "m"}[self.split_unit.get()]
            args.append(f"-v{split}{suffix}")
        args.append(destination)
        args.extend(str(path) for path in self.paths)
        self._set_busy(True)
        self.create_button.configure(text="Creating archive…")
        self._begin_progress("Preparing your archive…")
        threading.Thread(target=self.run_rar, args=(args,), daemon=True).start()

    def run_rar(self, args):
        captured = deque(maxlen=12000)
        try:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
            segment = ""
            last_percent = None
            if process.stdout:
                with process.stdout:
                    while True:
                        char = process.stdout.read(1)
                        if not char:
                            break
                        captured.append(char)
                        segment = (segment + char)[-500:]
                        if char == "%":
                            match = re.search(r"(\d{1,3})%$", segment)
                            if match:
                                value = min(100, int(match.group(1)))
                                if value != last_percent:
                                    self._emit("update_progress", value)
                                    last_percent = value
                        if char in "\r\n":
                            segment = ""
            self._emit("finished", process.wait(), "".join(captured).strip())
        except Exception as exc:
            self._emit("finished", 1, str(exc))

    def update_progress(self, value):
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(value / 100)
        self.progress_label.configure(text=f"{value}%")
        self.status.set("Creating your archive…")

    def finished(self, code, output):
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self._set_busy(False)
        if code == 0:
            self.progress.set(1)
            self.progress_label.configure(text="100%", text_color=GREEN)
            self.status.set("Your archive is ready.")
            messagebox.showinfo("Archive created", "Your RAR archive was created successfully.", parent=self.root)
        else:
            self.progress.grid_remove()
            self.progress_label.configure(text="")
            self.status.set("Archive creation failed.")
            messagebox.showerror("Could not create archive", output[-2500:] or "RAR reported an error.", parent=self.root)

    def close(self):
        if self._busy:
            messagebox.showinfo("Operation in progress", "Wait for the current operation to finish before closing.", parent=self.root)
            return
        self._alive = False
        self.root.destroy()


def main():
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")
    root = StudioWindow()
    RarDropApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
