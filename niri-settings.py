#!/usr/bin/env python3
"""
niri-settings - a GTK4 / libadwaita settings app for niri.

* Reads and edits your real KDL files in place (comments and formatting are preserved).
* Works with a split config: config.kdl + cfg/animation.kdl, display.kdl, input.kdl, ...
* Every change is applied live (niri hot-reloads its config) but only becomes permanent
  when you press "Save now" in the bottom-right corner. "Discard" puts everything back.
* Import & Backup page: point it at a saved config and it moves/copies it to ~/.config/niri/.

Needs: python3, PyGObject, GTK >= 4.10, libadwaita >= 1.4   (Arch: python-gobject gtk4 libadwaita)
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

APP_ID = "dev.niri.Settings"


# ==========================================================================
# KDL parser + in-place editor
# ==========================================================================


# --------------------------------------------------------------------------- parsing


class ParseError(Exception):
    pass


_BARE_STOP = set(' \t\r\n;{}()=\\"\u00a0\ufeff')
_ESC = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", '"': '"', "\\": "\\", "/": "/", "s": " "}


def unescape(raw: str) -> str:
    body, out, i = raw[1:-1], [], 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            d = body[i + 1]
            if d == "u" and body[i + 2: i + 3] == "{":
                j = body.find("}", i + 3)
                if j != -1:
                    try:
                        out.append(chr(int(body[i + 3: j], 16)))
                        i = j + 1
                        continue
                    except ValueError:
                        pass
            out.append(_ESC.get(d, d))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def quote(s: str) -> str:
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return f'"{s}"'


def fmt(v) -> str:
    """Python value -> KDL token."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        s = f"{v:.4f}".rstrip("0")
        return s + "0" if s.endswith(".") else s
    return quote(str(v))


def _tok_value(raw: str):
    if raw.startswith('"'):
        return unescape(raw)
    if raw[:1] == "r" and raw[1:2] in ('"', "#") and raw.endswith(('"', "#")) and '"' in raw:
        h = len(raw) - len(raw.rstrip("#"))
        return raw[raw.index('"') + 1: len(raw) - 1 - h]
    if raw in ("true", "#true"):
        return True
    if raw in ("false", "#false"):
        return False
    if raw in ("null", "#null"):
        return None
    try:
        return int(raw.replace("_", ""))
    except ValueError:
        pass
    try:
        return float(raw.replace("_", ""))
    except ValueError:
        return raw


class Val:
    __slots__ = ("raw", "start", "end")

    def __init__(self, raw, start, end):
        self.raw, self.start, self.end = raw, start, end

    @property
    def value(self):
        return _tok_value(self.raw)


class Node:
    def __init__(self):
        self.name = ""
        self.start = self.end = 0
        self.name_start = self.name_end = 0
        self.args: list[Val] = []
        self.props: dict[str, Val] = {}
        self.children: list[Node] | None = None
        self.body_start = self.body_end = None  # offsets of '{' and '}'
        self.disabled = False  # slashdash (/-)
        self.parent: Node | None = None

    @property
    def arg0(self):
        return self.args[0].value if self.args else None

    def prop(self, key, default=None):
        v = self.props.get(key)
        return v.value if v is not None else default

    def kids(self, name=None):
        return [c for c in (self.children or []) if not c.disabled and (name is None or c.name == name)]

    def tail(self) -> int:
        return max([self.name_end] + [a.end for a in self.args] + [v.end for v in self.props.values()])


class _Parser:
    def __init__(self, text):
        self.t, self.i, self.n = text, 0, len(text)

    def skip_block(self):
        t, i, depth = self.t, self.i, 0
        while i < self.n:
            if t.startswith("/*", i):
                depth += 1
                i += 2
            elif t.startswith("*/", i):
                depth -= 1
                i += 2
                if depth == 0:
                    self.i = i
                    return
            else:
                i += 1
        raise ParseError("unterminated /* comment")

    def skip_inline(self):
        t = self.t
        while self.i < self.n:
            c = t[self.i]
            if c in " \t\u00a0\ufeff":
                self.i += 1
            elif t.startswith("/*", self.i):
                self.skip_block()
            elif c == "\\":
                j = self.i + 1
                while j < self.n and t[j] in " \t":
                    j += 1
                if t.startswith("//", j):
                    while j < self.n and t[j] not in "\r\n":
                        j += 1
                if j < self.n and t[j] == "\r":
                    j += 1
                if j >= self.n or t[j] == "\n":
                    self.i = min(j + 1, self.n)
                    continue
                break
            else:
                break

    def skip_all(self):
        t = self.t
        while self.i < self.n:
            c = t[self.i]
            if c in " \t\r\n;\u00a0\ufeff":
                self.i += 1
            elif t.startswith("//", self.i):
                while self.i < self.n and t[self.i] not in "\r\n":
                    self.i += 1
            elif t.startswith("/*", self.i):
                self.skip_block()
            else:
                break

    def skip_type(self):
        if self.i < self.n and self.t[self.i] == "(":
            j = self.t.find(")", self.i)
            if j == -1:
                raise ParseError("unterminated type annotation")
            self.i = j + 1

    def token(self) -> str:
        t, i, n = self.t, self.i, self.n
        if i >= n:
            raise ParseError("unexpected end of file")
        c = t[i]
        if c == '"':
            j = i + 1
            while j < n and t[j] != '"':
                j += 2 if t[j] == "\\" else 1
            if j >= n:
                raise ParseError(f"unterminated string at offset {i}")
            self.i = j + 1
            return t[i: j + 1]
        if c == "r" and i + 1 < n and t[i + 1] in '"#':
            k, h = i + 1, 0
            while k < n and t[k] == "#":
                h += 1
                k += 1
            if k < n and t[k] == '"':
                end = t.find('"' + "#" * h, k + 1)
                if end == -1:
                    raise ParseError(f"unterminated raw string at offset {i}")
                self.i = end + 1 + h
                return t[i: self.i]
        j = i
        while j < n and t[j] not in _BARE_STOP:
            if t.startswith("//", j) or t.startswith("/*", j):
                break
            j += 1
        if j == i:
            raise ParseError(f"unexpected character {c!r} at offset {i}")
        self.i = j
        return t[i:j]

    def expect_close(self):
        self.skip_all()
        if self.i >= self.n or self.t[self.i] != "}":
            raise ParseError("missing closing }")
        self.i += 1

    def nodes(self, top: bool) -> list[Node]:
        out = []
        while True:
            self.skip_all()
            if self.i >= self.n:
                break
            if self.t[self.i] == "}":
                if top:
                    raise ParseError(f"unexpected }} at offset {self.i}")
                break
            start, disabled = self.i, False
            if self.t.startswith("/-", self.i):
                disabled = True
                self.i += 2
                self.skip_all()
            node = self.node()
            node.start, node.disabled = start, disabled
            out.append(node)
        return out

    def node(self) -> Node:
        n = Node()
        self.skip_type()
        n.name_start = self.i
        raw = self.token()
        n.name_end = last = self.i
        n.name = unescape(raw) if raw.startswith('"') else raw
        while True:
            self.skip_inline()
            if self.i >= self.n:
                break
            c = self.t[self.i]
            if c in "\r\n;}":
                break
            if self.t.startswith("//", self.i):
                while self.i < self.n and self.t[self.i] not in "\r\n":
                    self.i += 1
                break
            if c == "{":
                n.body_start = self.i
                self.i += 1
                n.children = self.nodes(False)
                self.expect_close()
                n.body_end = self.i - 1
                last = self.i
                for ch in n.children:
                    ch.parent = n
                break
            disabled = False
            if self.t.startswith("/-", self.i):
                disabled = True
                self.i += 2
                self.skip_inline()
                if self.i < self.n and self.t[self.i] == "{":
                    self.i += 1
                    self.nodes(False)
                    self.expect_close()
                    continue
            start = self.i
            self.skip_type()
            tok = self.token()
            end = self.i
            if self.i < self.n and self.t[self.i] == "=":
                self.i += 1
                self.skip_type()
                vs = self.i
                vt = self.token()
                end = self.i
                if not disabled:
                    n.props[tok.strip('"')] = Val(vt, vs, end)
            elif not disabled:
                n.args.append(Val(tok, start, end))
            if not disabled:
                last = end
        n.end = last
        return n


def parse_kdl(text: str) -> list[Node]:
    return _Parser(text).nodes(True)


# --------------------------------------------------------------------------- store

# Where brand-new settings go when no file already holds the section.
SECTION_FILE = {
    "layout": "layout.kdl",
    "animations": "animation.kdl",
    "input": "input.kdl",
    "output": "display.kdl",
    "binds": "keybinds.kdl",
    "window-rule": "rules.kdl",
    "layer-rule": "rules.kdl",
    "spawn-at-startup": "autostart.kdl",
    "spawn-sh-at-startup": "autostart.kdl",
    "environment": "autostart.kdl",
}


def config_root() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "niri"


JOURNAL = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "niri-settings" / "pending.json"
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PREFS = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "niri-settings" / "prefs.json"
MANAGED_NAME = "niri-settings.kdl"
MANAGED_HEADER = ("// Managed by niri-settings. Settings in this file override earlier files.\n"
                  "// config.kdl must include this file LAST.\n")


class Doc:
    def __init__(self, path, text, existed=True):
        self.path = Path(path)
        self.text = self.last_good = text
        self.existed = existed
        self.snap = None  # (text, existed) captured at the first unsaved edit
        self.nodes: list[Node] = []
        self.error = None
        self.reparse()

    def reparse(self):
        try:
            self.nodes, self.error = parse_kdl(self.text), None
        except ParseError as e:
            self.nodes, self.error = [], str(e)


def _match(nodes, elem):
    name, arg = (elem, None) if isinstance(elem, str) else elem
    for n in nodes:
        if n.disabled or n.name != name:
            continue
        if arg is not None and not (n.args and n.args[0].value == arg):
            continue
        yield n


def _find(nodes, path):
    for n in _match(nodes, path[0]):
        if len(path) == 1:
            yield n
        elif n.children:
            yield from _find(n.children, path[1:])


def _head(elem) -> str:
    return elem if isinstance(elem, str) else f"{elem[0]} {fmt(elem[1])}"


class Store:
    """All config files + edit operations. Path elements are 'name' or ('name', 'first-arg')."""

    def __init__(self, root=None, mode=None):
        self.root = Path(root) if root else config_root()
        self.mode = mode or self._load_pref()  # "managed" (own file) | "inplace" (edit your files)
        self.managed = None
        self.on_change = None
        self.changes: dict[str, tuple[str, str]] = {}
        self._recent: list[str] = []
        self.docs: list[Doc] = []
        self.baseline_ok = True
        self.load()

    @staticmethod
    def _load_pref():
        try:
            m = json.loads(PREFS.read_text()).get("mode")
            return m if m in ("managed", "inplace") else "managed"
        except Exception:
            return "managed"

    def set_mode(self, mode):
        self.mode = mode
        try:
            PREFS.parent.mkdir(parents=True, exist_ok=True)
            PREFS.write_text(json.dumps({"mode": mode}))
        except OSError:
            pass

    @property
    def managed_mode(self) -> bool:
        return self.mode == "managed" and self.managed is not None

    def managed_path(self) -> Path:
        cfg = self.root / "cfg"
        return (cfg if cfg.is_dir() else self.root) / MANAGED_NAME

    def is_editable(self, doc) -> bool:
        return not self.managed_mode or doc is self.managed

    # ---- loading ----
    def load(self):
        paths = []
        main = self.root / "config.kdl"
        if main.is_file():
            paths.append(main)
        if self.root.is_dir():
            paths += [p for p in sorted(self.root.rglob("*.kdl")) if p.is_file() and p not in paths]
        self.docs = []
        for p in paths:
            try:
                self.docs.append(Doc(p, p.read_text(encoding="utf-8")))
            except Exception as e:  # unreadable file: keep it visible but read-only
                d = Doc(p, "")
                d.error = str(e)
                self.docs.append(d)
        mp = os.path.realpath(self.managed_path())
        self.managed = next((d for d in self.docs if os.path.realpath(d.path) == mp), None)
        if self.managed is None:
            self.managed = Doc(self.managed_path(), "", existed=False)
            self.docs.append(self.managed)
        self.baseline_ok = self.validate()[0]

    # ---- reading ----
    def _rdocs(self):
        """Read order: our own file wins, then everything else."""
        if self.managed_mode:
            return [self.managed] + [d for d in self.docs if d is not self.managed]
        return self.docs

    def _wdocs(self):
        return [self.managed] if self.managed_mode else self.docs

    def find_all(self, path):
        docs = self._rdocs()
        if (self.managed_mode and isinstance(path[0], tuple) and path[0][0] == "output"
                and any(True for _ in _find(self.managed.nodes, [path[0]]))):
            docs = [self.managed]  # output blocks are not merged by niri: our copy is the whole truth
        for d in docs:
            for n in _find(d.nodes, path):
                yield d, n

    def _wfind(self, path):
        for d in self._wdocs():
            for n in _find(d.nodes, path):
                return d, n
        return None

    def flag_state(self, path) -> bool:
        for d, n in self.find_all(path):
            return not (n.args and n.args[0].value is False)
        return False

    def _other_state(self, path):
        """State of a flag in the files we do not write to (None = not set there)."""
        if isinstance(path[0], tuple) and path[0][0] == "output":
            return None
        for d, n in self.find_all(path):
            if d is not self.managed:
                return not (n.args and n.args[0].value is False)
        return None

    def section_enabled(self, path, default) -> bool:
        """focus-ring / border: explicit `on`/`off` wins; a bare legacy section in a normal file means on."""
        legacy = False
        for d, n in self.find_all(path):
            state = None
            for c in n.kids():
                if c.name == "on":
                    state = True
                elif c.name == "off":
                    state = False
            if state is not None:
                return state
            if d is not self.managed or not self.managed_mode:
                legacy = True
        return True if legacy else default

    def shadow_file(self, path):
        """Name of another file that still defines `path` (our file can't remove it)."""
        if not self.managed_mode:
            return None
        for d, n in self.find_all(path):
            if d is not self.managed:
                return d.path.name
        return None

    def find(self, path):
        return next(self.find_all(path), None)

    def get(self, path, default=None):
        loc = self.find(path)
        return loc[1].args[0].value if loc and loc[1].args else default

    def get_prop(self, path, key, default=None):
        loc = self.find(path)
        return loc[1].prop(key, default) if loc else default

    def children_of(self, path):
        """Every non-disabled child of every node matching path, across all files."""
        out = []
        for d, n in self.find_all(path):
            out += [(d, c) for c in n.kids()]
        return out

    def top_nodes(self, *names):
        return [(d, n) for d in self.docs for n in d.nodes if not n.disabled and n.name in names]

    def body_text(self, doc, node) -> str:
        if node.children is None:
            return ""
        return textwrap.dedent(doc.text[node.body_start + 1: node.body_end]).strip("\n")

    @property
    def dirty(self) -> bool:
        return bool(self.changes) or any(d.snap for d in self.docs)

    # ---- low-level edit ----
    def edit(self, doc, edits, page=None, desc=None, key=None):
        if doc.snap is None:
            doc.snap = (doc.text, doc.existed)
            self._write_journal()
        for a, b, new in sorted(edits, key=lambda e: -e[0]):
            doc.text = doc.text[:a] + new + doc.text[b:]
        doc.reparse()
        if page:
            k = key or desc or str(len(self.changes))
            self.changes[k] = (page, desc or k)
            self._recent.append(k)
        if self.on_change:
            self.on_change()

    def default_doc(self, top_name) -> Doc:
        if self.managed_mode:
            return self.managed
        fname = SECTION_FILE.get(top_name, "misc.kdl")
        for d in self.docs:
            if d.path.name == fname and not d.error:
                return d
        for d in self.docs:
            if d.path.name == "config.kdl" and not d.error:
                return d
        for d in self.docs:
            if not d.error:
                return d
        d = Doc(self.root / "config.kdl", "", existed=False)
        self.docs.append(d)
        return d

    @staticmethod
    def _chain(path, args, props, ind=0):
        pad = " " * ind
        head = _head(path[0])
        if len(path) == 1:
            head += "".join(" " + fmt(a) for a in (args or ())) + "".join(f" {k}={fmt(v)}" for k, v in (props or {}).items())
            return [pad + head]
        return [pad + head + " {"] + Store._chain(path[1:], args, props, ind + 4) + [pad + "}"]

    @staticmethod
    def _insert_in(doc, parent, lines):
        t = doc.text
        pls = t.rfind("\n", 0, parent.start) + 1
        pind = re.match(r"[ \t]*", t[pls:]).group(0)
        if parent.children is None:
            body = "\n".join(pind + "    " + l for l in lines)
            return (parent.tail(), parent.tail(), " {\n" + body + "\n" + pind + "}")
        b = parent.body_end
        ls = t.rfind("\n", 0, b) + 1
        if t[ls:b].strip() == "":
            cind = t[ls:b] + "    "
            return (ls, ls, "".join(cind + l + "\n" for l in lines))
        cind = pind + "    "
        return (b, b, "\n" + "\n".join(cind + l for l in lines) + "\n" + pind)

    @staticmethod
    def _append_top(doc, lines):
        t = doc.text
        sep = ""
        if t.strip():
            sep = ("" if t.endswith("\n") else "\n") + "\n"
        return (len(t), len(t), sep + "\n".join(lines) + "\n")

    # ---- high-level edits ----
    def _seed(self, path):
        """niri does not merge `output` blocks, so before overriding one setting we copy the whole block."""
        if not self.managed_mode or not isinstance(path[0], tuple) or path[0][0] != "output":
            return
        if any(True for _ in _find(self.managed.nodes, [path[0]])):
            return
        src = next(((d, n) for d, n in self.find_all([path[0]]) if d is not self.managed), None)
        if not src:
            return
        body = self.body_text(*src) if src[1].children is not None else None
        self.add_top_node("output", body or None, args=[path[0][1]])

    def create(self, path, args=(), props=None, page=None, desc=None):
        for k in range(len(path) - 1, 0, -1):
            loc = self._wfind(path[:k])
            if loc:
                doc, parent = loc
                e = self._insert_in(doc, parent, self._chain(path[k:], args, props))
                return self.edit(doc, [e], page, desc, key="/".join(map(str, path)))
        name = path[0] if isinstance(path[0], str) else path[0][0]
        doc = self.default_doc(name)
        e = self._append_top(doc, self._chain(path, args, props))
        self.edit(doc, [e], page, desc, key="/".join(map(str, path)))

    def set_value(self, path, value, page=None, desc=None):
        self._seed(path)
        loc = self._wfind(path)
        key = "/".join(map(str, path))
        if not loc:
            return self.create(path, [value], page=page, desc=desc)
        doc, node = loc
        if node.args:
            e = (node.args[0].start, node.args[0].end, fmt(value))
        else:
            e = (node.name_end, node.name_end, " " + fmt(value))
        self.edit(doc, [e], page, desc, key)

    def set_props(self, path, props, page=None, desc=None):
        self._seed(path)
        key = "/".join(map(str, path))
        for k, v in props.items():
            loc = self._wfind(path)
            if not loc:
                self.create(path, props={k: v}, page=page, desc=desc)
                continue
            doc, node = loc
            if k in node.props:
                pv = node.props[k]
                e = (pv.start, pv.end, fmt(v))
            else:
                e = (node.tail(), node.tail(), f" {k}={fmt(v)}")
            self.edit(doc, [e], page, desc, key)

    def delete_node(self, doc, node, page=None, desc=None):
        t, a, b = doc.text, node.start, node.end
        ls = t.rfind("\n", 0, a) + 1
        le = t.find("\n", b)
        le = len(t) if le == -1 else le
        if t[ls:a].strip() == "" and t[b:le].strip() in ("", ";"):
            a, b = ls, min(le + 1, len(t))
        self.edit(doc, [(a, b, "")], page, desc, key=desc)
        self._prune(doc)

    def _prune(self, doc):
        """In our own file, drop blocks that became empty (e.g. `focus-ring { }`)."""
        if doc is not self.managed:
            return

        def empty(nodes):
            for n in nodes:
                if n.children is not None:
                    r = empty(n.children)
                    if r is not None:
                        return r
                    if not n.children and not n.args and not n.props and not n.disabled:
                        return n
            return None
        while True:
            n = empty(doc.nodes)
            if n is None:
                break
            t, a, b = doc.text, n.start, n.end
            ls = t.rfind("\n", 0, a) + 1
            le = t.find("\n", b)
            le = len(t) if le == -1 else le
            if t[ls:a].strip() == "" and t[b:le].strip() == "":
                a, b = ls, min(le + 1, len(t))
            self.edit(doc, [(a, b, "")])
        if "\n\n\n" in doc.text:
            self.edit(doc, [(0, len(doc.text), re.sub(r"\n{3,}", "\n\n", doc.text))])

    def delete_path(self, path, page=None, desc=None):
        loc = self._wfind(path)
        if loc:
            self.delete_node(loc[0], loc[1], page, desc or "/".join(map(str, path)))

    def set_flag(self, path, on, page=None, desc=None):
        self._seed(path)
        if not self.managed_mode:
            loc = self.find(path)
            if on and not self.flag_state(path):
                if loc:  # an explicit `flag false` -> drop it, then add the plain flag
                    self.delete_node(loc[0], loc[1], page, desc)
                self.create(path, page=page, desc=desc)
            elif not on and loc:
                self.delete_node(loc[0], loc[1], page, desc)
            return
        # separate-file mode: we cannot delete a flag that another file sets, but niri lets us write `flag false`
        mn = next(_find(self.managed.nodes, path), None)
        base = bool(self._other_state(path))
        if mn is not None:
            if (not (mn.args and mn.args[0].value is False)) == on and on != base:
                return  # already overriding correctly
            self.delete_node(self.managed, mn, page, desc)
        if on != base:
            self.create(path, args=None if on else [False], page=page, desc=desc)

    def set_enabled(self, path, enabled, page=None, desc=None):
        """Sections toggled with `on`/`off` flags (focus-ring, border, ...)."""
        if not self.managed_mode:
            if enabled:
                if self.find(path) is None:
                    self.create(path, page=page, desc=desc)
                self.set_flag(path + ["off"], False, page, desc)
            else:
                self.set_flag(path + ["off"], True, page, desc)
            return
        self.delete_path(path + ["off" if enabled else "on"], page, desc)
        self.set_flag(path + ["on" if enabled else "off"], True, page, desc)

    def replace_args(self, doc, node, values, page=None, desc=None):
        if node.args:
            e = (node.args[0].start, node.args[-1].end, " ".join(fmt(v) for v in values))
        else:
            e = (node.name_end, node.name_end, " " + " ".join(fmt(v) for v in values))
        self.edit(doc, [e], page, desc)

    def set_body(self, doc, node, text, page=None, desc=None):
        t = doc.text
        ls = t.rfind("\n", 0, node.start) + 1
        ind = re.match(r"[ \t]*", t[ls:]).group(0)
        lines = [l.rstrip() for l in text.strip("\n").splitlines() if l.strip()]
        new = "{\n" + "".join(ind + "    " + l.strip() + "\n" for l in lines) + ind + "}"
        if node.children is None:
            e = (node.tail(), node.tail(), " " + new)
        else:
            e = (node.body_start, node.body_end + 1, new)
        self.edit(doc, [e], page, desc)

    def add_top_node(self, name, body_text=None, args=(), page=None, desc=None):
        """Append e.g. `window-rule { ... }` or `spawn-at-startup "x"` at top level."""
        head = name + "".join(" " + fmt(a) for a in args)
        if body_text is None:
            lines = [head]
        else:
            lines = [head + " {"] + ["    " + l.strip() for l in body_text.strip("\n").splitlines() if l.strip()] + ["}"]
        writable = self._wdocs()
        same = [(d, n) for d, n in self.top_nodes(name) if any(d is w for w in writable)]
        if same:
            doc, last = same[-1]
            e = (last.end, last.end, "\n" + "\n".join(lines))
        else:
            doc = self.default_doc(name)
            e = self._append_top(doc, lines)
        self.edit(doc, [e], page, desc)

    def add_child_line(self, parent_path, line, page=None, desc=None):
        loc = self._wfind(parent_path)
        if loc:
            doc, parent = loc
            return self.edit(doc, [self._insert_in(doc, parent, [line])], page, desc)
        name = parent_path[0] if isinstance(parent_path[0], str) else parent_path[0][0]
        doc = self.default_doc(name)
        lines = [_head(parent_path[0]) + " {", "    " + line, "}"]
        self.edit(doc, [self._append_top(doc, lines)], page, desc)

    # ---- separate-file mode: the include line in config.kdl ----
    def _main_doc(self):
        return next((d for d in self.docs if d.path == self.root / "config.kdl"), None)

    def include_status(self) -> str:
        """'n/a' | 'ok' | 'missing' | 'not-last'"""
        if not self.managed_mode:
            return "n/a"
        target, seen = os.path.realpath(self.managed.path), []

        def walk(doc, depth=0):
            for n in doc.nodes:
                if n.disabled or n.name != "include" or not n.args:
                    continue
                p = os.path.realpath(doc.path.parent / str(n.arg0))
                seen.append(p)
                sub = next((d for d in self.docs if os.path.realpath(d.path) == p), None)
                if sub is not None and depth < 8:
                    walk(sub, depth + 1)
        main = self._main_doc()
        if main:
            walk(main)
        if target not in seen:
            return "missing"
        return "ok" if seen[-1] == target else "not-last"

    def ensure_managed_file(self):
        if not self.managed.existed and not self.managed.text:
            self._write_text(Path(os.path.realpath(self.managed.path)), MANAGED_HEADER)
            self.managed.text = self.managed.last_good = MANAGED_HEADER
            self.managed.existed = True
            self.managed.reparse()

    def include_line(self) -> str:
        return f'include "{os.path.relpath(self.managed.path, self.root)}"'

    def add_include(self):
        """Create our file (so a non-optional include never points at nothing) and append the include."""
        main = self._main_doc()
        if main is None:
            main = Doc(self.root / "config.kdl", "", existed=False)
            self.docs.append(main)
        self.ensure_managed_file()
        self.edit(main, [self._append_top(main, [self.include_line()])], "import", "include niri-settings.kdl")

    def rewrite_bind(self, doc, node, key, action, page=None, desc=None):
        name = key if re.fullmatch(r"[A-Za-z0-9_+\-]+", key) else quote(key)
        edits = [(node.name_start, node.name_end, name)]
        if node.children is not None:
            edits.append((node.body_start + 1, node.body_end, " " + action.strip().rstrip(";") + "; "))
        self.edit(doc, edits, page, desc)

    # ---- disk / validation ----
    def validate(self):
        exe, main = shutil.which("niri"), self.root / "config.kdl"
        if not exe or not main.exists():
            return True, ""
        try:
            r = subprocess.run([exe, "validate", "-c", str(main)], capture_output=True, text=True, timeout=8)
        except Exception:
            return True, ""
        msg = _ANSI.sub("", (r.stderr or r.stdout)).strip()
        return r.returncode == 0, msg

    @staticmethod
    def _write_text(real: Path, text: str):
        """Write a file, coping with read-only folders/files. Raises OSError with a readable message."""
        if str(real).startswith("/nix/store/"):
            raise OSError(f"{real.name} is a read-only file in the Nix store (managed by home-manager/NixOS). "
                          "Replace the symlink with a real file or edit it in your Nix config.")
        real.parent.mkdir(parents=True, exist_ok=True)
        tmp = real.with_name(real.name + ".nsg-tmp")
        try:  # preferred: atomic replace
            tmp.write_text(text, encoding="utf-8")
            if real.exists():
                shutil.copymode(real, tmp)
            os.replace(tmp, real)
            return
        except OSError:
            tmp.unlink(missing_ok=True)
        try:  # folder not writable (or similar): overwrite the file itself
            if real.exists() and not os.access(real, os.W_OK):
                real.chmod(real.stat().st_mode | 0o200)
            real.write_text(text, encoding="utf-8")
        except OSError as e:
            raise OSError(f"Cannot write {real} ({e.strerror or e}). Check its permissions "
                          f"(try: chmod -R u+w {real.parent}).") from e

    @staticmethod
    def write_doc(doc):
        # write *through* symlinks (stow, chezmoi, ...)
        Store._write_text(Path(os.path.realpath(doc.path)), doc.text)

    def _revert(self, docs):
        for d in docs:
            d.text = d.last_good
            d.reparse()
        for k in self._recent:
            self.changes.pop(k, None)
        self._recent.clear()
        for d in self.docs:  # an edit that was fully reverted is no longer "pending"
            if d.snap and d.snap[1] and d.snap[0] == d.text:
                d.snap = None
        if any(d.snap for d in self.docs):
            self._write_journal()
        else:
            self._clear_journal()

    def flush(self) -> list[str]:
        """Write edited files to disk (niri hot-reloads them). Revert + report on any failure."""
        bad = [d for d in self.docs if d.text != d.last_good]
        if not bad:
            return []
        try:
            for d in bad:
                self.write_doc(d)
        except OSError as e:
            for d in bad:  # put back whatever was already written, then forget the edit
                try:
                    d.text, cur = d.last_good, d.text
                    self.write_doc(d)
                except OSError:
                    pass
            self._revert(bad)
            return [str(e)]
        ok, msg = self.validate()
        if ok or not self.baseline_ok:
            for d in bad:
                d.last_good = d.text
            self._recent.clear()
            return []
        try:
            for d in bad:
                d.text = d.last_good
                self.write_doc(d)
        except OSError:
            pass
        self._revert(bad)
        return [msg.splitlines()[0] if msg else "niri rejected that change"] + ([msg] if msg else [])

    def save(self):
        for d in self.docs:
            d.snap = None
        self.changes.clear()
        self._recent.clear()
        self._clear_journal()

    def discard(self):
        for d in self.docs:
            if d.snap is None:
                continue
            text, existed = d.snap
            real = Path(os.path.realpath(d.path))
            if existed:
                d.text = text
                self.write_doc(d)
            elif d is self.managed:
                if real.exists():  # keep an empty file: config.kdl may include it
                    self._write_text(real, MANAGED_HEADER)
            else:
                real.unlink(missing_ok=True)
        self._clear_journal()
        self.changes.clear()
        self._recent.clear()
        self.load()

    # ---- crash safety: remember the pre-edit state so a restart can offer to undo ----
    def _write_journal(self):
        files = {str(d.path): (d.snap[0] if d.snap[1] else None) for d in self.docs if d.snap}
        try:
            JOURNAL.parent.mkdir(parents=True, exist_ok=True)
            JOURNAL.write_text(json.dumps({"root": str(self.root), "files": files}))
        except OSError:
            pass

    def _clear_journal(self):
        JOURNAL.unlink(missing_ok=True)

    def pending_journal(self):
        try:
            j = json.loads(JOURNAL.read_text())
        except Exception:
            return None
        if j.get("root") != str(self.root):
            return None
        for p, text in j.get("files", {}).items():
            try:
                cur = Path(p).read_text(encoding="utf-8") if Path(p).exists() else None
            except Exception:
                cur = None
            if cur != text:
                return j
        self._clear_journal()
        return None

    def restore_journal(self, j):
        for p, text in j["files"].items():
            real = Path(os.path.realpath(p))
            if text is None and Path(p) == self.managed_path():
                if real.exists():
                    self._write_text(real, MANAGED_HEADER)
            elif text is None:
                real.unlink(missing_ok=True)
            else:
                self._write_text(real, text)
        self._clear_journal()
        self.load()


# ==========================================================================
# Config importer
# ==========================================================================

IGNORE = {".git", ".gitignore", ".DS_Store", "__pycache__"}


@dataclass
class Item:
    src: Path
    dst: Path
    replaces: bool


@dataclass
class Plan:
    source: Path
    items: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    error: str | None = None


@dataclass
class Result:
    moved: int
    backup: Path | None
    generated_main: bool
    leftover: Path | None


def _find_root(d: Path):
    for c in (d, d / "niri", d / ".config" / "niri", d / "config" / "niri"):
        if (c / "config.kdl").is_file():
            return c
    return None


def _same(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def plan_import(src, dest=None) -> Plan:
    dest = Path(dest) if dest else config_root()
    src = Path(src).expanduser()
    plan = Plan(source=src)
    if not src.exists():
        plan.error = "That path does not exist."
        return plan

    pairs: list[tuple[Path, Path]] = []
    if src.is_file():
        if src.suffix != ".kdl":
            plan.error = "Please pick a .kdl file or a folder."
            return plan
        if src.name == "config.kdl":
            pairs.append((src, dest / "config.kdl"))
            cfg = src.parent / "cfg"
            if cfg.is_dir():
                pairs.append((cfg, dest / "cfg"))
                plan.notes.append("Found the cfg/ folder next to config.kdl and included it.")
        else:
            pairs.append((src, dest / "cfg" / src.name))
            plan.notes.append("Single file: it will be placed in cfg/.")
    else:
        root = _find_root(src)
        if root:
            pairs = [(p, dest / p.name) for p in sorted(root.iterdir()) if p.name not in IGNORE]
            if root != src:
                plan.notes.append(f"Found a niri config inside: {root}")
        else:
            kdls = sorted(src.glob("*.kdl"))
            if not kdls:
                plan.error = "No config.kdl or .kdl files found there."
                return plan
            pairs = [(p, dest / "cfg" / p.name) for p in kdls]
            plan.notes.append("No config.kdl in that folder, so its .kdl files will go into cfg/.")

    for s, d in pairs:
        if _same(s, d):
            plan.error = "That config is already in ~/.config/niri - nothing to import."
            return plan
        plan.items.append(Item(s, d, d.exists() or d.is_symlink()))

    # Warn if the main config would not pull in the imported cfg/ files.
    main = dest / "config.kdl"
    cfg_files = [i.dst.name for i in plan.items if i.dst.parent.name == "cfg" and i.dst.suffix == ".kdl"]
    if cfg_files and not any(i.dst == main for i in plan.items):
        if main.exists():
            text = main.read_text(encoding="utf-8", errors="ignore")
            missing = [f for f in cfg_files if f not in text]
            if missing:
                plan.notes.append("Your config.kdl does not include: " + ", ".join(missing))
        else:
            plan.notes.append("There is no config.kdl yet - one with include lines can be generated for you.")
    return plan


def _remove(p: Path):
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.is_dir():
        shutil.rmtree(p)


def _copy(src: Path, dst: Path):
    if src.is_symlink():
        os.symlink(os.readlink(src), dst)
    elif src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)


def run_import(plan: Plan, move=True, backup=True, gen_main=True, dest=None) -> Result:
    dest = Path(dest) if dest else config_root()
    backup_path = None
    if backup and dest.exists() and any(dest.iterdir()):
        backup_path = dest.with_name(f"{dest.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copytree(dest, backup_path, symlinks=True)
    dest.mkdir(parents=True, exist_ok=True)

    for it in plan.items:
        if it.dst.exists() or it.dst.is_symlink():
            _remove(it.dst)
        it.dst.parent.mkdir(parents=True, exist_ok=True)
        if move:
            shutil.move(str(it.src), str(it.dst))
        else:
            _copy(it.src, it.dst)

    generated = False
    main = dest / "config.kdl"
    if gen_main and not main.exists():
        cfg = dest / "cfg"
        files = sorted(cfg.glob("*.kdl"), key=lambda f: (f.name == MANAGED_NAME, f.name)) if cfg.is_dir() else []
        if files:
            lines = ["// Generated by niri-settings: pulls in every file from cfg/ (needs niri 25.11+).", ""]
            lines += [f'include "cfg/{f.name}"' for f in files]
            main.write_text("\n".join(lines) + "\n", encoding="utf-8")
            generated = True

    leftover = plan.source if (move and plan.source.is_dir() and not any(plan.source.iterdir())) else None
    return Result(len(plan.items), backup_path, generated, leftover)


# ==========================================================================
# GTK application
# ==========================================================================


CSS = """
.sidebar-header { font-size: 0.72rem; font-weight: 800; letter-spacing: 0.08em; opacity: 0.5;
                  padding: 16px 14px 4px 14px; }
.nav-badge { background: alpha(@warning_color, 0.22); color: @warning_color; border-radius: 99px;
             padding: 0 8px; font-weight: 700; font-size: 0.8rem; }
.changes-pill { background: alpha(@warning_color, 0.18); color: @warning_color; border-radius: 99px;
                font-weight: 700; padding: 2px 12px; }
.dirty-bar { padding: 10px 14px; border-top: 1px solid alpha(currentColor, 0.12);
             background: @headerbar_bg_color; }
row.changed { box-shadow: inset 3px 0 0 @accent_bg_color; }
.monitor-canvas { border-radius: 14px; border: 1px solid alpha(currentColor, 0.12);
                  background: alpha(currentColor, 0.04); }
"""

NAV = [
    ("LOOK & FEEL", [("layout", "Layout", "view-grid-symbolic"),
                     ("animations", "Animations", "media-playback-start-symbolic"),
                     ("cursor", "Cursor", "input-mouse-symbolic")]),
    ("INPUT", [("keybinds", "Keybinds", "input-keyboard-symbolic"),
               ("keyboard", "Keyboard", "preferences-desktop-keyboard-symbolic"),
               ("pointer", "Mouse & Touchpad", "input-touchpad-symbolic")]),
    ("DISPLAY", [("monitors", "Monitors", "video-display-symbolic")]),
    ("WINDOW MANAGEMENT", [("rules", "Window Rules", "window-new-symbolic")]),
    ("STARTUP", [("autostart", "Autostart", "system-run-symbolic"),
                 ("env", "Env Variables", "utilities-terminal-symbolic")]),
    ("ADVANCED", [("misc", "Miscellaneous", "emblem-system-symbolic")]),
    ("SETUP", [("import", "Import & Backup", "folder-download-symbolic")]),
]
TITLES = {pid: t for _, items in NAV for pid, t, _ in items}

TRANSFORMS = [("normal", "Normal"), ("90", "90°"), ("180", "180°"), ("270", "270°"),
              ("flipped", "Flipped"), ("flipped-90", "Flipped 90°"),
              ("flipped-180", "Flipped 180°"), ("flipped-270", "Flipped 270°")]


def esc(s) -> str:
    return GLib.markup_escape_text(str(s))


def tilde(p) -> str:
    return str(p).replace(str(Path.home()), "~", 1)


# --------------------------------------------------------------------------- monitors


def niri_outputs() -> dict:
    """Ask the running niri about connected outputs. Empty dict if niri isn't running."""
    try:
        out = subprocess.run(["niri", "msg", "--json", "outputs"], capture_output=True, text=True, timeout=3).stdout
        data = json.loads(out)
    except Exception:
        return {}
    res = {}
    for name, o in data.items():
        modes = [(m["width"], m["height"], m["refresh_rate"]) for m in o.get("modes", [])]
        cur = o.get("current_mode")
        lg = o.get("logical") or {}
        tr = re.sub(r"(?i)flipped(\d+)", r"flipped-\1", str(lg.get("transform", "Normal"))).lower()
        res[name] = dict(make=o.get("make", ""), model=o.get("model", ""), modes=modes,
                         cur_idx=cur if cur is not None and cur < len(modes) else None,
                         vrr=bool(o.get("vrr_supported")), x=lg.get("x", 0), y=lg.get("y", 0),
                         scale=lg.get("scale", 1.0), transform=tr, lw=lg.get("width"), lh=lg.get("height"))
    return res


class MonitorCanvas(Gtk.DrawingArea):
    """Drag monitors around; releasing writes `position x= y=` into the config."""

    def __init__(self, win, mons):
        super().__init__()
        self.win, self.mons = win, mons
        self.names = list(mons)
        self.sel, self.override, self._drag, self._xf = None, {}, None, (1, 0, 0)
        self.set_content_height(230)
        self.set_hexpand(True)
        self.add_css_class("monitor-canvas")
        self.set_draw_func(self.draw)
        g = Gtk.GestureDrag()
        g.connect("drag-begin", self.on_begin)
        g.connect("drag-update", self.on_update)
        g.connect("drag-end", self.on_end)
        self.add_controller(g)

    def size(self, name):
        st, info, base = self.win.store, self.mons[name], [("output", name)]
        w = h = None
        mode = st.get(base + ["mode"])
        m = re.match(r"(\d+)x(\d+)", str(mode)) if mode else None
        if m:
            w, h = int(m.group(1)), int(m.group(2))
        elif info["cur_idx"] is not None:
            w, h = info["modes"][info["cur_idx"]][:2]
        elif info.get("lw"):
            return float(info["lw"]), float(info["lh"])
        else:
            w, h = 1920, 1080
        scale = float(st.get(base + ["scale"], info["scale"]) or 1)
        tr = str(st.get(base + ["transform"], info["transform"]))
        if tr in ("90", "270", "flipped-90", "flipped-270"):
            w, h = h, w
        return w / scale, h / scale

    def pos(self, name):
        if name in self.override:
            return self.override[name]
        st, info = self.win.store, self.mons[name]
        p = [("output", name), "position"]
        return (st.get_prop(p, "x", info["x"]), st.get_prop(p, "y", info["y"]))

    def rects(self):
        return {n: (*self.pos(n), *self.size(n)) for n in self.names}

    def draw(self, area, cr, w, h):
        rects = self.rects()
        if not rects:
            return
        minx = min(r[0] for r in rects.values())
        miny = min(r[1] for r in rects.values())
        maxx = max(r[0] + r[2] for r in rects.values())
        maxy = max(r[1] + r[3] for r in rects.values())
        pad = 26
        sc = min((w - 2 * pad) / max(maxx - minx, 1), (h - 2 * pad) / max(maxy - miny, 1))
        ox = (w - (maxx - minx) * sc) / 2 - minx * sc
        oy = (h - (maxy - miny) * sc) / 2 - miny * sc
        self._xf = (sc, ox, oy)
        dark = Adw.StyleManager.get_default().get_dark()
        fg = (1, 1, 1) if dark else (0, 0, 0)
        for i, name in enumerate(self.names):
            x, y, rw, rh = rects[name]
            sx, sy, sw, sh = x * sc + ox, y * sc + oy, rw * sc, rh * sc
            sel = name == self.sel or (self.sel is None and i == 0)
            r = 8
            cr.new_sub_path()
            cr.arc(sx + sw - r, sy + r, r, -1.5708, 0)
            cr.arc(sx + sw - r, sy + sh - r, r, 0, 1.5708)
            cr.arc(sx + r, sy + sh - r, r, 1.5708, 3.1416)
            cr.arc(sx + r, sy + r, r, 3.1416, 4.7124)
            cr.close_path()
            if sel:
                cr.set_source_rgba(0.21, 0.52, 0.89, 0.22)
            else:
                cr.set_source_rgba(*fg, 0.07)
            cr.fill_preserve()
            if sel:
                cr.set_source_rgba(0.21, 0.52, 0.89, 1)
            else:
                cr.set_source_rgba(*fg, 0.35)
            cr.set_line_width(1.6)
            cr.stroke()
            cr.select_font_face("Sans")
            cr.set_font_size(max(14, min(sh * 0.4, 44)))
            cr.set_source_rgba(0.35, 0.65, 1, 1) if sel else cr.set_source_rgba(*fg, 0.6)
            s = str(i + 1)
            e = cr.text_extents(s)
            cr.move_to(sx + sw / 2 - e.width / 2 - e.x_bearing, sy + sh / 2 + e.height / 2 - 2)
            cr.show_text(s)
            cr.set_font_size(11)
            cr.set_source_rgba(*fg, 0.6)
            label = f"{name}  {int(x)},{int(y)}"
            e = cr.text_extents(label)
            cr.move_to(sx + sw / 2 - e.width / 2 - e.x_bearing, sy + sh - 8)
            cr.show_text(label)

    def _hit(self, px, py):
        sc, ox, oy = self._xf
        for name, (x, y, w, h) in self.rects().items():
            if x * sc + ox <= px <= (x + w) * sc + ox and y * sc + oy <= py <= (y + h) * sc + oy:
                return name
        return None

    def on_begin(self, g, x, y):
        name = self._hit(x, y)
        if name:
            self.sel = name
            self._drag = (name, self.pos(name))
        else:
            self._drag = None
        self.queue_draw()

    def on_update(self, g, dx, dy):
        if not self._drag:
            return
        name, (x0, y0) = self._drag
        sc = self._xf[0]
        nx, ny = x0 + dx / sc, y0 + dy / sc
        w, h = self.size(name)
        for other, (ox_, oy_, ow, oh) in self.rects().items():
            if other == name:
                continue
            for cand in (ox_ + ow, ox_ - w, ox_):
                if abs(cand - nx) < 30:
                    nx = cand
            for cand in (oy_, oy_ + oh - h, oy_ + oh, oy_ - h):
                if abs(cand - ny) < 30:
                    ny = cand
        self.override[name] = (round(nx), round(ny))
        self.queue_draw()

    def on_end(self, g, dx, dy):
        if not self._drag:
            return
        name = self._drag[0]
        x, y = self.override.pop(name, self.pos(name))
        self._drag = None
        self.win.store.set_props([("output", name), "position"], {"x": int(x), "y": int(y)},
                                 "monitors", f"{name} position")
        self.queue_draw()


# --------------------------------------------------------------------------- window


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Niri Settings", default_width=1020, default_height=780)
        self.store = Store()
        self.store.on_change = self.on_store_change
        self.pages, self.current, self._flush_id = {}, None, 0
        self.imp = dict(src=None, plan=None, move=True, backup=True, gen=True)
        self.builders = {
            "layout": self.pg_layout, "animations": self.pg_animations, "cursor": self.pg_cursor,
            "keybinds": self.pg_keybinds, "keyboard": self.pg_keyboard, "pointer": self.pg_pointer,
            "monitors": self.pg_monitors, "rules": self.pg_rules, "autostart": self.pg_autostart,
            "env": self.pg_env, "misc": self.pg_misc, "import": self.pg_import,
        }
        self.build_shell()
        self.connect("close-request", self.on_close)
        GLib.idle_add(self.startup_checks)

    # ---- shell -------------------------------------------------------------
    def build_shell(self):
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        split = Adw.NavigationSplitView()
        split.set_min_sidebar_width(230)
        self.split = split
        self.toasts.set_child(split)

        # sidebar
        side_tb = Adw.ToolbarView()
        side_tb.add_top_bar(Adw.HeaderBar(title_widget=Adw.WindowTitle.new("Niri Settings", "")))
        self.nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=["navigation-sidebar"])
        self.nav_rows, self.badges = {}, {}
        for header, items in NAV:
            hr = Gtk.ListBoxRow(selectable=False, activatable=False)
            hr.set_child(Gtk.Label(label=header, xalign=0, css_classes=["sidebar-header"]))
            self.nav.append(hr)
            for pid, title, icon in items:
                box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
                box.append(Gtk.Image.new_from_icon_name(icon))
                box.append(Gtk.Label(label=title, xalign=0, hexpand=True))
                badge = Gtk.Label(label="0", css_classes=["nav-badge"], visible=False)
                box.append(badge)
                row = Gtk.ListBoxRow(name=pid)
                row.set_child(box)
                self.nav.append(row)
                self.nav_rows[pid], self.badges[pid] = row, badge
        self.nav.connect("row-selected", self.on_nav)
        side_tb.set_content(Gtk.ScrolledWindow(child=self.nav, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        split.set_sidebar(Adw.NavigationPage.new(side_tb, "Niri Settings"))

        # content
        tb = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.win_title = Adw.WindowTitle.new("", "")
        header.set_title_widget(self.win_title)
        reload_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload from disk")
        reload_btn.connect("clicked", lambda *_: self.reload())
        header.pack_start(reload_btn)

        menu = Gio.Menu()
        menu.append("Import config…", "win.goto-import")
        menu.append("Reload from disk", "win.reload")
        menu.append("Open config folder", "win.open-folder")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True))
        self.pill = Gtk.MenuButton(label="0", css_classes=["changes-pill", "flat"], visible=False,
                                   tooltip_text="Unsaved changes")
        self.pill_pop = Gtk.Popover()
        self.pill.set_popover(self.pill_pop)
        header.pack_end(self.pill)
        for name, fn in (("goto-import", lambda *_: self.select("import")), ("reload", lambda *_: self.reload()),
                         ("open-folder", lambda *_: Gio.AppInfo.launch_default_for_uri(
                             Gio.File.new_for_path(str(self.store.root)).get_uri(), None))):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", fn)
            self.add_action(act)
        tb.add_top_bar(header)
        self.banner = Adw.Banner.new("")
        self.banner.connect("button-clicked", lambda *_: self.include_flow())
        tb.add_top_bar(self.banner)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        tb.set_content(self.stack)

        bar = Gtk.Box(spacing=12, css_classes=["dirty-bar"])
        bar.append(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
        bar.append(Gtk.Label(label="Unsaved changes — applied live, confirm to keep them", xalign=0, hexpand=True,
                             ellipsize=Pango.EllipsizeMode.END))
        discard = Gtk.Button(label="Discard")
        discard.connect("clicked", lambda *_: self.do_discard())
        save = Gtk.Button(label="Save now", css_classes=["suggested-action"])
        save.connect("clicked", lambda *_: self.do_save())
        bar.append(discard)
        bar.append(save)
        self.revealer = Gtk.Revealer(child=bar, transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        tb.add_bottom_bar(self.revealer)
        self.content_page = Adw.NavigationPage.new(tb, "Settings")
        split.set_content(self.content_page)
        self.select("layout")

    def select(self, pid):
        self.nav.select_row(self.nav_rows[pid])

    def on_nav(self, lb, row):
        if row is None:
            return
        self.current = row.get_name()
        self.win_title.set_title(TITLES[self.current])
        self.content_page.set_title(TITLES[self.current])
        self.show_page(self.current)
        self.split.set_show_content(True)

    def show_page(self, pid):
        if pid not in self.pages:
            w = self.builders[pid]()
            self.pages[pid] = w
            self.stack.add_named(w, pid)
        self.stack.set_visible_child_name(pid)

    def invalidate(self, pid):
        w = self.pages.pop(pid, None)
        if w is not None:
            self.stack.remove(w)

    def rebuild_page(self, pid):
        def go():
            self.invalidate(pid)
            if self.current == pid:
                self.show_page(pid)
            return False
        GLib.idle_add(go)

    def rebuild_all(self):
        for pid in list(self.pages):
            self.invalidate(pid)
        if self.current:
            self.show_page(self.current)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast.new(text))

    # ---- live-apply / save / discard ------------------------------------------
    def on_store_change(self):
        for pid in list(self.pages):  # other pages may now show stale data: rebuild lazily
            if pid != self.current:
                self.invalidate(pid)
        if self._flush_id:
            GLib.source_remove(self._flush_id)
        self._flush_id = GLib.timeout_add(350, self._flush)
        self.refresh_state()

    def _flush(self):
        self._flush_id = 0
        errs = self.store.flush()
        if errs:
            self.toast("Change reverted: " + errs[0][:180])
            self.rebuild_all()
        self.refresh_state()
        return GLib.SOURCE_REMOVE

    def flush_now(self):
        if self._flush_id:
            GLib.source_remove(self._flush_id)
            self._flush_id = 0
        errs = self.store.flush()
        if errs:
            self.toast("Change reverted: " + errs[0][:180])
        return not errs

    def update_banner(self):
        st = self.store.include_status()
        if st == "missing":
            self.banner.set_title(f"Changes are saved to {tilde(self.store.managed.path)}, but config.kdl does not "
                                  "include it yet - nothing will apply until it does.")
            self.banner.set_button_label("Add include line")
            self.banner.set_revealed(True)
        elif st == "not-last":
            self.banner.set_title("The niri-settings include is not the last include in config.kdl, "
                                  "so later files can override your changes.")
            self.banner.set_button_label(None)
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

    def include_flow(self):
        line = self.store.include_line()
        if os.path.realpath(self.store.root / "config.kdl").startswith("/nix/store/"):
            return self.manual_include(line)
        was_dirty = self.store.dirty
        try:
            self.store.add_include()
        except OSError as e:
            self.toast(str(e)[:180])
            return self.manual_include(line)
        if not was_dirty:
            if self.flush_now():
                self.store.save()
                self.toast("Include added to config.kdl")
            else:
                self.manual_include(line)
        self.refresh_state()

    def manual_include(self, line):
        try:
            self.store.ensure_managed_file()
        except OSError:
            pass
        d, show = self.dialog("Add this line to config.kdl",
                              f"The app cannot edit {tilde(self.store.root / 'config.kdl')}. Add this as its LAST line:"
                              f"\n\n{line}", [("close", "Close", None),
                                                ("copy", "Copy line", Adw.ResponseAppearance.SUGGESTED)])

        def resp(dlg, r):
            if r == "copy":
                clip = self.get_clipboard()
                try:
                    clip.set_text(line)
                except Exception:  # noqa: BLE001
                    clip.set(line)
                self.toast("Copied")
        d.connect("response", resp)
        show()

    def refresh_state(self):
        self.update_banner()
        self.revealer.set_reveal_child(self.store.dirty)
        counts = {}
        for pid, _ in self.store.changes.values():
            counts[pid] = counts.get(pid, 0) + 1
        for pid, badge in self.badges.items():
            n = counts.get(pid, 0)
            badge.set_visible(n > 0)
            badge.set_label(str(n))
        total = len(self.store.changes)
        self.pill.set_visible(total > 0)
        self.pill.set_label(str(total))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=10, margin_bottom=10,
                      margin_start=12, margin_end=12)
        box.append(Gtk.Label(label="Pending changes", xalign=0, css_classes=["heading"]))
        for pid, desc in self.store.changes.values():
            box.append(Gtk.Label(label=f"{TITLES.get(pid, pid)}  ›  {desc}", xalign=0))
        self.pill_pop.set_child(box)

    def do_save(self):
        if not self.flush_now():
            return
        self.store.save()
        self.rebuild_all()
        self.refresh_state()
        self.toast("Configuration saved")

    def do_discard(self):
        if self._flush_id:
            GLib.source_remove(self._flush_id)
            self._flush_id = 0
        try:
            self.store.discard()
        except OSError as e:
            return self.toast(f"Could not restore files: {e}"[:160])
        self.rebuild_all()
        self.refresh_state()
        self.toast("Changes discarded, previous config restored")

    def reload(self):
        if self.store.dirty:
            self.toast("Save or discard your pending changes first")
            return
        self.store.load()
        self.rebuild_all()
        self.toast("Reloaded from disk")

    def on_close(self, *_):
        if not self.store.dirty:
            return False
        d, show = self.dialog("Unsaved changes", "Your changes are applied live but not confirmed yet.",
                              [("cancel", "Cancel", None), ("discard", "Discard", Adw.ResponseAppearance.DESTRUCTIVE),
                               ("save", "Save", Adw.ResponseAppearance.SUGGESTED)])

        def resp(dlg, r):
            if r == "save":
                self.flush_now()
                self.store.save()
                self.destroy()
            elif r == "discard":
                self.store.discard()
                self.destroy()
        d.connect("response", resp)
        show()
        return True

    def startup_checks(self):
        j = self.store.pending_journal()
        if j:
            d, show = self.dialog("Unsaved changes from last time",
                                  "The app closed while changes were applied but not saved. "
                                  "Restore the config from before those changes, or keep what is on disk?",
                                  [("keep", "Keep current", None), ("restore", "Restore previous",
                                                                     Adw.ResponseAppearance.SUGGESTED)])

            def resp(dlg, r):
                if r == "restore":
                    self.store.restore_journal(j)
                    self.rebuild_all()
                    self.toast("Previous config restored")
                else:
                    self.store._clear_journal()
            d.connect("response", resp)
            show()
        self.update_banner()
        ro = [x for x in self.store.docs if os.path.realpath(x.path).startswith("/nix/store/")]
        ro = [] if self.store.managed_mode else ro
        if ro:
            d, show = self.dialog(
                "Config is managed by Nix",
                f"{len(ro)} of your config files are symlinks into the read-only Nix store (home-manager), so edits "
                "cannot be saved. Replace them with real files (e.g. cp --remove-destination) or change them in "
                "your Nix config. You can still use Import & Backup to place a config there.",
                [("ok", "OK", None)])
            show()
        bad = [x for x in self.store.docs if x.error]
        if bad:
            self.toast(f"Could not parse {bad[0].path.name}: {bad[0].error[:80]}")
        return False

    # ---- dialogs -----------------------------------------------------------------
    def dialog(self, heading, body, responses):
        if hasattr(Adw, "AlertDialog"):
            d = Adw.AlertDialog.new(heading, body)
            show = lambda: d.present(self)  # noqa: E731
        else:
            d = Adw.MessageDialog.new(self, heading, body)
            show = d.present
        for rid, label, appearance in responses:
            d.add_response(rid, label)
            if appearance is not None:
                d.set_response_appearance(rid, appearance)
        d.set_close_response(responses[0][0])
        d.set_default_response(responses[-1][0])
        return d, show

    def form(self, heading, fields, ok_label, cb, body=""):
        d, show = self.dialog(heading, body, [("cancel", "Cancel", None),
                                              ("ok", ok_label, Adw.ResponseAppearance.SUGGESTED)])
        grp, rows = Adw.PreferencesGroup(), []
        for label, init in fields:
            r = Adw.EntryRow(title=label)
            r.set_text(init)
            grp.add(r)
            rows.append(r)
        d.set_extra_child(grp)

        def resp(dlg, r):
            if r == "ok":
                cb([x.get_text().strip() for x in rows])
        d.connect("response", resp)
        show()

    def text_dialog(self, heading, body, initial, cb):
        d, show = self.dialog(heading, body, [("cancel", "Cancel", None), ("ok", "Apply", Adw.ResponseAppearance.SUGGESTED)])
        tv = Gtk.TextView(monospace=True, top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        tv.get_buffer().set_text(initial)
        sw = Gtk.ScrolledWindow(child=tv, min_content_height=220, min_content_width=420)
        sw.add_css_class("card")
        d.set_extra_child(sw)

        def resp(dlg, r):
            if r == "ok":
                b = tv.get_buffer()
                cb(b.get_text(b.get_start_iter(), b.get_end_iter(), False))
        d.connect("response", resp)
        show()

    # ---- row helpers ------------------------------------------------------------------
    @staticmethod
    def page():
        return Adw.PreferencesPage()

    @staticmethod
    def group(page, title=None, desc=None):
        g = Adw.PreferencesGroup()
        if title:
            g.set_title(title)
        if desc:
            g.set_description(desc)
        page.add(g)
        return g

    def r_switch(self, pid, title, sub, path, mode="flag", default=False, default_enabled=True):
        st = self.store
        if mode == "flag":
            active = st.flag_state(path)
        elif mode == "enabled":
            active = st.section_enabled(path, default_enabled)
        elif mode == "on":
            active = st.flag_state(path + ["on"])
        else:
            active = bool(st.get(path, default))
        row = Adw.SwitchRow(title=title, subtitle=sub or "")
        row.set_active(active)

        def cb(r, _):
            a = r.get_active()
            if mode == "flag":
                st.set_flag(path, a, pid, title)
            elif mode == "enabled":
                st.set_enabled(path, a, pid, title)
            elif mode == "on":
                st.set_flag(path + ["on"], a, pid, title)
            else:
                st.set_value(path, a, pid, title)
            r.add_css_class("changed")
        row.connect("notify::active", cb)
        return row

    def _note_shadow(self, path):
        f = self.store.shadow_file(path)
        if f:
            self.toast(f"Still set in {f}. Switch to 'Edit my existing files in place' to remove it.")

    def r_spin(self, pid, title, sub, path, lo, hi, step, digits=0, default=0):
        v = self.store.get(path, default)
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = float(default)
        adj = Gtk.Adjustment(value=v, lower=lo, upper=hi, step_increment=step, page_increment=step * 5)
        row = Adw.SpinRow.new(adj, 0, digits)
        row.set_title(title)
        row.set_subtitle(sub or "")

        def cb(r, _):
            val = r.get_value()
            self.store.set_value(path, int(round(val)) if digits == 0 else round(val, digits), pid, title)
            r.add_css_class("changed")
        row.connect("notify::value", cb)
        return row

    def r_combo(self, pid, title, sub, path, options, labels=None, unset=False, default=None):
        off = 1 if unset else 0
        items = (["(default)"] if unset else []) + list(labels or options)
        row = Adw.ComboRow(title=title, subtitle=sub or "")
        row.set_model(Gtk.StringList.new(items))
        cur = self.store.get(path)
        cur = None if cur is None else str(cur)
        if cur in options:
            row.set_selected(options.index(cur) + off)
        elif default in options:
            row.set_selected(options.index(default) + off)

        def cb(r, _):
            i = r.get_selected() - off
            if i < 0:
                self.store.delete_path(path, pid, title)
                self._note_shadow(path)
            else:
                self.store.set_value(path, options[i], pid, title)
            r.add_css_class("changed")
        row.connect("notify::selected", cb)
        return row

    def r_entry(self, pid, title, path):
        row = Adw.EntryRow(title=title)
        row.set_show_apply_button(True)
        cur = self.store.get(path)
        row.set_text("" if cur is None else str(cur))

        def cb(r):
            t = r.get_text().strip()
            if t:
                self.store.set_value(path, t, pid, title)
            else:
                self.store.delete_path(path, pid, title)
                self._note_shadow(path)
            r.add_css_class("changed")
        row.connect("apply", cb)
        return row

    def r_color(self, pid, title, path, default="#808080"):
        row = Adw.ActionRow(title=title)
        btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(), valign=Gtk.Align.CENTER)
        rgba = Gdk.RGBA()
        if not rgba.parse(str(self.store.get(path, default))):
            rgba.parse(default)
        btn.set_rgba(rgba)

        def cb(b, _):
            c = b.get_rgba()
            hexs = "#%02x%02x%02x" % (round(c.red * 255), round(c.green * 255), round(c.blue * 255))
            self.store.set_value(path, hexs, pid, title)
            row.add_css_class("changed")
        btn.connect("notify::rgba", cb)
        row.add_suffix(btn)
        return row

    def icon_btn(self, icon, tip, fn, css=None):
        b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER, css_classes=["flat"] + (css or []))
        b.connect("clicked", lambda *_: fn())
        return b

    # ---- pages: look & feel ---------------------------------------------------------------
    def pg_layout(self):
        pg, L = self.page(), ["layout"]
        g = self.group(pg, "General")
        g.add(self.r_spin("layout", "Gaps", "Space between windows, in logical pixels", L + ["gaps"], 0, 128, 1, 0, 16))
        g.add(self.r_combo("layout", "Center focused column", "When to keep the focused column centered",
                           L + ["center-focused-column"], ["never", "always", "on-overflow"], unset=True))
        g.add(self.r_switch("layout", "Always center a single column", "Center the column when it is alone on the workspace",
                            L + ["always-center-single-column"]))
        g.add(self.r_spin("layout", "Default column width", "Proportion of the screen for new windows",
                          L + ["default-column-width", "proportion"], 0.1, 1.0, 0.05, 2, 0.5))
        g = self.group(pg, "Focus ring")
        fr = L + ["focus-ring"]
        g.add(self.r_switch("layout", "Enabled", "Outline drawn around the focused window", fr, "enabled"))
        g.add(self.r_spin("layout", "Width", "Ring thickness in pixels", fr + ["width"], 0, 32, 1, 0, 4))
        g.add(self.r_color("layout", "Active color", fr + ["active-color"], "#7fc8ff"))
        g.add(self.r_color("layout", "Inactive color", fr + ["inactive-color"], "#505050"))
        g = self.group(pg, "Border")
        bd = L + ["border"]
        g.add(self.r_switch("layout", "Enabled", "Border drawn inside the window edge", bd, "enabled", default_enabled=False))
        g.add(self.r_spin("layout", "Width", "Border thickness in pixels", bd + ["width"], 0, 32, 1, 0, 4))
        g.add(self.r_color("layout", "Active color", bd + ["active-color"], "#ffc87f"))
        g.add(self.r_color("layout", "Inactive color", bd + ["inactive-color"], "#505050"))
        g = self.group(pg, "Shadow")
        sh = L + ["shadow"]
        g.add(self.r_switch("layout", "Enabled", "Drop shadow behind windows", sh, "on"))
        g.add(self.r_spin("layout", "Softness", "Blur radius", sh + ["softness"], 0, 100, 1, 0, 30))
        g.add(self.r_spin("layout", "Spread", "Shadow size", sh + ["spread"], 0, 100, 1, 0, 5))
        return pg

    def pg_animations(self):
        pg, A = self.page(), ["animations"]
        g = self.group(pg, "Animations")
        g.add(self.r_switch("animations", "Enabled", "Turn all animations on or off", A, "enabled"))
        g.add(self.r_spin("animations", "Slowdown", "1.0 = normal speed, higher = slower", A + ["slowdown"], 0.1, 10, 0.1, 2, 1.0))
        return pg

    def pg_cursor(self):
        pg, C = self.page(), ["cursor"]
        g = self.group(pg, "Cursor")
        g.add(self.r_entry("cursor", "Cursor theme", C + ["xcursor-theme"]))
        g.add(self.r_spin("cursor", "Cursor size", "", C + ["xcursor-size"], 8, 128, 1, 0, 24))
        g.add(self.r_switch("cursor", "Hide when typing", "", C + ["hide-when-typing"]))
        g.add(self.r_spin("cursor", "Hide after inactivity (ms)", "0 keeps it visible", C + ["hide-after-inactive-ms"],
                          0, 60000, 100, 0, 0))
        return pg

    # ---- pages: input ----------------------------------------------------------------------------
    def pg_keyboard(self):
        pg, KB = self.page(), ["input", "keyboard"]
        g = self.group(pg, "Layout (XKB)")
        for title, key in (("Layout", "layout"), ("Variant", "variant"), ("Options", "options"), ("Model", "model")):
            g.add(self.r_entry("keyboard", title, KB + ["xkb", key]))
        g = self.group(pg, "Typing")
        g.add(self.r_spin("keyboard", "Repeat delay (ms)", "Delay before a held key repeats", KB + ["repeat-delay"], 50, 2000, 10, 0, 600))
        g.add(self.r_spin("keyboard", "Repeat rate", "Repeats per second", KB + ["repeat-rate"], 1, 120, 1, 0, 25))
        g.add(self.r_switch("keyboard", "Num Lock on at startup", "", KB + ["numlock"]))
        g.add(self.r_combo("keyboard", "Track layout", "Keep one layout globally or per window", KB + ["track-layout"],
                           ["global", "window"], unset=True))
        return pg

    def pg_pointer(self):
        pg, I_ = self.page(), ["input"]
        g = self.group(pg, "Focus")
        g.add(self.r_switch("pointer", "Focus follows mouse", "Focus windows by hovering them", I_ + ["focus-follows-mouse"]))
        g.add(self.r_switch("pointer", "Warp mouse to focus", "Move the pointer to newly focused windows", I_ + ["warp-mouse-to-focus"]))
        for title, key, touch in (("Touchpad", "touchpad", True), ("Mouse", "mouse", False)):
            P = I_ + [key]
            g = self.group(pg, title)
            g.add(self.r_switch("pointer", "Enabled", "", P, "enabled"))
            if touch:
                g.add(self.r_switch("pointer", "Tap to click", "", P + ["tap"]))
                g.add(self.r_switch("pointer", "Disable while typing", "", P + ["dwt"]))
                g.add(self.r_switch("pointer", "Disable while trackpoint in use", "", P + ["dwtp"]))
                g.add(self.r_switch("pointer", "Disable with external mouse", "", P + ["disabled-on-external-mouse"]))
            g.add(self.r_switch("pointer", "Natural scrolling", "", P + ["natural-scroll"]))
            g.add(self.r_switch("pointer", "Middle-click emulation", "Press both buttons for a middle click", P + ["middle-emulation"]))
            g.add(self.r_spin("pointer", "Pointer speed", "-1 slowest, 1 fastest", P + ["accel-speed"], -1, 1, 0.1, 2, 0))
            g.add(self.r_combo("pointer", "Acceleration profile", "", P + ["accel-profile"], ["adaptive", "flat"], unset=True))
            if touch:
                g.add(self.r_combo("pointer", "Scroll method", "", P + ["scroll-method"],
                                   ["two-finger", "edge", "on-button-down", "no-scroll"], unset=True))
                g.add(self.r_combo("pointer", "Click method", "", P + ["click-method"],
                                   ["button-areas", "clickfinger"], unset=True))
        return pg

    def nth(self, path, i):
        items = self.store.children_of(path)
        return items[i] if i < len(items) else None

    def pg_keybinds(self):
        pg = self.page()
        search = Gtk.SearchEntry(placeholder_text="Filter keybinds…")
        sg = self.group(pg)
        sg.add(search)
        g = self.group(pg, "Keybinds")
        add = Gtk.Button(label="Add keybind", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        g.set_header_suffix(add)
        rows = []
        for i, (doc, node) in enumerate(self.store.children_of(["binds"])):
            action = " ".join(l.strip() for l in self.store.body_text(doc, node).splitlines())
            ro = not self.store.is_editable(doc)
            row = Adw.ActionRow(title=esc(node.name), subtitle=esc(action + (f"   —   {doc.path.name}" if ro else "")))
            row.set_subtitle_lines(1)
            b1 = self.icon_btn("document-edit-symbolic", "Edit", lambda i=i: self.edit_bind(i))
            b2 = self.icon_btn("user-trash-symbolic", "Delete", lambda i=i: self.del_bind(i))
            for b in ((b1, b2) if ro else ()):
                b.set_sensitive(False)
                b.set_tooltip_text(f"Defined in {doc.path.name}: read-only in separate-file mode. "
                                   "Add the same key here to override it.")
            row.add_suffix(b1)
            row.add_suffix(b2)
            g.add(row)
            rows.append((row, f"{node.name} {action}".lower()))
        search.connect("search-changed", lambda e: [r.set_visible(e.get_text().lower() in t) for r, t in rows])
        add.connect("clicked", lambda *_: self.form(
            "Add keybind", [("Key combination", "Mod+"), ("Action", 'spawn "foot"')], "Add",
            lambda v: v[0] and v[1] and (self.store.add_child_line(
                ["binds"], f"{v[0]} {{ {v[1].rstrip(';')}; }}", "keybinds", f"add {v[0]}"), self.rebuild_page("keybinds")),
            body="Actions use niri's syntax, e.g.  spawn \"foot\"   or   close-window"))
        return pg

    def edit_bind(self, i):
        loc = self.nth(["binds"], i)
        if not loc:
            return
        doc, node = loc
        if not self.store.is_editable(doc):
            return
        action = "; ".join(l.strip().rstrip(";") for l in self.store.body_text(doc, node).splitlines() if l.strip())

        def ok(v):
            if not v[0] or not v[1]:
                return
            cur = self.nth(["binds"], i)
            if cur:
                self.store.rewrite_bind(cur[0], cur[1], v[0], v[1], "keybinds", f"edit {v[0]}")
                self.rebuild_page("keybinds")
        self.form("Edit keybind", [("Key combination", node.name), ("Action", action)], "Apply", ok)

    def del_bind(self, i):
        loc = self.nth(["binds"], i)
        if loc and self.store.is_editable(loc[0]):
            self.store.delete_node(loc[0], loc[1], "keybinds", f"remove {loc[1].name}")
            self.rebuild_page("keybinds")

    # ---- pages: monitors ------------------------------------------------------------------------------
    def pg_monitors(self):
        pg, st = self.page(), self.store
        mons = niri_outputs()
        for d, n in st.find_all(["output"]):
            name = n.arg0
            if name and name not in mons:
                mons[name] = dict(make="", model="", modes=[], cur_idx=None, vrr=False, x=0, y=0,
                                  scale=1.0, transform="normal", lw=None, lh=None)
        if not mons:
            g = self.group(pg)
            g.add(Adw.StatusPage(title="No monitors found", icon_name="video-display-symbolic",
                                 description="Start niri to detect outputs, or add an  output \"NAME\" { }  block."))
            return pg
        names = sorted(mons, key=lambda n: (mons[n]["x"], n))
        mons = {n: mons[n] for n in names}
        g = self.group(pg, desc="Drag monitors to reposition them. Edges snap together.")
        self.canvas = MonitorCanvas(self, mons)
        g.add(self.canvas)

        for idx, name in enumerate(names, 1):
            info, base = mons[name], [("output", name)]
            title = f"{idx}. {info['make']} {info['model']}".strip(" .") if info["model"] else f"{idx}. {name}"
            g = self.group(pg, title)
            suffix = Gtk.Box(spacing=10, valign=Gtk.Align.CENTER)
            suffix.append(Gtk.Label(label=name, css_classes=["dim-label"]))
            sw = Gtk.Switch(valign=Gtk.Align.CENTER)
            sw.set_active(st.find(base + ["off"]) is None)

            def tog(s, _, base=base, name=name):
                st.set_flag(base + ["off"], not s.get_active(), "monitors", f"{name} enabled")
            sw.connect("notify::active", tog)
            suffix.append(sw)
            g.set_header_suffix(suffix)

            if info["modes"]:
                modes = sorted(set(info["modes"]), key=lambda m: (-m[0] * m[1], -m[2]))
                row = Adw.ComboRow(title="Resolution", subtitle="Resolution and refresh rate")
                row.set_model(Gtk.StringList.new([f"{w}x{h}@{r / 1000:.2f} Hz" for w, h, r in modes]))
                cfg, sel = st.get(base + ["mode"]), 0
                m = re.match(r"(\d+)x(\d+)(?:@([\d.]+))?", str(cfg)) if cfg else None
                if m:
                    w, h, rate = int(m.group(1)), int(m.group(2)), float(m.group(3) or 0)
                    for k, (mw, mh, mr) in enumerate(modes):
                        if (mw, mh) == (w, h) and (not rate or abs(mr / 1000 - rate) < 0.05):
                            sel = k
                            break
                elif info["cur_idx"] is not None:
                    sel = modes.index(info["modes"][info["cur_idx"]])
                row.set_selected(sel)

                def mode_cb(r, _, modes=modes, base=base, name=name):
                    w, h, rt = modes[r.get_selected()]
                    st.set_value(base + ["mode"], f"{w}x{h}@{rt / 1000:.3f}", "monitors", f"{name} resolution")
                    r.add_css_class("changed")
                    self.canvas.queue_draw()
                row.connect("notify::selected", mode_cb)
                g.add(row)

            scale = float(st.get(base + ["scale"], info["scale"]) or 1)
            srow = Adw.SpinRow.new(Gtk.Adjustment(value=scale, lower=0.5, upper=4, step_increment=0.25,
                                                  page_increment=0.5), 0, 2)
            srow.set_title("Scale")
            srow.set_subtitle("Display scaling factor")

            def scale_cb(r, _, base=base, name=name):
                st.set_value(base + ["scale"], round(r.get_value(), 2), "monitors", f"{name} scale")
                r.add_css_class("changed")
                self.canvas.queue_draw()
            srow.connect("notify::value", scale_cb)
            g.add(srow)

            trow = Adw.ComboRow(title="Transform", subtitle="Screen rotation")
            trow.set_model(Gtk.StringList.new([l for _, l in TRANSFORMS]))
            cur = str(st.get(base + ["transform"], info["transform"]))
            vals = [v for v, _ in TRANSFORMS]
            trow.set_selected(vals.index(cur) if cur in vals else 0)

            def tr_cb(r, _, base=base, name=name):
                st.set_value(base + ["transform"], vals[r.get_selected()], "monitors", f"{name} transform")
                r.add_css_class("changed")
                self.canvas.queue_draw()
            trow.connect("notify::selected", tr_cb)
            g.add(trow)

            if info["vrr"] or st.find(base + ["variable-refresh-rate"]):
                g.add(self.r_switch("monitors", "Variable refresh rate", "Adaptive sync (FreeSync / G-Sync)",
                                    base + ["variable-refresh-rate"]))
        return pg

    # ---- pages: rules / startup / misc ----------------------------------------------------------------
    @staticmethod
    def rule_summary(node):
        parts = [", ".join(f"{k}: {v.value}" for k, v in m.props.items()) or "any" for m in node.kids("match")]
        others = []
        for c in node.kids():
            if c.name in ("match", "exclude"):
                continue
            others.append(c.name + (" " + " ".join(str(a.value) for a in c.args) if c.args else ""))
        return " | ".join(parts) or "All windows", " · ".join(others) or "no properties"

    def pg_rules(self):
        pg = self.page()
        for kind, title, tmpl in (("window-rule", "Window rules", 'match app-id="^firefox$"\nopen-floating true'),
                                  ("layer-rule", "Layer rules", 'match namespace="^notifications$"\nblock-out-from "screencast"')):
            g = self.group(pg, title)
            add = Gtk.Button(label="Add rule", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
            g.set_header_suffix(add)
            add.connect("clicked", lambda *_, kind=kind, tmpl=tmpl: self.text_dialog(
                f"New {kind}", "One niri rule property per line.", tmpl,
                lambda t, kind=kind: (self.store.add_top_node(kind, t, page="rules", desc=f"add {kind}"),
                                      self.rebuild_page("rules"))))
            for i, (doc, node) in enumerate(self.store.top_nodes(kind)):
                t, sub = self.rule_summary(node)
                ro = not self.store.is_editable(doc)
                row = Adw.ActionRow(title=esc(t), subtitle=esc(sub + (f"   —   {doc.path.name}" if ro else "")))
                row.set_subtitle_lines(2)
                b1 = self.icon_btn("document-edit-symbolic", "Edit", lambda i=i, kind=kind: self.edit_rule(kind, i))
                b2 = self.icon_btn("user-trash-symbolic", "Delete", lambda i=i, kind=kind: self.del_rule(kind, i))
                for b in ((b1, b2) if ro else ()):
                    b.set_sensitive(False)
                    b.set_tooltip_text(f"Defined in {doc.path.name}: read-only in separate-file mode")
                row.add_suffix(b1)
                row.add_suffix(b2)
                g.add(row)
            if not self.store.top_nodes(kind):
                g.add(Adw.ActionRow(title="No rules yet", css_classes=["dim-label"]))
        return pg

    def edit_rule(self, kind, i):
        items = self.store.top_nodes(kind)
        if i >= len(items):
            return
        doc, node = items[i]
        if not self.store.is_editable(doc):
            return

        def ok(text):
            cur = self.store.top_nodes(kind)
            if i < len(cur):
                self.store.set_body(cur[i][0], cur[i][1], text, "rules", f"edit {kind}")
                self.rebuild_page("rules")
        self.text_dialog(f"Edit {kind}", "Raw KDL, one entry per line.", self.store.body_text(doc, node), ok)

    def del_rule(self, kind, i):
        items = self.store.top_nodes(kind)
        if i < len(items) and self.store.is_editable(items[i][0]):
            self.store.delete_node(items[i][0], items[i][1], "rules", f"remove {kind}")
            self.rebuild_page("rules")

    def pg_autostart(self):
        pg, names = self.page(), ("spawn-at-startup", "spawn-sh-at-startup")
        g = self.group(pg, "Programs started with niri", "Press the check mark (or Enter) to apply an edit.")
        items = self.store.top_nodes(*names)
        for i, (doc, node) in enumerate(items):
            sh = node.name == "spawn-sh-at-startup"
            text = str(node.arg0 or "") if sh else shlex.join(str(a.value) for a in node.args)
            row = Adw.EntryRow(title="Shell command" if sh else "Command")
            row.set_text(text)
            row.set_show_apply_button(True)
            row.connect("apply", lambda r, i=i, sh=sh: self.apply_autostart(i, r.get_text(), sh))
            rm = self.icon_btn("user-trash-symbolic", "Remove", lambda i=i: self.del_autostart(i))
            if not self.store.is_editable(doc):
                row.set_editable(False)
                row.set_show_apply_button(False)
                rm.set_sensitive(False)
                rm.set_tooltip_text(f"Defined in {doc.path.name}: read-only in separate-file mode")
            row.add_suffix(rm)
            g.add(row)
        if not items:
            g.add(Adw.ActionRow(title="Nothing configured", css_classes=["dim-label"]))
        g2 = self.group(pg, "Add program")
        new = Adw.EntryRow(title="Command, e.g.  waybar   or   foot -e htop")
        new.set_show_apply_button(True)

        def add(r):
            try:
                parts = shlex.split(r.get_text())
            except ValueError:
                return self.toast("Unbalanced quotes in command")
            if parts:
                self.store.add_top_node("spawn-at-startup", args=parts, page="autostart", desc=f"add {parts[0]}")
                self.rebuild_page("autostart")
        new.connect("apply", add)
        g2.add(new)
        return pg

    def apply_autostart(self, i, text, sh):
        items = self.store.top_nodes("spawn-at-startup", "spawn-sh-at-startup")
        if i >= len(items) or not text.strip() or not self.store.is_editable(items[i][0]):
            return
        try:
            parts = [text.strip()] if sh else shlex.split(text)
        except ValueError:
            return self.toast("Unbalanced quotes in command")
        self.store.replace_args(items[i][0], items[i][1], parts, "autostart", f"edit {parts[0]}")

    def del_autostart(self, i):
        items = self.store.top_nodes("spawn-at-startup", "spawn-sh-at-startup")
        if i < len(items) and self.store.is_editable(items[i][0]):
            self.store.delete_node(items[i][0], items[i][1], "autostart", "remove autostart entry")
            self.rebuild_page("autostart")

    def pg_env(self):
        pg = self.page()
        g = self.group(pg, "Environment variables", "Set for every program niri starts.")
        kids = self.store.children_of(["environment"])
        for doc, node in kids:
            row = Adw.EntryRow(title=node.name)
            row.set_text("" if node.arg0 is None else str(node.arg0))
            row.set_show_apply_button(True)
            row.connect("apply", lambda r, k=node.name: self.store.set_value(["environment", k], r.get_text(), "env", k))
            rm = self.icon_btn("user-trash-symbolic", "Remove", lambda k=node.name: self.del_env(k))
            if not self.store.is_editable(doc):
                rm.set_sensitive(False)
                rm.set_tooltip_text(f"Defined in {doc.path.name}. Edit the value to override it here.")
            row.add_suffix(rm)
            g.add(row)
        if not kids:
            g.add(Adw.ActionRow(title="No variables set", css_classes=["dim-label"]))
        g2 = self.group(pg, "Add variable")
        new = Adw.EntryRow(title="KEY=value")
        new.set_show_apply_button(True)

        def add(r):
            k, _, v = r.get_text().partition("=")
            if k.strip():
                self.store.set_value(["environment", k.strip()], v.strip(), "env", k.strip())
                self.rebuild_page("env")
        new.connect("apply", add)
        g2.add(new)
        return pg

    def del_env(self, key):
        self.store.delete_path(["environment", key], "env", f"remove {key}")
        self.rebuild_page("env")

    def pg_misc(self):
        pg = self.page()
        g = self.group(pg, "General")
        g.add(self.r_switch("misc", "Prefer server-side decorations", "Ask apps not to draw their own title bars",
                            ["prefer-no-csd"]))
        g.add(self.r_entry("misc", "Screenshot path", ["screenshot-path"]))
        g.add(self.r_switch("misc", "Skip hotkey overlay at startup", "", ["hotkey-overlay", "skip-at-startup"]))
        g.add(self.r_switch("misc", "Disable primary clipboard", "Middle-click paste", ["clipboard", "disable-primary"]))
        g.add(self.r_switch("misc", "Hide config-error notification", "", ["config-notification", "disable-failed"]))
        return pg

    # ---- page: import & backup ---------------------------------------------------------------------------
    def pg_import(self):
        pg, imp = self.page(), self.imp
        mg = self.group(pg, "Where changes are saved",
                        "Separate file (recommended): your own files are never touched. Changes go to "
                        + tilde(self.store.managed_path()) + ", which config.kdl must include last. Keybinds, rules "
                        "and autostart entries from your other files stay read-only. In place: edits your .kdl files.")
        mrow = Adw.ComboRow(title="Save mode")
        mrow.set_model(Gtk.StringList.new(["Separate file (recommended)", "Edit my existing files in place"]))
        mrow.set_selected(0 if self.store.mode == "managed" else 1)

        def mode_cb(r, _):
            new = "managed" if r.get_selected() == 0 else "inplace"
            if new == self.store.mode:
                return
            if self.store.dirty:
                self.toast("Save or discard your pending changes first")
                r.set_selected(0 if self.store.mode == "managed" else 1)
                return
            self.store.set_mode(new)
            self.store.load()

            def later():
                self.rebuild_all()
                self.refresh_state()
                return False
            GLib.idle_add(later)
        mrow.connect("notify::selected", mode_cb)
        mg.add(mrow)
        if self.store.managed_mode:
            st_ = self.store.include_status()
            text = {"ok": "Present: ", "missing": "Missing: ", "not-last": "Present, but not last: "}[st_]
            irow = Adw.ActionRow(title="Include in config.kdl", subtitle=esc(text + self.store.include_line()))
            if st_ == "missing":
                ab = Gtk.Button(label="Add it", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
                ab.connect("clicked", lambda *_: self.include_flow())
                irow.add_suffix(ab)
            mg.add(irow)

        g = self.group(pg, "Import an existing niri config",
                       "Pick a saved config (the niri folder, a folder of .kdl files like cfg/, a dotfiles repo, or "
                       "a config.kdl). It is placed in " + tilde(self.store.root) + "/ for you.")
        src_row = Adw.ActionRow(title="Source", subtitle=esc(tilde(imp["src"])) if imp["src"] else "Nothing selected yet")
        b_dir = Gtk.Button(label="Folder…", valign=Gtk.Align.CENTER)
        b_file = Gtk.Button(label="File…", valign=Gtk.Align.CENTER)
        src_row.add_suffix(b_dir)
        src_row.add_suffix(b_file)
        g.add(src_row)

        og = self.group(pg, "Options")
        for key, title, sub in (("move", "Move files", "Off = copy them and leave the originals where they are"),
                                ("backup", "Back up my current config first",
                                 esc("Saved next to it as " + tilde(self.store.root) + ".bak-<date>")),
                                ("gen", "Create config.kdl if missing",
                                 "Generates include lines for every file in cfg/")):
            r = Adw.SwitchRow(title=title, subtitle=sub)
            r.set_active(imp[key])
            r.connect("notify::active", lambda w, _, key=key: imp.__setitem__(key, w.get_active()))
            og.add(r)

        prev = self.group(pg, "What will happen")
        prev_rows = []
        go_group = self.group(pg)
        go = Gtk.Button(label="Import", halign=Gtk.Align.END, css_classes=["suggested-action", "pill"], sensitive=False)
        go_group.add(go)

        def refresh():
            for r in prev_rows:
                prev.remove(r)
            prev_rows.clear()
            plan = imp["plan"]
            if plan is None:
                rows = [Adw.ActionRow(title="Choose a source to see what will be imported", css_classes=["dim-label"])]
            elif plan.error:
                rows = [Adw.ActionRow(title=esc(plan.error), css_classes=["error"])]
            else:
                rows = []
                for it in plan.items:
                    sub = f"from {esc(tilde(it.src))}" + ("  ·  replaces existing" if it.replaces else "  ·  new")
                    kind = "folder-symbolic" if it.src.is_dir() else "text-x-generic-symbolic"
                    r = Adw.ActionRow(title=esc(tilde(it.dst)), subtitle=sub)
                    r.add_prefix(Gtk.Image.new_from_icon_name(kind))
                    rows.append(r)
                for n in plan.notes:
                    rows.append(Adw.ActionRow(title=esc(n), css_classes=["dim-label"]))
            for r in rows:
                prev.add(r)
                prev_rows.append(r)
            go.set_sensitive(bool(plan and not plan.error and plan.items))

        def picked(path):
            imp["src"] = path
            imp["plan"] = plan_import(path, self.store.root)
            src_row.set_subtitle(esc(tilde(path)))
            refresh()

        def choose(folder):
            dlg = Gtk.FileDialog(title="Select niri config " + ("folder" if folder else "file"))

            def done(d, res):
                try:
                    f = d.select_folder_finish(res) if folder else d.open_finish(res)
                except GLib.Error:
                    return
                if f and f.get_path():
                    picked(f.get_path())
            (dlg.select_folder if folder else dlg.open)(self, None, done)

        b_dir.connect("clicked", lambda *_: choose(True))
        b_file.connect("clicked", lambda *_: choose(False))
        go.connect("clicked", lambda *_: self.do_import())
        refresh()

        dg = self.group(pg, "Detected config files")
        for d in self.store.docs:
            if not d.existed and not d.text:
                continue
            note = "written by this app" if (d is self.store.managed and self.store.managed_mode) else ""
            r = Adw.ActionRow(title=esc(d.path.relative_to(self.store.root)) if self.store.root in d.path.parents
                              else esc(d.path.name), subtitle=esc(d.error) if d.error else note)
            if d.error:
                r.add_css_class("error")
            dg.add(r)
        if not self.store.docs:
            dg.add(Adw.ActionRow(title="No .kdl files in " + tilde(self.store.root), css_classes=["dim-label"]))
        return pg

    def do_import(self):
        imp = self.imp
        plan = imp["plan"]
        if not plan or plan.error:
            return
        if self.store.dirty:
            return self.toast("Save or discard your pending changes first")
        replaced = [tilde(i.dst) for i in plan.items if i.replaces]
        body = f"{len(plan.items)} item(s) will be {'moved' if imp['move'] else 'copied'} into {tilde(self.store.root)}."
        if replaced:
            body += "\n\nThese will be replaced: " + ", ".join(replaced) + "."
            body += "\nA backup of your current config is made first." if imp["backup"] else \
                "\nBackup is OFF - the replaced files cannot be recovered."
        risky = bool(replaced) and not imp["backup"]
        d, show = self.dialog("Import config?", body, [
            ("cancel", "Cancel", None),
            ("go", "Import", Adw.ResponseAppearance.DESTRUCTIVE if risky else Adw.ResponseAppearance.SUGGESTED)])

        def resp(dlg, r):
            if r != "go":
                return
            try:
                res = run_import(plan, imp["move"], imp["backup"], imp["gen"], self.store.root)
            except Exception as e:  # noqa: BLE001
                return self.toast(f"Import failed: {e}")
            self.store.load()
            imp["src"], imp["plan"] = None, None
            self.rebuild_all()
            msg = f"Imported {res.moved} item(s)."
            if res.backup:
                msg += f" Backup: {tilde(res.backup)}."
            if res.generated_main:
                msg += " Created config.kdl."
            ok, err = self.store.validate()
            if ok:
                self.toast(msg)
            else:
                d2, show2 = self.dialog("Imported, but niri reports a problem",
                                        msg + "\n\n" + err[:600], [("ok", "OK", None)])
                show2()
        d.connect("response", resp)
        show()


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=getattr(Gio.ApplicationFlags, "DEFAULT_FLAGS", Gio.ApplicationFlags.FLAGS_NONE))

    def do_activate(self):
        win = self.props.active_window
        if not win:
            css = Gtk.CssProvider()
            try:
                css.load_from_string(CSS)
            except AttributeError:
                css.load_from_data(CSS.encode())
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            win = Window(self)
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
