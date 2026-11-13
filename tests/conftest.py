import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docksmith.builder import Builder  # noqa: E402
from docksmith.paths import StatePaths  # noqa: E402

IS_ROOT_LINUX = sys.platform.startswith("linux") and os.geteuid() == 0 and shutil.which("sh") is not None
needs_root = pytest.mark.skipif(not IS_ROOT_LINUX, reason="isolation tests need root on Linux")


@pytest.fixture(autouse=True)
def _umask():
    old = os.umask(0o022)
    yield
    os.umask(old)


@pytest.fixture
def state(tmp_path, monkeypatch) -> StatePaths:
    root = tmp_path / "docksmith-home"
    monkeypatch.setenv("DOCKSMITH_HOME", str(root))
    return StatePaths(root).ensure()


@pytest.fixture
def builder(state) -> Builder:
    return Builder.from_paths(state)


@pytest.fixture
def ctx(tmp_path) -> Path:
    d = tmp_path / "ctx"
    d.mkdir()
    return d


def write(path: Path, content: str, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if mode is not None:
        os.chmod(path, mode)
    return path
