#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - SmaliX Desktop entry point.

Wires the UI (ui_components.py) to the backend (backend_worker.py):

    python main.py

Requires:  pip install customtkinter
External:  JDK, apktool (jar or .bat), Android SDK build-tools (zipalign, apksigner),
           optional jadx (Tools -> Download JADX)
"""

from __future__ import annotations

import queue
import re
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Optional

import customtkinter as ctk

from backend_worker import BackendWorker
from config import APP_DIR, APP_NAME, APP_VERSION, C, Tools
from smali_utils import (explain_line, flip_if_line, is_binary, parse_method_header, read_text,
                         return_patch_body, smali_class_name, smali_to_java_outline)
from sync_mapper import map_java_to_smali
from ui_components import (BottomTabs, CodePane, Header, ProjectTree, StatusBar, Toolbar,
                           ToolsDialog, apply_styles, reveal_line_center)

MAX_EDIT_BYTES = 5 * 1024 * 1024


class SmaliXApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title(APP_NAME)
        self.geometry("1560x920")
        self.minsize(1100, 680)
        self.configure(fg_color=C["bg"])

        self.tools = Tools()
        self.ui_q: "queue.Queue[tuple]" = queue.Queue()
        self.backend = BackendWorker(self.tools, log=self.log, post=self.post,
                                     on_busy=self._on_busy, on_status=self.set_status)
        self.backend.on_jadx_ready = self._reload_java

        self.current_file: Optional[Path] = None
        self._java_owner: Optional[Path] = None   # smali file whose Java is shown
        self._hl_job = None
        self._blink_job = None
        self._callers_cache: dict = {}
        self.fast_var = tk.BooleanVar(value=False)
        self.random_var = tk.BooleanVar(value=False)
        self.sync_var = tk.BooleanVar(value=True)
        self._active_pane = None            # last focused editor pane (target of Ctrl+F)

        apply_styles(self)
        self._build_ui()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(40, self._poll_queue)
        self.after(150, self._set_initial_sashes)
        self.after(300, self._startup_report)

    # ------------------------------------------------------------------ #
    #  UI assembly
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        Header(self).pack(fill="x", padx=16, pady=(10, 0))
        self.toolbar = Toolbar(self, {
            "open": self.cmd_open, "save": self.cmd_save, "rebuild": self.cmd_rebuild,
            "sign": self.cmd_sign, "rebuild_sign": self.cmd_rebuild_sign, "clean": self.cmd_clean,
            "about": self.cmd_about, "tools": self.cmd_tools, "search": self.cmd_search,
        }, self.fast_var, self.random_var, self.sync_var)
        self.toolbar.pack(fill="x", padx=16, pady=10)

        self.vpane = tk.PanedWindow(self, orient="vertical", sashwidth=6, bg=C["bg"], bd=0,
                                    sashrelief="flat", opaqueresize=True)
        self.vpane.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        self.hpane = tk.PanedWindow(self.vpane, orient="horizontal", sashwidth=6, bg=C["bg"], bd=0,
                                    sashrelief="flat", opaqueresize=True)
        self.vpane.add(self.hpane, minsize=220, stretch="always")

        self.project_tree = ProjectTree(self.hpane, self._on_tree_select, self._on_classes_ready)
        self.java = CodePane(self.hpane, "JAVA", "decompiled (JADX) \u00b7 click a line to jump to its smali",
                             mode="java", editable=False)
        self.smali = CodePane(self.hpane, "SMALI", "editable \u00b7 click a line to explain",
                              mode="smali", editable=True)
        self.hpane.add(self.project_tree.outer, minsize=180, stretch="never")
        self.hpane.add(self.java.outer, minsize=260, stretch="always")
        self.hpane.add(self.smali.outer, minsize=260, stretch="always")
        self._active_pane = self.smali
        for pane in (self.java, self.smali):
            pane.text.bind("<FocusIn>", lambda e, p=pane: setattr(self, "_active_pane", p), add="+")
        for seq in ("<Control-f>", "<Control-F>"):     # fallback when focus is outside an editor
            self.bind_all(seq, self._on_ctrl_f)
        self.java.text.bind("<ButtonRelease-1>", self._on_java_click)
        self.smali.text.bind("<ButtonRelease-1>", self._on_smali_click)
        self.smali.text.bind("<KeyRelease>", self._on_smali_key)
        self.smali.text.bind("<Control-s>", lambda e: (self.cmd_save(), "break")[1])

        self.tabs = BottomTabs(self.vpane, {
            "patch_true": lambda: self.patch_return("true"),
            "patch_false": lambda: self.patch_return("false"),
            "patch_null": lambda: self.patch_return("null"),
            "patch_void": lambda: self.patch_return("void"),
            "patch_flip": self.patch_flip_if,
            "patch_nop": self.patch_nop_line,
            "search": self.cmd_search,
            "goto": self._goto_result,
            "tab_changed": self._on_tab_changed,
        })
        self.vpane.add(self.tabs, minsize=140, stretch="never")
        self.tabs.select_tab("console")

        self.statusbar = StatusBar(self)
        self.statusbar.pack(fill="x", padx=16, pady=(0, 6))

    def _set_initial_sashes(self) -> None:
        try:
            self.update_idletasks()
            w, h = self.hpane.winfo_width(), self.vpane.winfo_height()
            self.hpane.sash_place(0, int(w * 0.22), 0)
            self.hpane.sash_place(1, int(w * 0.22 + (w * 0.78) / 2), 0)
            self.vpane.sash_place(0, 0, int(h * 0.62))
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    #  Thread-safe plumbing (backend -> UI)
    # ------------------------------------------------------------------ #
    def post(self, fn, *args) -> None:
        self.ui_q.put(("call", fn, args))

    def log(self, msg: str, tag: str = "") -> None:
        self.ui_q.put(("log", msg, tag))

    def _poll_queue(self) -> None:
        try:
            for _ in range(400):
                item = self.ui_q.get_nowait()
                if item[0] == "log":
                    self.tabs.append_console(item[1], item[2])
                else:
                    try:
                        item[1](*item[2])
                    except Exception:
                        import traceback
                        self.tabs.append_console(traceback.format_exc(), "err")
        except queue.Empty:
            pass
        self.after(40, self._poll_queue)

    def set_status(self, text: str) -> None:
        self.statusbar.set_status(text)

    def _on_busy(self, busy: bool, label: str) -> None:
        self.toolbar.set_busy(busy)
        self.configure(cursor="watch" if busy else "")
        if busy:
            self.set_status(label + " ...")

    def _submit(self, label: str, fn, on_done=None) -> None:
        self.tabs.select_tab("console")
        if not self.backend.submit(label, fn, on_done):
            messagebox.showinfo(APP_NAME, "Another task is still running.")

    def _on_close(self) -> None:
        if self._dirty() and not messagebox.askyesno(APP_NAME, "You have unsaved smali edits. Quit anyway?"):
            return
        self.backend.close()
        self.destroy()

    def _startup_report(self) -> None:
        self.log(f"{APP_NAME} v{APP_VERSION}", "info")
        for k in Tools.KEYS:
            p = self.tools.get(k)
            if p:
                self.log(f"  {k:<9}: {p}", "dim")
            else:
                self.log(f"  {k:<9}: NOT FOUND" + (" (optional)" if k == "jadx" else ""),
                         "dim" if k == "jadx" else "err")
        jar = self.tools.latest_android_jar()
        self.log(f"  sdk jar  : {jar or 'none (install an Android SDK Platform for framework fixes)'}", "dim")
        if not self.tools.get("apktool") or not self.tools.get("java"):
            self.log("Hint: install a JDK + apktool and click 'Tools' to set paths.", "info")
        if not self.tools.get("zipalign") or not self.tools.get("apksigner"):
            self.log("Hint: install Android SDK build-tools (zipalign, apksigner) for signing.", "info")

    # ------------------------------------------------------------------ #
    #  Toolbar commands
    # ------------------------------------------------------------------ #
    def _dirty(self) -> bool:
        return bool(self.current_file) and bool(self.smali.text.edit_modified())

    def _need_project(self) -> bool:
        if not self.backend.proj or not self.backend.proj.exists():
            messagebox.showinfo(APP_NAME, "Open an APK first.")
            return False
        return True

    def cmd_open(self) -> None:
        if self._dirty() and not messagebox.askyesno(APP_NAME, "Discard unsaved smali edits?"):
            return
        path = filedialog.askopenfilename(title="Open APK", filetypes=[("Android package", "*.apk"),
                                                                        ("All files", "*.*")])
        if not path:
            return
        apk, fast = Path(path), self.fast_var.get()
        self._submit("Decompiling " + apk.name, lambda: self.backend.decompile(apk, fast), self._on_decompiled)

    def _on_decompiled(self, scan) -> None:
        self.current_file = None
        self._callers_cache.clear()
        self.project_tree.populate(self.backend.proj, scan)

    def _on_classes_ready(self, first: Optional[Path], stats: str, ok: bool) -> None:
        self.statusbar.set_stats(stats)
        self.statusbar.set_structure("\u25a0 structure ok" if ok else "\u25a0 no smali classes found", ok)
        if first:
            self.open_file(first)

    def cmd_save(self) -> None:
        if not self.current_file:
            self.set_status("Nothing to save")
            return
        try:
            data = self.smali.get_content()
            self.current_file.write_text(data, encoding="utf-8", newline="\n")
            self.smali.text.edit_modified(False)
            self.set_status(f"Saved {self.current_file.name}")
            self.log(f"Saved {self.current_file}", "ok")
            if self.current_file.suffix == ".smali":
                self._refresh_java(self.current_file, data)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Cannot save file:\n{e}")

    def cmd_rebuild(self) -> None:
        if not self._need_project():
            return
        if self._dirty():
            self.cmd_save()
        self._submit("Rebuilding", self.backend.rebuild, lambda _r: self._after_build())

    def _after_build(self) -> None:
        self.project_tree.refresh_files()
        self.statusbar.set_structure("\u25a0 structure ok", True)

    def cmd_sign(self) -> None:
        if not self._need_project():
            return
        rnd = self.random_var.get()
        self._submit("Sign + align", lambda: self.backend.sign_align(rnd),
                     lambda _r: self.project_tree.refresh_files())

    def cmd_rebuild_sign(self) -> None:
        if not self._need_project():
            return
        if self._dirty():
            self.cmd_save()
        rnd = self.random_var.get()

        def both():
            self.backend.rebuild()
            return self.backend.sign_align(rnd)
        self._submit("Rebuild + sign", both, lambda _r: self._after_build())

    def cmd_clean(self) -> None:
        if not self._need_project():
            return
        self._submit("Cleaning", self.backend.clean, lambda _r: self.project_tree.refresh_files())

    def cmd_about(self) -> None:
        messagebox.showinfo("About " + APP_NAME,
                            f"{APP_NAME} v{APP_VERSION}\nAPK Decompiler & Smali Editor\n\n"
                            "Backends: apktool, zipalign, apksigner, jadx (optional)\n"
                            "Use only on apps you own or are authorised to analyse.")

    def cmd_tools(self) -> None:
        def jadx() -> None:
            self.tabs.select_tab("console")
            self.backend.run_bg(self.backend.download_jadx)

        def fix() -> None:
            self.tabs.select_tab("console")
            self._submit("Fixing framework", self.backend.fix_framework_now)

        ToolsDialog(self, self.tools, on_saved=self._startup_report, on_download_jadx=jadx,
                    on_fix_framework=fix)

    # ------------------------------------------------------------------ #
    #  Editors
    # ------------------------------------------------------------------ #
    def _on_tree_select(self, path: Path, header: Optional[str]) -> None:
        self.open_file(path, header=header)

    def open_file(self, path: Path, line: Optional[int] = None, header: Optional[str] = None,
                  refresh_java: bool = True) -> None:
        if self.current_file != path:
            if self._dirty() and messagebox.askyesno(APP_NAME, f"Save changes to {self.current_file.name}?"):
                self.cmd_save()
            try:
                size = path.stat().st_size
                if size > MAX_EDIT_BYTES:
                    raise ValueError(f"File is too large to edit here ({size / 1048576:.1f} MB).")
                if is_binary(path):
                    raise ValueError("Binary file - it cannot be shown as text.")
                text = read_text(path)
            except ValueError as e:
                self._show_unreadable(path, str(e))
                return
            except OSError as e:
                messagebox.showerror(APP_NAME, f"Cannot open {path}:\n{e}")
                return
            self.current_file = path
            self.smali.set_content(text, syntax=path.suffix == ".smali")
            if refresh_java:
                self._refresh_java(path, text)
        if header:
            idx = self.smali.text.search(header, "1.0", stopindex="end")
            if idx:
                line = int(idx.split(".")[0])
        if line:
            self._goto_line(line)

    def _show_unreadable(self, path: Path, reason: str) -> None:
        self.current_file = None
        self._java_owner = None
        lines = [path.name, reason]
        if path.suffix == ".xml" or path.name == "resources.arsc":
            lines += ["", "This file is still in compiled (binary) form because 'fast (code-only)' was",
                      "ticked when the APK was opened. Untick it and use Open APK again to get a",
                      "readable AndroidManifest.xml and res/ files."]
        msg = "\n".join(("# " + l) if l else "#" for l in lines)
        self.smali.set_content(msg)
        self.java.set_content(f"// {path.name}\n// {reason}")
        self.set_status(f"{path.name}: {reason}")

    def _goto_line(self, line: int) -> None:
        t = self.smali.text
        t.tag_remove("curline", "1.0", "end")
        t.tag_add("curline", f"{line}.0", f"{line}.0 lineend+1c")
        t.mark_set("insert", f"{line}.0")
        t.see(f"{line}.0")
        t.yview_moveto(max(0.0, (line - 4) / max(1, int(t.index("end-1c").split(".")[0]))))

    def _refresh_java(self, path: Path, text: Optional[str]) -> None:
        self._java_owner = path if path.suffix == ".smali" else None
        if path.suffix != ".smali":
            self.java.set_content(f"// {path.name}\n// Resource / config file - edit it in the right-hand pane.")
            return
        if text is None:
            text = read_text(path)
        content = None
        cls = smali_class_name(text)
        jdir = self.backend.jadx_dir
        if jdir and cls:
            cand = jdir / "sources" / (cls[1:-1].split("$")[0] + ".java")
            if cand.exists():
                content = read_text(cand)
        self.java.set_content(content if content is not None else smali_to_java_outline(text))

    def _reload_java(self) -> None:
        if self.current_file and self.current_file.suffix == ".smali":
            self._refresh_java(self.current_file, None)

    def _pane_for_focus(self) -> CodePane:
        """Editor that owns the keyboard focus (or the one used last)."""
        try:
            w = str(self.focus_get())
        except (KeyError, tk.TclError):
            w = ""
        for pane in (self.java, self.smali):
            p = str(pane.outer)
            if w == p or w.startswith(p + "."):
                return pane
        return self._active_pane or self.smali

    def _on_ctrl_f(self, _e=None) -> str:
        self._pane_for_focus().find.show()
        return "break"

    def _on_smali_key(self, _e=None) -> None:
        self.smali.find.schedule_refresh()        # keep open find results in sync while typing
        if self.current_file and self.current_file.suffix == ".smali":
            if self._hl_job:
                self.after_cancel(self._hl_job)
            self._hl_job = self.after(350, self.smali.rehighlight)

    def _on_smali_click(self, _e=None) -> None:
        t = self.smali.text
        ln = int(t.index("insert").split(".")[0])
        line = t.get(f"{ln}.0", f"{ln}.0 lineend")
        t.tag_remove("curline", "1.0", "end")
        t.tag_add("curline", f"{ln}.0", f"{ln}.0 lineend+1c")
        self.tabs.set_explain(line.strip(), explain_line(line))
        if self.tabs.current_name() in ("calls", "callers", "graph"):
            self._refresh_relations()

    # ------------------------------------------------------------------ #
    #  Java -> Smali synchronisation
    # ------------------------------------------------------------------ #
    def _on_java_click(self, e) -> None:
        """Click in the Java view -> compute the matching smali line, scroll + blink."""
        jt = self.java.text
        if not self.sync_var.get() or jt.tag_ranges("sel"):   # ignore drag-selections (copying code)
            return
        owner = self._java_owner
        if not owner or not owner.exists():
            return
        jline = int(jt.index(f"@{e.x},{e.y}").split(".")[0])
        jt.tag_remove("curline", "1.0", "end")
        jt.tag_add("curline", f"{jline}.0", f"{jline}.0 lineend+1c")

        java_lines = self.java.get_content().split("\n")
        smali_text = self.smali.get_content() if self.current_file == owner else read_text(owner)

        def siblings() -> dict:          # Outer$Inner.smali, Outer$1.smali ... (loaded only if needed)
            out = {}
            for p in owner.parent.glob(owner.stem + "$*.smali"):
                try:
                    out[p.stem] = read_text(p).split("\n")
                except OSError:
                    pass
            return out

        res = map_java_to_smali(java_lines, jline, smali_text.split("\n"), siblings)
        target = owner if not res.file_key else owner.parent / f"{res.file_key}.smali"
        if self.current_file != target:
            self.open_file(target, refresh_java=False)      # keeps the Java view where it is
            if self.current_file != target:
                return                                      # cancelled / unreadable
        self._sync_smali_to(res.line)
        self._blink_smali(res.block_start, res.block_end)
        self.set_status(f"Java line {jline} \u2192 Smali line {res.line}  \u00b7  {res.note}")

    def _sync_smali_to(self, line: int) -> None:
        t = self.smali.text
        t.tag_remove("curline", "1.0", "end")
        t.tag_add("curline", f"{line}.0", f"{line}.0 lineend+1c")
        t.mark_set("insert", f"{line}.0")
        reveal_line_center(t, line)                         # centre the line in the editor

    def _blink_smali(self, start: int, end: int, flashes: int = 3, interval: int = 170) -> None:
        """Flash the smali block [start, end] a few times (temporary highlight)."""
        t = self.smali.text
        if self._blink_job:
            self.after_cancel(self._blink_job)
            self._blink_job = None
        t.tag_configure("syncblink", background="#2563eb")
        t.tag_raise("syncblink")
        t.tag_raise("sel")

        def step(n: int) -> None:
            t.tag_remove("syncblink", "1.0", "end")
            if n >= flashes * 2:
                self._blink_job = None
                return
            if n % 2 == 0:
                t.tag_add("syncblink", f"{start}.0", f"{end}.0 lineend+1c")
            self._blink_job = self.after(interval, step, n + 1)
        step(0)

    # ------------------------------------------------------------------ #
    #  Method awareness: Calls / Called by / Graph
    # ------------------------------------------------------------------ #
    def current_method(self) -> Optional[dict]:
        t = self.smali.text
        if not self.current_file or self.current_file.suffix != ".smali":
            return None
        cur = int(t.index("insert").split(".")[0])
        last = int(t.index("end-1c").split(".")[0])
        start = None
        for i in range(cur, 0, -1):
            s = t.get(f"{i}.0", f"{i}.0 lineend")
            if s.startswith(".end method") and i != cur:
                return None
            if s.startswith(".method"):
                start = i
                break
        if start is None:
            return None
        end = next((i for i in range(start, last + 1)
                    if t.get(f"{i}.0", f"{i}.0 lineend").startswith(".end method")), None)
        if end is None:
            return None
        header = t.get(f"{start}.0", f"{start}.0 lineend").strip()
        flags, name, params, ret = parse_method_header(header)
        cls = smali_class_name(self.smali.get_content()) or ""
        return {"start": start, "end": end, "header": header, "flags": flags, "name": name,
                "params": params, "ret": ret, "ref": f"{cls}->{name}({params}){ret}", "cls": cls}

    def _calls_of(self, m: dict) -> list:
        out = []
        for i in range(m["start"], m["end"] + 1):
            s = self.smali.text.get(f"{i}.0", f"{i}.0 lineend").strip()
            if s.startswith("invoke"):
                mm = re.search(r"(L[^;]+;->[^\s]+)$", s)
                out.append((i, mm.group(1) if mm else s))
        return out

    def _on_tab_changed(self, name: str) -> None:
        if name in ("calls", "callers", "graph"):
            self._refresh_relations()

    def _refresh_relations(self) -> None:
        m = self.current_method()
        self.tabs.calls.clear()
        if not m:
            self.tabs.callers.clear()
            self.tabs.draw_graph(None)
            return
        calls = self._calls_of(m)
        for ln, target in calls:
            self.tabs.calls.add((ln, target), (self.current_file, ln))
        ref = m["ref"]
        if ref in self._callers_cache:
            self._show_callers(ref, self._callers_cache[ref], calls, m)
            return
        self.tabs.callers.clear()
        self.tabs.callers.message("searching ...", "", "")

        def done(results) -> None:
            self._callers_cache[ref] = results
            self._show_callers(ref, results, calls, m)
        self.backend.run_bg(lambda: self.backend.find_callers(ref), done)

    def _show_callers(self, ref: str, results: list, calls: list, m: dict) -> None:
        cur = self.current_method()
        if not cur or cur["ref"] != ref:
            return
        proj = self.backend.proj
        self.tabs.callers.clear()
        if not results:
            self.tabs.callers.message("(no callers found)", "", "")
        for p, n, text in results[:1000]:
            rel = p.relative_to(proj).as_posix() if proj else str(p)
            self.tabs.callers.add((rel, n, text), (p, n))
        caller_names = []
        for p, _n, _t in results:
            if p.stem not in caller_names:
                caller_names.append(p.stem)
        callee_names = []
        for _ln, t in calls:
            if ";->" in t:
                label = f"{t.split(';->')[0].split('/')[-1]}.{t.split(';->')[-1].split('(')[0]}"
            else:
                label = t
            if label not in callee_names:
                callee_names.append(label)
        self.tabs.draw_graph({"center": f"{m['cls'].split('/')[-1].rstrip(';')}.{m['name']}",
                              "callers": caller_names, "callees": callee_names})

    def _goto_result(self, path: Path, line: int) -> None:
        self.open_file(path, line=line)

    # ------------------------------------------------------------------ #
    #  Search
    # ------------------------------------------------------------------ #
    def cmd_search(self, query: str) -> None:
        query = query.strip()
        if not query:
            return
        if not self.backend.proj:
            messagebox.showinfo(APP_NAME, "Open an APK first.")
            return
        self.tabs.select_tab("search")
        self.tabs.search_entry.delete(0, "end")
        self.tabs.search_entry.insert(0, query)
        self.tabs.search.clear()
        self.tabs.search.message("searching ...", "", "")
        self.backend.run_bg(lambda: self.backend.search_text(query), lambda hits: self._show_search(hits, query))

    def _show_search(self, hits: list, query: str) -> None:
        proj = self.backend.proj
        self.tabs.search.clear()
        for p, n, text in hits:
            self.tabs.search.add((p.relative_to(proj).as_posix() if proj else str(p), n, text), (p, n))
        self.set_status(f"{len(hits)} match(es) for '{query}'" + (" (capped)" if len(hits) >= 3000 else ""))

    # ------------------------------------------------------------------ #
    #  Patch assistant
    # ------------------------------------------------------------------ #
    def patch_return(self, kind: str) -> None:
        m = self.current_method()
        if not m:
            return self.tabs.set_patch_message("Place the cursor inside a method first.", False)
        if "abstract" in m["flags"] or "native" in m["flags"]:
            return self.tabs.set_patch_message("Abstract / native methods have no body to patch.", False)
        try:
            body = return_patch_body(kind, m["ret"])
        except ValueError as e:
            return self.tabs.set_patch_message(str(e), False)
        t = self.smali.text
        t.edit_separator()
        t.delete(f"{m['start'] + 1}.0", f"{m['end']}.0")
        t.insert(f"{m['start'] + 1}.0", "\n".join(body) + "\n")
        t.edit_separator()
        t.edit_modified(True)
        self.smali.rehighlight()
        self.tabs.set_patch_message(f"Patched {m['name']} -> return {kind}. Click Save to write the file.")

    def patch_flip_if(self) -> None:
        t = self.smali.text
        ln = int(t.index("insert").split(".")[0])
        res = flip_if_line(t.get(f"{ln}.0", f"{ln}.0 lineend"))
        if not res:
            return self.tabs.set_patch_message("Put the cursor on an 'if-*' line.", False)
        new_line, old, new = res
        t.edit_separator()
        t.delete(f"{ln}.0", f"{ln}.0 lineend")
        t.insert(f"{ln}.0", new_line)
        t.edit_separator()
        t.edit_modified(True)
        self.smali.rehighlight()
        self.tabs.set_patch_message(f"Line {ln}: if-{old} -> if-{new}")

    def patch_nop_line(self) -> None:
        t = self.smali.text
        if not self.current_file or self.current_file.suffix != ".smali":
            return self.tabs.set_patch_message("Open a smali file first.", False)
        ln = int(t.index("insert").split(".")[0])
        line = t.get(f"{ln}.0", f"{ln}.0 lineend")
        if not line.strip() or line.strip().startswith((".", "#", ":")):
            return self.tabs.set_patch_message("That line is not an instruction.", False)
        t.edit_separator()
        t.delete(f"{ln}.0", f"{ln}.0 lineend")
        t.insert(f"{ln}.0", "    nop")
        t.edit_separator()
        t.edit_modified(True)
        self.smali.rehighlight()
        extra = " (a following move-result may now be invalid)" if line.strip().startswith("invoke") else ""
        self.tabs.set_patch_message(f"Line {ln} replaced with nop{extra}.")


def main() -> None:
    try:
        from ctypes import windll  # type: ignore
        windll.shcore.SetProcessDpiAwareness(1)  # crisp text on Windows HiDPI
    except Exception:
        pass
    APP_DIR.mkdir(parents=True, exist_ok=True)
    SmaliXApp().mainloop()


if __name__ == "__main__":
    main()
