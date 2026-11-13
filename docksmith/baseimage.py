"""Creating base images in the local store (done once, before any build).

Two offline options:

* ``import_rootfs`` - import a root filesystem tarball you downloaded once
  during setup (e.g. Alpine's ``alpine-minirootfs-*.tar.gz``);
* ``create_host_base`` - assemble a minimal userland (sh, coreutils, ...) and
  its shared libraries from the host machine. No network needed at all.

Either way the result is a normal image with one deterministic layer.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from .fsutil import Snapshotter, diff_snapshots, make_tempdir, resolve_in_root, rmtree
from .image import ImageStore
from .isolation import DEFAULT_PATH
from .layer import LayerStore, build_layer_tar, extract_tar
from .models import DocksmithError, Image, ImageConfig, ImageLayer
from .paths import StatePaths
from .refs import parse_ref

HOST_BINARIES = (
    "sh", "bash", "cat", "echo", "ls", "env", "mkdir", "rm", "rmdir", "cp", "mv", "ln",
    "chmod", "chown", "grep", "sed", "awk", "head", "tail", "wc", "touch", "date", "uname",
    "dirname", "basename", "tr", "tee", "sort", "uniq", "cut", "find", "xargs", "test", "[",
    "true", "false", "sleep", "printf", "pwd", "id", "whoami", "hostname", "stat", "du",
    "df", "readlink", "realpath", "md5sum", "sha256sum", "expr", "seq", "yes", "diff", "tar",
    "gzip", "ps",
)
_HOST_SEARCH_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _rootfs_path(host_path: str) -> str:
    """Map a host path into a merged-/usr layout (``/bin/x`` -> ``/usr/bin/x``)."""
    parts = host_path.lstrip("/").split("/")
    if parts and parts[0] in ("bin", "sbin", "lib", "lib32", "lib64", "libx32"):
        parts.insert(0, "usr")
    return "/" + "/".join(parts)


def _copy_host_file(rootfs: str, host_path: str, dest: str, mode: int | None = None) -> None:
    real = os.path.realpath(host_path)
    target = resolve_in_root(rootfs, dest, follow_last=False)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.lexists(target):
        return
    shutil.copyfile(real, target)
    os.chmod(target, mode if mode is not None else (os.stat(real).st_mode & 0o777))


def _shared_libs(binary: str) -> list[str]:
    try:
        out = subprocess.run(["ldd", binary], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    libs: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if "=>" in line:
            rhs = line.split("=>", 1)[1].split()
            if rhs and rhs[0].startswith("/"):
                libs.append(rhs[0])
        else:
            first = line.split()[0] if line.split() else ""
            if first.startswith("/"):
                libs.append(first)
    return libs


def _populate_host_userland(rootfs: str) -> int:
    for d in ("usr/bin", "usr/sbin", "usr/lib", "usr/lib64", "etc", "root", "tmp", "home", "var/tmp"):
        os.makedirs(os.path.join(rootfs, d), exist_ok=True)
    os.chmod(os.path.join(rootfs, "tmp"), 0o1777)
    os.chmod(os.path.join(rootfs, "var/tmp"), 0o1777)
    os.chmod(os.path.join(rootfs, "root"), 0o700)
    for link in ("bin", "sbin", "lib", "lib64"):
        os.symlink(f"usr/{link}", os.path.join(rootfs, link))

    count = 0
    seen_libs: set[str] = set()
    for name in HOST_BINARIES:
        host = shutil.which(name, path=_HOST_SEARCH_PATH)
        if not host:
            continue
        _copy_host_file(rootfs, host, f"/usr/bin/{name}", 0o755)
        count += 1
        for lib in _shared_libs(os.path.realpath(host)):
            if lib not in seen_libs:
                seen_libs.add(lib)
                _copy_host_file(rootfs, lib, _rootfs_path(lib))
    if not os.path.lexists(os.path.join(rootfs, "usr/bin/sh")):
        raise DocksmithError("could not find a host /bin/sh to build the base image from")

    files = {
        "etc/passwd": "root:x:0:0:root:/root:/bin/sh\nnobody:x:65534:65534:nobody:/nonexistent:/bin/false\n",
        "etc/group": "root:x:0:\nnogroup:x:65534:\n",
        "etc/hostname": "docksmith\n",
        "etc/os-release": 'NAME="Docksmith host base"\nID=docksmith\n',
    }
    for rel, content in files.items():
        with open(os.path.join(rootfs, rel), "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(os.path.join(rootfs, rel), 0o644)
    return count


def _save_single_layer_image(paths: StatePaths, rootfs: str, ref: str, created_by: str,
                             cmd: list[str]) -> Image:
    layers = LayerStore(paths.layers)
    store = ImageStore(paths.images, layers)
    after = Snapshotter().snapshot(rootfs)
    changed, deleted = diff_snapshots({}, after)
    digest, size = layers.put(build_layer_tar(rootfs, changed, deleted))
    name, tag = parse_ref(ref)
    has_sh = os.path.lexists(resolve_in_root(rootfs, "/bin/sh", follow_last=True))
    img = Image(
        name=name,
        tag=tag,
        config=ImageConfig(cmd=cmd if has_sh else [], env={"PATH": DEFAULT_PATH}, working_dir="/"),
        layers=[ImageLayer(digest=digest, size=size, created_by=created_by)],
    )
    return store.save(img)


def create_host_base(paths: StatePaths, ref: str = "base:latest") -> tuple[Image, int]:
    paths.ensure()
    rootfs = make_tempdir(paths.tmp, "base-")
    try:
        count = _populate_host_userland(rootfs)
        img = _save_single_layer_image(paths, rootfs, ref, f"BASE host userland ({count} binaries)", ["/bin/sh"])
        return img, count
    finally:
        rmtree(rootfs)


def import_rootfs(paths: StatePaths, tarball: str, ref: str) -> Image:
    if not os.path.isfile(tarball):
        raise DocksmithError(f"rootfs tarball {tarball!r} not found")
    paths.ensure()
    rootfs = make_tempdir(paths.tmp, "import-")
    try:
        try:
            extract_tar(tarball, rootfs)
        except Exception as exc:  # tarfile raises a variety of errors
            raise DocksmithError(f"could not import {tarball!r}: {exc}") from None
        return _save_single_layer_image(paths, rootfs, ref, f"IMPORT {os.path.basename(tarball)}", ["/bin/sh"])
    finally:
        rmtree(rootfs)
