"""Docksmithfile parser.

Supports the six required instructions (FROM, WORKDIR, COPY, ENV, RUN, CMD),
``#`` comments, blank lines, CRLF line endings and ``\\`` line continuations.
Any unknown instruction fails immediately with its line number.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import Instruction, ParseError
from .refs import parse_ref

SUPPORTED = ("FROM", "WORKDIR", "COPY", "ENV", "RUN", "CMD")
OUT_OF_SCOPE = ("ADD", "ARG", "ENTRYPOINT", "EXPOSE", "VOLUME", "SHELL", "USER", "LABEL",
                "HEALTHCHECK", "ONBUILD", "STOPSIGNAL", "MAINTAINER")


def parse_file(path: str | Path) -> list[Instruction]:
    path = Path(path)
    if not str(path).strip():
        raise ParseError("Docksmithfile path cannot be empty")
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ParseError(f"Docksmithfile not found: {path}") from None
    except OSError as exc:
        raise ParseError(f"failed to read Docksmithfile {path}: {exc}") from None
    return parse_string(content)


def parse_string(content: str) -> list[Instruction]:
    instructions: list[Instruction] = []
    for line_no, raw in _logical_lines(content):
        trimmed = raw.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        instructions.append(_parse_instruction(raw, trimmed, line_no))
    if not instructions:
        raise ParseError("Docksmithfile contains no valid instructions")
    return instructions


def _logical_lines(content: str):
    """Yield ``(starting_line_number, logical_line)`` joining ``\\`` continuations."""
    buf: list[str] = []
    start = 0
    for idx, line in enumerate(content.split("\n"), start=1):
        line = line.rstrip("\r")
        if not buf:
            start = idx
            stripped = line.strip()
            # Comments are never continued.
            if not stripped or stripped.startswith("#"):
                yield idx, line
                continue
        elif line.strip().startswith("#"):
            # Comment lines inside a continuation are dropped (Docker behaviour).
            continue
        if line.rstrip().endswith("\\") and not line.rstrip().endswith("\\\\"):
            buf.append(line.rstrip()[:-1])
            continue
        buf.append(line)
        yield start, " ".join(part.strip() if i else part.rstrip() for i, part in enumerate(buf))
        buf = []
    if buf:
        yield start, " ".join(part.strip() if i else part.rstrip() for i, part in enumerate(buf))


def _parse_instruction(raw: str, trimmed: str, line_no: int) -> Instruction:
    parts = trimmed.split(None, 1)
    op = parts[0].upper()
    remainder = parts[1].strip() if len(parts) > 1 else ""

    def err(msg: str) -> ParseError:
        return ParseError(f"line {line_no}: {msg}")

    if op not in SUPPORTED:
        hint = " (out of scope for Docksmith)" if op in OUT_OF_SCOPE else ""
        raise err(f"unknown instruction {op}{hint}")

    def inst(args: list[str]) -> Instruction:
        return Instruction(op=op, args=args, raw=raw, text=remainder, line=line_no)

    if op == "CMD":
        if not remainder:
            raise err("CMD must be JSON array")
        try:
            value = json.loads(remainder)
        except json.JSONDecodeError:
            raise err("CMD must be JSON array, e.g. CMD [\"sh\", \"app.sh\"]") from None
        if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
            raise err("CMD must be JSON array of strings, e.g. CMD [\"sh\", \"app.sh\"]")
        return inst(value)

    try:
        args = split_args(remainder)
    except ValueError as exc:
        raise err(str(exc)) from None

    if op == "FROM":
        if len(args) != 1:
            raise err("FROM requires exactly one argument (image name)")
        if args[0].lower() != "scratch":
            try:
                parse_ref(args[0])
            except Exception as exc:  # noqa: BLE001 - re-raise as parse error
                raise err(str(exc)) from None
        return inst(args)

    if op == "COPY":
        if len(args) < 2:
            raise err("COPY requires at least source and destination")
        return inst(args)

    if op == "RUN":
        if not args:
            raise err("RUN requires a command")
        return inst(args)

    if op == "WORKDIR":
        if len(args) != 1:
            raise err("WORKDIR requires exactly one path argument")
        return inst(args)

    # ENV: one or more KEY=value tokens.
    if not args:
        raise err("invalid ENV format (expected ENV KEY=value)")
    for token in args:
        key, sep, _ = token.partition("=")
        if not sep or not key.strip():
            raise err("invalid ENV format (expected ENV KEY=value)")
    return inst(args)


def split_args(s: str) -> list[str]:
    """Split a command line into arguments, honouring quotes and backslash escapes."""
    out: list[str] = []
    cur: list[str] = []
    have_token = False
    in_single = in_double = escaped = False
    for ch in s:
        if escaped:
            cur.append(ch)
            escaped = False
            continue
        if ch == "\\" and not in_single:
            escaped = True
            have_token = True
        elif ch == "'" and not in_double:
            in_single = not in_single
            have_token = True
        elif ch == '"' and not in_single:
            in_double = not in_double
            have_token = True
        elif ch in " \t" and not (in_single or in_double):
            if have_token:
                out.append("".join(cur))
                cur, have_token = [], False
        else:
            cur.append(ch)
            have_token = True
    if escaped:
        cur.append("\\")
    if in_single or in_double:
        raise ValueError("unterminated quoted string")
    if have_token:
        out.append("".join(cur))
    return out
