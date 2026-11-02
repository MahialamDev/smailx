#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ui_components.py - reusable widgets for SmaliX Desktop.

    apply_styles      ttk dark theme
    CodeText          tk.Text that emits <<Change>>
    LineNumbers       line-number gutter
    CodePane          titled panel with a (highlighted) code editor
    Header / Toolbar / StatusBar
    ProjectTree       real project folders + merged Classes view (lazy loaded)
    ResultsTree       double-clickable result list (Calls / Called by / Search)
    BottomTabs        Explain, Patch Assistant, Calls, Called by, Graph, Search, Console
    ToolsDialog       tool path editor

The widgets contain no business logic: they report user actions through
callbacks supplied by main.py.
"""

from __future__ import annotations

import re
import tkinter as tk
from bisect import bisect_right
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Callable, Optional

import customtkinter as ctk

from config import C, MONO, SMALL, UI_BOLD, UI_FONT, Tools
from smali_utils import clear_syntax, configure_tags, highlight, method_label, read_text


# --------------------------------------------------------------------------- #
#  Styles
# --------------------------------------------------------------------------- #
def apply_styles(root: tk.Misc) -> None:
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure("Smalix.Treeview", background=C["panel"], fieldbackground=C["panel"],
                 foreground=C["fg"], borderwidth=0, rowheight=24, font=UI_FONT)
    st.map("Smalix.Treeview", background=[("selected", C["sel"])],
           foreground=[("selected", "#ffffff")])
    st.layout("Smalix.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    st.configure("Smalix.TNotebook", background=C["bg"], borderwidth=0, tabmargins=(2, 4, 2, 0))
    st.configure("Smalix.TNotebook.Tab", background=C["dark"], foreground=C["fg"],
                 padding=(14, 4), borderwidth=1, font=(UI_FONT[0], 9, "bold"))
    st.map("Smalix.TNotebook.Tab",
           background=[("selected", C["panel"]), ("active", "#16202f")],
           foreground=[("selected", C["accent"])])


def _scrollbar(parent, command, orientation="vertical", bg=None):
    bg = bg or C["panel"]
    kw = dict(width=12) if orientation == "vertical" else dict(height=12)
    return ctk.CTkScrollbar(parent, command=command, orientation=orientation, fg_color=bg,
                            button_color="#243247", button_hover_color="#35496a", **kw)


# --------------------------------------------------------------------------- #
#  Code editor widgets
# --------------------------------------------------------------------------- #
class CodeText(tk.Text):
    """tk.Text that emits <<Change>> whenever its content, cursor or view changes."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._orig = self._w + "_orig"
        self.tk.call("rename", self._w, self._orig)
        self.tk.createcommand(self._w, self._proxy)

    def _proxy(self, *args):
        result = self.tk.call((self._orig,) + args)
        if args and (args[0] in ("insert", "replace", "delete")
                     or args[0:3] == ("mark", "set", "insert")
                     or args[0:2] in (("xview", "moveto"), ("xview", "scroll"),
                                      ("yview", "moveto"), ("yview", "scroll"))):
            try:
                self.event_generate("<<Change>>", when="tail")
            except tk.TclError:
                pass
        return result


class LineNumbers(tk.Canvas):
    """Line-number gutter that follows a CodeText widget."""

    def __init__(self, parent, text: tk.Text) -> None:
        super().__init__(parent, width=44, bg=C["gutter"], highlightthickness=0, bd=0)
        self.text = text
        self._pending = False
        text.bind("<<Change>>", self._schedule, add="+")
        text.bind("<Configure>", self._schedule, add="+")
        self.after(50, self._schedule)

    def _schedule(self, _e=None) -> None:
        if not self._pending:
            self._pending = True
            self.after_idle(self.redraw)

    def redraw(self) -> None:
        self._pending = False
        try:
            self.delete("all")
            total = int(self.text.index("end-1c").split(".")[0])
            w = max(42, len(str(total)) * 9 + 20)
            if int(self.cget("width")) != w:
                self.configure(width=w)
            cur = int(self.text.index("insert").split(".")[0])
            idx = self.text.index("@0,0")
            last = -1
            while True:
                info = self.text.dlineinfo(idx)
                if info is None:
                    break
                ln = int(str(idx).split(".")[0])
                if ln == last:
                    break
                last = ln
                self.create_text(w - 8, info[1] + 1, anchor="ne", text=str(ln), font=MONO,
                                 fill=C["accent"] if ln == cur else "#4b5a70")
                idx = self.text.index(f"{idx}+1line")
            self.create_line(w - 1, 0, w - 1, 4000, fill=C["border"])
        except tk.TclError:
            pass


def reveal_line_center(text: tk.Text, line: int, index: Optional[str] = None) -> None:
    """Scroll `text` so that `line` sits in the middle of the view (like revealLineInCenter)."""
    total = max(1, int(text.index("end-1c").split(".")[0]))
    text.yview_moveto(max(0.0, (line - 1) / total))
    try:
        text.update_idletasks()
        info = text.dlineinfo(f"{line}.0")
        visible = max(1, text.winfo_height() // max(1, info[3] if info else 16))
        text.yview_moveto(max(0.0, (line - 1 - visible // 2) / total))
        text.see(index or f"{line}.0")          # also brings the match into view horizontally
    except tk.TclError:
        pass


class FindBar(tk.Frame):
    """
    Compact Ctrl+F bar attached to a CodeText.

      * live search while typing (plain text or regex, optional case sensitivity)
      * "N of M" counter, up/down buttons, Enter / Shift+Enter, Up / Down keys, Esc to close
      * every match gets a light background, the active match a stronger one
      * jumping to a match centres its line and blinks that line for ~1.6 s
      * `matches` holds (start, end, line) tuples and `matching_lines` the sorted line numbers

    It only reads the text and adds tags, so it also works on the read-only Java view.
    """
    MAX_MATCHES = 5000
    BLINK_FLASHES = 4
    BLINK_INTERVAL = 200          # ms  -> 4 flashes * 2 * 200 ms = 1.6 s

    def __init__(self, parent, text: tk.Text, anchor: tk.Widget) -> None:
        super().__init__(parent, bg=C["panel"])
        self.text, self.anchor = text, anchor
        self.visible = False
        self.matches: list = []
        self.matching_lines: list = []
        self.current = -1
        self._capped = False
        self._sig = None
        self._job = self._blink_job = self._refresh_job = None

        # priority: findmatch < findcur < findblink (all above the current-line tag)
        text.tag_configure("findmatch", background="#3d3a1e")
        text.tag_configure("findcur", background="#b7791f")
        text.tag_configure("findblink", background="#2563eb")
        text.tag_raise("sel")

        self.query = tk.StringVar()
        self.case_var = tk.BooleanVar(value=False)
        self.regex_var = tk.BooleanVar(value=False)

        row = tk.Frame(self, bg=C["panel"])
        row.pack(fill="x", padx=8, pady=4)
        self.entry = ctk.CTkEntry(row, textvariable=self.query, width=230, height=26, font=UI_FONT,
                                  placeholder_text="Find", fg_color="#0b0e15", border_color=C["border"])
        self.entry.pack(side="left")
        self.counter = tk.Label(row, text="0 of 0", width=12, fg=C["dim"], bg=C["panel"], font=UI_FONT)
        self.counter.pack(side="left", padx=(8, 4))
        for label, cmd in (("\u25b2", self.prev), ("\u25bc", self.next)):
            ctk.CTkButton(row, text=label, width=28, height=26, corner_radius=4, font=(UI_FONT[0], 9),
                          fg_color="#0b0e15", hover_color="#1b2230", border_width=1,
                          border_color=C["border"], text_color=C["fg"], command=cmd).pack(side="left", padx=(0, 4))
        self._toggle(row, "Aa", self.case_var)
        self._toggle(row, ".*", self.regex_var)
        ctk.CTkButton(row, text="\u2715", width=28, height=26, corner_radius=4, font=(UI_FONT[0], 9),
                      fg_color="#0b0e15", hover_color="#3b1d1d", text_color=C["fg"],
                      command=self.close).pack(side="right")

        self.query.trace_add("write", lambda *_: self._schedule())
        self.entry.bind("<Return>", lambda e: (self.next(), "break")[1])
        self.entry.bind("<Shift-Return>", lambda e: (self.prev(), "break")[1])
        self.entry.bind("<Down>", lambda e: (self.next(), "break")[1])
        self.entry.bind("<Up>", lambda e: (self.prev(), "break")[1])
        self.entry.bind("<Escape>", lambda e: (self.close(), "break")[1])

    # -- construction helpers ------------------------------------------------
    def _toggle(self, row, label: str, var: tk.BooleanVar) -> None:
        def paint() -> None:
            on = var.get()
            btn.configure(fg_color=C["accent"] if on else "#0b0e15", text_color="#05080f" if on else C["fg"])

        def click() -> None:
            var.set(not var.get())
            paint()
            self._run()

        btn = ctk.CTkButton(row, text=label, width=32, height=26, corner_radius=4,
                            font=(UI_FONT[0], 9, "bold"), border_width=1, border_color=C["border"],
                            fg_color="#0b0e15", hover_color="#1b2230", text_color=C["fg"], command=click)
        btn.pack(side="left", padx=(6, 0))

    def _set_counter(self, text: str, ok: bool = True) -> None:
        self.counter.configure(text=text, fg=C["fg"] if ok else C["red"])

    # -- show / hide -----------------------------------------------------------
    def show(self) -> None:
        if not self.visible:
            self.pack(side="top", fill="x", before=self.anchor)
            self.visible = True
        try:                                   # pre-fill with a short single-line selection
            sel = self.text.get("sel.first", "sel.last")
            if sel and "\n" not in sel and len(sel) <= 120:
                self.query.set(sel)
        except tk.TclError:
            pass
        self.entry.focus_set()
        self.entry.select_range(0, "end")
        if self.query.get():
            self._run()

    def close(self) -> None:
        for job in (self._job, self._blink_job, self._refresh_job):
            if job:
                self.after_cancel(job)
        self._job = self._blink_job = self._refresh_job = None
        for tag in ("findmatch", "findcur", "findblink"):
            self.text.tag_remove(tag, "1.0", "end")
        self.matches, self.matching_lines, self.current = [], [], -1
        self.pack_forget()
        self.visible = False
        self.text.focus_set()

    # -- searching -------------------------------------------------------------
    def _schedule(self) -> None:
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(120, self._run)

    def _run(self, reveal: bool = True, keep_start: Optional[tuple] = None) -> None:
        """Re-compute all matches for the current query and highlight them."""
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        t = self.text
        for tag in ("findmatch", "findcur"):
            t.tag_remove(tag, "1.0", "end")
        self.matches, self.matching_lines, self.current, self._capped = [], [], -1, False
        q = self.query.get()
        if not q:
            self._set_counter("0 of 0")
            return
        flags = re.MULTILINE | (0 if self.case_var.get() else re.IGNORECASE)
        try:
            pattern = re.compile(q if self.regex_var.get() else re.escape(q), flags)
        except re.error:
            self._set_counter("bad regex", ok=False)
            return

        content = t.get("1.0", "end-1c")
        self._sig = (len(content), hash(content))
        starts = [0] + [m.end() for m in re.finditer("\n", content)]   # char offset of every line start
        lines = set()
        for m in pattern.finditer(content):
            if m.end() == m.start():
                continue
            sl = bisect_right(starts, m.start())
            el = bisect_right(starts, m.end())
            s_idx = f"{sl}.{m.start() - starts[sl - 1]}"
            e_idx = f"{el}.{m.end() - starts[el - 1]}"
            t.tag_add("findmatch", s_idx, e_idx)
            self.matches.append((s_idx, e_idx, sl))
            lines.add(sl)
            if len(self.matches) >= self.MAX_MATCHES:
                self._capped = True
                break
        self.matching_lines = sorted(lines)
        if not self.matches:
            self._set_counter("0 of 0", ok=False)
            return

        def pos(idx: str) -> tuple:
            a, b = idx.split(".")
            return int(a), int(b)

        anchor = keep_start or pos(t.index("insert"))
        start_i = next((i for i, mt in enumerate(self.matches) if pos(mt[0]) >= anchor), 0)
        self._select(start_i, reveal=reveal)

    def _select(self, i: int, reveal: bool = True) -> None:
        t = self.text
        t.tag_remove("findcur", "1.0", "end")
        self.current = i
        s_idx, e_idx, line = self.matches[i]
        t.tag_add("findcur", s_idx, e_idx)
        n = len(self.matches)
        self._set_counter(f"{i + 1} of {n}{'+' if self._capped else ''}")
        if reveal:
            t.mark_set("insert", s_idx)
            reveal_line_center(t, line, s_idx)
            self._blink(line)

    def next(self) -> None:
        if self.matches:
            self._select((self.current + 1) % len(self.matches))

    def prev(self) -> None:
        if self.matches:
            self._select((self.current - 1) % len(self.matches))

    # -- keep results in sync with edits / file switches -------------------------------
    def refresh(self) -> None:
        """Re-run the search if the text changed (no jumping, no blinking)."""
        if not self.visible or not self.query.get():
            return
        content = self.text.get("1.0", "end-1c")
        if self._sig == (len(content), hash(content)):
            return
        keep = None
        if 0 <= self.current < len(self.matches):
            a, b = self.matches[self.current][0].split(".")
            keep = (int(a), int(b))
        self._run(reveal=False, keep_start=keep)

    def schedule_refresh(self) -> None:
        if self.visible:
            if self._refresh_job:
                self.after_cancel(self._refresh_job)
            self._refresh_job = self.after(350, self.refresh)

    # -- blink -------------------------------------------------------------------
    def _blink(self, line: int) -> None:
        """Flash the whole matched line for ~1.6 s so it draws attention."""
        if self._blink_job:
            self.after_cancel(self._blink_job)
            self._blink_job = None
        t = self.text

        def step(n: int) -> None:
            t.tag_remove("findblink", "1.0", "end")
            if n >= self.BLINK_FLASHES * 2:
                self._blink_job = None
                return
            if n % 2 == 0:
                t.tag_add("findblink", f"{line}.0", f"{line}.0 lineend+1c")
            self._blink_job = self.after(self.BLINK_INTERVAL, step, n + 1)
        step(0)


def make_panel(parent, title: str, hint: str = "") -> tuple:
    outer = tk.Frame(parent, bg=C["border"], padx=1, pady=1)
    inner = tk.Frame(outer, bg=C["panel"])
    inner.pack(fill="both", expand=True)
    head = tk.Frame(inner, bg=C["panel"])
    head.pack(fill="x", padx=10, pady=(7, 3))
    tk.Label(head, text=title, fg="#c9d4e5", bg=C["panel"], font=SMALL).pack(side="left")
    if hint:
        tk.Label(head, text="  \u00b7  " + hint, fg=C["dim"], bg=C["panel"],
                 font=(UI_FONT[0], 8)).pack(side="left")
    body = tk.Frame(inner, bg=C["panel"])
    body.pack(fill="both", expand=True)
    return outer, body


def make_code_text(parent, wrap: str = "none", gutter: bool = False) -> tuple:
    frame = tk.Frame(parent, bg=C["code"])
    txt = CodeText(frame, bg=C["code"], fg=C["fg"], insertbackground="#ffffff",
                   selectbackground=C["sel"], font=MONO, wrap=wrap, undo=True, relief="flat",
                   borderwidth=0, highlightthickness=0, padx=10, pady=6, spacing1=1, spacing3=1)
    vs = _scrollbar(frame, txt.yview, bg=C["code"])
    txt.configure(yscrollcommand=vs.set)
    frame.grid_rowconfigure(0, weight=1)
    frame.grid_columnconfigure(1, weight=1)
    if gutter:
        LineNumbers(frame, txt).grid(row=0, column=0, sticky="ns")
    txt.grid(row=0, column=1, sticky="nsew")
    vs.grid(row=0, column=2, sticky="ns")
    if wrap == "none":
        hs = _scrollbar(frame, txt.xview, "horizontal", bg=C["code"])
        txt.configure(xscrollcommand=hs.set)
        hs.grid(row=1, column=1, sticky="ew")
    return frame, txt


class CodePane:
    """A titled panel holding a syntax-highlighted editor (`mode` = 'java' | 'smali') with Ctrl+F find."""

    def __init__(self, parent, title: str, hint: str, mode: str, editable: bool) -> None:
        self.mode, self.editable = mode, editable
        self.outer, body = make_panel(parent, title, hint)
        frame, self.text = make_code_text(body, gutter=True)
        frame.pack(fill="both", expand=True)
        configure_tags(self.text, mode)
        if not editable:
            self.text.configure(state="disabled")

        # in-editor "Find & Jump" (created after configure_tags so its tags get higher priority)
        self.find = FindBar(body, self.text, anchor=frame)
        for seq in ("<Control-f>", "<Control-F>"):
            self.text.bind(seq, self._on_ctrl_f)
        self.text.bind("<F3>", lambda e: (self.find.next(), "break")[1])
        self.text.bind("<Shift-F3>", lambda e: (self.find.prev(), "break")[1])
        self.text.bind("<Escape>", self._on_escape)
        # a read-only (disabled) Text does not take focus on click by default - Ctrl+F needs it
        self.text.bind("<Button-1>", lambda e: self.text.focus_set(), add="+")

    def _on_ctrl_f(self, _e=None) -> str:
        self.find.show()
        return "break"

    def _on_escape(self, _e=None) -> Optional[str]:
        if self.find.visible:
            self.find.close()
            return "break"
        return None

    def set_content(self, content: str, syntax: bool = True) -> None:
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("1.0", content)
        t.edit_reset()
        t.edit_modified(False)
        if syntax:
            highlight(t, self.mode)
        else:
            clear_syntax(t)
        if not self.editable:
            t.configure(state="disabled")
        self.find.refresh()                    # keep an open find bar in sync with the new text

    def rehighlight(self) -> None:
        highlight(self.text, self.mode)

    def get_content(self) -> str:
        return self.text.get("1.0", "end-1c")


# --------------------------------------------------------------------------- #
#  Header / toolbar / status bar
# --------------------------------------------------------------------------- #
class Header(tk.Frame):
    def __init__(self, parent) -> None:
        super().__init__(parent, bg=C["bg"])
        tk.Label(self, text="\u25c8", fg=C["accent"], bg=C["bg"], font=(UI_FONT[0], 24)).pack(side="left")
        box = tk.Frame(self, bg=C["bg"])
        box.pack(side="left", padx=8)
        tk.Label(box, text="SmaliX", fg="#ffffff", bg=C["bg"], font=(UI_FONT[0], 14, "bold")).pack(anchor="w")
        tk.Label(box, text="APK Decompiler & Smali Editor", fg=C["dim"], bg=C["bg"],
                 font=(UI_FONT[0], 8)).pack(anchor="w")


class Toolbar(tk.Frame):
    """commands keys: open save rebuild sign rebuild_sign clean about tools search(query)"""

    def __init__(self, parent, commands: dict, fast_var: tk.BooleanVar, random_var: tk.BooleanVar,
                 sync_var: tk.BooleanVar) -> None:
        super().__init__(parent, bg=C["bg"])
        self.action_buttons: list = []

        def add(key, text, color, hover, fg, border=False, lock=True):
            kw = dict(text=text, command=commands[key], height=32, corner_radius=6, font=UI_BOLD,
                      fg_color=color, hover_color=hover, text_color=fg)
            if border:
                kw.update(border_width=1, border_color=C["border"])
            b = ctk.CTkButton(self, **kw)
            b.pack(side="left", padx=(0, 8))
            if lock:
                self.action_buttons.append(b)
            return b

        add("open", "Open APK", C["teal"], "#17a898", "#05080f")
        add("save", "Save", "#0b0e15", "#1b2230", "#ffffff", border=True, lock=False)
        add("rebuild", "Rebuild", C["blue"], "#4673cc", "#ffffff")
        add("sign", "Sign - Align", C["purple"], "#8638b8", "#ffffff")
        add("rebuild_sign", "Rebuild - Sign", C["green"], "#1aa04d", "#04140a")
        add("clean", "Clean", "#0b0e15", "#1b2230", "#ffffff", border=True)

        for text, var in (("fast (code-only)", fast_var), ("random key", random_var),
                          ("sync java\u2192smali", sync_var)):
            ctk.CTkCheckBox(self, text=text, variable=var, width=20, checkbox_width=16, checkbox_height=16,
                            border_width=1, corner_radius=3, fg_color=C["accent"], hover_color="#17a898",
                            text_color=C["dim"], font=(UI_FONT[0], 9)).pack(side="left", padx=(8, 0))

        right = tk.Frame(self, bg=C["bg"])
        right.pack(side="right")
        for key, text in (("about", "About"), ("tools", "Tools")):
            ctk.CTkButton(right, text=text, width=70, height=32, corner_radius=6, font=UI_BOLD,
                          fg_color="#0b0e15", hover_color="#1b2230", border_width=1,
                          border_color=C["border"], command=commands[key]).pack(side="right", padx=(8, 0))
        self.search_entry = ctk.CTkEntry(right, width=260, height=32, corner_radius=6,
                                         placeholder_text="search project...  (Enter)",
                                         fg_color="#0b0e15", border_color=C["border"], font=UI_FONT)
        self.search_entry.pack(side="right")
        self.search_entry.bind("<Return>", lambda e: commands["search"](self.search_entry.get()))

    def set_busy(self, busy: bool) -> None:
        for b in self.action_buttons:
            b.configure(state="disabled" if busy else "normal")


class StatusBar(tk.Frame):
    def __init__(self, parent) -> None:
        super().__init__(parent, bg=C["bg"])
        self.status = tk.Label(self, text="Ready", fg=C["dim"], bg=C["bg"], font=UI_FONT, anchor="w")
        self.status.pack(side="left")
        self.struct = tk.Label(self, text="", fg=C["str"], bg=C["bg"], font=UI_FONT)
        self.struct.pack(side="left", padx=16)
        self.stats = tk.Label(self, text="", fg=C["dim"], bg=C["bg"], font=UI_FONT)
        self.stats.pack(side="right")

    def set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def set_structure(self, text: str, ok: bool = True) -> None:
        self.struct.configure(text=text, fg=C["str"] if ok else C["red"])

    def set_stats(self, text: str) -> None:
        self.stats.configure(text=text)


# --------------------------------------------------------------------------- #
#  Project tree
# --------------------------------------------------------------------------- #
class ProjectTree:
    """
    Shows (1) a merged 'Classes' view (package -> class -> methods) and (2) the real
    project folder: assets, build, kotlin, original, res, smali, unknown, AndroidManifest.xml,
    apktool.yml ... Folders and classes are loaded lazily when expanded.

    on_select(path, header_or_None)   user selected a file / class / method
    on_ready(first_path, stats_text, ok)   all classes inserted
    """
    CHUNK = 400

    def __init__(self, parent, on_select: Callable, on_ready: Callable) -> None:
        self.on_select, self.on_ready = on_select, on_ready
        self.outer, body = make_panel(parent, "PROJECT")
        self.tree = ttk.Treeview(body, style="Smalix.Treeview", show="tree", selectmode="browse")
        vs = _scrollbar(body, self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=(0, 6))
        for tag, col in (("group", C["accent"]), ("pkg", "#93c5fd"), ("cls", C["orange"]),
                         ("method", "#c4b5fd"), ("res", C["str"]), ("folder", C["accent"]),
                         ("dim", C["dim"])):
            self.tree.tag_configure(tag, foreground=col)
        self.tree.tag_configure("group", font=UI_BOLD)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<<TreeviewOpen>>", self._on_open)

        self.project_dir: Optional[Path] = None
        self.node_info: dict = {}
        self.pkg_nodes: dict = {}
        self.real_nodes: list = []
        self.root_c = ""
        self._class_iter = iter(())
        self._total_classes = self._total_methods = 0
        self._package = ""
        self._first = self._pkg_first = self._main = None

    # -- population --------------------------------------------------------
    def populate(self, project_dir: Path, scan) -> None:
        self.project_dir = project_dir
        self.tree.delete(*self.tree.get_children())
        self.node_info.clear()
        self.pkg_nodes.clear()
        self.real_nodes = []
        self._package = scan.package
        self.root_c = self.tree.insert("", "end", text="\u25a0 Classes", open=True, tags=("group",))
        self.refresh_files()
        self._class_iter = iter(scan.classes)
        self._total_classes, self._total_methods = len(scan.classes), scan.methods
        self._first = self._pkg_first = self._main = None
        self._pump()

    def refresh_files(self) -> None:
        """(Re)list the real project folder - called after build / sign / clean."""
        for iid in self.real_nodes:
            if self.tree.exists(iid):
                self.tree.delete(iid)
        self.real_nodes = []
        if self.project_dir and self.project_dir.exists():
            self.real_nodes = self._list_dir("", self.project_dir)

    def _list_dir(self, parent: str, path: Path) -> list:
        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return []
        out = []
        for n, p in enumerate(entries):
            if n >= 4000:
                iid = self.tree.insert(parent, "end", text=f"... {len(entries) - n} more not shown", tags=("dim",))
                self.node_info[iid] = ("more",)
                out.append(iid)
                break
            out.append(self._add_entry(parent, p))
        return out

    def _add_entry(self, parent: str, p: Path) -> str:
        if p.is_dir():
            iid = self.tree.insert(parent, "end", text="\u25b8 " + p.name, tags=("folder",))
            self.node_info[iid] = ("dir", p)
            self.node_info[self.tree.insert(iid, "end", text="...")] = ("dummy",)
        elif p.suffix == ".smali":
            iid = self.tree.insert(parent, "end", text="\u25cf " + p.stem, tags=("cls",))
            self.node_info[iid] = ("class", p)
            self.node_info[self.tree.insert(iid, "end", text="...")] = ("dummy",)
        else:
            iid = self.tree.insert(parent, "end", text="\u25aa " + p.name, tags=("res",))
            self.node_info[iid] = ("file", p)
        return iid

    def _pump(self) -> None:
        """Insert classes in chunks so the GUI stays responsive on huge apps."""
        count = 0
        for pkg, name, path in self._class_iter:
            node = self.pkg_nodes.get(pkg)
            if node is None:
                node = self.tree.insert(self.root_c, "end", text="\u25aa " + (pkg or "(default)"), tags=("pkg",))
                self.pkg_nodes[pkg] = node
            iid = self.tree.insert(node, "end", text="\u25cf " + name, tags=("cls",))
            self.node_info[iid] = ("class", path)
            self.node_info[self.tree.insert(iid, "end", text="...")] = ("dummy",)
            in_app = not self._package or pkg.startswith(self._package)
            if name == "MainActivity" and in_app and self._main is None:
                self._main = path
            elif in_app and self._pkg_first is None:
                self._pkg_first = path
            if self._first is None:
                self._first = path
            count += 1
            if count >= self.CHUNK:
                self.tree.after(5, self._pump)
                return
        if len(self.pkg_nodes) <= 6:
            for n in self.pkg_nodes.values():
                self.tree.item(n, open=True)
        stats = f"{self._package}  \u00b7  {self._total_classes} classes \u00b7 {self._total_methods} methods"
        self.on_ready(self._main or self._pkg_first or self._first, stats, self._total_classes > 0)

    # -- events ------------------------------------------------------------
    def _on_open(self, _e=None) -> None:
        iid = self.tree.focus()
        info = self.node_info.get(iid)
        if not info or info[0] not in ("class", "dir"):
            return
        kids = self.tree.get_children(iid)
        if not (kids and self.node_info.get(kids[0], ("",))[0] == "dummy"):
            return
        self.tree.delete(kids[0])
        if info[0] == "dir":
            self._list_dir(iid, info[1])
            return
        try:
            text = read_text(info[1])
        except OSError:
            return
        for m in re.finditer(r"^\.method\s+.*$", text, re.M):
            header = m.group(0).strip()
            mid = self.tree.insert(iid, "end", text="\u2022 " + method_label(header), tags=("method",))
            self.node_info[mid] = ("method", info[1], header)

    def _on_select(self, _e=None) -> None:
        sel = self.tree.selection()
        info = self.node_info.get(sel[0]) if sel else None
        if not info:
            return
        if info[0] in ("file", "class"):
            self.on_select(info[1], None)
        elif info[0] == "method":
            self.on_select(info[1], info[2])


# --------------------------------------------------------------------------- #
#  Results list
# --------------------------------------------------------------------------- #
class ResultsTree:
    def __init__(self, parent, columns: tuple, on_activate: Callable) -> None:
        self.on_activate = on_activate
        self.tree = ttk.Treeview(parent, style="Smalix.Treeview", columns=[c for c, _ in columns],
                                 show="headings")
        for name, width in columns:
            self.tree.heading(name, text=name.title())
            self.tree.column(name, width=width, anchor="w", stretch=(name == columns[-1][0]))
        vs = _scrollbar(parent, self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self._activate)
        self.nav: dict = {}

    def clear(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.nav.clear()

    def message(self, *values) -> None:
        self.tree.insert("", "end", values=values)

    def add(self, values: tuple, target: Optional[tuple] = None) -> None:
        iid = self.tree.insert("", "end", values=values)
        if target:
            self.nav[iid] = target

    def _activate(self, _e=None) -> None:
        sel = self.tree.selection()
        target = self.nav.get(sel[0]) if sel else None
        if target:
            self.on_activate(target[0], target[1])


# --------------------------------------------------------------------------- #
#  Bottom tabs
# --------------------------------------------------------------------------- #
class BottomTabs(ttk.Notebook):
    """
    handlers: patch_true patch_false patch_null patch_void patch_flip patch_nop
              search(query)  goto(path, line)  tab_changed(name)
    tab names: explain patch calls callers graph search console
    """

    def __init__(self, parent, handlers: dict) -> None:
        super().__init__(parent, style="Smalix.TNotebook")
        self.h = handlers
        self._frames: dict = {}
        self._graph_data: Optional[dict] = None

        # Explain
        f = self._add("explain", "Explain")
        ef, self.explain_txt = make_code_text(f, wrap="word")
        ef.pack(fill="both", expand=True)
        self.explain_txt.tag_configure("title", foreground=C["accent"], font=(MONO[0], MONO[1], "bold"))
        self._set_explain_raw("Click a line in the Smali editor to see an explanation.")

        # Patch assistant
        f = self._add("patch", "Patch Assistant")
        tk.Label(f, bg=C["panel"], fg=C["dim"], font=UI_FONT, justify="left",
                 text="Place the cursor inside a method (or on an instruction) in the Smali editor, then pick a patch.\n"
                      "Every patch is a normal edit - use Ctrl+Z to undo, Save to write it to disk."
                 ).pack(anchor="w", padx=14, pady=(12, 8))
        row = tk.Frame(f, bg=C["panel"])
        row.pack(anchor="w", padx=14)
        for text, key, color in (("Return TRUE / 1", "patch_true", C["green"]),
                                 ("Return FALSE / 0", "patch_false", C["red"]),
                                 ("Return NULL", "patch_null", C["orange"]),
                                 ("Return VOID (empty method)", "patch_void", C["blue"]),
                                 ("Flip if-* on cursor line", "patch_flip", C["purple"]),
                                 ("NOP cursor line", "patch_nop", "#64748b")):
            ctk.CTkButton(row, text=text, command=handlers[key], height=30, corner_radius=6, fg_color=color,
                          hover_color=color, text_color="#05080f", font=UI_BOLD).pack(side="left", padx=(0, 8), pady=4)
        self.patch_status = tk.Label(f, bg=C["panel"], fg=C["accent"], font=UI_FONT, text="")
        self.patch_status.pack(anchor="w", padx=14, pady=8)

        # Calls / Called by
        goto = handlers["goto"]
        self.calls = ResultsTree(self._add("calls", "Calls \u2192"), (("line", 70), ("call", 900)), goto)
        self.callers = ResultsTree(self._add("callers", "Called by \u2190"),
                                   (("class", 360), ("line", 70), ("code", 700)), goto)

        # Graph
        f = self._add("graph", "Graph")
        self.graph = tk.Canvas(f, bg=C["code"], highlightthickness=0)
        self.graph.pack(fill="both", expand=True)
        self.graph.bind("<Configure>", lambda e: self.draw_graph(self._graph_data))

        # Search
        f = self._add("search", "Search")
        top = tk.Frame(f, bg=C["panel"])
        top.pack(fill="x", padx=8, pady=6)
        self.search_entry = ctk.CTkEntry(top, height=28, fg_color="#0b0e15", border_color=C["border"],
                                         placeholder_text="text to find in smali / resources")
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.search_entry.bind("<Return>", lambda e: handlers["search"](self.search_entry.get()))
        ctk.CTkButton(top, text="Search", width=80, height=28, fg_color=C["accent"], hover_color="#17a898",
                      text_color="#05080f", font=UI_BOLD,
                      command=lambda: handlers["search"](self.search_entry.get())).pack(side="left")
        rf = tk.Frame(f, bg=C["panel"])
        rf.pack(fill="both", expand=True)
        self.search = ResultsTree(rf, (("file", 420), ("line", 70), ("text", 700)), goto)

        # Console
        f = self._add("console", "Console")
        cf, self.console = make_code_text(f, wrap="none")
        cf.pack(fill="both", expand=True)
        self.console.configure(state="disabled", undo=False)
        for tag, col in (("err", C["red"]), ("ok", C["str"]), ("cmd", C["cyan"]),
                         ("info", C["yellow"]), ("dim", C["dim"])):
            self.console.tag_configure(tag, foreground=col)

        self.bind("<<NotebookTabChanged>>", lambda e: handlers["tab_changed"](self.current_name()))

    # -- helpers -----------------------------------------------------------
    def _add(self, name: str, text: str) -> tk.Frame:
        f = tk.Frame(self, bg=C["panel"])
        self._frames[name] = f
        self.add(f, text=text)
        return f

    def select_tab(self, name: str) -> None:
        self.select(self._frames[name])

    def current_name(self) -> str:
        sel = self.select()
        for name, f in self._frames.items():
            if str(f) == str(sel):
                return name
        return ""

    # -- console -----------------------------------------------------------
    def append_console(self, msg: str, tag: str = "") -> None:
        c = self.console
        c.configure(state="normal")
        if not tag:
            low = msg.lower()
            if "error" in low or "exception" in low or "failed" in low:
                tag = "err"
            elif low.strip().startswith(("done", "- sign success", "successfully", "i: ")):
                tag = "ok"
        c.insert("end", msg.rstrip("\n") + "\n", tag or ())
        if int(c.index("end-1c").split(".")[0]) > 6000:
            c.delete("1.0", "1000.0")
        c.see("end")
        c.configure(state="disabled")

    # -- explain / patch ---------------------------------------------------
    def _set_explain_raw(self, text: str) -> None:
        t = self.explain_txt
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("1.0", text)
        t.configure(state="disabled")

    def set_explain(self, title: str, body: str) -> None:
        t = self.explain_txt
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("1.0", f"{title}\n\n{body}")
        t.tag_add("title", "1.0", "1.end")
        t.configure(state="disabled")

    def set_patch_message(self, msg: str, ok: bool = True) -> None:
        self.patch_status.configure(text=msg, fg=C["accent"] if ok else C["red"])

    # -- graph -------------------------------------------------------------
    def draw_graph(self, data: Optional[dict]) -> None:
        """data = {'center': str, 'callers': [str], 'callees': [str]} or None"""
        self._graph_data = data
        c = self.graph
        c.delete("all")
        w, h = max(c.winfo_width(), 400), max(c.winfo_height(), 120)
        if not data:
            c.create_text(w / 2, h / 2, fill=C["dim"], font=UI_FONT,
                          text="Put the cursor inside a method in the Smali editor to see its call graph.")
            return
        cx, cy = w / 2, h / 2

        def node(x, y, text, fill, fg="#05080f", bw=210):
            c.create_rectangle(x - bw / 2, y - 12, x + bw / 2, y + 12, fill=fill, outline="")
            c.create_text(x, y, text=text[:34], fill=fg, font=(UI_FONT[0], 8, "bold"))

        node(cx, cy, data["center"], C["accent"])
        for side, names in (("L", data["callers"][:14]), ("R", data["callees"][:14])):
            n = len(names)
            x = 130 if side == "L" else w - 130
            for i, nm in enumerate(names):
                y = (i + 1) * h / (n + 1)
                if side == "L":
                    c.create_line(x + 105, y, cx - 105, cy, fill="#2b3b55", arrow="last")
                    node(x, y, nm, "#243247", C["fg"])
                else:
                    c.create_line(cx + 105, cy, x - 105, y, fill="#2b3b55", arrow="last")
                    node(x, y, nm, "#2a2146", "#c4b5fd")
        c.create_text(130, 14, text="CALLED BY", fill=C["dim"], font=SMALL)
        c.create_text(w - 130, 14, text="CALLS", fill=C["dim"], font=SMALL)


# --------------------------------------------------------------------------- #
#  Tools dialog
# --------------------------------------------------------------------------- #
class ToolsDialog(ctk.CTkToplevel):
    def __init__(self, parent, tools: Tools, on_saved: Callable, on_download_jadx: Callable,
                 on_fix_framework: Callable) -> None:
        super().__init__(parent)
        self.tools = tools
        self.on_saved = on_saved
        self.title("Tool paths")
        self.geometry("800x430")
        self.configure(fg_color=C["bg"])
        self.transient(parent)
        self.after(100, self.grab_set)
        self.entries: dict = {}
        for i, key in enumerate(Tools.KEYS):
            ctk.CTkLabel(self, text=key, width=80, anchor="w", font=UI_BOLD).grid(row=i, column=0, padx=14, pady=8)
            e = ctk.CTkEntry(self, width=540, fg_color="#0b0e15", border_color=C["border"])
            e.insert(0, tools.paths.get(key, ""))
            e.grid(row=i, column=1, pady=8)
            self.entries[key] = e
            ctk.CTkButton(self, text="...", width=36,
                          command=lambda k=key, ent=e: self._browse(k, ent)).grid(row=i, column=2, padx=8)

        bf = tk.Frame(self, bg=C["bg"])
        bf.grid(row=len(Tools.KEYS), column=0, columnspan=3, pady=16)

        def run_and_close(fn: Callable) -> Callable:
            def go() -> None:
                self.destroy()
                fn()
            return go

        for text, color, cmd, fg in (
                ("Auto-detect", C["blue"], self._autodetect, "#ffffff"),
                ("Download JADX", C["purple"], run_and_close(on_download_jadx), "#ffffff"),
                ("Fix framework", C["orange"], run_and_close(on_fix_framework), "#05080f"),
                ("Save", C["teal"], self._save, "#05080f"),
                ("Cancel", "#334155", self.destroy, "#ffffff")):
            ctk.CTkButton(bf, text=text, command=cmd, fg_color=color, text_color=fg).pack(side="left", padx=6)
        ctk.CTkLabel(self, text="'Fix framework' updates apktool's Android framework - use it when a rebuild "
                                "fails with 'incompatible with attribute ...' errors.",
                     text_color=C["dim"], font=(UI_FONT[0], 9)).grid(row=len(Tools.KEYS) + 1, column=0,
                                                                     columnspan=3, padx=14)

    def _browse(self, key: str, entry: ctk.CTkEntry) -> None:
        p = filedialog.askopenfilename(title=f"Locate {key}")
        if p:
            entry.delete(0, "end")
            entry.insert(0, p)

    def _autodetect(self) -> None:
        self.tools.detect(only_missing=False)
        for k, e in self.entries.items():
            e.delete(0, "end")
            e.insert(0, self.tools.paths.get(k, ""))

    def _save(self) -> None:
        self.tools.paths = {k: e.get().strip().strip('"') for k, e in self.entries.items() if e.get().strip()}
        try:
            self.tools.save()
        except OSError as ex:
            from tkinter import messagebox
            messagebox.showerror("SmaliX", f"Cannot save config:\n{ex}")
        self.destroy()
        self.on_saved()
