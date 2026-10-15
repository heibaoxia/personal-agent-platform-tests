#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic read-only validator for the `agent-scaffold` file contract.

Contract: openspec/changes/create-extensible-agent-scaffold/specs/agent-scaffold/spec.md
Layout:   <workspace>/AGENTS.md plus optional .agent/{context.yaml,behaviors.md,
          skills/<id>.md,workflows/<id>.md,integrations/<id>.md}

Usage:
    python tools/validate_scaffold.py <workspace> [--json]
    python tools/validate_scaffold.py --self-test

Exit codes: 0 pass, 1 contract error, 2 usage error.

Self-check (no filesystem writes, no fixtures):
    python tools/validate_scaffold.py --self-test

Guarantees: standard library only; reads the target workspace and never writes,
deletes or modifies it; no network access, no process spawning, no third-party
imports. JSON output is byte-identical for the same input.

# ponytail: YAML is a hand-written subset (maps, sequences, scalars, flow
# collections tracked by brackets). Flattened nesting inside a flow collection
# (e.g. `[a: [b: 1]]`) is refused as unsupported instead of implemented.
# Upgrade when the contract needs nested flow mappings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys

# ---------------------------------------------------------------- constants --

AGENT_FILE = "AGENTS.md"
DESCRIPTION_FILE = "scaffold-description.md"
CONTEXT_FILE = ".agent/context.yaml"
BEHAVIOR_FILE = ".agent/behaviors.md"
OBJECT_ROOTS = {
    "skills": ".agent/skills/",
    "workflows": ".agent/workflows/",
    "integrations": ".agent/integrations/",
}
COLLECTION_ORDER = ("skills", "workflows", "integrations")

CONTRACT_VERSION = "1"
CONTEXT_STATES = ("temporary", "pending", "confirmed")
CONTEXT_WRITE_POLICY = "confirm"
SIDE_EFFECTS = ("none", "writes-workspace", "calls-external")
CONFIRMATION = ("not-required", "required")
INTEGRATION_KINDS = ("mcp", "cli", "http")
INTEGRATION_FALLBACKS = ("block", "degrade")

STR, BOOL, SLUG, STR_LIST, STEP_LIST, OBJECT_INDEX = (
    "string", "boolean", "slug", "string-list", "step-list", "object-index",
)
REQUIREMENTS_LIST = "requirements-list"
SOURCE_FILES = "context-source-files"

# Frontmatter is the machine truth for the index; unknown keys are reported.
ENTRY_SCHEMA = {
    "agentId": (SLUG, None, None),
    # The contract version is the literal string "1"; anything else is an enum error.
    "contractVersion": (STR, None, (CONTRACT_VERSION,)),
    "purpose": (STR, None, None),
    "skills": (OBJECT_INDEX, None, None),
    "workflows": (OBJECT_INDEX, None, None),
    "integrations": (OBJECT_INDEX, None, None),
}
REQUIRED_ENTRY_FIELDS = ("agentId", "contractVersion", "purpose")
INDEX_COLLECTIONS = ("skills", "workflows", "integrations")

OBJECT_SCHEMAS = {
    "skills": {
        "id": (SLUG, None, None),
        "purpose": (STR, None, None),
        "triggers": (STR_LIST, None, None),
        "excludes": (STR_LIST, None, None),
        "inputs": (STR_LIST, None, None),
        "outputs": (STR_LIST, None, None),
        "sideEffects": (STR, None, SIDE_EFFECTS),
        "confirmation": (STR, None, CONFIRMATION),
        "implementation": (STR, None, None),
        "requires": (REQUIREMENTS_LIST, None, None),
        "readBefore": (STR_LIST, None, None),
    },
    "workflows": {
        "id": (SLUG, None, None),
        "purpose": (STR, None, None),
        "steps": (STEP_LIST, None, None),
        "inputs": (STR_LIST, None, None),
        "outputs": (STR_LIST, None, None),
        "usesSkills": (STR_LIST, None, None),
    },
    "integrations": {
        "id": (SLUG, None, None),
        "kind": (STR, None, INTEGRATION_KINDS),
        "purpose": (STR, None, None),
        "requires": (STR_LIST, None, None),
        "fallback": (STR, None, INTEGRATION_FALLBACKS),
        "fallbackNote": (STR, None, None),
        "enabled": (BOOL, None, None),
    },
}
REQUIRED_OBJECT_FIELDS = {
    "skills": ("id", "purpose", "triggers", "excludes", "inputs", "outputs",
               "sideEffects", "confirmation", "implementation"),
    "workflows": ("id", "purpose", "steps"),
    "integrations": ("id", "kind", "purpose", "requires", "fallback",
                     "fallbackNote", "enabled"),
}
OPTIONAL_OBJECT_FIELDS = {
    "skills": ("workflow", "workflows"),   # skill -> workflow references
}

CONTEXT_SCHEMA = {
    "categories": (STR_LIST, None, None),
    "sources": (STR_LIST, None, None),
    "sourceFiles": (SOURCE_FILES, None, None),
    "states": (STR_LIST, None, None),
    "writePolicy": (STR, None, None),
    "retention": (STR, None, None),
}
REQUIRED_CONTEXT_FIELDS = ("categories", "sources", "states", "writePolicy")

STEPS_MIN = 2
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_INT_RE = re.compile(r"^[+-]?\d+$")
_NUM_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)[eE]?[+-]?\d*$")
JSON_SCHEMA_VERSION = 1


# ------------------------------------------------------------------- errors --

def err(code: str, path: str, line, message: str) -> dict:
    return {"code": code, "path": path, "line": line, "message": message}


class Fail(Exception):
    """One deterministic contract error, raised by the validators."""

    def __init__(self, code: str, message: str, line=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line


class Collector:
    """Ordered error list with a dedupe guard for multi-pass checks."""

    def __init__(self):
        self.items = []
        self._seen = set()

    def add(self, e: dict) -> None:
        key = (e["code"], e["path"], e["line"], e["message"])
        if key not in self._seen:
            self._seen.add(key)
            self.items.append(e)

    def fail(self, code: str, path: str, line, message: str) -> None:
        self.add(err(code, path, line, message))


def sorted_errors(errors):
    return sorted(errors, key=lambda e: (e["path"], -1 if e["line"] is None else e["line"], e["code"]))


# -------------------------------------------------------- limited YAML parse --

class YamlFail(Exception):
    """Parser rejection; reported as SCAFFOLD_YAML_UNSUPPORTED or _SYNTAX."""

    def __init__(self, kind: str, detail: str, line: int):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.line = line


class Unsupported(YamlFail):
    def __init__(self, detail: str, line: int):
        super().__init__("unsupported", detail, line)


class Syntax(YamlFail):
    def __init__(self, detail: str, line: int):
        super().__init__("syntax", detail, line)


class Block:
    """Parsed node plus the 1-based source line of every key / sequence item."""

    __slots__ = ("data", "lines")

    def __init__(self, data, lines):
        self.data = data
        self.lines = lines


def split_lines(text: str):
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def indent_of(line: str, lineno: int):
    """Return (indent, stripped). Tabs are refused: indentation must be spaces."""
    stripped = line.lstrip(" ")
    if stripped.startswith("\t"):
        raise Syntax("tab used for indentation", lineno)
    return len(line) - len(stripped), stripped


def is_blank(line: str) -> bool:
    stripped = line.lstrip(" ")
    return stripped == "" or stripped.startswith("#")


def _scan_quote(text: str, i: int, lineno: int) -> int:
    """Return the index just past the quoted scalar starting at i."""
    quote = text[i]
    j = i + 1
    while j < len(text):
        if quote == "'":
            if text[j] == "'":
                if j + 1 < len(text) and text[j + 1] == "'":
                    j += 2
                    continue
                return j + 1
            j += 1
        else:
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == '"':
                return j + 1
            j += 1
    raise Syntax("unterminated quoted scalar", lineno)


def unquote(token: str, lineno: int) -> str:
    if token.startswith('"'):
        if len(token) < 2 or not token.endswith('"'):
            raise Syntax("unterminated double-quoted scalar", lineno)
        out = []
        i = 1
        while i < len(token) - 1:
            ch = token[i]
            if ch != "\\":
                out.append(ch)
                i += 1
                continue
            i += 1
            if i >= len(token) - 1:
                raise Syntax("dangling escape in double-quoted scalar", lineno)
            esc = token[i]
            out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}.get(esc, esc))
            i += 1
        return "".join(out)
    if token.startswith("'"):
        if len(token) < 2 or not token.endswith("'"):
            raise Syntax("unterminated single-quoted scalar", lineno)
        return token[1:-1].replace("''", "'")
    raise Syntax("internal: unquote() called on an unquoted token", lineno)


def _flow_end(text: str, lineno: int):
    """Index just past the trailing bracket of a flow collection, and whether it was found."""
    quote = None
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if quote == "'":
                if ch == "'":
                    if i + 1 < len(text) and text[i + 1] == "'":
                        i += 2
                        continue
                    quote = None
            elif ch == "\\":
                i += 2
                continue
            elif ch == '"':
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return i + 1, True
            if depth < 0:
                raise Syntax("unbalanced flow collection", lineno)
        i += 1
    return len(text), False


def _resolve(text: str, lineno: int) -> Block:
    text = text.strip()
    if text[:1] == "[":
        return _flow_seq(text, lineno)
    if text[:1] == "{":
        return _flow_map(text, lineno)
    return _scalar(text, lineno)


def _flow_seq(text: str, lineno: int) -> Block:
    end, found = _flow_end(text, lineno)
    if not found:
        raise Syntax("unbalanced flow sequence", lineno)
    if text[end:].strip() != "":
        raise Syntax("trailing content after flow collection", lineno)
    body = text[1:end - 1].strip()
    if body == "":
        raise Syntax("empty flow sequence is not supported", lineno)
    data = []
    itemlines = []
    for part in _split_flow(body, lineno):
        token = part.strip()
        if token == "":
            raise Syntax("empty element in flow sequence", lineno)
        if token[:1] in "{[":
            if token[:1] == "{":
                raise Unsupported("mapping inside a flow sequence is not supported", lineno)
            data.append(_flow_seq(token, lineno).data)
            itemlines.append(lineno)
            continue
        if ":" in token:
            raise Unsupported("mapping inside a flow sequence is not supported", lineno)
        data.append(_scalar(token, lineno).data)
        itemlines.append(lineno)
    return Block(data, itemlines)


def _flow_map(text: str, lineno: int) -> Block:
    end, found = _flow_end(text, lineno)
    if not found:
        raise Syntax("unbalanced flow mapping", lineno)
    if text[end:].strip() != "":
        raise Syntax("trailing content after flow collection", lineno)
    body = text[1:end - 1].strip()
    if body == "":
        raise Syntax("empty flow mapping is not supported", lineno)
    data = {}
    keylines = {}
    for part in _split_flow(body, lineno):
        token = part.strip()
        if token == "":
            raise Syntax("empty element in flow mapping", lineno)
        key, rest = _split_key(token, lineno)
        if key in data:
            raise Syntax("duplicate key '%s'" % key, lineno)
        value = rest.strip()
        if value[:1] in "{[":
            if value[:1] == "{":
                raise Unsupported("nested mapping inside a flow mapping is not supported", lineno)
            data[key] = _flow_seq(value, lineno).data
        elif ":" in value:
            raise Unsupported("nested mapping inside a flow mapping is not supported", lineno)
        else:
            data[key] = _scalar(value, lineno).data
        keylines[key] = lineno
    return Block(data, keylines)


def _split_flow(text: str, lineno: int):
    """Split `a, b, [c]` on top-level commas, tracking quotes and brackets."""
    parts = []
    depth = 0
    quote = None
    start = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if quote == "'":
                if ch == "'":
                    if i + 1 < len(text) and text[i + 1] == "'":
                        i += 2
                        continue
                    quote = None
            elif ch == "\\":
                i += 2
                continue
            elif ch == '"':
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth < 0:
                raise Syntax("unbalanced flow collection", lineno)
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    if quote:
        raise Syntax("unterminated quoted scalar", lineno)
    if depth != 0:
        raise Syntax("unbalanced flow collection", lineno)
    parts.append(text[start:])
    return parts


def _split_key(stripped: str, lineno: int):
    """Split one `key: rest` line, honouring a quoted key."""
    if not stripped:
        raise Syntax("empty line in block", lineno)
    if stripped[0] in "'\"":
        end = _scan_quote(stripped, 0, lineno)
        rest = stripped[end:]
        if not rest.startswith(":"):
            raise Syntax("quoted key without ':'", lineno)
        return unquote(stripped[:end], lineno), rest[1:]
    idx = stripped.find(":")
    if idx < 0:
        raise Syntax("expected 'key: value'", lineno)
    return stripped[:idx].strip(), stripped[idx + 1:]


def _strip_comment(text: str, lineno: int) -> str:
    """Drop a trailing comment, but never inside quotes or a flow collection."""
    quote = None
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if quote == "'":
                if ch == "'":
                    if i + 1 < len(text) and text[i + 1] == "'":
                        i += 2
                        continue
                    quote = None
            elif ch == "\\":
                i += 2
                continue
            elif ch == '"':
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "#" and depth == 0 and (i == 0 or text[i - 1] in " \t"):
            return text[:i]
        i += 1
    return text


def _strip_scalar(token: str, lineno: int) -> str:
    token = _strip_comment(token, lineno)
    if token.strip()[:1] in "&*!":
        raise Unsupported("anchor, alias or tag is not supported", lineno)
    return token.strip()


def _scalar(text: str, lineno: int) -> Block:
    text = text.strip()
    if text == "" or text == "~" or text in ("null", "Null", "NULL"):
        return Block(None, None)
    if text[0] in "'\"":
        end = _scan_quote(text, 0, lineno)
        if text[end:].strip() != "":
            raise Syntax("trailing content after quoted scalar", lineno)
        return Block(unquote(text[:end], lineno), None)
    start = text[:1]
    if start in "&*!":
        raise Unsupported("anchor, alias or tag is not supported", lineno)
    if start in "|>":
        raise Unsupported("block scalar (| or >) is not supported", lineno)
    if start in "[{":
        return _resolve(text, lineno)
    if text in ("true", "True", "TRUE"):
        return Block(True, None)
    if text in ("false", "False", "FALSE"):
        return Block(False, None)
    if _INT_RE.match(text):
        return Block(int(text), None)
    if _NUM_RE.match(text):
        return Block(float(text), None)
    return Block(text, None)


def _inline_value(key: str, rest: str, lines, pos: int, indent: int, lineno: int) -> Block:
    """Value that appears on the key's own line."""
    text = rest.strip()
    if text == "":
        return Block(None, None)
    text = _strip_scalar(text, lineno)
    if text == "":
        return Block(None, None)
    return _resolve(text, lineno)


def _parse_map(lines, pos: int, indent: int):
    data = {}
    keylines = {}
    while pos < len(lines):
        line = lines[pos]
        if is_blank(line):
            pos += 1
            continue
        cur, stripped = indent_of(line, pos + 1)
        if cur < indent:
            break
        if stripped.startswith("- ") or stripped == "-":
            break
        if cur > indent:
            raise Syntax("unexpected indentation (expected %d spaces)" % indent, pos + 1)
        key, rest = _split_key(stripped, pos + 1)
        if key == "":
            raise Syntax("empty mapping key", pos + 1)
        if key in data:
            raise Syntax("duplicate key '%s'" % key, pos + 1)
        if key[:1] in "&*!":
            raise Unsupported("anchor, alias or tag is not supported", pos + 1)
        block = _inline_value(key, rest, lines, pos, indent, pos + 1)
        data[key] = block.data
        keylines[key] = pos + 1
        pos += 1
        if block.data is not None:
            continue
        nested_pos = pos
        while nested_pos < len(lines) and is_blank(lines[nested_pos]):
            nested_pos += 1
        if nested_pos >= len(lines):
            continue
        nested_indent, _ = indent_of(lines[nested_pos], nested_pos + 1)
        if nested_indent <= indent:
            continue
        child, pos = _parse_block(lines, nested_pos, nested_indent)
        data[key] = child.data
    return Block(data, keylines), pos


def _gather_item_body(lines, pos: int, indent: int, rest: str):
    """Body of a `- key: value` / `- - item` entry continued on the following lines."""
    body = [" " * (indent + 2) + rest]
    j = pos + 1
    while j < len(lines):
        if is_blank(lines[j]):
            j += 1
            continue
        child_indent, _ = indent_of(lines[j], j + 1)
        if child_indent <= indent:
            break
        j += 1
    body.extend(lines[pos + 1:j])
    return body, j


def _parse_seq(lines, pos: int, indent: int):
    data = []
    itemlines = []
    while pos < len(lines):
        line = lines[pos]
        if is_blank(line):
            pos += 1
            continue
        cur, stripped = indent_of(line, pos + 1)
        if cur < indent:
            break
        if not (stripped == "-" or stripped.startswith("- ")):
            break
        if cur > indent:
            raise Syntax("unexpected indentation (expected %d spaces)" % indent, pos + 1)
        itemlines.append(pos + 1)
        rest = stripped[1:].strip()
        if rest == "":
            child, pos = _parse_child(lines, pos, indent)
            data.append(child.data)
            continue
        rest = _strip_scalar(rest, pos + 1)
        if rest == "":
            child, pos = _parse_child(lines, pos, indent)
            data.append(child.data)
            continue
        if rest == "-" or rest.startswith("- "):
            body, pos = _gather_item_body(lines, pos, indent, rest)
            child, _ = _parse_seq(body, 0, indent + 2)
            data.append(child.data)
            continue
        if _looks_like_entry(rest):
            body, pos = _gather_item_body(lines, pos, indent, rest)
            child, _ = _parse_map(body, 0, indent + 2)
            data.append(child.data)
            continue
        data.append(_resolve(rest, pos + 1).data)
        pos += 1
    return Block(data, itemlines), pos


def _looks_like_entry(rest: str) -> bool:
    if rest[:1] in "{[":
        return True
    if rest[0] in "'\"":
        idx = _scan_quote(rest, 0, 1)
        return rest[idx:].lstrip().startswith(":")
    idx = rest.find(":")
    return idx >= 0 and re.match(r"^[A-Za-z0-9_-]+$", rest[:idx].strip()) is not None


def _parse_child(lines, pos: int, indent: int):
    """Block value of `key:` or `-`, on the following more-indented lines."""
    j = pos + 1
    while j < len(lines) and is_blank(lines[j]):
        j += 1
    if j >= len(lines):
        return Block(None, None), j
    child_indent, _ = indent_of(lines[j], j + 1)
    if child_indent <= indent:
        return Block(None, None), j
    return _parse_block(lines, j, child_indent)


def _parse_block(lines, pos: int, indent: int):
    """Every parse helper returns `(Block, next_position)`, never a bare Block."""
    while pos < len(lines) and is_blank(lines[pos]):
        pos += 1
    if pos >= len(lines):
        return Block(None, None), pos
    stripped = lines[pos].lstrip(" ")
    if stripped == "-" or stripped.startswith("- "):
        return _parse_seq(lines, pos, indent)
    return _parse_map(lines, pos, indent)


def parse_yaml_subset(text: str) -> Block:
    """Parse the supported YAML subset; raise Unsupported/Syntax otherwise."""
    block, _ = _parse_block(split_lines(text), 0, 0)   # a document starts at column 0
    return block


# ------------------------------------------------------------ object loading --

class Validator:
    def __init__(self, root: str, collector: Collector):
        self.root = root
        self.col = collector
        self.texts = {}
        self.entries = {c: {} for c in COLLECTION_ORDER}    # id -> index entry
        self.by_path = {c: {} for c in COLLECTION_ORDER}    # path -> index entry
        self.disk = {c: {} for c in COLLECTION_ORDER}       # path -> declared id / None
        self.disk_ids = {c: {} for c in COLLECTION_ORDER}   # declared id -> path
        self.entry_block = None
        self.refs = []            # (file, index collection, ref, line)
        self.external_effects = []  # (file, line)
        self.context_categories = set()
        self.context_refs = []    # (file, category, line)

    # -- paths and reading ---------------------------------------------------
    def abs_path(self, rel: str) -> str:
        return os.path.join(self.root, rel.replace("/", os.sep))

    def load_text(self, rel: str):
        """Read one file once; read failures become contract errors."""
        if rel in self.texts:
            return self.texts[rel]
        full = self.abs_path(rel)
        text = None
        try:
            if os.path.lexists(full) and not is_within(os.path.realpath(full), os.path.realpath(self.root)):
                self.col.fail("SCAFFOLD_PATH_ESCAPE", rel, None,
                              "symbolic link points outside the workspace")
                self.texts[rel] = None
                return None
            if os.path.isfile(full):
                with open(full, "r", encoding="utf-8-sig", newline="") as handle:
                    text = handle.read()
            else:
                self.col.fail("SCAFFOLD_FILE_MISSING", rel, None, "file not found: %s" % rel)
        except UnicodeDecodeError:
            self.col.fail("SCAFFOLD_FILE_NOT_UTF8", rel, None, "file is not valid UTF-8")
        except OSError as exc:
            self.col.fail("SCAFFOLD_FILE_UNREADABLE", rel, None,
                          "cannot read file: %s" % (exc.strerror or exc))
        self.texts[rel] = text
        return text

    def parse_document(self, rel: str, require_frontmatter: bool):
        """Parse a file's YAML body; returns (Block|None, opener line).

        A file that opens with `---` is frontmatter. A file without an opener
        (e.g. context.yaml) is parsed as a bare YAML body: either the whole file,
        or the leading document when a lone `---` closes it before the prose.
        """
        text = self.load_text(rel)
        if text is None:
            return None, None
        lines = split_lines(text)
        if all(line.strip() == "" for line in lines):
            self.col.fail("SCAFFOLD_FILE_EMPTY", rel, None, "file is empty")
            return None, None
        start = 0
        if lines[0].strip() == "---":
            start = 1
        elif require_frontmatter:
            self.col.fail("SCAFFOLD_FRONTMATTER_MISSING", rel, 1,
                          "file must begin with a '---' frontmatter block")
            return None, None
        closer = None
        for i in range(max(start, 1), len(lines)):
            if lines[i].strip() == "---":
                closer = i
                break
        if start == 1 and closer is None:
            self.col.fail("SCAFFOLD_FRONTMATTER_UNTERMINATED", rel, 1,
                          "frontmatter block is not closed by '---'")
            return None, 1
        end = closer if closer is not None else len(lines)
        block = self.parse_yaml_text(rel, "\n".join(lines[start:end]))
        return block, (1 if start == 1 else None)

    def parse_yaml_text(self, rel: str, text: str):
        """Parse one YAML subset document, reporting unsupported constructs and syntax errors."""
        try:
            return parse_yaml_subset(text)
        except Unsupported as exc:
            self.col.fail("SCAFFOLD_YAML_UNSUPPORTED", rel, exc.line,
                          "unsupported YAML construct: %s" % exc.detail)
        except YamlFail as exc:
            self.col.fail("SCAFFOLD_YAML_SYNTAX", rel, exc.line,
                          "YAML syntax error: %s" % exc.detail)
        return None

    @staticmethod
    def line_of(block, key: str, default=None):
        if block is None or not isinstance(block.lines, dict):
            return default
        return block.lines.get(key, default)

    # -- entry file ----------------------------------------------------------
    def check_entry(self):
        rel = AGENT_FILE
        block, opener = self.parse_document(rel, require_frontmatter=True)
        self.entry_block = block
        if block is None:
            return
        data = block.data
        if not isinstance(data, dict):
            self.col.fail("SCAFFOLD_ENTRY_TYPE", rel, opener,
                          "frontmatter must be a mapping of entry fields")
            return
        absent = [f for f in REQUIRED_ENTRY_FIELDS if f not in data]
        missing = set(absent)
        if absent:
            self.col.fail("SCAFFOLD_FIELD_MISSING", rel, opener,
                          "missing required field(s): %s" % ", ".join(repr(f) for f in absent))
        for key in sorted(data):
            if missing:
                singular = key[:-1] if key.endswith("s") else key
                if key in missing or (singular in missing and isinstance(data.get(singular), list)):
                    self.col.fail("SCAFFOLD_FIELD_NAME", rel, self.line_of(block, key, opener),
                                  "field '%s' must be named '%s'" % (key, key[:-1] if key.endswith("s") else key))
        self.collect_fields(rel, data, block, ENTRY_SCHEMA, (), None,
                            collect={"skills": "skills", "workflows": "workflows",
                                     "integrations": "integrations"})
        self.check_entry_index(rel, data, block, opener)
        self.check_cross_collection_ids(rel, data, block, opener)

    def check_cross_collection_ids(self, rel, data, block, opener):
        """An index id is unique across skills, workflows and integrations, not just inside one list."""
        owner = {}
        for collection in INDEX_COLLECTIONS:
            entries = data.get(collection)
            if not isinstance(entries, list):
                continue
            line = self.line_of(block, collection, opener)
            for item in entries:
                ident = item.get("id") if isinstance(item, dict) else None
                if not isinstance(ident, str) or ident == "":
                    continue
                first = owner.setdefault(ident, collection)
                if first != collection:
                    self.col.fail("SCAFFOLD_DUPLICATE_ID", rel, line,
                                  "duplicate id '%s' in the %s index; it is already declared in the %s index"
                                  % (ident, collection, first))

    def check_entry_index(self, rel, data, block, opener):
        for collection in INDEX_COLLECTIONS:
            entries = data.get(collection)
            line = self.line_of(block, collection, opener)
            if entries is None:
                continue
            if not isinstance(entries, list):
                self.col.fail("SCAFFOLD_FIELD_TYPE", rel, line,
                              "field '%s' must be a list of inline objects, got %s"
                              % (collection, type_name(entries)))
                continue
            if not entries:
                self.col.fail("SCAFFOLD_FIELD_EMPTY", rel, line,
                              "field '%s' must not be an empty list; omit it when unused" % collection)
                continue
            for position, item in enumerate(entries, start=1):
                if not isinstance(item, dict):
                    self.col.fail("SCAFFOLD_FIELD_TYPE", rel, line,
                                  "%s entry %d must be an inline object "
                                  "(e.g. {id: x, path: %sx.md}), got %s"
                                  % (collection, position, OBJECT_ROOTS[collection], type_name(item)))
                    continue
                ident = item.get("id")
                path = item.get("path")
                for key in item:
                    if key not in ("id", "path"):
                        self.col.fail("SCAFFOLD_FIELD_UNKNOWN", rel, line,
                                      "unknown field '%s' in %s entry %d" % (key, collection, position))
                self.check_scalar(rel, "%s entry %d id" % (collection, position), ident, line, SLUG, None)
                self.check_scalar(rel, "%s entry %d path" % (collection, position), path, line, STR, None)
                if isinstance(ident, str) and ident in self.entries[collection]:
                    self.col.fail("SCAFFOLD_DUPLICATE_ID", rel, line,
                                  "duplicate id '%s' in the %s index" % (ident, collection))
                if not isinstance(path, str) or not isinstance(ident, str):
                    continue
                if not self.check_relative_path(rel, path, line, OBJECT_ROOTS[collection],
                                                "%s entry %d" % (collection, position)):
                    continue
                normalized = normalize_path(path)
                if normalized in self.by_path[collection]:
                    self.col.fail("SCAFFOLD_DUPLICATE_ID", rel, line,
                                  "path '%s' is declared twice in the %s index" % (path, collection))
                    continue
                entry = {"id": ident, "path": path, "normalized": normalized, "line": line}
                self.entries[collection][ident] = entry
                self.by_path[collection][normalized] = entry

    # -- field tables --------------------------------------------------------
    def collect_fields(self, rel, data, block, schema, required, prefix, collect=None):
        """Check field presence, names, types, enums and cross-field policy."""
        for field in required:
            if field not in data:
                self.col.fail("SCAFFOLD_FIELD_MISSING", rel, self.line_of(block, field, None),
                              "missing required field '%s'" % name_of(prefix, field))
        for field in sorted(data):
            line = self.line_of(block, field, None)
            name = name_of(prefix, field)
            spec = schema.get(field)
            if spec is None:
                if field in (collect or {}) or field in OPTIONAL_OBJECT_FIELDS.get(prefix or "", ()):
                    continue
                self.col.fail("SCAFFOLD_FIELD_UNKNOWN", rel, line, "unknown field '%s'" % name)
                continue
            self.check_value(rel, name, data[field], line, spec)
        self.check_cross_field(rel, data, block, prefix)

    def check_value(self, rel, name, value, line, spec):
        kind, element, allowed = spec
        if kind in (STR, BOOL, SLUG):
            self.check_scalar(rel, name, value, line, kind, allowed)
            return
        if not isinstance(value, list):
            self.col.fail("SCAFFOLD_FIELD_TYPE", rel, line,
                          "field '%s' must be a list, got %s" % (name, type_name(value)))
            return
        if kind == STEP_LIST:
            self.check_steps(rel, name, value, line)
            return
        if not value:
            self.col.fail("SCAFFOLD_FIELD_EMPTY", rel, line, "field '%s' must not be empty" % name)
            return
        if kind == SOURCE_FILES:
            self.check_context_source_files(rel, value, line)
            return
        if kind == REQUIREMENTS_LIST:
            return  # entries are checked with source-line context in check_requires()
        if kind == OBJECT_INDEX:
            return  # index entries are checked item-by-item in check_entry_index()
        for item in value:
            self.check_scalar(rel, "%s entry" % name, item, line, element or STR, None)

    def check_context_source_files(self, rel, entries, line):
        required = {"id", "path", "purpose", "readPolicy", "writePolicy"}
        seen_ids = set()
        seen_paths = set()
        for index, entry in enumerate(entries, 1):
            prefix = "sourceFiles entry %d" % index
            if not isinstance(entry, dict):
                self.col.fail("SCAFFOLD_CONTEXT_SOURCE_TYPE", rel, line,
                              "%s must be a mapping" % prefix)
                continue
            for field in sorted(required - entry.keys()):
                self.col.fail("SCAFFOLD_CONTEXT_SOURCE_FIELD_MISSING", rel, line,
                              "%s is missing '%s'" % (prefix, field))
            for field in sorted(entry.keys() - required):
                self.col.fail("SCAFFOLD_CONTEXT_SOURCE_FIELD_UNKNOWN", rel, line,
                              "%s contains unknown field '%s'" % (prefix, field))

            ident = entry.get("id")
            if not isinstance(ident, str) or not SLUG_RE.fullmatch(ident):
                self.col.fail("SCAFFOLD_CONTEXT_SOURCE_ID", rel, line,
                              "%s id must be a stable lowercase slug" % prefix)
            elif ident in seen_ids:
                self.col.fail("SCAFFOLD_CONTEXT_SOURCE_DUPLICATE", rel, line,
                              "%s duplicates id '%s'" % (prefix, ident))
            else:
                seen_ids.add(ident)

            purpose = entry.get("purpose")
            if not isinstance(purpose, str) or not purpose.strip():
                self.col.fail("SCAFFOLD_CONTEXT_SOURCE_PURPOSE", rel, line,
                              "%s purpose must be a nonempty string" % prefix)
            if entry.get("readPolicy") != "on-demand":
                self.col.fail("SCAFFOLD_CONTEXT_SOURCE_READ_POLICY", rel, line,
                              "%s readPolicy must be 'on-demand'" % prefix)
            if entry.get("writePolicy") not in ("read-only", "confirm"):
                self.col.fail("SCAFFOLD_CONTEXT_SOURCE_WRITE_POLICY", rel, line,
                              "%s writePolicy must be 'read-only' or 'confirm'" % prefix)

            path = entry.get("path")
            if not isinstance(path, str) or not path.strip():
                self.col.fail("SCAFFOLD_CONTEXT_SOURCE_PATH", rel, line,
                              "%s path must be a nonempty workspace-relative file path" % prefix)
                continue
            if (path.startswith(("/", "\\", "~")) or re.match(r"^[A-Za-z]:", path)
                    or "\\" in path or any(part in ("", ".", "..") for part in path.split("/"))
                    or any(character in path for character in "*?[]")
                    or any(re.search(r'[:<>"|*\x00-\x1f]', part) or part.endswith((".", " "))
                           or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
                           for part in path.split("/"))):
                self.col.fail("SCAFFOLD_CONTEXT_SOURCE_PATH", rel, line,
                              "%s path '%s' must be a safe normalized workspace-relative path without wildcards"
                              % (prefix, path))
                continue
            folded = path.casefold()
            if folded in seen_paths:
                self.col.fail("SCAFFOLD_CONTEXT_SOURCE_DUPLICATE", rel, line,
                              "%s duplicates path '%s'" % (prefix, path))
            seen_paths.add(folded)
            self.check_context_source_target(rel, path, line, prefix)

    def check_context_source_target(self, rel, path, line, prefix):
        candidate = os.path.abspath(os.path.join(self.root, *path.split("/")))
        if not is_within(candidate, os.path.abspath(self.root)):
            self.col.fail("SCAFFOLD_CONTEXT_SOURCE_PATH", rel, line,
                          "%s path '%s' escapes the workspace" % (prefix, path))
            return
        current = os.path.abspath(self.root)
        parts = path.split("/")
        for index, part in enumerate(parts):
            current = os.path.join(current, part)
            try:
                mode = os.lstat(current).st_mode
            except FileNotFoundError:
                self.col.fail("SCAFFOLD_CONTEXT_SOURCE_MISSING", rel, line,
                              "%s source '%s' does not exist" % (prefix, path))
                return
            except OSError as exc:
                self.col.fail("SCAFFOLD_CONTEXT_SOURCE_UNREADABLE", rel, line,
                              "%s source '%s' metadata cannot be checked: %s"
                              % (prefix, path, exc.strerror or exc))
                return
            if stat.S_ISLNK(mode):
                self.col.fail("SCAFFOLD_CONTEXT_SOURCE_SYMLINK", rel, line,
                              "%s source '%s' must not traverse a symbolic link" % (prefix, path))
                return
            if index < len(parts) - 1 and not stat.S_ISDIR(mode):
                self.col.fail("SCAFFOLD_CONTEXT_SOURCE_NOT_FILE", rel, line,
                              "%s source parent in '%s' is not a directory" % (prefix, path))
                return
            if index == len(parts) - 1 and not stat.S_ISREG(mode):
                self.col.fail("SCAFFOLD_CONTEXT_SOURCE_NOT_FILE", rel, line,
                              "%s source '%s' is not a regular file" % (prefix, path))
                return

    def check_scalar(self, rel, name, value, line, kind, allowed):
        if kind == BOOL:
            if not isinstance(value, bool):
                self.col.fail("SCAFFOLD_FIELD_TYPE", rel, line,
                              "field '%s' must be a boolean, got %s" % (name, type_name(value)))
            return
        if not isinstance(value, str):
            self.col.fail("SCAFFOLD_FIELD_TYPE", rel, line,
                          "field '%s' must be a %s, got %s" % (name, kind, type_name(value)))
            return
        if kind == STR:
            if value.strip() == "":
                self.col.fail("SCAFFOLD_FIELD_EMPTY", rel, line, "field '%s' must not be empty" % name)
                return
            if allowed and value not in allowed:
                self.col.fail("SCAFFOLD_ENUM_INVALID", rel, line,
                              "field '%s' must be one of %s, got '%s'"
                              % (name, enum_text(allowed), value))
            return
        if kind == SLUG:
            self.check_slug(rel, name, value, line)

    def check_slug(self, rel, name, value, line):
        if value.strip() == "":
            self.col.fail("SCAFFOLD_FIELD_EMPTY", rel, line, "field '%s' must not be empty" % name)
            return
        if not SLUG_RE.match(value):
            self.col.fail("SCAFFOLD_SLUG_INVALID", rel, line,
                          "field '%s' must be a lowercase slug (a-z, 0-9, single hyphens), got '%s'"
                          % (name, value))

    def check_steps(self, rel, name, value, line):
        if len(value) < STEPS_MIN:
            self.col.fail("SCAFFOLD_FIELD_EMPTY", rel, line,
                          "field '%s' must contain at least %d entries, got %d"
                          % (name, STEPS_MIN, len(value)))
        seen = set()
        for index, step in enumerate(value, start=1):
            if step is None or step == {} or (isinstance(step, str) and step.strip() == ""):
                self.col.fail("SCAFFOLD_FIELD_EMPTY", rel, line, "%s entry %d must not be empty" % (name, index))
                continue
            if not isinstance(step, dict):
                continue  # plain string steps are allowed by the contract
            for field in ("id", "purpose"):
                if field not in step:
                    self.col.fail("SCAFFOLD_FIELD_MISSING", rel, line,
                                  "missing required field '%s' in %s entry %d" % (field, name, index))
            for field in sorted(step):
                if field not in ("id", "purpose", "usesSkill", "usesSkills"):
                    self.col.fail("SCAFFOLD_FIELD_UNKNOWN", rel, line,
                                  "unknown field '%s' in %s entry %d" % (field, name, index))
            if "id" in step:
                ident = step["id"]
                self.check_scalar(rel, "%s entry %d id" % (name, index), ident, line, SLUG, None)
                if isinstance(ident, str):
                    if ident in seen:
                        self.col.fail("SCAFFOLD_DUPLICATE_STEP", rel, line,
                                      "duplicate step id '%s' in %s" % (ident, name))
                    seen.add(ident)
            if "purpose" in step:
                self.check_scalar(rel, "%s entry %d purpose" % (name, index), step["purpose"], line, STR, None)
            if "usesSkills" in step:
                self.check_value(rel, "%s entry %d usesSkills" % (name, index), step["usesSkills"], line,
                                 (STR_LIST, None, None))
                self.collect_refs(rel, step["usesSkills"], line)
            if "usesSkill" in step:
                self.collect_refs(rel, step["usesSkill"], line)

    def check_cross_field(self, rel, data, block, prefix):
        if not prefix:
            return  # policy fields only exist on Skill / Integration files
        line_of = lambda k: self.line_of(block, k, None)
        effects = data.get("sideEffects")
        if isinstance(effects, str) and effects in SIDE_EFFECTS:
            if effects != "none" and data.get("confirmation") != "required":
                self.col.fail("SCAFFOLD_POLICY_CONFIRMATION", rel,
                              line_of("confirmation") or line_of("sideEffects"),
                              "sideEffects '%s' requires confirmation: required" % effects)
            if effects == "calls-external":
                self.external_effects.append((rel, line_of("sideEffects")))
        if data.get("fallback") == "degrade" and data.get("fallbackNote") == "":
            self.col.fail("SCAFFOLD_FIELD_EMPTY", rel, line_of("fallbackNote"),
                          "field 'fallbackNote' must not be empty when fallback is 'degrade'")

    # -- paths ---------------------------------------------------------------
    def check_relative_path(self, rel, path, line, allowed_dir, prefix):
        label = ("%s " % prefix) if prefix else ""
        if path.strip() == "":
            self.col.fail("SCAFFOLD_FIELD_EMPTY", rel, line, "%spath must not be empty" % label)
            return False
        if path.startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:", path):
            self.col.fail("SCAFFOLD_PATH_ABSOLUTE", rel, line,
                          "%spath must be relative to the workspace root, got '%s'" % (label, path))
            return False
        if any(part in (".", "..") for part in re.split(r"[\\/]+", path) if part):
            self.col.fail("SCAFFOLD_PATH_ESCAPE", rel, line,
                          "%spath must not contain '.' or '..' segments, got '%s'" % (label, path))
            return False
        if allowed_dir and not normalize_path(path).startswith(allowed_dir):
            self.col.fail("SCAFFOLD_PATH_OUTSIDE_DIR", rel, line,
                          "%spath '%s' must live under '%s'" % (label, path, allowed_dir))
            return False
        real = os.path.realpath(self.abs_path(path))
        if not is_within(real, os.path.realpath(self.root)):
            self.col.fail("SCAFFOLD_PATH_ESCAPE", rel, line,
                          "%spath '%s' resolves outside the workspace" % (label, path))
            return False
        return True

    # -- objects -------------------------------------------------------------
    def check_objects(self):
        for collection in COLLECTION_ORDER:
            for entry in sorted(self.by_path[collection].values(), key=lambda e: (e["line"], e["id"])):
                self.check_object(collection, entry)
            self.scan_disk(collection)

    def check_object(self, collection, entry):
        rel = entry["normalized"]
        field = collection
        if self.load_text(rel) is None:
            self.col.fail("SCAFFOLD_INDEX_MISSING_FILE", AGENT_FILE, entry["line"],
                          "%s entry '%s' declares missing file '%s'" % (field, entry["id"], rel))
            return
        expected = normalize_path(OBJECT_ROOTS[collection] + entry["id"] + ".md")
        if rel != expected:
            self.col.fail("SCAFFOLD_INDEX_PATH_MISMATCH", rel, entry["line"],
                          "%s entry '%s' path should be '%s', got '%s'"
                          % (field, entry["id"], expected, rel))
        block, opener = self.parse_document(rel, require_frontmatter=True)
        if block is None:
            return
        data = block.data
        if not isinstance(data, dict):
            self.col.fail("SCAFFOLD_ENTRY_TYPE", rel, opener,
                          "%s file must declare a mapping of fields" % field[:-1])
            return
        self.collect_fields(rel, data, block, OBJECT_SCHEMAS[collection],
                            REQUIRED_OBJECT_FIELDS[collection], collection)
        if collection == "skills" and "requires" in data:
            self.check_requires(rel, data["requires"], block)
        if collection == "skills" and "readBefore" in data:
            self.check_read_before(rel, data["readBefore"], block)
        declared = data.get("id")
        if isinstance(declared, str) and declared:
            if declared != entry["id"]:
                self.col.fail("SCAFFOLD_INDEX_ID_MISMATCH", rel, self.line_of(block, "id", opener),
                              "declared id '%s' does not match the %s index id '%s'"
                              % (declared, field, entry["id"]))
            basename = os.path.basename(rel)[:-len(".md")]
            if declared != basename:
                self.col.fail("SCAFFOLD_FILE_ID_MISMATCH", rel, self.line_of(block, "id", opener),
                              "declared id '%s' must match the file name '%s.md'" % (declared, basename))
            self.disk[collection][rel] = declared
            self.disk_ids[collection].setdefault(declared, rel)
        else:
            self.disk[collection][rel] = None
        if collection == "skills":
            self.collect_refs(rel, data.get("workflow"), self.line_of(block, "workflow", opener), "workflows")
            self.collect_refs(rel, data.get("workflows"), self.line_of(block, "workflows", opener), "workflows")
        if collection == "workflows":
            self.collect_refs(rel, data.get("usesSkills"), self.line_of(block, "usesSkills", opener))
            steps = data.get("steps")
            if isinstance(steps, list):
                for step in steps:
                    if isinstance(step, dict) and "usesSkills" in step:
                        self.collect_refs(rel, step["usesSkills"], self.line_of(block, "steps", opener))

    def scan_disk(self, collection):
        directory = OBJECT_ROOTS[collection]
        full = self.abs_path(directory)
        if not os.path.isdir(full):
            return
        try:
            names = sorted(os.listdir(full))
        except OSError as exc:
            self.col.fail("SCAFFOLD_FILE_UNREADABLE", directory, None,
                          "cannot list directory: %s" % (exc.strerror or exc))
            return
        if not names:
            self.col.fail("SCAFFOLD_DISK_UNREGISTERED", directory, None,
                          "optional object directory must not be empty")
            return
        for name in names:
            rel = directory + name
            path = os.path.join(full, name)
            if os.path.isdir(path):
                self.col.fail("SCAFFOLD_DISK_UNREGISTERED", rel, None,
                              "'%s' holds files, not directories" % collection)
                continue
            if os.path.islink(path):
                if not is_within(os.path.realpath(path), os.path.realpath(self.root)):
                    self.col.fail("SCAFFOLD_PATH_ESCAPE", rel, None,
                                  "symbolic link points outside the workspace")
                    continue
            if not name.endswith(".md"):
                self.col.fail("SCAFFOLD_DISK_NAME_INVALID", rel, None,
                              "%s files must be named <id>.md" % collection[:-1])
            if rel not in self.by_path[collection] and rel not in self.disk[collection]:
                self.col.fail("SCAFFOLD_DISK_UNREGISTERED", rel, None,
                              "file is not declared in the '%s' index of %s" % (collection, AGENT_FILE))
                self.disk[collection][rel] = None

    def check_duplicate_ids(self):
        for collection in COLLECTION_ORDER:
            groups = {}
            for rel, declared in self.disk[collection].items():
                if declared:
                    groups.setdefault(declared, []).append(rel)
            for declared in sorted(groups):
                peers = sorted(groups[declared])
                if len(peers) < 2:
                    continue
                for rel in peers:
                    others = ", ".join("'%s'" % p for p in peers if p != rel)
                    self.col.fail("SCAFFOLD_DUPLICATE_ID", rel, None,
                                  "id '%s' is also declared by %s" % (declared, others))

    def check_requires(self, rel, value, block):
        """Validate Skill preconditions and collect their static references."""
        line = self.line_of(block, "requires", 1)
        if not isinstance(value, list) or not value:
            return
        self.check_requires_style(rel, line)
        required = ("id", "purpose", "from", "ifMissing")
        allowed = set(required) | {"contextCategory", "integration", "note"}
        seen = set()
        for index, item in enumerate(value, start=1):
            label = "requires entry %d" % index
            if not isinstance(item, dict):
                self.col.fail("SCAFFOLD_FIELD_TYPE", rel, line,
                              "%s must be a block-style mapping, got %s" % (label, type_name(item)))
                continue
            for field in required:
                if field not in item:
                    self.col.fail("SCAFFOLD_FIELD_MISSING", rel, line,
                                  "missing required field '%s' in %s" % (field, label))
            for field in sorted(item):
                if field not in allowed:
                    self.col.fail("SCAFFOLD_FIELD_UNKNOWN", rel, line,
                                  "unknown field '%s' in %s" % (field, label))
            for field in ("id", "purpose"):
                if field in item:
                    self.check_scalar(rel, "%s %s" % (label, field), item[field], line,
                                      SLUG if field == "id" else STR, None)
                    if field == "purpose" and isinstance(item[field], str) and any(
                            char in item[field] for char in ("\r", "\n")):
                        self.col.fail("SCAFFOLD_FIELD_TYPE", rel, line,
                                      "field '%s purpose' must be a single-line string" % label)
            ident = item.get("id")
            if isinstance(ident, str) and ident:
                if ident in seen:
                    self.col.fail("SCAFFOLD_DUPLICATE_ID", rel, line,
                                  "duplicate requires id '%s'" % ident)
                seen.add(ident)
            for field in ("contextCategory", "integration", "note"):
                if field in item:
                    self.check_scalar(rel, "%s %s" % (label, field), item[field], line, STR, None)
            source = item.get("from")
            self.check_scalar(rel, "%s from" % label, source, line, STR,
                              ("user", "context", "integration"))
            policy = item.get("ifMissing")
            self.check_scalar(rel, "%s ifMissing" % label, policy, line, STR,
                              ("ask", "refuse", "degrade"))
            if source == "context":
                if "contextCategory" not in item:
                    self.col.fail("SCAFFOLD_FIELD_MISSING", rel, line,
                                  "missing required field 'contextCategory' in %s" % label)
                if "integration" in item:
                    self.col.fail("SCAFFOLD_REQUIREMENT_SOURCE", rel, line,
                                  "field 'integration' is only allowed when from is 'integration'")
                category = item.get("contextCategory")
                if isinstance(category, str) and category.strip():
                    self.context_refs.append((rel, category, line))
            elif source == "integration":
                if "integration" not in item:
                    self.col.fail("SCAFFOLD_FIELD_MISSING", rel, line,
                                  "missing required field 'integration' in %s" % label)
                if "contextCategory" in item:
                    self.col.fail("SCAFFOLD_REQUIREMENT_SOURCE", rel, line,
                                  "field 'contextCategory' is only allowed when from is 'context'")
                integration = item.get("integration")
                if isinstance(integration, str) and integration.strip():
                    self.collect_refs(rel, integration, line, "integrations")
            elif source == "user":
                for field in ("contextCategory", "integration"):
                    if field in item:
                        self.col.fail("SCAFFOLD_REQUIREMENT_SOURCE", rel, line,
                                      "field '%s' is not allowed when from is 'user'" % field)
            if policy == "degrade":
                if "note" not in item:
                    self.col.fail("SCAFFOLD_FIELD_MISSING", rel, line,
                                  "missing required field 'note' when ifMissing is 'degrade'")
            elif "note" in item:
                self.col.fail("SCAFFOLD_REQUIREMENT_POLICY", rel, line,
                              "field 'note' is only allowed when ifMissing is 'degrade'")

    def check_requires_style(self, rel, line):
        """Require the list and each mapping entry to use block YAML syntax."""
        text = self.load_text(rel)
        if text is None or line is None:
            return
        source = split_lines(text)
        offset = 1 if source and source[0].strip() == "---" else 0
        index = line - 1 + offset
        if index < 0 or index >= len(source):
            return
        raw = source[index]
        value = raw.split(":", 1)[1].strip() if ":" in raw else ""
        if value and not value.startswith("#"):
            self.col.fail("SCAFFOLD_FIELD_TYPE", rel, line,
                          "field 'requires' must use a block-style list")
            return
        base_indent = len(raw) - len(raw.lstrip(" "))
        for item_line in source[index + 1:]:
            stripped = item_line.lstrip(" ")
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(item_line) - len(stripped)
            if indent <= base_indent:
                break
            if stripped.startswith(("- {", "- [")):
                self.col.fail("SCAFFOLD_FIELD_TYPE", rel, line,
                              "requires entries must use block-style mappings")
                return

    def check_read_before(self, rel, value, block):
        """Validate Skill execution-before-read paths without reading them."""
        if not isinstance(value, list):
            return
        line = self.line_of(block, "readBefore", None)
        for index, path in enumerate(value, start=1):
            if not isinstance(path, str) or not path.strip():
                continue  # check_value() reports the field type or empty error.
            if not self.check_relative_path(
                    rel, path, line, None, "readBefore entry %d" % index):
                continue
            normalized = normalize_path(path)
            if not os.path.isfile(self.abs_path(normalized)):
                self.col.fail(
                    "SCAFFOLD_READ_BEFORE_MISSING", rel, line,
                    "readBefore path '%s' does not resolve to a file in the workspace"
                    % path,
                )

    def check_context_requirement_refs(self):
        for rel, category, line in self.context_refs:
            if category not in self.context_categories:
                self.col.fail("SCAFFOLD_CONTEXT_CATEGORY_DANGLING", rel, line,
                              "context category '%s' is not declared in %s" % (category, CONTEXT_FILE))

    # -- references ----------------------------------------------------------
    def collect_refs(self, rel, value, line, target="skills"):
        """Record object references together with the index they must resolve against."""
        if value is None:
            return
        refs = value if isinstance(value, list) else [value]
        for ref in refs:
            self.refs.append((rel, target, ref, line))

    def check_refs(self):
        for rel, target, ref, line in self.refs:
            label = target[:-1]      # 'skills' -> 'skill', 'workflows' -> 'workflow'
            if not isinstance(ref, str) or not ref.strip():
                self.col.fail("SCAFFOLD_REF_INVALID", rel, line,
                              "%s reference must be a non-empty slug string" % label)
                continue
            if not SLUG_RE.match(ref):
                self.col.fail("SCAFFOLD_SLUG_INVALID", rel, line,
                              "%s reference '%s' must be a lowercase slug" % (label, ref))
                continue
            entry = self.entries[target].get(ref)
            if entry is not None:
                if not os.path.isfile(self.abs_path(entry["normalized"])):
                    self.col.fail("SCAFFOLD_REF_DANGLING", rel, line,
                                  "%s reference '%s' resolves to missing file '%s'"
                                  % (label, ref, entry["normalized"]))
                continue
            if ref in self.disk_ids[target]:
                self.col.fail("SCAFFOLD_REF_UNINDEXED", rel, line,
                              "%s reference '%s' exists on disk but is not declared in the %s index"
                              % (label, ref, target))
                continue
            self.col.fail("SCAFFOLD_REF_DANGLING", rel, line,
                          "%s reference '%s' is not declared in the %s index of %s"
                          % (label, ref, target, AGENT_FILE))

    def check_external_integration(self):
        if self.entries["integrations"] or self.disk["integrations"]:
            return
        for rel, line in self.external_effects:
            self.col.fail("SCAFFOLD_POLICY_EXTERNAL_INTEGRATION", rel, line,
                          "sideEffects 'calls-external' requires at least one entry in the integrations index")

    # -- optional objects ----------------------------------------------------
    def check_optional_files(self):
        root = os.path.realpath(self.root)
        context_path = self.abs_path(CONTEXT_FILE)
        if os.path.lexists(context_path):
            if not is_within(os.path.realpath(context_path), root):
                self.col.fail("SCAFFOLD_PATH_ESCAPE", CONTEXT_FILE, None,
                              "symbolic link points outside the workspace")
            else:
                self.check_context()
        behavior_path = self.abs_path(BEHAVIOR_FILE)
        if os.path.lexists(behavior_path):
            if not is_within(os.path.realpath(behavior_path), root):
                self.col.fail("SCAFFOLD_PATH_ESCAPE", BEHAVIOR_FILE, None,
                              "symbolic link points outside the workspace")
            else:
                text = self.load_text(BEHAVIOR_FILE)
                if text is not None and text.strip() == "":
                    self.col.fail("SCAFFOLD_FILE_EMPTY", BEHAVIOR_FILE, None,
                                  "behavior file must not be empty when it is present")
        # ponytail: behaviors.md is prose; only presence and non-emptiness are
        # checked. Upgrade when the contract gives it machine-readable fields.

    def check_context(self):
        rel = CONTEXT_FILE
        block, opener = self.parse_document(rel, require_frontmatter=False)
        if block is None:
            return
        data = block.data
        if not isinstance(data, dict):
            self.col.fail("SCAFFOLD_ENTRY_TYPE", rel, opener or 1,
                          "context file must declare a mapping of fields")
            return
        self.collect_fields(rel, data, block, CONTEXT_SCHEMA, REQUIRED_CONTEXT_FIELDS, None)
        categories = data.get("categories")
        if isinstance(categories, list):
            self.context_categories = {item for item in categories if isinstance(item, str)}
        states = data.get("states")
        if isinstance(states, list) and states:
            missing = [s for s in CONTEXT_STATES if s not in states]
            if missing:
                self.col.fail("SCAFFOLD_STATE_INCOMPLETE", rel, self.line_of(block, "states", opener),
                              "field 'states' must contain exactly %s, missing %s"
                              % (enum_text(CONTEXT_STATES), enum_text(tuple(missing))))
            extra = [s for s in states if s not in CONTEXT_STATES]
            if extra:
                self.col.fail("SCAFFOLD_ENUM_INVALID", rel, self.line_of(block, "states", opener),
                              "field 'states' contains unsupported value(s): %s"
                              % enum_text(tuple(extra)))
            if len(states) != len(set(map(str, states))):
                self.col.fail("SCAFFOLD_DUPLICATE_ID", rel, self.line_of(block, "states", opener),
                              "field 'states' must not contain duplicate entries")
        policy = data.get("writePolicy")
        if isinstance(policy, str) and policy != CONTEXT_WRITE_POLICY:
            self.col.fail("SCAFFOLD_ENUM_INVALID", rel, self.line_of(block, "writePolicy", opener),
                          "field 'writePolicy' must be '%s' in contract version %s, got '%s'"
                          % (CONTEXT_WRITE_POLICY, CONTRACT_VERSION, policy))

    # -- driver --------------------------------------------------------------
    def run(self):
        self.check_entry()
        self.check_objects()
        self.check_optional_files()
        self.check_context_requirement_refs()
        self.check_refs()
        self.check_duplicate_ids()
        self.check_external_integration()
        if os.path.lexists(self.abs_path(DESCRIPTION_FILE)):
            description_info(self.root, self.col)


def name_of(prefix, field):
    return "%s.%s" % (prefix, field) if prefix else field


def normalize_path(path: str) -> str:
    normalized = str(os.path.normpath(path.replace("\\", "/"))).replace(os.sep, "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def is_within(path: str, root: str) -> bool:
    path = os.path.normcase(os.path.normpath(path))
    root = os.path.normcase(os.path.normpath(root))
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def enum_text(values):
    return ", ".join("'%s'" % v for v in values)


def type_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def validate(workspace: str):
    """Validate one workspace; returns sorted errors (empty list means it passes)."""
    root = os.path.abspath(workspace)
    collector = Collector()
    Validator(root, collector).run()
    return sorted_errors(collector.items)


def description_body(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return ""
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "".join(lines[index + 1:])
    return ""


def description_metadata(text: str) -> dict:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("Description needs frontmatter")
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return parse_yaml_subset("\n".join(lines[1:index])).data
    raise ValueError("Description frontmatter is unterminated")


def description_info(workspace: str, collector=None) -> dict:
    collector = collector if collector is not None else Collector()
    validator = Validator(os.path.abspath(workspace), collector)
    block, _ = validator.parse_document(DESCRIPTION_FILE, require_frontmatter=True)
    data = block.data if block is not None and isinstance(block.data, dict) else {}
    required = {"schemaVersion", "baselineVersion", "origin", "generatorVersion", "contentSha256"}
    for field in sorted(required):
        line = validator.line_of(block, field, 1)
        value = data.get(field)
        valid = value is not None
        if field == "schemaVersion":
            valid = type(value) is int and value == 1
        elif field == "baselineVersion":
            valid = type(value) is int and value > 0
        elif field == "origin":
            valid = value in ("confirmed", "reconstructed")
        elif field == "generatorVersion":
            valid = isinstance(value, str) and bool(value.strip())
        elif field == "contentSha256":
            valid = isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))
        if not valid:
            collector.fail("DESCRIPTION_FIELD_INVALID", DESCRIPTION_FILE, line, "invalid or missing " + field)
    text = validator.texts.get(DESCRIPTION_FILE) or ""
    body = description_body(text)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if not body.strip():
        collector.fail("DESCRIPTION_BODY_EMPTY", DESCRIPTION_FILE, None, "description body must not be empty")
    if data.get("contentSha256") != digest:
        collector.fail("DESCRIPTION_HASH_MISMATCH", DESCRIPTION_FILE, validator.line_of(block, "contentSha256", 1), "description body hash does not match")
    entry = validator.load_text(AGENT_FILE) or ""
    if DESCRIPTION_FILE not in description_body(entry):
        collector.fail("DESCRIPTION_NOT_DISCOVERABLE", AGENT_FILE, None, "entry body must point to " + DESCRIPTION_FILE)
    return {"path": DESCRIPTION_FILE, **data, "body": body, "errors": sorted_errors(collector.items)}


# ----------------------------------------------------------------------- CLI --

EXIT_PASS, EXIT_CONTRACT, EXIT_USAGE = 0, 1, 2
USAGE = "usage: validate_scaffold.py <workspace> [--json] | --self-test"


def parse_argv(argv):
    parser = argparse.ArgumentParser(prog="validate_scaffold.py", usage=USAGE,
                                     description="Read-only validator for an agent-scaffold workspace.")
    parser.add_argument("workspace", nargs="?", help="workspace directory to validate")
    parser.add_argument("--json", action="store_true", help="emit a machine-readable JSON report")
    parser.add_argument("--self-test", action="store_true", help="run the embedded parser self-check")
    try:
        return parser.parse_args(argv), None
    except SystemExit as exc:
        return None, exc.code


def emit(errors, workspace, as_json):
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass
    if as_json:
        payload = {
            "schemaVersion": JSON_SCHEMA_VERSION,
            "workspace": workspace,
            "counts": {"errors": len(errors)},
            "errors": errors,
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False, indent=2) + "\n")
        return
    for item in errors:
        line = "" if item["line"] is None else item["line"]
        sys.stdout.write("%s:%s: %s: %s\n" % (item["path"], line, item["code"], item["message"]))
    if errors:
        sys.stdout.write("FAIL %s: %d error(s)\n" % (workspace, len(errors)))
    else:
        sys.stdout.write("PASS %s: contract ok\n" % workspace)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args, code = parse_argv(argv)
    if args is None:
        if code not in (0, None):
            sys.stderr.write(USAGE + "\n")
            return EXIT_USAGE
        return EXIT_PASS
    if args.self_test:
        if args.workspace:
            sys.stderr.write("validate_scaffold.py: --self-test takes no workspace\n" + USAGE + "\n")
            return EXIT_USAGE
        return EXIT_PASS if self_test() else EXIT_CONTRACT
    if not args.workspace:
        sys.stderr.write("validate_scaffold.py: a workspace path is required\n" + USAGE + "\n")
        return EXIT_USAGE
    workspace = os.path.abspath(args.workspace)
    if not os.path.exists(workspace):
        sys.stderr.write("validate_scaffold.py: workspace path does not exist: %s\n" % args.workspace)
        return EXIT_USAGE
    if not os.path.isdir(workspace):
        sys.stderr.write("validate_scaffold.py: workspace path is not a directory: %s\n" % args.workspace)
        return EXIT_USAGE
    errors = validate(workspace)
    emit(errors, workspace, args.json)
    return EXIT_CONTRACT if errors else EXIT_PASS


# ----------------------------------------------------------------- self-test --

SELF_CASES = (
    ("map", "a: 1\nb: text\n", {"a": 1, "b": "text"}),
    ("nested", "a:\n  b: 1\n  c:\n    - x\n    - y\n", {"a": {"b": 1, "c": ["x", "y"]}}),
    ("seq-of-maps", "steps:\n  - id: one\n    purpose: do\n  - id: two\n    purpose: more\n",
     {"steps": [{"id": "one", "purpose": "do"}, {"id": "two", "purpose": "more"}]}),
    ("flow", "t: [a, b]\nu: {id: x, path: .agent/skills/x.md}\n",
     {"t": ["a", "b"], "u": {"id": "x", "path": ".agent/skills/x.md"}}),
    ("scalars", "a: true\nb: null\nc: '1'\nd: \"x # y\"\ne: 1.5\n",
     {"a": True, "b": None, "c": "1", "d": "x # y", "e": 1.5}),
    ("comment", "a: 1 # note\n", {"a": 1}),
    ("seq-of-seq", "a:\n  - - 1\n    - 2\n", {"a": [[1, 2]]}),
    ("requires-block", "requires:\n  - id: one\n    purpose: need\n    from: user\n    ifMissing: ask\n",
     {"requires": [{"id": "one", "purpose": "need", "from": "user", "ifMissing": "ask"}]}),
    ("read-before-block", "readBefore:\n  - AGENTS.md\n  - .agent/context.yaml\n",
     {"readBefore": ["AGENTS.md", ".agent/context.yaml"]}),
)
SELF_REJECTS = (
    "a: &x 1\n", "a: *x\n", "a: !!str 1\n", "a: |\n  text\n", "a: >\n  text\n",
    "a: 1\na: 2\n", "a: [1, ]\n", "a: [b: 1]\n", "requires: [{id: x}]\n",
    "a:\n    b: 1\n  c: 2\n",
)
SELF_SLUGS_OK = ("a", "a-b", "a1", "1a", "a-b-c")
SELF_SLUGS_BAD = ("A", "a_b", "-a", "a-", "a--b", "", "a b", "a/b")


def self_test() -> bool:
    """Embedded parser and helper sanity check; no filesystem access, no writes."""
    ok = True
    for name, text, expected in SELF_CASES:
        try:
            got = parse_yaml_subset(text).data
        except YamlFail as exc:
            print("self-test FAIL %s: unexpected %s" % (name, exc.detail))
            ok = False
            continue
        if got != expected:
            print("self-test FAIL %s: parsed %r, expected %r" % (name, got, expected))
            ok = False
    for text in SELF_REJECTS:
        try:
            parse_yaml_subset(text)
        except YamlFail:
            continue
        print("self-test FAIL accept: %r was accepted" % text)
        ok = False
    for slug in SELF_SLUGS_OK:
        if not SLUG_RE.match(slug):
            print("self-test FAIL slug accept: %r" % slug)
            ok = False
    for slug in SELF_SLUGS_BAD:
        if SLUG_RE.match(slug):
            print("self-test FAIL slug reject: %r" % slug)
            ok = False
    if type_name(True) != "boolean":
        print("self-test FAIL: booleans must not be reported as integers")
        ok = False
    if normalize_path("./a/../b.md") != "b.md":
        print("self-test FAIL: normalize_path")
        ok = False
    print("self-test: ok" if ok else "self-test: errors found")
    return ok


if __name__ == "__main__":
    sys.exit(main())
