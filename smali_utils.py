#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smali_utils.py - pure helpers for smali / Java text.

  * descriptor parsing and method-header parsing
  * a smali -> Java outline generator (fallback when JADX is not installed)
  * opcode / directive explanations
  * syntax highlighting for Java and smali (works on any tk.Text-like widget)
  * small patch builders used by the Patch Assistant

No GUI toolkit is imported here; widgets are duck-typed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from config import C, MONO

if TYPE_CHECKING:  # pragma: no cover
    import tkinter as tk

# --------------------------------------------------------------------------- #
#  File helpers
# --------------------------------------------------------------------------- #
SKIP_PREFIX = ("android", "kotlin", "java", "javax", "com.google", "com.android", "org.jetbrains",
               "org.intellij", "dalvik", "j$", "sun", "org.apache", "okhttp3", "okio")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(4096)
    except OSError:
        return False


def guess_package(pkgs: list[str]) -> str:
    """Best guess of the app's own package from its smali packages."""
    counts: dict[str, int] = {}
    for p in pkgs:
        if not p or p.startswith(SKIP_PREFIX):
            continue
        key = ".".join(p.split(".")[:3])
        counts[key] = counts.get(key, 0) + 1
    return max(counts, key=counts.get) if counts else ""


# --------------------------------------------------------------------------- #
#  Descriptors / headers
# --------------------------------------------------------------------------- #
PRIMS = {"V": "void", "Z": "boolean", "B": "byte", "S": "short", "C": "char",
         "I": "int", "J": "long", "F": "float", "D": "double"}


def split_descriptors(s: str) -> list[str]:
    """'Ljava/lang/String;I[I' -> ['Ljava/lang/String;', 'I', '[I']"""
    out, i = [], 0
    while i < len(s):
        start = i
        while i < len(s) and s[i] == "[":
            i += 1
        if i < len(s) and s[i] == "L":
            i = s.index(";", i) + 1
        else:
            i += 1
        out.append(s[start:i])
    return out


def short_type(t: str) -> str:
    """Tree-label style: objects -> simple name, primitives -> raw code."""
    if t.startswith("["):
        return short_type(t[1:]) + "[]"
    if t.startswith("L"):
        return t[1:-1].split("/")[-1].split("$")[-1]
    return t


def java_type(t: str) -> str:
    if t.startswith("["):
        return java_type(t[1:]) + "[]"
    if t.startswith("L"):
        return t[1:-1].replace("/", ".").replace("$", ".")
    return PRIMS.get(t, t)


def parse_method_header(header: str):
    """'.method private rid(Ljava/lang/String;)I' -> (flags, name, params, ret)"""
    rest = header[len(".method"):].strip()
    lp, rp = rest.index("("), rest.index(")")
    head = rest[:lp].split()
    return head[:-1], head[-1], rest[lp + 1:rp], rest[rp + 1:].strip()


def method_label(header: str) -> str:
    try:
        _, name, params, ret = parse_method_header(header)
        return f"{name}({','.join(short_type(t) for t in split_descriptors(params))}){short_type(ret)}"
    except Exception:
        return header


def smali_class_name(text: str) -> Optional[str]:
    m = re.search(r"^\.class\s+(?:[\w-]+\s+)*(L[^;\s]+;)", text, re.M)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
#  smali -> Java outline (JADX fallback)
# --------------------------------------------------------------------------- #
def smali_to_java_outline(text: str) -> str:
    cls = smali_class_name(text)
    if not cls:
        return "// Not a smali class file"
    flags = re.search(r"^\.class\s+((?:[\w-]+\s+)*)L", text, re.M).group(1).split()
    full = cls[1:-1].replace("/", ".")
    pkg, _, simple = full.rpartition(".")
    sup = re.search(r"^\.super\s+(L[^;]+;)", text, re.M)
    ifaces = re.findall(r"^\.implements\s+(L[^;]+;)", text, re.M)
    mods = [f for f in flags if f in ("public", "final", "abstract")]
    kind = "interface" if "interface" in flags else "enum" if "enum" in flags else "class"
    imports: set[str] = set()

    def reg(t: str) -> str:
        base = t.lstrip("[")
        if base.startswith("L"):
            fq = base[1:-1].replace("/", ".").replace("$", ".")
            if not fq.startswith("java.lang.") and "." in fq:
                imports.add(fq)
            return fq.rsplit(".", 1)[-1] + "[]" * t.count("[")
        return java_type(t)

    body: list[str] = []
    for m in re.finditer(r"^\.field\s+(.*?)\s*(\S+):(\S+?)(?:\s*=\s*(.+))?$", text, re.M):
        fl, name, typ, val = m.groups()
        fl = " ".join(f for f in fl.split() if f in ("public", "private", "protected", "static", "final"))
        line = " ".join(x for x in (fl, reg(typ), name) if x)
        body.append("    " + line + (f" = {val};" if val else ";"))
    if body:
        body.append("")

    for m in re.finditer(r"^\.method\s+.*$", text, re.M):
        header = m.group(0)
        end = text.find(".end method", m.end())
        chunk = text[m.end():end if end != -1 else len(text)]
        n = len([l for l in chunk.splitlines()
                 if l.strip() and not l.strip().startswith((".", "#", ":"))])
        fl, name, params, ret = parse_method_header(header)
        mods2 = " ".join(f for f in fl if f in ("public", "private", "protected", "static", "final",
                                                "abstract", "native", "synchronized"))
        plist = ", ".join(f"{reg(t)} p{i}" for i, t in enumerate(split_descriptors(params)))
        if name == "<clinit>":
            body.append(f"    static {{ /* {n} smali instructions */ }}\n")
            continue
        if name == "<init>":
            sig_head = " ".join(x for x in (mods2, simple) if x)
        else:
            sig_head = " ".join(x for x in (mods2, reg(ret), name) if x)
        sig = f"    {sig_head}({plist})"
        if "abstract" in fl or "native" in fl:
            body.append(sig + ";\n")
        else:
            body.append(sig + " {")
            body.append(f"        /* {n} smali instructions - install JADX for full source */")
            body.append("    }\n")

    decl = " ".join(mods + [kind, simple])
    if sup and sup.group(1) != "Ljava/lang/Object;":
        decl += f" extends {reg(sup.group(1))}"
    if ifaces:
        decl += f" implements {', '.join(reg(i) for i in ifaces)}"
    head: list[str] = []
    if pkg:
        head.append(f"package {pkg};\n")
    for imp in sorted(imports):
        head.append(f"import {imp};")
    if imports:
        head.append("")
    head.append("/* smali outline (JADX not available) */")
    return "\n".join(head) + "\n" + decl + " {\n" + "\n".join(body) + "}\n"


# --------------------------------------------------------------------------- #
#  Explain
# --------------------------------------------------------------------------- #
OPCODES = {
    "invoke-virtual": "Call a virtual (overridable) instance method on an object.",
    "invoke-direct": "Call a constructor or private method (no virtual dispatch).",
    "invoke-static": "Call a static method (no object instance).",
    "invoke-interface": "Call an interface method on an object.",
    "invoke-super": "Call the superclass implementation of a method.",
    "invoke-custom": "Call a dynamically linked call site (lambda / invokedynamic).",
    "move-result-object": "Store the object returned by the previous invoke into a register.",
    "move-result-wide": "Store the 64-bit value returned by the previous invoke.",
    "move-result": "Store the value returned by the previous invoke into a register.",
    "move-exception": "Store the caught exception into a register (first instruction of a handler).",
    "move-object": "Copy an object reference from one register to another.",
    "move": "Copy a value from one register to another.",
    "const-string": "Load a string constant into a register.",
    "const-class": "Load a Class object into a register.",
    "const/4": "Load a 4-bit signed constant (-8..7) into a register.",
    "const/16": "Load a 16-bit signed constant into a register.",
    "const/high16": "Load a constant into the high 16 bits of a register.",
    "const-wide": "Load a 64-bit constant into a register pair.",
    "const": "Load a 32-bit constant into a register.",
    "new-instance": "Allocate a new object (constructor must still be called with invoke-direct).",
    "new-array": "Allocate a new array of the given type and length.",
    "check-cast": "Cast a reference to a type; throws ClassCastException on failure.",
    "instance-of": "Set register to 1 if the object is an instance of the type, else 0.",
    "iget": "Read an instance field into a register.",
    "iput": "Write a register into an instance field.",
    "sget": "Read a static field into a register.",
    "sput": "Write a register into a static field.",
    "aget": "Read an array element into a register.",
    "aput": "Write a register into an array element.",
    "array-length": "Store the length of an array into a register.",
    "if-eqz": "Jump to label if register == 0 / false / null.",
    "if-nez": "Jump to label if register != 0 / true / non-null.",
    "if-ltz": "Jump to label if register < 0.",
    "if-gez": "Jump to label if register >= 0.",
    "if-gtz": "Jump to label if register > 0.",
    "if-lez": "Jump to label if register <= 0.",
    "if-eq": "Jump to label if both registers are equal.",
    "if-ne": "Jump to label if registers are not equal.",
    "if-lt": "Jump to label if first < second.",
    "if-ge": "Jump to label if first >= second.",
    "if-gt": "Jump to label if first > second.",
    "if-le": "Jump to label if first <= second.",
    "goto": "Unconditional jump to a label.",
    "packed-switch": "Jump table for consecutive integer cases.",
    "sparse-switch": "Jump table for sparse integer cases.",
    "return-void": "Return from a void method.",
    "return-object": "Return an object reference.",
    "return-wide": "Return a 64-bit value.",
    "return": "Return a 32-bit value.",
    "throw": "Throw the exception object in the register.",
    "nop": "Do nothing.",
    "add-int": "Integer addition.", "sub-int": "Integer subtraction.", "mul-int": "Integer multiplication.",
    "div-int": "Integer division.", "rem-int": "Integer remainder.", "xor-int": "Bitwise XOR.",
    "and-int": "Bitwise AND.", "or-int": "Bitwise OR.", "shl-int": "Shift left.", "shr-int": "Shift right.",
    "neg-int": "Negate an integer.", "not-int": "Bitwise NOT.",
    "int-to-long": "Widen int to long.", "int-to-char": "Narrow int to char.",
    "cmp": "Compare two values: -1, 0 or 1.",
}

DIRECTIVES = {
    ".class": "Declares the class name and access flags.",
    ".super": "Declares the superclass.",
    ".implements": "Declares an implemented interface.",
    ".source": "Original source file name (debug info).",
    ".field": "Declares a field.",
    ".method": "Start of a method (flags, name, parameter and return descriptors).",
    ".end method": "End of the current method.",
    ".locals": "Number of local registers (v0..vN). Parameters follow as p0..pN.",
    ".registers": "Total register count (locals + parameters).",
    ".param": "Names / annotates a method parameter.",
    ".line": "Maps the following instructions to a source line number (debug info).",
    ".prologue": "Marks the end of the method prologue (debug info).",
    ".annotation": "Start of an annotation block.",
    ".end annotation": "End of an annotation block.",
    ".catch": "Declares an exception handler range.",
    ".catchall": "Declares a catch-all handler range.",
    ".array-data": "Inline array initialisation data.",
    ".packed-switch": "Packed switch data table.",
    ".sparse-switch": "Sparse switch data table.",
    ".restart": "Restarts a local variable (debug info).",
    ".local": "Declares a named local variable (debug info).",
}


def explain_line(line: str) -> str:
    s = line.strip()
    if not s:
        return "(empty line)"
    if s.startswith("#"):
        return "Comment - ignored by the assembler."
    if s.startswith(":"):
        return f"Label '{s}' - a jump target for goto / if-* / switch instructions."
    if s.startswith("."):
        key = next((d for d in sorted(DIRECTIVES, key=len, reverse=True) if s.startswith(d)), None)
        return f"Directive: {DIRECTIVES.get(key, 'Smali assembler directive.')}"
    op = s.split()[0].split(",")[0]
    desc = OPCODES.get(op)
    if desc is None:
        base = max((k for k in OPCODES if op.startswith(k)), key=len, default=None)
        desc = OPCODES.get(base, "Dalvik instruction.") if base else "Dalvik instruction."
    out = [f"Opcode  : {op}", f"Meaning : {desc}"]
    if op.startswith("invoke"):
        m = re.search(r"\{(.*?)\},\s*(L[^;]+;)->([^(]+)\((.*?)\)(\S+)", s)
        if m:
            regs, cls, name, params, ret = m.groups()
            plist = [java_type(t) for t in split_descriptors(params)]
            out += ["", f"Target  : {java_type(cls)}.{name}({', '.join(plist)}) -> {java_type(ret)}",
                    f"Registers: {regs or '(none)'}"]
    elif op == "const-string":
        m = re.match(r'const-string(?:/jumbo)?\s+(\S+),\s*(".*")', s)
        if m:
            out += ["", f"Loads the string {m.group(2)} into {m.group(1)}"]
    elif op.startswith(("iget", "iput", "sget", "sput")):
        m = re.search(r"(L[^;]+;)->([^:]+):(\S+)", s)
        if m:
            out += ["", f"Field   : {java_type(m.group(1))}.{m.group(2)} : {java_type(m.group(3))}"]
    elif op.startswith(("if-", "goto")):
        m = re.search(r":(\S+)\s*$", s)
        if m:
            out += ["", f"Branch target: :{m.group(1)}"]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
#  Patch builders (pure functions; the UI applies the result)
# --------------------------------------------------------------------------- #
_FLIP = {"eqz": "nez", "nez": "eqz", "ltz": "gez", "gez": "ltz", "gtz": "lez", "lez": "gtz",
         "eq": "ne", "ne": "eq", "lt": "ge", "ge": "lt", "gt": "le", "le": "gt"}
_IF_RE = re.compile(r"(\s*)if-(eqz|nez|ltz|gez|gtz|lez|eq|ne|lt|ge|gt|le)(\s.*)")


def return_patch_body(kind: str, ret: str) -> list[str]:
    """Replacement body for a method returning `ret`. Raises ValueError if not applicable."""
    if kind in ("true", "false"):
        if ret not in ("Z", "B", "S", "C", "I"):
            raise ValueError(f"Method returns '{ret}', not a boolean/int.")
        val = "0x1" if kind == "true" else "0x0"
        return ["    .locals 1", f"    const/4 v0, {val}", "    return v0"]
    if kind == "null":
        if not ret.startswith(("L", "[")):
            raise ValueError(f"Method returns '{ret}', not an object.")
        return ["    .locals 1", "    const/4 v0, 0x0", "    return-object v0"]
    if kind == "void":
        if ret != "V":
            raise ValueError(f"Method returns '{ret}', not void.")
        return ["    .locals 0", "    return-void"]
    raise ValueError(f"Unknown patch '{kind}'.")


def flip_if_line(line: str) -> Optional[tuple[str, str, str]]:
    """'if-eqz v0, :x' -> ('if-nez v0, :x', 'eqz', 'nez'); None if not an if-* line."""
    m = _IF_RE.match(line)
    if not m:
        return None
    new = _FLIP[m.group(2)]
    return f"{m.group(1)}if-{new}{m.group(3)}", m.group(2), new


# --------------------------------------------------------------------------- #
#  Syntax highlighting
# --------------------------------------------------------------------------- #
JAVA_KW = ("abstract assert boolean break byte case catch char class const continue default do double "
           "else enum extends final finally float for goto if implements import instanceof int interface "
           "long native new null package private protected public return short static strictfp super "
           "switch synchronized this throw throws transient try void volatile while true false").split()

JAVA_RE = re.compile(
    r"(?P<comment>//.*|/\*.*?\*/|/\*.*$)"
    r'|(?P<string>"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])\')'
    r"|(?P<annot>@\w+)"
    r"|(?P<kw>\b(?:" + "|".join(JAVA_KW) + r")\b)"
    r"|(?P<num>\b(?:0x[0-9a-fA-F]+|\d+)[lLfFdD]?\b)"
    r"|(?P<type>\b[A-Z]\w*\b)")

SMALI_RE = re.compile(
    r"(?P<comment>\#.*)"
    r'|(?P<string>"(?:\\.|[^"\\])*")'
    r"|(?P<directive>\.[a-z][\w-]*(?: method| annotation)?)"
    r"|(?P<label>:[\w$]+)"
    r"|(?P<cls>(?<![\w/])L[\w/$]+;)"
    r"|(?P<reg>\b[vp]\d+\b)"
    r"|(?P<num>-?\b0x[0-9a-fA-F]+[LlSsTt]?\b)")

SMALI_OP = re.compile(r"^\s*([a-z][\w\-/]*)")

_ALL_TAGS = ("kw", "type", "string", "comment", "num", "annot", "op_call", "op_const", "op_flow",
             "op_other", "directive", "label", "cls", "reg")


def configure_tags(txt: "tk.Text", mode: str) -> None:
    italic = (MONO[0], MONO[1], "italic")
    if mode == "java":
        txt.tag_configure("kw", foreground=C["pink"])
        txt.tag_configure("type", foreground="#e5e7eb")
        txt.tag_configure("string", foreground=C["str"])
        txt.tag_configure("comment", foreground="#5b6478", font=italic)
        txt.tag_configure("num", foreground=C["yellow"])
        txt.tag_configure("annot", foreground=C["yellow"])
    else:
        txt.tag_configure("op_call", foreground=C["cyan"])
        txt.tag_configure("op_const", foreground=C["orange"])
        txt.tag_configure("op_flow", foreground=C["pink"])
        txt.tag_configure("op_other", foreground="#c4b5fd")
        txt.tag_configure("directive", foreground=C["yellow"])
        txt.tag_configure("label", foreground="#fde68a")
        txt.tag_configure("cls", foreground="#93c5fd")
        txt.tag_configure("reg", foreground="#e5e7eb")
        txt.tag_configure("string", foreground=C["str"])
        txt.tag_configure("comment", foreground="#5b6478", font=italic)
        txt.tag_configure("num", foreground=C["yellow"])
    txt.tag_configure("curline", background=C["cur"])
    txt.tag_raise("sel")


def clear_syntax(txt: "tk.Text") -> None:
    for n in _ALL_TAGS:
        txt.tag_remove(n, "1.0", "end")


def highlight(txt: "tk.Text", mode: str, max_lines: int = 20000) -> None:
    clear_syntax(txt)
    content = txt.get("1.0", "end-1c")
    rx = JAVA_RE if mode == "java" else SMALI_RE
    for i, line in enumerate(content.split("\n"), 1):
        if i > max_lines:
            break
        if mode == "smali":
            m = SMALI_OP.match(line)
            if m:
                op = m.group(1)
                tag = ("op_call" if op.startswith("invoke") else
                       "op_const" if op.startswith("const") else
                       "op_flow" if op.startswith(("move", "return", "goto", "if-", "throw", "packed", "sparse"))
                       else "op_other")
                txt.tag_add(tag, f"{i}.{m.start(1)}", f"{i}.{m.end(1)}")
        for m in rx.finditer(line):
            if m.lastgroup:
                txt.tag_add(m.lastgroup, f"{i}.{m.start()}", f"{i}.{m.end()}")
