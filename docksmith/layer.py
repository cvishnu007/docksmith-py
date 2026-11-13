"""Content-addressed, deterministic delta layers.

A layer is a tar archive holding only the paths added or modified by one step,
plus OCI-style whiteout entries (``.wh.<name>``) for paths it deleted. Tars are
byte-for-byte reproducible: entries are sorted, timestamps are zero and owners
are root, so identical content always yields the identical digest.
"""

from __future__ import annotations

import hashlib
import io
import os
import posixpath
import shutil
import stat
import tarfile

from .fsutil import remove_path, resolve_in_root
from .models import DocksmithError

DIGEST_PREFIX = "sha256:"
WHITEOUT_PREFIX = ".wh."
OPAQUE_WHITEOUT = ".wh..wh..opq"


class LayerError(DocksmithError):
    pass


def digest_hex(digest: str) -> str:
    d = (digest or "").strip()
    if not d.startswith(DIGEST_PREFIX):
        raise LayerError(f"invalid digest {digest!r}: must start with {DIGEST_PREFIX!r}")
    hexpart = d[len(DIGEST_PREFIX):]
    if len(hexpart) != 64 or any(c not in "0123456789abcdef" for c in hexpart):
        raise LayerError(f"invalid digest {digest!r}")
    return hexpart


def sha256_digest(data: bytes) -> str:
    return DIGEST_PREFIX + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _parents(rel: str):
    d = posixpath.dirname(rel)
    while d:
        yield d
        d = posixpath.dirname(d)


def build_layer_tar(root: str, changed: list[str], deleted: list[str]) -> bytes:
    """Serialise a delta (``changed`` paths + ``deleted`` whiteouts) of ``root``."""
    entries: dict[str, str] = {}
    for rel in changed:
        entries[rel] = "node"
        for p in _parents(rel):
            entries.setdefault(p, "node")
    for rel in deleted:
        parent, base = posixpath.split(rel)
        wh = posixpath.join(parent, WHITEOUT_PREFIX + base) if parent else WHITEOUT_PREFIX + base
        entries[wh] = "whiteout"
        for p in _parents(wh):
            entries.setdefault(p, "node")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tw:
        for name in sorted(entries):
            ti = tarfile.TarInfo(name)
            ti.mtime = 0
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            if entries[name] == "whiteout":
                ti.type = tarfile.REGTYPE
                ti.mode = 0o644
                ti.size = 0
                tw.addfile(ti)
                continue
            disk = os.path.join(root, *name.split("/"))
            st = os.lstat(disk)
            ti.mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISDIR(st.st_mode):
                ti.type = tarfile.DIRTYPE
                tw.addfile(ti)
            elif stat.S_ISLNK(st.st_mode):
                ti.type = tarfile.SYMTYPE
                ti.mode = 0o777
                ti.linkname = os.readlink(disk)
                tw.addfile(ti)
            elif stat.S_ISREG(st.st_mode):
                ti.type = tarfile.REGTYPE
                ti.size = st.st_size
                with open(disk, "rb") as f:
                    tw.addfile(ti, f)
            # other file types are skipped (never snapshotted either)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _normalise_member_name(name: str) -> str | None:
    n = name.replace("\\", "/")
    while n.startswith("./"):
        n = n[2:]
    n = n.lstrip("/")
    n = posixpath.normpath(n) if n else ""
    if n in ("", "."):
        return None
    if n == ".." or n.startswith("../") or "/../" in f"/{n}/":
        raise LayerError(f"invalid path in layer tar: {name!r}")
    return n


def extract_tar(tar_source, root: str) -> None:
    """Apply a layer tar on top of ``root``; later layers overwrite earlier ones.

    ``tar_source`` is a path or a binary file object. Entries can never be written
    outside ``root``: names with ``..`` are rejected and symlinks in the existing
    tree are resolved relative to ``root``.
    """
    root = os.fspath(root)
    if isinstance(tar_source, (str, os.PathLike)):
        tf = tarfile.open(os.fspath(tar_source), mode="r:*")
    else:
        tf = tarfile.open(fileobj=tar_source, mode="r:*")
    with tf:
        for m in tf:
            rel = _normalise_member_name(m.name)
            if rel is None:  # the archive root itself ("./")
                continue
            parent_rel, base = posixpath.split(rel)
            parent = resolve_in_root(root, parent_rel, follow_last=True)
            os.makedirs(parent, exist_ok=True)

            if base == OPAQUE_WHITEOUT:
                for child in os.listdir(parent):
                    remove_path(os.path.join(parent, child))
                continue
            if base.startswith(WHITEOUT_PREFIX):
                remove_path(os.path.join(parent, base[len(WHITEOUT_PREFIX):]))
                continue

            target = os.path.join(parent, base)
            mode = m.mode & 0o7777
            if m.isdir():
                if os.path.lexists(target) and (os.path.islink(target) or not os.path.isdir(target)):
                    os.unlink(target)
                os.makedirs(target, exist_ok=True)
                os.chmod(target, mode or 0o755)
            elif m.isreg():
                if os.path.lexists(target):
                    remove_path(target)
                src = tf.extractfile(m)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as out:
                    if src is not None:
                        shutil.copyfileobj(src, out, 1 << 20)
                os.chmod(target, mode or 0o644)
            elif m.issym():
                if os.path.lexists(target):
                    remove_path(target)
                os.symlink(m.linkname, target)
            elif m.islnk():
                link_rel = _normalise_member_name(m.linkname)
                if link_rel is None:
                    continue
                src_path = resolve_in_root(root, link_rel)
                if os.path.lexists(target):
                    remove_path(target)
                try:
                    os.link(src_path, target, follow_symlinks=False)
                except OSError:
                    if os.path.isfile(src_path) and not os.path.islink(src_path):
                        shutil.copy2(src_path, target)
            # character/block devices and fifos are ignored


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class LayerStore:
    """``layers/`` directory: immutable tars named by the sha256 of their bytes."""

    def __init__(self, layers_dir: str | os.PathLike):
        self.dir = os.fspath(layers_dir)

    def path(self, digest: str) -> str:
        return os.path.join(self.dir, f"sha256_{digest_hex(digest)}.tar")

    def exists(self, digest: str) -> bool:
        try:
            return os.path.isfile(self.path(digest))
        except LayerError:
            return False

    def size(self, digest: str) -> int:
        return os.path.getsize(self.path(digest))

    def put(self, data: bytes) -> tuple[str, int]:
        """Store ``data``; never rewrites an existing layer (layers are immutable)."""
        digest = sha256_digest(data)
        path = self.path(digest)
        if os.path.isfile(path):
            return digest, os.path.getsize(path)
        os.makedirs(self.dir, exist_ok=True)
        tmp = f"{path}.tmp-{os.getpid()}"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o444)
        os.replace(tmp, path)
        return digest, len(data)

    def extract(self, digest: str, root: str) -> None:
        path = self.path(digest)
        if not os.path.isfile(path):
            raise LayerError(
                f"layer {digest} is missing from {self.dir} (was it removed by rmi?); rebuild the image"
            )
        extract_tar(path, root)

    def remove(self, digest: str) -> bool:
        try:
            path = self.path(digest)
        except LayerError:
            return False
        try:
            os.chmod(path, 0o644)
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False
