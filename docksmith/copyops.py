"""COPY: source resolution (with ``*`` / ``**`` globs), hashing and copying.

Resolution is shared by the cache-key computation and the actual copy, so the
cache can never disagree with what lands in the layer.

Semantics (Docker-compatible):

* a directory source copies its *contents* into the destination directory
  (``COPY . /app`` puts the context's files directly under ``/app``);
* a single file source with a destination not ending in ``/`` (and not an
  existing directory) is copied to exactly that path, otherwise into the dir;
* glob sources (``*``, ``?``, ``[...]``, ``**``) copy every match into the
  destination directory, keeping each match's path relative to the pattern's
  fixed prefix (``COPY src/**/*.py /app/`` keeps ``src``'s sub-directories);
* ``Docksmithfile``, ``.docksmith``, OS junk files and patterns listed in a
  ``.docksmithignore`` file in the context are never copied.
"""

from __future__ import annotations

import fnmatch
import glob
import hashlib
import os
import posixpath
import shutil
import stat
from dataclasses import dataclass

from .fsutil import is_within, resolve_in_root, sha256_file
from .models import BuildError

GLOB_CHARS = frozenset("*?[")
IGNORE_FILE = ".docksmithignore"
_ALWAYS_IGNORED = {".docksmith", "Docksmithfile", IGNORE_FILE, ".DS_Store", "Thumbs.db", "__MACOSX"}


def has_glob(s: str) -> bool:
    return any(c in GLOB_CHARS for c in s)


class _Ignore:
    def __init__(self, ctx: str):
        self.patterns: list[str] = []
        path = os.path.join(ctx, IGNORE_FILE)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        self.patterns.append(line.strip("/"))

    def __call__(self, rel: str) -> bool:
        parts = rel.split("/")
        if any(p in _ALWAYS_IGNORED or p.startswith("._") for p in parts):
            return True
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            for pat in self.patterns:
                if fnmatch.fnmatchcase(prefix, pat) or ("/" not in pat and fnmatch.fnmatchcase(parts[i - 1], pat)):
                    return True
        return False


@dataclass(frozen=True)
class SourceEntry:
    """One item to copy: ``placement`` is its path relative to the destination dir."""

    placement: str
    ctx_rel: str
    abs_path: str
    kind: str  # "f" file, "d" directory, "l" symlink
    mode: int


@dataclass
class CopyPlan:
    entries: list[SourceEntry]
    single_file: bool  # exactly one non-glob file source (eligible for file->file copy)


def _kind(st: os.stat_result) -> str | None:
    if stat.S_ISLNK(st.st_mode):
        return "l"
    if stat.S_ISDIR(st.st_mode):
        return "d"
    if stat.S_ISREG(st.st_mode):
        return "f"
    return None


def _static_prefix(pattern: str) -> str:
    parts = pattern.split("/")
    fixed: list[str] = []
    for p in parts[:-1]:
        if has_glob(p):
            break
        fixed.append(p)
    return "/".join(fixed)


def _clean_rel(src: str) -> str:
    rel = posixpath.normpath(src.strip().lstrip("/")) if src.strip().lstrip("/") else "."
    return rel


def plan_copy(ctx: str, sources: list[str]) -> CopyPlan:
    """Resolve COPY sources inside build context ``ctx`` into a sorted copy plan."""
    ctx = os.path.abspath(ctx)
    ignored = _Ignore(ctx)
    plan: dict[str, SourceEntry] = {}

    def add(placement: str, ctx_rel: str) -> None:
        abs_path = os.path.join(ctx, *ctx_rel.split("/")) if ctx_rel != "." else ctx
        st = os.lstat(abs_path)
        kind = _kind(st)
        if kind is None or not placement:
            return
        plan[placement] = SourceEntry(placement, ctx_rel, abs_path, kind, stat.S_IMODE(st.st_mode))

    def add_tree(dir_rel: str, placement_base: str) -> None:
        abs_dir = os.path.join(ctx, *dir_rel.split("/")) if dir_rel != "." else ctx
        for dirpath, dirnames, filenames in os.walk(abs_dir):
            rel_dir = os.path.relpath(dirpath, ctx).replace(os.sep, "/")
            dirnames[:] = sorted(d for d in dirnames if not ignored(_join(rel_dir, d)))
            for name in sorted(dirnames + filenames):
                child_rel = _join(rel_dir, name)
                if ignored(child_rel):
                    continue
                inner = posixpath.relpath(child_rel, dir_rel) if dir_rel != "." else child_rel
                add(_join(placement_base, inner), child_rel)

    single_file = False
    for src in sources:
        rel = _clean_rel(src)
        if rel == ".." or rel.startswith("../"):
            raise BuildError(f"COPY source {src!r} is outside the build context")
        if has_glob(rel):
            matches = sorted(glob.glob(rel, root_dir=ctx, recursive=True, include_hidden=False))
            matches = [m.rstrip("/") for m in matches if m.rstrip("/") and not ignored(m.rstrip("/"))]
            if not matches:
                raise BuildError(f"COPY source pattern {src!r} matched no files in the build context")
            base = _static_prefix(rel)
            for m in matches:
                if not _inside(os.path.join(ctx, m), ctx):
                    raise BuildError(f"COPY source {m!r} resolves outside the build context")
                placement = posixpath.relpath(m, base) if base else m
                add(placement, m)
                if os.path.isdir(os.path.join(ctx, m)) and not os.path.islink(os.path.join(ctx, m)):
                    add_tree(m, placement)
            continue

        abs_src = os.path.join(ctx, *rel.split("/")) if rel != "." else ctx
        if not os.path.lexists(abs_src):
            raise BuildError(f"COPY source {src!r} not found in build context {ctx}")
        if not _inside(abs_src, ctx):
            raise BuildError(f"COPY source {src!r} resolves outside the build context")
        if os.path.isdir(abs_src):
            add_tree(rel, "")
        else:
            if ignored(rel):
                raise BuildError(f"COPY source {src!r} is excluded (ignored file)")
            add(posixpath.basename(rel), rel)
            single_file = len(sources) == 1

    entries = [plan[k] for k in sorted(plan)]
    return CopyPlan(entries=entries, single_file=single_file)


def _inside(abs_path: str, ctx: str) -> bool:
    # Symlinks are copied as links (never followed), so only their location matters.
    check = os.path.dirname(abs_path) if os.path.islink(abs_path) else abs_path
    return is_within(check, ctx)


def _join(a: str, b: str) -> str:
    if not a or a == ".":
        return b
    if not b:
        return a
    return f"{a}/{b}"


def hash_sources(ctx: str, sources: list[str]) -> str:
    """SHA-256 over every source entry (path, type, mode, content hash), sorted by path."""
    plan = plan_copy(ctx, sources)
    h = hashlib.sha256()
    h.update(f"single={int(plan.single_file)}\n".encode())
    for e in plan.entries:
        if e.kind == "f":
            content = sha256_file(e.abs_path)
        elif e.kind == "l":
            content = os.readlink(e.abs_path)
        else:
            content = ""
        h.update(f"{e.kind}\t{e.placement}\t{e.ctx_rel}\t{e.mode:o}\t{content}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def resolve_container_path(workdir: str, p: str) -> str:
    """Resolve a (possibly relative) container path against ``workdir``."""
    p = p.strip()
    base = workdir or "/"
    if not p:
        return posixpath.normpath(base)
    joined = p if p.startswith("/") else posixpath.join(base, p)
    out = posixpath.normpath(joined)
    return "/" + out.lstrip("/")


def _copy_entry(e: SourceEntry, target: str) -> None:
    if os.path.lexists(target) and (e.kind != "d" or not os.path.isdir(target) or os.path.islink(target)):
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        else:
            os.unlink(target)
    if e.kind == "d":
        os.makedirs(target, exist_ok=True)
        os.chmod(target, e.mode)
    elif e.kind == "l":
        os.symlink(os.readlink(e.abs_path), target)
    else:
        shutil.copyfile(e.abs_path, target, follow_symlinks=False)
        os.chmod(target, e.mode)


def apply_copy(rootfs: str, ctx: str, args: list[str], workdir: str) -> None:
    """Execute ``COPY <src>... <dest>`` into ``rootfs``."""
    if len(args) < 2:
        raise BuildError("COPY requires at least source and destination")
    sources, dest_arg = args[:-1], args[-1]
    plan = plan_copy(ctx, sources)
    dest = resolve_container_path(workdir, dest_arg)
    dest_says_dir = dest_arg.endswith("/") or posixpath.basename(dest_arg.rstrip("/")) in (".", "..", "")

    dest_host = resolve_in_root(rootfs, dest, follow_last=True)
    if plan.single_file and not dest_says_dir and not os.path.isdir(dest_host):
        e = plan.entries[0]
        parent = resolve_in_root(rootfs, posixpath.dirname(dest), follow_last=True)
        os.makedirs(parent, exist_ok=True)
        _copy_entry(e, os.path.join(parent, posixpath.basename(dest)))
        return

    if os.path.lexists(dest_host) and not os.path.isdir(dest_host):
        raise BuildError(f"COPY destination {dest_arg!r} exists and is not a directory")
    os.makedirs(dest_host, exist_ok=True)
    for e in plan.entries:
        parent_rel, base = posixpath.split(e.placement)
        parent = resolve_in_root(rootfs, _join(dest, parent_rel) if parent_rel else dest, follow_last=True)
        os.makedirs(parent, exist_ok=True)
        _copy_entry(e, os.path.join(parent, base))
