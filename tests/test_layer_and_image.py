import io
import json
import os
import tarfile

import pytest

from docksmith.fsutil import Snapshotter, diff_snapshots, resolve_in_root
from docksmith.image import ImageStore, compute_manifest_digest
from docksmith.layer import LayerError, LayerStore, build_layer_tar, extract_tar
from docksmith.models import Image, ImageConfig, ImageLayer, ImageNotFound

from conftest import write


def _layer(root, before):
    after = Snapshotter().snapshot(root)
    changed, deleted = diff_snapshots(before, after)
    return build_layer_tar(str(root), changed, deleted)


def test_tar_is_deterministic_sorted_and_zeroed(tmp_path):
    root = tmp_path / "r"
    write(root / "z.txt", "z")
    write(root / "a" / "b.txt", "b")
    data1 = _layer(root, {})
    os.utime(root / "z.txt", (12345, 12345))
    data2 = _layer(root, {})
    assert data1 == data2
    with tarfile.open(fileobj=io.BytesIO(data1)) as tf:
        members = tf.getmembers()
    assert [m.name for m in members] == ["a", "a/b.txt", "z.txt"]
    assert all(m.mtime == 0 and m.uid == 0 and m.gid == 0 for m in members)


def test_delta_contains_only_changes_and_whiteouts(tmp_path):
    root = tmp_path / "r"
    write(root / "keep.txt", "k")
    write(root / "edit.txt", "1")
    write(root / "gone" / "x.txt", "x")
    snap = Snapshotter()
    before = snap.snapshot(root)
    write(root / "edit.txt", "2")
    write(root / "new" / "n.txt", "n")
    (root / "gone" / "x.txt").unlink()
    (root / "gone").rmdir()
    after = snap.snapshot(root)
    changed, deleted = diff_snapshots(before, after)
    assert changed == ["edit.txt", "new", "new/n.txt"]
    assert deleted == ["gone"]  # child implied
    data = build_layer_tar(str(root), changed, deleted)
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        assert tf.getnames() == [".wh.gone", "edit.txt", "new", "new/n.txt"]


def test_whiteouts_applied_on_extract(tmp_path):
    base = tmp_path / "base"
    write(base / "a.txt", "a")
    write(base / "d" / "x", "x")
    snap = Snapshotter()
    l1 = build_layer_tar(str(base), *diff_snapshots({}, snap.snapshot(base)))
    before = snap.snapshot(base)
    (base / "a.txt").unlink()
    (base / "d" / "x").unlink()
    (base / "d").rmdir()
    l2 = _layer(base, before)
    out = tmp_path / "out"
    out.mkdir()
    extract_tar(io.BytesIO(l1), str(out))
    extract_tar(io.BytesIO(l2), str(out))
    assert sorted(os.listdir(out)) == []


def test_extract_rejects_traversal(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo("../escape.txt")
        ti.size = 1
        tf.addfile(ti, io.BytesIO(b"x"))
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(LayerError, match="invalid path"):
        extract_tar(io.BytesIO(buf.getvalue()), str(out))
    assert not (tmp_path / "escape.txt").exists()


def test_extract_through_symlink_stays_in_root(tmp_path):
    outside = tmp_path / "host"
    outside.mkdir()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = str(outside)
        tf.addfile(link)
        f = tarfile.TarInfo("link/pwned.txt")
        f.size = 1
        tf.addfile(f, io.BytesIO(b"x"))
    root = tmp_path / "root"
    root.mkdir()
    extract_tar(io.BytesIO(buf.getvalue()), str(root))
    assert not (outside / "pwned.txt").exists()
    assert os.path.exists(resolve_in_root(str(root), "/link/pwned.txt", follow_last=True))


def test_layer_store_is_content_addressed_and_immutable(tmp_path):
    store = LayerStore(tmp_path / "layers")
    d1, s1 = store.put(b"abc")
    d2, s2 = store.put(b"abc")
    assert (d1, s1) == (d2, s2)
    assert os.path.basename(store.path(d1)) == f"sha256_{d1[7:]}.tar"
    assert store.remove(d1) and not store.exists(d1)


def _image(**kw):
    defaults = dict(
        name="myapp", tag="latest",
        config=ImageConfig(cmd=["echo", "hi"], env={"B": "2", "A": "1"}, working_dir="/app"),
        layers=[ImageLayer("sha256:" + "a" * 64, 10, "COPY . /app")],
    )
    defaults.update(kw)
    return Image(**defaults)


def test_manifest_digest_follows_spec(state):
    store = ImageStore(state.images, LayerStore(state.layers))
    saved = store.save(_image())
    path = state.images / "myapp__latest.json"
    assert ImageStore.verify_digest(str(path))
    data = json.loads(path.read_text())
    assert data["digest"] == saved.digest
    assert data["config"]["Env"] == ["A=1", "B=2"]
    assert list(data) == ["name", "tag", "digest", "created", "config", "layers"]
    assert data["layers"][0] == {"digest": "sha256:" + "a" * 64, "size": 10, "createdBy": "COPY . /app"}
    loaded = store.load("myapp:latest")
    assert compute_manifest_digest(loaded) == loaded.digest == saved.digest


def test_created_preserved_for_identical_rebuild(state):
    store = ImageStore(state.images, LayerStore(state.layers))
    first = store.save(_image())
    second = store.save(_image())
    assert second.created == first.created and second.digest == first.digest
    changed = store.save(_image(config=ImageConfig(cmd=["other"])))
    assert changed.digest != first.digest


def test_list_and_remove_deletes_layers(state):
    layers = LayerStore(state.layers)
    store = ImageStore(state.images, layers)
    digest, size = layers.put(b"layer-bytes")
    store.save(_image(name="x", tag="y", layers=[ImageLayer(digest, size, "RUN echo")]))
    assert [i.ref for i in store.list()] == ["x:y"]
    store.remove("x:y")
    assert not layers.exists(digest)
    assert store.list() == []
    with pytest.raises(ImageNotFound):
        store.remove("x:y")


def test_tag_defaults_to_latest(state):
    store = ImageStore(state.images, LayerStore(state.layers))
    store.save(_image(name="base", tag="latest"))
    assert store.load("base").ref == "base:latest"
