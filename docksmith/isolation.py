"""The single process-isolation primitive used by both ``RUN`` and ``docksmith run``.

The child process (between fork and exec):

1. ``unshare(CLONE_NEWNS | CLONE_NEWUTS | CLONE_NEWIPC)`` - private mount table,
   hostname and IPC namespace;
2. makes all mounts private, bind-mounts the rootfs onto itself and
   ``pivot_root``s into it, then detaches the old root - the host filesystem is
   no longer reachable from the process at all (stronger than a bare chroot);
3. falls back to ``chroot(2)`` if the kernel/sandbox forbids namespaces;
4. ``chdir``s to the working directory and ``exec``s the command with exactly
   the given environment.

No container runtime (docker, runc, ...) is involved - only OS primitives.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import signal
import socket
import subprocess
import sys

from .models import IsolationError

DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
HOSTNAME = "docksmith"

_CLONE_NEWNS = 0x00020000
_CLONE_NEWUTS = 0x04000000
_CLONE_NEWIPC = 0x08000000
_MS_BIND = 4096
_MS_REC = 16384
_MS_PRIVATE = 1 << 18
_MNT_DETACH = 2
_SYS_PIVOT_ROOT = {"x86_64": 155, "amd64": 155, "aarch64": 41, "arm64": 41, "riscv64": 41,
                   "ppc64le": 203, "s390x": 217, "i686": 217, "i386": 217, "armv7l": 218}

_libc = None
if sys.platform.startswith("linux"):
    try:
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    except OSError:  # pragma: no cover
        _libc = None

# Set to "chroot" to skip the pivot_root path (e.g. for debugging).
ISOLATION_MODE_ENV = "DOCKSMITH_ISOLATION"


def _check(ret: int, what: str) -> None:
    if ret != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"{what}: {os.strerror(err)}")


def _pivot_into(rootfs: str) -> None:
    """Runs in the child: enter new namespaces and pivot_root into ``rootfs``."""
    if _libc is None:
        raise OSError("libc unavailable")
    nr = _SYS_PIVOT_ROOT.get(platform.machine())
    if nr is None:
        raise OSError(f"pivot_root syscall number unknown for {platform.machine()}")
    _check(_libc.unshare(_CLONE_NEWNS | _CLONE_NEWUTS | _CLONE_NEWIPC), "unshare")
    _check(_libc.mount(None, b"/", None, _MS_REC | _MS_PRIVATE, None), "mount private")
    root_b = os.fsencode(rootfs)
    _check(_libc.mount(root_b, root_b, None, _MS_BIND | _MS_REC, None), "bind rootfs")
    os.chdir(rootfs)
    _check(_libc.syscall(nr, b".", b"."), "pivot_root")
    # The old root is now stacked on "/"; detaching it hides the host completely.
    if _libc.umount2(b".", _MNT_DETACH) != 0:
        os._exit(126)  # never continue with the host root visible
    os.chroot(".")
    os.chdir("/")
    try:
        socket.sethostname(HOSTNAME)
    except OSError:
        pass


def _make_preexec(rootfs: str, workdir: str):
    mode = os.environ.get(ISOLATION_MODE_ENV, "").strip().lower()

    def preexec() -> None:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        isolated = False
        if mode != "chroot":
            try:
                _pivot_into(rootfs)
                isolated = True
            except OSError:
                isolated = False
        if not isolated:
            os.chroot(rootfs)
            os.chdir("/")
        os.chdir(workdir or "/")

    return preexec


def process_env(env: dict[str, str]) -> dict[str, str]:
    """Environment for the isolated process: image/override env plus sane defaults."""
    out = {"PATH": DEFAULT_PATH, "HOME": "/root", "HOSTNAME": HOSTNAME}
    out.update(env or {})
    return out


def check_supported() -> None:
    if not sys.platform.startswith("linux"):
        raise IsolationError(
            "process isolation requires Linux (chroot/namespaces); use a Linux VM on macOS/Windows"
        )
    if os.geteuid() != 0:
        raise IsolationError(
            "process isolation requires root (chroot/pivot_root); re-run with sudo"
        )


def run_isolated(rootfs: str, cmd: list[str], env: dict[str, str], workdir: str = "/") -> int:
    """Run ``cmd`` with ``rootfs`` as its ``/``; blocks until exit, returns the exit code."""
    if not cmd:
        raise IsolationError("empty command")
    check_supported()
    rootfs = os.path.abspath(rootfs)
    try:
        proc = subprocess.Popen(
            cmd,
            env=process_env(env),
            preexec_fn=_make_preexec(rootfs, workdir),
            close_fds=True,
        )
    except FileNotFoundError:
        raise IsolationError(f"executable {cmd[0]!r} not found inside the image filesystem") from None
    except PermissionError:
        raise IsolationError(f"executable {cmd[0]!r} is not executable inside the image") from None
    except subprocess.SubprocessError as exc:
        raise IsolationError(f"failed to enter isolated root: {exc}") from None
    except OSError as exc:
        raise IsolationError(f"failed to start {cmd[0]!r} in isolation: {exc}") from None

    while True:
        try:
            code = proc.wait()
            break
        except KeyboardInterrupt:
            # The child received the same SIGINT; keep waiting so cleanup happens after it exits.
            continue
    return 128 - code if code < 0 else code
