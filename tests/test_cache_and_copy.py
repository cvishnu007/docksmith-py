import os

import pytest

from docksmith.cache import BuildCache, cache_key
from docksmith.copyops import apply_copy, hash_sources, plan_copy, resolve_container_path
from docksmith.models import BuildError

from conftest import write


def test_key_env_order_independent():
    assert cache_key("", "RUN echo", "/app", {"A": "1", "B": "2"}) == cache_key(
        "", "RUN echo", "/app", {"B": "2", "A": "1"}
    )


def test_key_raw_whitespace_matters():
    assert cache_key("", "COPY . /app", "/", {}) != cache_key("", "COPY    .    /app", "/", {})


def test_key_components_each_matter():
    base = cache_key("sha256:" + "a" * 64, "RUN x", "/w", {"A": "1"}, "h")
    assert base != cache_key("sha256:" + "b" * 64, "RUN x", "/w", {"A": "1"}, "h")
    assert base != cache_key("sha256:" + "a" * 64, "RUN x", "/v", {"A": "1"}, "h")
    assert base != cache_key("sha256:" + "a" * 64, "RUN x", "/w", {"A": "2"}, "h")
    assert base != cache_key("sha256:" + "a" * 64, "RUN x", "/w", {"A": "1"}, "h2")
    # unset workdir ("") differs from "/"
    assert cache_key("", "RUN x", "", {}) != cache_key("", "RUN x", "/", {})


def test_store_round_trip(tmp_path):
    c = BuildCache(tmp_path / "cache")
    key = "deadbeef" * 8
    assert c.get(key) is None
    c.put(key, "sha256:" + "a" * 64)
    assert c.get(key) == "sha256:" + "a" * 64


def test_copy_hash_changes_with_content(ctx):
    write(ctx / "f.txt", "x")
    h1 = hash_sources(str(ctx), ["f.txt"])
    write(ctx / "f.txt", "y")
    assert hash_sources(str(ctx), ["f.txt"]) != h1


def test_copy_hash_ignores_docksmithfile(ctx):
    write(ctx / "a.txt", "a")
    write(ctx / "Docksmithfile", "FROM scratch")
    h1 = hash_sources(str(ctx), ["."])
    write(ctx / "Docksmithfile", "FROM scratch\n# edited")
    assert hash_sources(str(ctx), ["."]) == h1


def test_glob_single_star(ctx):
    write(ctx / "a.txt", "a")
    write(ctx / "b.txt", "b")
    write(ctx / "c.md", "c")
    write(ctx / "sub" / "d.txt", "d")
    plan = plan_copy(str(ctx), ["*.txt"])
    assert [e.placement for e in plan.entries] == ["a.txt", "b.txt"]


def test_glob_double_star_keeps_structure(ctx):
    write(ctx / "src" / "a.py", "")
    write(ctx / "src" / "pkg" / "b.py", "")
    write(ctx / "src" / "pkg" / "c.txt", "")
    plan = plan_copy(str(ctx), ["src/**/*.py"])
    assert [e.placement for e in plan.entries] == ["a.py", "pkg/b.py"]


def test_glob_no_match_is_error(ctx):
    with pytest.raises(BuildError, match="matched no files"):
        plan_copy(str(ctx), ["*.nothing"])


def test_source_outside_context_rejected(ctx):
    with pytest.raises(BuildError, match="outside the build context"):
        plan_copy(str(ctx), ["../etc/passwd"])


def test_missing_source(ctx):
    with pytest.raises(BuildError, match="not found"):
        plan_copy(str(ctx), ["nope.txt"])


def test_dockerignore_style_file(ctx):
    write(ctx / "keep.txt", "k")
    write(ctx / "secret.env", "s")
    write(ctx / "build" / "out.o", "o")
    write(ctx / ".docksmithignore", "*.env\nbuild\n")
    plan = plan_copy(str(ctx), ["."])
    assert [e.placement for e in plan.entries] == ["keep.txt"]


def test_apply_copy_semantics(ctx, tmp_path):
    root = tmp_path / "rootfs"
    root.mkdir()
    write(ctx / "f.txt", "hello", 0o755)
    write(ctx / "dir" / "x.txt", "x")
    # file -> explicit file path
    apply_copy(str(root), str(ctx), ["f.txt", "/opt/renamed.txt"], "/")
    assert (root / "opt" / "renamed.txt").read_text() == "hello"
    assert os.stat(root / "opt" / "renamed.txt").st_mode & 0o777 == 0o755
    # file -> directory (trailing slash) relative to workdir
    apply_copy(str(root), str(ctx), ["f.txt", "sub/"], "/work")
    assert (root / "work" / "sub" / "f.txt").exists()
    # file -> "." means workdir
    apply_copy(str(root), str(ctx), ["f.txt", "."], "/w2")
    assert (root / "w2" / "f.txt").exists()
    # directory copies its contents
    apply_copy(str(root), str(ctx), ["dir", "/d"], "/")
    assert (root / "d" / "x.txt").read_text() == "x"
    # file into an existing directory without trailing slash
    apply_copy(str(root), str(ctx), ["f.txt", "/d"], "/")
    assert (root / "d" / "f.txt").exists()


def test_apply_copy_does_not_escape_rootfs_via_symlink(ctx, tmp_path):
    root = tmp_path / "rootfs"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    os.symlink(str(outside), root / "evil")  # absolute link pointing at host dir
    write(ctx / "f.txt", "x")
    apply_copy(str(root), str(ctx), ["f.txt", "/evil/"], "/")
    assert not (outside / "f.txt").exists()
    assert (root / str(outside).lstrip("/") / "f.txt").exists()


def test_resolve_container_path():
    assert resolve_container_path("/app", "sub") == "/app/sub"
    assert resolve_container_path("/app", "/abs") == "/abs"
    assert resolve_container_path("/app", "../x") == "/x"
    assert resolve_container_path("", "a") == "/a"
