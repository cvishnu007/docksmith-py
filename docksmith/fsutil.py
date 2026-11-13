"""Filesystem helpers that never escape a given root directory."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import tempfile

# Top-level directories of a root filesystem that are runtime-only (device nodes,
# kernel views). They are never recorded in layer deltas.
RUNTIME_DIRS = frozenset({"dev", "proc", "sys"})

_MAX_SYMLINKS = 40


def resolve_in_root(root: str | os.PathLike, path: str, *, follow_last: bool = False) -> str:
    """Resolve ``path`` as if ``root`` were ``/`` (like ``chroot`` would).

    Symlinks inside the tree are followed, but absolute targets are re-rooted at
    ``root`` and ``..`` can never climb above it, so the result is always inside
    ``root``. Missing components are simply appended.
    """
    root = os.fspath(root)
    pending = [p for p in path.split("/") if p not in ("", ".")]
    pending.reverse()
    resolved: list[str] = []
    hops = 0
    while pending:
        comp = pending.pop()
        if comp == ".":
            continue
        if comp == "..":
            if resolved:
                resolved.pop()
            continue
        candidate = os.path.join(root, *resolved, comp)
        is_last = not pending
        if (follow_last or not is_last) and os.path.islink(candidate):
            hops += 1
            if hops > _MAX_SYMLINKS:
                raise OSError(errno.ELOOP, "too many levels of symbolic links", path)
            target = os.readlink(candidate)
            if target.startswith("/"):
                resolved = []
            pending.extend(reversed([p for p in target.split("/") if p not in ("", ".")]))
            continue
        resolved.append(comp)
    return os.path.join(root, *resolved)


def remove_path(path: str) -> None:
    """Remove a file, symlink or directory tree if it exists."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        rmtree(path)
    else:
        os.unlink(path)


def rmtree(path: str | os.PathLike) -> None:
    def onexc(func, p, exc):  # make read-only dirs writable and retry once
        try:
            parent = os.path.dirname(p)
            os.chmod(parent, 0o700)
            if os.path.isdir(p) and not os.path.islink(p):
                os.chmod(p, 0o700)
            func(p)
        except FileNotFoundError:
            pass

    if os.path.lexists(path):
        shutil.rmtree(path, onexc=onexc)


def make_tempdir(base: str | os.PathLike, prefix: str) -> str:
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=base)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Snapshots: a map of every path in a rootfs to (kind, mode, content-id).
# ---------------------------------------------------------------------------

Entry = tuple[str, int, str]  # ("f"|"d"|"l", permission bits, sha256 / link target / "")


class Snapshotter:
    """Takes rootfs snapshots, re-using file hashes whose stat signature is unchanged."""

    def __init__(self) -> None:
        self._hash_cache: dict[tuple, str] = {}

    def snapshot(self, root: str | os.PathLike) -> dict[str, Entry]:
        root = os.fspath(root)
        result: dict[str, Entry] = {}
        self._walk(root, "", result)
        return result

    def _walk(self, abs_dir: str, rel_dir: str, out: dict[str, Entry]) -> None:
        try:
            with os.scandir(abs_dir) as it:
                entries = sorted(it, key=lambda e: e.name)
        except FileNotFoundError:
            return
        for e in entries:
            if not rel_dir and e.name in RUNTIME_DIRS:
                continue
            rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
            st = e.stat(follow_symlinks=False)
            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("l", 0o777, os.readlink(e.path))
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("d", mode, "")
                self._walk(e.path, rel, out)
            elif stat.S_ISREG(st.st_mode):
                sig = (e.path, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
                digest = self._hash_cache.get(sig)
                if digest is None:
                    digest = sha256_file(e.path)
                    self._hash_cache[sig] = digest
                out[rel] = ("f", mode, digest)
            # sockets, fifos and device nodes are never part of an image


def diff_snapshots(before: dict[str, Entry], after: dict[str, Entry]) -> tuple[list[str], list[str]]:
    """Return ``(changed, deleted)`` relative paths between two snapshots.

    ``changed`` holds every added or modified path. ``deleted`` holds only the
    top-most removed paths (children of a removed directory are implied).
    """
    changed = sorted(p for p, v in after.items() if before.get(p) != v)
    gone = {p for p in before if p not in after}
    deleted = sorted(p for p in gone if os.path.dirname(p) not in gone)
    return changed, deleted


def ensure_dev_nodes(rootfs: str) -> None:
    """Create a minimal /dev (null, zero, random, urandom) inside ``rootfs``.

    /dev is excluded from snapshots, so these never end up in a layer. Creating
    device nodes needs root; failures are ignored (programs then just lack them).
    """
    dev = os.path.join(rootfs, "dev")
    if os.path.islink(dev):
        return
    os.makedirs(dev, exist_ok=True)
    for name, major, minor in (("null", 1, 3), ("zero", 1, 5), ("random", 1, 8), ("urandom", 1, 9)):
        p = os.path.join(dev, name)
        if os.path.lexists(p):
            continue
        try:
            os.mknod(p, 0o666 | stat.S_IFCHR, os.makedev(major, minor))
            os.chmod(p, 0o666)
        except OSError:
            pass
    for name in ("shm", "pts"):
        os.makedirs(os.path.join(dev, name), exist_ok=True)


def ensure_dir_in_root(rootfs: str, container_path: str) -> str:
    """mkdir -p ``container_path`` inside ``rootfs`` (symlink-safe); returns host path."""
    host = resolve_in_root(rootfs, container_path, follow_last=True)
    os.makedirs(host, exist_ok=True)
    return host


def is_within(child: str | os.PathLike, parent: str | os.PathLike) -> bool:
    child = os.path.realpath(child)
    parent = os.path.realpath(parent)
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)
