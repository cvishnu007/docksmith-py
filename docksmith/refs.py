"""Image reference (``name[:tag]``) parsing."""

from __future__ import annotations

import re

from .models import DocksmithError

DEFAULT_TAG = "latest"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


def parse_ref(ref: str, *, default_tag: str = DEFAULT_TAG) -> tuple[str, str]:
    """Split ``name[:tag]`` into ``(name, tag)``; the tag defaults to ``latest``."""
    ref = (ref or "").strip()
    if not ref:
        raise DocksmithError("invalid image reference '': expected name[:tag]")
    name, sep, tag = ref.rpartition(":")
    if not sep or "/" in tag:
        name, tag = ref, default_tag
    if not name or not tag:
        raise DocksmithError(f"invalid image reference {ref!r}: expected non-empty name and tag")
    if not _NAME_RE.match(name):
        raise DocksmithError(
            f"invalid image reference {ref!r}: name may only contain letters, digits, '.', '_', '-' and '/'"
        )
    if not _TAG_RE.match(tag):
        raise DocksmithError(
            f"invalid image reference {ref!r}: tag may only contain letters, digits, '.', '_' and '-'"
        )
    return name, tag


def format_ref(name: str, tag: str) -> str:
    return f"{name}:{tag}"
