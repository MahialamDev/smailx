#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backend_worker.py - everything that runs outside the GUI thread.

BackendWorker owns the project state (opened APK, decoded folder, last built
APK, JADX output) and implements the long-running jobs:

    decompile  (apktool d)        rebuild  (apktool b)
    sign_align (zipalign + apksigner)       clean
    start_jadx / download_jadx              search_text / find_callers

It never imports the GUI toolkit.  It talks to the UI only through the
callbacks passed to the constructor:

    log(msg, tag)          - append a line to the console (thread-safe by contract)
    post(fn, *args)        - run fn(*args) on the Tk main thread
    on_busy(busy, label)   - a task started / finished
    on_status(text)        - status bar text
    on_jadx_ready()        - JADX output became available (set after construction)

Framework self-healing
----------------------
Apps targeting new Android versions need a recent android framework.  If
apktool has an old cached ``1.apk`` (typical error:
"'0x40000000' is incompatible with attribute foregroundServiceType") the worker
moves that file aside and retries, then falls back to installing the newest
<sdk>/platforms/android-NN/android.jar as the framework.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, NamedTuple, Optional

from config import (APP_DIR, CREATE_NO_WINDOW, DEBUG_KEYSTORE, IS_WIN, JADX_HOME, WORK_ROOT,
                    ToolError, Tools)
from smali_utils import guess_package


class CmdResult(NamedTuple):
    rc: int
    lines: list


@dataclass
class ScanResult:
    classes: list = field(default_factory=list)   # [(package, class_name, Path)]
    methods: int = 0
    package: str = ""


class BackendWorker:
    BUILD_FRAMEWORK_ERRORS = ("is incompatible with attribute", "no resource identifier found",
                              "resource android:attr")
    DECODE_STALE_MARK = "unresolved attr reference: ns=android"

    def __init__(self, tools: Tools,
                 log: Callable[[str, str], None],
                 post: Callable[..., None],
                 on_busy: Callable[[bool, str], None],
                 on_status: Callable[[str], None]) -> None:
        self.tools = tools
        self.log = log
        self.post = post
        self.on_busy = on_busy
        self.on_status = on_status
        self.on_jadx_ready: Callable[[], None] = lambda: None

        self.proc: Optional[subprocess.Popen] = None
        self.busy = False
        self._lock = threading.Lock()

        self.apk_path: Optional[Path] = None
        self.proj: Optional[Path] = None
        self.last_built: Optional[Path] = None
        self.jadx_dir: Optional[Path] = None

    # ------------------------------------------------------------------ #
    #  Threading
    # ------------------------------------------------------------------ #
    def submit(self, label: str, fn: Callable[[], object],
               on_done: Optional[Callable[[object], None]] = None) -> bool:
        """Run fn() on a worker thread. Returns False if another task is running."""
        with self._lock:
            if self.busy:
                return False
            self.busy = True
        self.post(self.on_busy, True, label)

        def target() -> None:
            ok, result = False, None
            try:
                result = fn()
                ok = True
            except ToolError as e:
                self.log(f"ERROR: {e}", "err")
                self.post(self.on_status, f"Failed: {e}")
            except Exception:
                self.log(traceback.format_exc(), "err")
                self.post(self.on_status, "Failed - see console")
            finally:
                with self._lock:
                    self.busy = False
                self.post(self.on_busy, False, "")
            if ok and on_done:
                self.post(on_done, result)

        threading.Thread(target=target, daemon=True, name="smalix-task").start()
        return True

    def run_bg(self, fn: Callable[[], object], on_result: Optional[Callable[[object], None]] = None) -> None:
        """Lightweight background thread (searches, downloads) that doesn't lock the toolbar."""
        def target() -> None:
            try:
                result = fn()
            except Exception:
                self.log(traceback.format_exc(), "err")
                return
            if on_result:
                self.post(on_result, result)

        threading.Thread(target=target, daemon=True, name="smalix-bg").start()

    def close(self) -> None:
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  Subprocess runner
    # ------------------------------------------------------------------ #
    def run_cmd(self, cmd: list, cwd: Optional[Path] = None, quiet: bool = False) -> CmdResult:
        self.log("$ " + " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd), "cmd")
        try:
            proc = subprocess.Popen(
                [str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW, bufsize=1)
        except FileNotFoundError as e:
            raise ToolError(f"Cannot execute '{cmd[0]}': {e}")
        except OSError as e:
            raise ToolError(f"Failed to start '{cmd[0]}': {e}")
        self.proc = proc
        lines: list = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if len(lines) < 6000:
                lines.append(line)
            if not quiet:
                self.log(line, "")
        rc = proc.wait()
        self.proc = None
        if rc != 0:
            self.log(f"[exit code {rc}]", "err")
        return CmdResult(rc, lines)

    # ------------------------------------------------------------------ #
    #  Framework self-healing
    # ------------------------------------------------------------------ #
    @staticmethod
    def framework_dir() -> Path:
        if IS_WIN:
            base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
            return base / "apktool" / "framework"
        if sys.platform == "darwin":
            return Path.home() / "Library" / "apktool" / "framework"
        data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
        return data / "apktool" / "framework"

    def reset_cached_framework(self) -> bool:
        """Move apktool's cached AOSP framework (1.apk) aside so a fresh one is installed."""
        apk = self.framework_dir() / "1.apk"
        if not apk.exists():
            self.log("  no cached framework (1.apk) found.", "dim")
            return False
        backup = apk.with_name("1.apk.smalix-old")
        try:
            if backup.exists():
                backup.unlink()
            apk.rename(backup)
        except OSError as e:
            self.log(f"  cannot move {apk}: {e}", "err")
            return False
        self.log(f"  moved {apk} -> {backup.name}", "info")
        return True

    def install_sdk_framework(self) -> bool:
        """apktool if <sdk>/platforms/android-NN/android.jar"""
        jar = self.tools.latest_android_jar()
        if not jar:
            self.log("  no Android SDK platform found (install 'Android SDK Platform 34+' in "
                     "SDK Manager) - cannot install a newer framework.", "dim")
            return False
        self.log(f"  installing framework from {jar}", "info")
        res = self.run_cmd(self.tools.apktool_cmd() + ["if", str(jar)])
        return res.rc == 0

    def fix_framework_now(self) -> None:
        """Manual 'Fix framework' button: reset the cache, then install the SDK framework."""
        self.log("=== FIX FRAMEWORK ===", "info")
        self.tools.require("apktool")
        reset = self.reset_cached_framework()
        installed = self.install_sdk_framework()
        if reset or installed:
            self.log("Framework updated. Re-open the APK (or click Rebuild) to use it.", "ok")
        else:
            self.log("Nothing changed.", "dim")

    def _framework_steps(self) -> list:
        return [("moving the cached framework aside", self.reset_cached_framework),
                ("installing the newest SDK android.jar as framework", self.install_sdk_framework)]

    @classmethod
    def _decode_is_stale(cls, lines: list) -> bool:
        return any(cls.DECODE_STALE_MARK in l.lower() for l in lines)

    @classmethod
    def _build_framework_error(cls, lines: list) -> bool:
        low = [l.lower() for l in lines]
        return any(p in l for l in low for p in cls.BUILD_FRAMEWORK_ERRORS)

    # ------------------------------------------------------------------ #
    #  Jobs
    # ------------------------------------------------------------------ #
    def _need_project(self) -> None:
        if not self.proj or not self.apk_path or not self.proj.exists():
            raise ToolError("Open an APK first.")

    def decompile(self, apk: Path, fast: bool) -> ScanResult:
        cmd = self.tools.apktool_cmd()
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        proj = WORK_ROOT / apk.stem
        self.log(f"=== DECOMPILE {apk} ===", "info")
        args = cmd + ["d"] + (["-r"] if fast else []) + ["-f", "-o", str(proj), str(apk)]
        if fast:
            self.log("fast (code-only): resources and AndroidManifest.xml are NOT decoded", "dim")
        res = self.run_cmd(args)
        if res.rc != 0:
            raise ToolError("apktool decompile failed - see console")

        for desc, action in self._framework_steps():
            if not self._decode_is_stale(res.lines):
                break
            self.log(f"Old Android framework detected - {desc} and decompiling again ...", "info")
            if not action():
                continue
            res = self.run_cmd(args)
            if res.rc != 0:
                raise ToolError("apktool decompile failed - see console")
        if self._decode_is_stale(res.lines):
            self.log("Note: some android attributes are still unresolved (the framework is older than "
                     "the APK). Rebuild may fail - use Tools -> 'Fix framework'.", "info")

        self.apk_path, self.proj, self.last_built, self.jadx_dir = apk, proj, None, None
        self.log("Scanning project ...", "dim")
        scan = self.scan_project(proj)
        if self.tools.get("jadx"):
            self.start_jadx()
        else:
            self.log("JADX not found - Java pane shows a smali-based outline. "
                     "Tools -> 'Download JADX' installs it automatically.", "dim")
        self.post(self.on_status, f"Opened {apk.name}")
        return scan

    def scan_project(self, proj: Path) -> ScanResult:
        classes: list = []
        n_methods = 0
        for p in self.iter_smali_files(proj):
            rel = p.parent.relative_to(self._smali_root(proj, p))
            classes.append((".".join(rel.parts), p.stem, p))
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                    n_methods += sum(1 for ln in fh if ln.startswith(".method"))
            except OSError:
                pass
        package = ""
        manifest = proj / "AndroidManifest.xml"
        if manifest.exists():
            head = manifest.read_bytes()[:4096]
            if head.lstrip().startswith(b"<"):
                m = re.search(rb'package="([^"]+)"', head)
                package = m.group(1).decode("utf-8", "ignore") if m else ""
            else:
                self.log("AndroidManifest.xml is still binary (fast mode). Untick 'fast (code-only)' "
                         "and re-open the APK to get readable manifest / res files.", "info")
        if not package:
            package = guess_package([c[0] for c in classes])
        return ScanResult(classes, n_methods, package)

    @staticmethod
    def _smali_root(proj: Path, file: Path) -> Path:
        rel = file.relative_to(proj)
        return proj / rel.parts[0]

    @staticmethod
    def iter_smali_files(proj: Path) -> Iterator[Path]:
        for sd in sorted(proj.glob("smali*")):
            if not sd.is_dir():
                continue
            for dp, dn, files in os.walk(sd):
                dn.sort()
                for f in sorted(files):
                    if f.endswith(".smali"):
                        yield Path(dp) / f

    def rebuild(self) -> Path:
        self._need_project()
        cmd = self.tools.apktool_cmd()
        dist = self.proj / "dist"
        dist.mkdir(exist_ok=True)
        out = dist / f"{self.apk_path.stem}-smalix-rebuilt.apk"
        self.log(f"=== REBUILD {self.proj.name} ===", "info")
        args = cmd + ["b", str(self.proj), "-o", str(out)]
        res = self.run_cmd(args)
        if res.rc != 0 and self._build_framework_error(res.lines):
            for desc, action in self._framework_steps():
                self.log(f"Build failed because the Android framework is too old - {desc} "
                         f"and retrying ...", "info")
                if not action():
                    continue
                res = self.run_cmd(args)
                if res.rc == 0:
                    break
        if res.rc != 0 or not out.exists():
            if self._build_framework_error(res.lines):
                raise ToolError("build failed due to the framework / manifest. Use Tools -> 'Fix framework', "
                                "then Open APK again so the manifest is decoded with the new framework.")
            raise ToolError("apktool build failed - fix the errors above and retry")
        self.last_built = out
        self.log(f"Built {out}", "ok")
        self.post(self.on_status, f"Built {out.name}")
        return out

    # -- signing -----------------------------------------------------------
    def _keystore(self, random_key: bool) -> tuple:
        keytool = self.tools.require("keytool")
        if random_key:
            tmp = Path(tempfile.mkdtemp(prefix="smalix_ks_"))
            ks, pw, alias = tmp / "random.keystore", secrets.token_hex(8), "k" + secrets.token_hex(3)
            dn = f"CN={secrets.token_hex(4)}, OU=Android, O=Android, L=US, ST=US, C=US"
            self.log("Generating random signing key ...", "dim")
        else:
            dbg = Path.home() / ".android" / "debug.keystore"
            if dbg.exists():
                return dbg, "android", "androiddebugkey", "android"
            ks, pw, alias = DEBUG_KEYSTORE, "android", "androiddebugkey"
            dn = "CN=Android Debug, OU=Android, O=US, L=US, ST=US, C=US"
            if ks.exists():
                return ks, pw, alias, pw
            APP_DIR.mkdir(parents=True, exist_ok=True)
            self.log("Creating debug keystore ...", "dim")
        res = self.run_cmd([keytool, "-genkeypair", "-keystore", str(ks), "-storepass", pw, "-keypass", pw,
                            "-alias", alias, "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
                            "-dname", dn], quiet=True)
        if res.rc != 0 or not ks.exists():
            raise ToolError("keytool failed to create keystore")
        return ks, pw, alias, pw

    def sign_align(self, random_key: bool) -> Path:
        self._need_project()
        zipalign = self.tools.require("zipalign")
        apksigner = self.tools.require("apksigner")
        if not self.last_built or not self.last_built.exists():
            raise ToolError("No rebuilt APK yet - click 'Rebuild' first (or use 'Rebuild - Sign').")
        src = self.last_built
        dist = self.proj / "dist"
        dist.mkdir(exist_ok=True)
        stem = self.apk_path.stem
        aligned = dist / f"{stem}-smalix-aligned.apk"
        signed = dist / f"{stem}-smalix-aligned-debugSigned.apk"
        for f in (aligned, signed):
            if f.exists():
                f.unlink()

        self.log("=== ZIPALIGN ===", "info")
        if self.run_cmd([zipalign, "-f", "-p", "4", str(src), str(aligned)]).rc != 0 or not aligned.exists():
            raise ToolError("zipalign failed")

        ks, pw, alias, kpw = self._keystore(random_key)
        self.log("=== APKSIGNER ===", "info")
        res = self.run_cmd([apksigner, "sign", "--ks", str(ks), "--ks-pass", f"pass:{pw}",
                            "--key-pass", f"pass:{kpw}", "--ks-key-alias", alias,
                            "--out", str(signed), str(aligned)])
        if res.rc != 0 or not signed.exists():
            raise ToolError("apksigner sign failed")
        self.log("- sign success", "ok")

        self.log("\nVERIFY", "info")
        h = hashlib.sha256()
        with open(signed, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        self.log(f"file: {signed} ({signed.stat().st_size / 1048576:.2f} MiB)")
        self.log(f"checksum: {h.hexdigest()} (sha256)")
        if self.run_cmd([zipalign, "-c", "4", str(signed)], quiet=True).rc == 0:
            self.log("- zipalign verified", "ok")
        self.run_cmd([apksigner, "verify", "-v", "--print-certs", str(signed)])
        Path(str(signed) + ".idsig").unlink(missing_ok=True)
        self.log(f"\nDONE: {signed}", "ok")
        self.post(self.on_status, f"Done: {signed}")
        return signed

    def clean(self) -> None:
        self._need_project()
        for d in ("build", "dist"):
            p = self.proj / d
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
                self.log(f"Removed {p}", "info")
        self.last_built = None
        self.log("Clean finished.", "ok")
        self.post(self.on_status, "Cleaned build / dist folders")

    # -- JADX --------------------------------------------------------------
    def start_jadx(self) -> None:
        """Decompile to Java in the background; the UI is told when it finishes."""
        jadx = self.tools.get("jadx")
        if not jadx or not self.apk_path or not self.proj:
            return
        apk, jdir = self.apk_path, self.proj.parent / (self.apk_path.stem + "_jadx")
        self.log("JADX is decompiling in the background - the Java pane updates when it finishes.", "info")

        def work() -> None:
            self.run_cmd([jadx, "-d", str(jdir), "--no-res", "--show-bad-code", "--add-debug-lines", "-q",
                          str(apk)], quiet=True)
            if (jdir / "sources").exists():
                self.jadx_dir = jdir
                self.log("JADX finished - Java view is now real decompiled code.", "ok")
                self.post(self.on_jadx_ready)
            else:
                self.log("JADX produced no output.", "err")

        self.run_bg(work)

    def download_jadx(self) -> None:
        import urllib.request
        import zipfile
        hdr = {"User-Agent": "SmaliX-Desktop"}
        try:
            self.log("Fetching latest JADX release info ...", "info")
            req = urllib.request.Request("https://api.github.com/repos/skylot/jadx/releases/latest", headers=hdr)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            asset = next((a for a in data.get("assets", [])
                          if re.fullmatch(r"jadx-[\d.]+\.zip", a["name"])), None)
            if not asset:
                raise ToolError("no jadx-x.y.z.zip asset in the latest release")
            JADX_HOME.mkdir(parents=True, exist_ok=True)
            zpath = JADX_HOME / asset["name"]
            self.log(f"Downloading {asset['name']} ({asset['size'] / 1048576:.0f} MB) ...", "info")
            req = urllib.request.Request(asset["browser_download_url"], headers=hdr)
            with urllib.request.urlopen(req, timeout=120) as r, open(zpath, "wb") as fh:
                shutil.copyfileobj(r, fh)
            cur = JADX_HOME / "current"
            if cur.exists():
                shutil.rmtree(cur, ignore_errors=True)
            with zipfile.ZipFile(zpath) as z:
                z.extractall(cur)
            zpath.unlink(missing_ok=True)
            exe = cur / "bin" / ("jadx.bat" if IS_WIN else "jadx")
            if not exe.exists():
                raise ToolError("jadx executable not found after extraction")
            if not IS_WIN:
                exe.chmod(0o755)
            self.tools.paths["jadx"] = str(exe)
            self.tools.save()
            self.log(f"JADX installed: {exe}", "ok")
        except Exception as e:  # network / zip / permission problems
            self.log(f"JADX download failed: {e}", "err")
            return
        self.start_jadx()

    # ------------------------------------------------------------------ #
    #  Search helpers (run through run_bg)
    # ------------------------------------------------------------------ #
    def find_callers(self, ref: str) -> list:
        """All smali lines that reference `ref` (excluding the .method definition)."""
        results: list = []
        if not self.proj:
            return results
        for p in self.iter_smali_files(self.proj):
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                    for n, line in enumerate(fh, 1):
                        if ref in line and not line.startswith(".method"):
                            results.append((p, n, line.strip()))
            except OSError:
                continue
        return results

    def search_text(self, query: str, limit: int = 3000) -> list:
        hits: list = []
        proj = self.proj
        if not proj:
            return hits
        q = query.lower()
        exts = (".smali", ".xml", ".yml", ".json", ".txt")
        for dp, dn, files in os.walk(proj):
            if Path(dp) == proj:
                dn[:] = [d for d in dn if d not in ("build", "dist")]
            for f in files:
                if not f.endswith(exts):
                    continue
                p = Path(dp) / f
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                        for n, line in enumerate(fh, 1):
                            if q in line.lower():
                                hits.append((p, n, line.strip()[:300]))
                                if len(hits) >= limit:
                                    return hits
                except OSError:
                    continue
        return hits
