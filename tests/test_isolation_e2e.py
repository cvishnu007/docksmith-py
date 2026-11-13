"""End-to-end tests of RUN + `docksmith run` through the real isolation primitive.

These need root on Linux and are skipped otherwise.
"""

import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from docksmith.baseimage import create_host_base, import_rootfs
from docksmith.layer import LayerStore
from docksmith.models import BuildError, DocksmithError
from docksmith.parser import parse_string
from docksmith.runtime import Runtime

from conftest import needs_root, write

pytestmark = needs_root
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def base(state):
    img, _ = create_host_base(state, "base:latest")
    return img


def _cli(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "docksmith", *args],
        cwd=REPO, capture_output=True, text=True, env={**os.environ, **(env or {})},
    )


def test_run_executes_inside_image_not_host(state, builder, base, ctx, tmp_path):
    marker = tmp_path / "host-only.txt"
    marker.write_text("host")
    text = f"FROM base\nRUN test ! -e {marker} && echo inside > /proof.txt\n"
    img = builder.build(parse_string(text), "iso:1", str(ctx))
    with tarfile.open(builder.layers.path(img.layers[-1].digest)) as tf:
        assert tf.getnames() == ["proof.txt"]  # delta only; no host binaries leaked in
    assert not Path("/proof.txt").exists()


def test_run_failure_stops_build(builder, base, ctx):
    with pytest.raises(BuildError, match="exited with code 7"):
        builder.build(parse_string("FROM base\nRUN exit 7\n"), "bad:1", str(ctx))


def test_run_deletion_becomes_whiteout(builder, base, ctx, state):
    write(ctx / "f.txt", "x")
    text = "FROM base\nCOPY f.txt /data/\nRUN rm /data/f.txt\n"
    img = builder.build(parse_string(text), "del:1", str(ctx))
    saved = builder.images.save(img)
    rt = Runtime(state, LayerStore(state.layers))
    assert rt.run(saved, ["sh", "-c", "test ! -e /data/f.txt && test -d /data"]) == 0


def test_container_env_workdir_override_and_exit_code(builder, base, ctx, state):
    text = 'FROM base\nWORKDIR /srv/app\nENV GREETING=hello\nCMD ["sh", "-c", "exit 0"]\n'
    saved = builder.images.save(builder.build(parse_string(text), "env:1", str(ctx)))
    rt = Runtime(state, LayerStore(state.layers))
    assert rt.run(saved, ["sh", "-c", '[ "$(pwd)" = /srv/app ] && [ "$GREETING" = hello ]']) == 0
    assert rt.run(saved, ["sh", "-c", '[ "$GREETING" = bye ]'], {"GREETING": "bye"}) == 0
    assert rt.run(saved, ["sh", "-c", "exit 42"]) == 42
    assert rt.run(saved) == 0


def test_container_writes_never_reach_host(builder, base, ctx, state, tmp_path):
    saved = builder.images.load("base:latest")
    rt = Runtime(state, LayerStore(state.layers))
    target = tmp_path / "should-not-exist.txt"
    code = rt.run(saved, ["sh", "-c", f"mkdir -p {tmp_path} && echo hacked > {target} && echo x > /root-file"])
    assert code == 0
    assert not target.exists()
    assert not Path("/root-file").exists()
    # host files are not visible inside
    assert rt.run(saved, ["sh", "-c", f"test ! -e {REPO}/docksmith/cli.py"]) == 0
    # nothing left behind in the scratch area
    assert os.listdir(state.tmp) == []


def test_no_cmd_is_clear_error(state, builder, ctx, base):
    saved = builder.images.save(builder.build(parse_string("FROM scratch\nWORKDIR /x\n"), "nocmd:1", str(ctx)))
    with pytest.raises(DocksmithError, match="no CMD"):
        Runtime(state, LayerStore(state.layers)).run(saved)


def test_import_rootfs_tarball_with_links(state, tmp_path):
    # Re-export the host base as a tarball with absolute symlinks + a hardlink, like
    # alpine-minirootfs (busybox applets are symlinks), then import it.
    base, _ = create_host_base(state, "hostbase:1")
    layer_path = LayerStore(state.layers).path(base.layers[0].digest)
    out = tmp_path / "rootfs.tar.gz"
    with tarfile.open(layer_path) as src, tarfile.open(out, "w:gz") as dst:
        for m in src.getmembers():
            dst.addfile(m, src.extractfile(m) if m.isreg() else None)
        ln = tarfile.TarInfo("usr/bin/hello")
        ln.type = tarfile.SYMTYPE
        ln.linkname = "/usr/bin/echo"
        dst.addfile(ln)
        hard = tarfile.TarInfo("usr/bin/cat2")
        hard.type = tarfile.LNKTYPE
        hard.linkname = "usr/bin/cat"
        dst.addfile(hard)
    img = import_rootfs(state, str(out), "imported:1")
    assert img.config.cmd == ["/bin/sh"]
    rt = Runtime(state, LayerStore(state.layers))
    assert rt.run(img, ["sh", "-c", "hello ok >/dev/null && cat2 /etc/hostname >/dev/null"]) == 0


def test_cli_demo_flow(tmp_path):
    home = tmp_path / "home"
    env = {"DOCKSMITH_HOME": str(home)}
    assert _cli("base", env=env).returncode == 0
    cold = _cli("build", "-t", "sample:latest", "sample-app", env=env)
    assert cold.returncode == 0, cold.stderr
    assert cold.stdout.count("[CACHE MISS]") == 2
    warm = _cli("build", "-t", "sample:latest", "sample-app", env=env)
    assert warm.stdout.count("[CACHE HIT]") == 2
    assert cold.stdout.splitlines()[-1].split()[2] == warm.stdout.splitlines()[-1].split()[2]
    run = _cli("run", "-e", "MESSAGE=Hi Professor", "sample:latest", env=env)
    assert "MESSAGE=Hi Professor" in run.stdout and "Exit code: 0" in run.stdout
    images = _cli("images", env=env).stdout
    assert "sample" in images and "CREATED" in images
    bad = _cli("run", "sample:latest", "sh", "-c", "exit 5", env=env)
    assert bad.returncode == 5 and "Exit code: 5" in bad.stdout
    rmi = _cli("rmi", "sample:latest", env=env)
    assert rmi.returncode == 0
    again = _cli("rmi", "sample:latest", env=env)
    assert again.returncode == 1 and "not found" in again.stderr
