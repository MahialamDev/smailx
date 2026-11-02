#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_mapper.py - map a clicked JADX (Java) line to the matching smali line.

JADX and baksmali output share no line table, so the mapping is *calculated*:

  1. Find the Java method that contains the clicked line (brace matching) and the
     smali method it came from (name + parameter count/types; honours the
     "renamed from: x" comments JADX writes for obfuscated names).  Methods of
     inner / anonymous classes are looked up in the sibling  Outer$N.smali  files.
  2. Inside that method, try in order
        a. debug line  - a  /* line: N */  comment on the Java line (JADX option
                         --add-debug-lines) matched with the smali  `.line N`
        b. token match - string literals, called method names, `new X(`, field
                         accesses, int constants and return/if/throw keywords are
                         searched in the smali instructions and scored
        c. position    - the relative position of the Java line inside its method
  3. The result also carries a small block (first matched line .. the call that
     consumes it) so the UI can flash the whole statement.

The module is pure Python (no GUI) and works on lists of text lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from smali_utils import java_type, parse_method_header, split_descriptors

CONTROL = {"if", "for", "while", "switch", "catch", "synchronized", "return", "throw", "new", "else",
           "do", "try", "this", "super", "assert", "case", "instanceof", "yield"}
MODIFIERS = {"public", "private", "protected", "static", "final", "synchronized", "native",
             "abstract", "default", "strictfp"}
BAD_PREFIX_WORDS = {"return", "new", "throw", "else", "case", "assert", "yield"}

_STR_RE = re.compile(r'"(?:\\.|[^"\\])*"')
_CHR_RE = re.compile(r"'(?:\\.|[^'\\])'")
_BLOCK_CMT = re.compile(r"/\*.*?\*/")
_DBG_RE = re.compile(r"/\*[^*]*?\bline\s*[:=]?\s*(\d+)[^*]*\*/", re.I)
_CLASS_RE = re.compile(r"\b(?:class|interface|enum)\s+([A-Za-z_$][\w$]*)")
_METHOD_RE = re.compile(
    r"^(?P<prefix>[\w$<>\[\],.?@\s]*?)\b(?P<name>[A-Za-z_$][\w$]*)\s*\((?P<params>[^()]*)\)"
    r"\s*(?:throws\s+[\w.$,\s]+)?\{\s*$")
_FIELD_RE = re.compile(r"^(?:[\w$<>\[\],.?]+\s+)+([A-Za-z_$][\w$]*)\s*(?:=[^;]*)?;\s*$")


# --------------------------------------------------------------------------- #
#  Result types
# --------------------------------------------------------------------------- #
@dataclass
class MapResult:
    line: int                      # 1-based smali line to scroll to
    block_start: int
    block_end: int
    confidence: str                # debug-line | match | method | approx | class | none
    note: str
    file_key: Optional[str] = None  # stem of a sibling smali file (inner class) if not the main one
    score: float = 0.0


@dataclass
class SmaliMethod:
    name: str
    params: list
    ret: str
    start: int                     # line of .method   (1-based)
    end: int                       # line of .end method


@dataclass
class JavaSpan:
    start: int                     # 0-based
    end: int
    name: str
    smali_name: str
    nparams: int
    ptypes: list
    inner: Optional[str]           # name of the enclosing inner class, if any


# --------------------------------------------------------------------------- #
#  Text helpers
# --------------------------------------------------------------------------- #
def _unescape(s: str) -> str:
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    return (s.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")
             .replace("\\r", "\r").replace("\\'", "'").replace("\\\\", "\\"))


def strip_code(line: str) -> str:
    """Remove string/char literals and comments so braces and calls can be analysed."""
    line = _STR_RE.sub('""', line)
    line = _CHR_RE.sub("''", line)
    line = _BLOCK_CMT.sub("", line)
    return re.sub(r"//.*$", "", line)


def _span_end(lines: list, start: int) -> Optional[int]:
    depth, seen = 0, False
    for i in range(start, len(lines)):
        for ch in strip_code(lines[i]):
            if ch == "{":
                depth += 1
                seen = True
            elif ch == "}":
                depth -= 1
        if seen and depth <= 0:
            return i
    return None


def _split_params(s: str) -> list:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "<(":
            depth += 1
        elif ch in ">)":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [p.strip() for p in out]


def _param_type(p: str) -> str:
    p = re.sub(r"@\w+(\([^)]*\))?\s*", "", p)
    p = re.sub(r"\bfinal\s+", "", p).strip()
    parts = p.rsplit(None, 1)
    t = parts[0] if len(parts) == 2 else p
    t = re.sub(r"<.*>", "", t)
    arr = t.count("[]") + (1 if "..." in t else 0)
    t = t.replace("[]", "").replace("...", "").strip().rsplit(".", 1)[-1]
    return t + "[]" * arr


# --------------------------------------------------------------------------- #
#  Java side
# --------------------------------------------------------------------------- #
def _enclosing_class(lines: list, idx: int) -> Optional[str]:
    for i in range(idx, -1, -1):
        m = _CLASS_RE.search(strip_code(lines[i]))
        if m:
            end = _span_end(lines, i)
            if end is not None and end >= idx:
                return m.group(1)
    return None


def java_method_span(lines: list, idx: int) -> Optional[JavaSpan]:
    """The method (or static initialiser) whose body contains line `idx` (0-based)."""
    class_names = set()
    first_class = None
    for l in lines:
        m = _CLASS_RE.search(strip_code(l))
        if m:
            class_names.add(m.group(1))
            first_class = first_class or m.group(1)

    for i in range(min(idx, len(lines) - 1), -1, -1):
        code = strip_code(lines[i]).strip()
        name = smali_name = None
        params = ""
        if code == "static {":
            name = smali_name = "<clinit>"
        else:
            m = _METHOD_RE.match(code)
            if not m or m.group("name") in CONTROL:
                continue
            words = m.group("prefix").split()
            if any(w in BAD_PREFIX_WORDS for w in words):
                continue
            ret_words = [w for w in words if w not in MODIFIERS and not w.startswith("@")]
            name, params = m.group("name"), m.group("params")
            if not ret_words:                       # constructor
                if name not in class_names:
                    continue
                smali_name = "<init>"
            else:
                smali_name = name
        end = _span_end(lines, i)
        if end is None or end < idx:
            continue
        for k in (i - 1, i - 2, i - 3):             # JADX: /* renamed from: a */
            if k >= 0:
                rm = re.search(r"renamed from:\s*([\w$<>]+)", lines[k])
                if rm:
                    smali_name = rm.group(1)
                    break
        plist = _split_params(params)
        encl = _enclosing_class(lines, i)
        inner = encl if encl and encl != first_class else None
        return JavaSpan(i, end, name, smali_name, len(plist), [_param_type(p) for p in plist], inner)
    return None


def java_tokens(line: str, field_names: set) -> list:
    """[(kind, value, weight)] describing what a Java line does."""
    nocmt = _BLOCK_CMT.sub("", line)
    code = strip_code(line)
    toks: list = []
    for s in _STR_RE.findall(nocmt):
        v = _unescape(s[1:-1])
        if v.strip():
            toks.append(("str", v, 6.0))
    for m in re.finditer(r"(?<![\w$])(new\s+)?([A-Za-z_$][\w$]*)(?:<[^<>()]*>)?\s*\(", code):
        if m.group(1):
            toks.append(("new", m.group(2), 3.0))
        elif m.group(2) not in CONTROL:
            toks.append(("call", m.group(2), 3.0))
    for m in re.finditer(r"(?<=\.)([A-Za-z_$][\w$]*)(?!\s*[\w$(])", code):
        toks.append(("field", m.group(1), 3.0))
    for m in re.finditer(r"(?<![\w$.])([A-Za-z_$][\w$]*)(?!\s*[\w$(])", code):
        if m.group(1) in field_names:
            toks.append(("field", m.group(1), 3.0))
    for m in re.finditer(r"(?<![\w$.])(-?\d+)(?![\w$.])", code):
        toks.append(("num", m.group(1), 1.5))
    for word, op in (("return", "return"), ("throw", "throw"), ("if", "if-"), ("while", "if-"),
                     ("for", "if-"), ("switch", "packed-switch"), ("instanceof", "instance-of")):
        if re.search(rf"(?<![\w$]){word}(?![\w$])", code):
            toks.append(("kw", op, 1.0))
    return toks


# --------------------------------------------------------------------------- #
#  Smali side
# --------------------------------------------------------------------------- #
def parse_smali_methods(lines: list) -> list:
    out, cur = [], None
    for i, s in enumerate(lines, 1):
        if s.startswith(".method"):
            try:
                _fl, name, params, ret = parse_method_header(s.strip())
            except Exception:
                cur = None
                continue
            cur = SmaliMethod(name, split_descriptors(params), ret, i, i)
        elif s.startswith(".end method") and cur:
            cur.end = i
            out.append(cur)
            cur = None
    return out


def _smali_fields(lines: list) -> set:
    out = set()
    for l in lines:
        m = re.match(r"\.field\s+(?:[\w-]+\s+)*([^\s:]+):", l)
        if m:
            out.add(m.group(1))
    return out


def _match_methods(span: JavaSpan, methods: list) -> list:
    named = [m for m in methods if m.name == span.smali_name]
    if not named:
        return []
    exact = [m for m in named if len(m.params) == span.nparams]
    if not exact:   # inner-class / enum constructors get hidden extra parameters
        closest = min(abs(len(m.params) - span.nparams) for m in named)
        exact = [m for m in named if abs(len(m.params) - span.nparams) == closest]
    if len(exact) > 1:
        def tscore(m: SmaliMethod) -> int:
            return sum(1 for jt, d in zip(span.ptypes, m.params) if jt == java_type(d).rsplit(".", 1)[-1])
        best = max(tscore(m) for m in exact)
        exact = [m for m in exact if tscore(m) == best]
    return exact


def _line_score(tok: tuple, op: str, s: str) -> float:
    kind, val, w = tok
    if kind == "str":
        if op.startswith("const-string"):
            m = _STR_RE.search(s)
            return w if m and _unescape(m.group(0)[1:-1]) == val else 0.0
    elif kind == "call":
        return w if op.startswith("invoke") and f"->{val}(" in s else 0.0
    elif kind == "new":
        if op == "new-instance" and re.search(rf"[/$L]{re.escape(val)};\s*$", s):
            return w
        if op.startswith("invoke-direct") and f"{val};-><init>" in s:
            return w
    elif kind == "field":
        return w if op.startswith(("iget", "iput", "sget", "sput")) and f"->{val}:" in s else 0.0
    elif kind == "num":
        if op.startswith("const") and not op.startswith(("const-string", "const-class")):
            n = int(val)
            h = f"-0x{-n:x}" if n < 0 else f"0x{n:x}"
            return w if re.search(rf"(?<![\w]){re.escape(h)}(?![0-9a-fA-F])", s) else 0.0
    elif kind == "kw":
        return w if op.startswith(val) else 0.0
    return 0.0


def _is_instruction(s: str) -> bool:
    return bool(s) and s[0] not in ".#:"


# --------------------------------------------------------------------------- #
#  Mapping inside one method
# --------------------------------------------------------------------------- #
def _map_within(span: JavaSpan, jlines: list, jidx: int, slines: list, m: SmaliMethod,
                fields: set, note_prefix: str = "") -> MapResult:
    ms, me = m.start, m.end
    body_s, body_e = ms + 1, max(ms + 1, me - 1)

    if jidx == span.start:
        return MapResult(ms, ms, ms, "method", note_prefix + "method declaration")
    if jidx == span.end:
        return MapResult(me, me, me, "method", note_prefix + "end of method")

    # a. debug line comment from JADX (--add-debug-lines)
    for back in range(0, 3):
        k = jidx - back
        if k <= span.start:
            break
        dm = _DBG_RE.search(jlines[k])
        if dm:
            n = int(dm.group(1))
            for i in range(body_s, body_e + 1):
                lm = re.match(r"\s*\.line\s+(\d+)\s*$", slines[i - 1])
                if lm and int(lm.group(1)) == n:
                    end = i
                    for j in range(i + 1, min(body_e, i + 10) + 1):
                        if re.match(r"\s*\.line\s+\d+", slines[j - 1]):
                            break
                        end = j
                    note = f"debug line {n}" + ("" if back == 0 else " (nearby)")
                    return MapResult(i, i, end, "debug-line", note_prefix + note, None, 100.0)
            break

    # position estimate
    body_len = max(1, span.end - span.start - 1)
    frac = min(1.0, max(0.0, (jidx - span.start - 1) / body_len))
    expected = body_s + round(frac * max(0, body_e - body_s))
    width = max(1, body_e - body_s)

    # b. token matching
    toks = java_tokens(jlines[jidx], fields)
    scores: dict = {}
    for i in range(body_s, body_e + 1):
        s = slines[i - 1].strip()
        if not _is_instruction(s):
            continue
        op = s.split()[0]
        sc, kinds = 0.0, []
        for tk in toks:
            v = _line_score(tk, op, s)
            if v:
                sc += v
                kinds.append(tk[0])
        if sc:
            scores[i] = (sc, kinds)

    def prox(i: int) -> float:
        return 1.0 - min(1.0, abs(i - expected) / width)

    strong = [i for i, (sc, _k) in scores.items() if sc >= 3.0]
    if strong:
        best = max(strong, key=lambda i: (scores[i][0] + prox(i), -abs(i - expected)))
        end = best
        for j in range(best + 1, min(body_e, best + 8) + 1):
            if j in scores and any(k in ("call", "new", "field") for k in scores[j][1]):
                end = j
                break
        kinds = sorted({k for k in scores[best][1]} | ({"call"} if end != best else set()))
        return MapResult(best, best, end, "match", note_prefix + "matched " + " + ".join(kinds),
                         None, scores[best][0])

    weak = [i for i in scores if abs(i - expected) <= max(6, width // 4)]
    if weak:
        best = max(weak, key=lambda i: (scores[i][0] + prox(i), -abs(i - expected)))
        return MapResult(best, best, best, "approx", note_prefix + "approximate (weak match)", None, 0.5)

    # c. relative position
    return MapResult(expected, expected, expected, "approx",
                     note_prefix + "approximate (position inside method)", None, 0.0)


def _map_outside_method(jlines: list, jidx: int, slines: list, fields: set) -> MapResult:
    code = strip_code(jlines[jidx]).strip()
    class_line = next((i for i, l in enumerate(slines, 1) if l.startswith(".class")), 1)
    if not code or code.startswith(("package ", "import ")) or _CLASS_RE.search(code):
        return MapResult(class_line, class_line, class_line, "class", "class declaration")
    fm = _FIELD_RE.match(code)
    if fm:
        name = fm.group(1)
        for i, l in enumerate(slines, 1):
            if re.match(rf"\.field\s+(?:[\w-]+\s+)*{re.escape(name)}:", l):
                return MapResult(i, i, i, "match", f"field {name}")
    # no enclosing method: search the whole file
    span = JavaSpan(-1, len(jlines), "?", "?", 0, [], None)
    whole = SmaliMethod("?", [], "V", 0, len(slines) + 1)
    r = _map_within(span, jlines, jidx, slines, whole, fields, "no enclosing method - ")
    return r


# --------------------------------------------------------------------------- #
#  Public API
# --------------------------------------------------------------------------- #
def map_java_to_smali(java_lines: list, java_line: int, smali_lines: list,
                      get_siblings: Optional[Callable[[], dict]] = None) -> MapResult:
    """
    java_line     1-based line clicked in the Java view
    smali_lines   lines of the smali file that owns the Java view (outer class)
    get_siblings  optional callable -> {stem: lines} of Outer$*.smali files (inner / anonymous
                  classes); only called when the method is not found in the main file
    """
    jidx = java_line - 1
    if not (0 <= jidx < len(java_lines)):
        return MapResult(1, 1, 1, "none", "click is outside the code")
    fields = _smali_fields(smali_lines)
    span = java_method_span(java_lines, jidx)
    if span is None:
        return _map_outside_method(java_lines, jidx, smali_lines, fields)

    cands = _match_methods(span, parse_smali_methods(smali_lines))
    if cands:
        return _map_within(span, java_lines, jidx, smali_lines, cands[0], fields)

    if get_siblings:
        best_pair = None
        for key, lines in get_siblings().items():
            cs = _match_methods(span, parse_smali_methods(lines))
            if not cs:
                continue
            r = _map_within(span, java_lines, jidx, lines, cs[0], _smali_fields(lines), f"{key}: ")
            r.file_key = key
            bonus = 2.0 if span.inner and key.endswith("$" + span.inner) else 0.0
            if best_pair is None or r.score + bonus > best_pair[0]:
                best_pair = (r.score + bonus, r)
        if best_pair:
            return best_pair[1]

    r = _map_within(span, java_lines, jidx, smali_lines, SmaliMethod("?", [], "V", 0, len(smali_lines) + 1),
                    fields, f"method '{span.name}' not found in smali - ")
    return r
