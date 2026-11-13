import os

import pytest

from docksmith.models import BuildError, Image, ImageConfig
from docksmith.parser import parse_string

from conftest import write


def build(builder, ctx, text, ref="t:1", **kw):
    steps = []
    img = builder.build(parse_string(text), ref, str(ctx), on_step=steps.append, **kw)
    return img, [s.cache_status for s in steps if s.op in ("COPY", "RUN")]


DF = "FROM scratch\nCOPY a.txt /app/\nCOPY a.txt /app2/\n"


def test_layer_digests_deterministic(builder, ctx):
    write(ctx / "a.txt", "a")
    img1, s1 = build(builder, ctx, DF)
    img2, s2 = build(builder, ctx, DF)
    assert s1 == ["CACHE MISS", "CACHE MISS"]
    assert s2 == ["CACHE HIT", "CACHE HIT"]
    assert [l.digest for l in img1.layers] == [l.digest for l in img2.layers]


def test_identical_rebuild_gives_identical_manifest(builder, ctx):
    write(ctx / "a.txt", "a")
    first = builder.images.save(build(builder, ctx, DF)[0])
    second = builder.images.save(build(builder, ctx, DF, no_cache=True)[0])
    assert first.digest == second.digest and first.created == second.created


def test_copy_cache_invalidates_and_cascades(builder, ctx):
    write(ctx / "a.txt", "a")
    write(ctx / "b.txt", "b")
    text = "FROM scratch\nCOPY a.txt /app/\nCOPY b.txt /app/\nCOPY a.txt /x/\n"
    img1, _ = build(builder, ctx, text)
    write(ctx / "b.txt", "B")
    img2, statuses = build(builder, ctx, text)
    assert statuses == ["CACHE HIT", "CACHE MISS", "CACHE MISS"]
    assert img1.layers[0].digest == img2.layers[0].digest
    assert img1.layers[1].digest != img2.layers[1].digest


@pytest.mark.parametrize("change", [
    ("ENV A=1", "ENV A=2"),
    ("WORKDIR /w", "WORKDIR /v"),
    ("COPY a.txt /app/", "COPY  a.txt /app/"),
])
def test_invalidation_triggers(builder, ctx, change):
    write(ctx / "a.txt", "a")
    tpl = "FROM scratch\nCOPY a.txt /first/\n{}\nCOPY a.txt /app/\n"
    build(builder, ctx, tpl.format(change[0]))
    _, statuses = build(builder, ctx, tpl.format(change[1]))
    assert statuses[0] == "CACHE HIT"
    assert statuses[-1] == "CACHE MISS"


def test_missing_layer_file_is_a_miss(builder, ctx):
    write(ctx / "a.txt", "a")
    img, _ = build(builder, ctx, DF)
    os.unlink(builder.layers.path(img.layers[0].digest))
    _, statuses = build(builder, ctx, DF)
    assert statuses == ["CACHE MISS", "CACHE MISS"]


def test_no_cache_skips_lookups_and_writes(builder, ctx, state):
    write(ctx / "a.txt", "a")
    _, statuses = build(builder, ctx, DF, no_cache=True)
    assert statuses == ["CACHE MISS", "CACHE MISS"]
    assert os.listdir(state.cache) == []
    _, statuses = build(builder, ctx, DF)
    assert statuses == ["CACHE MISS", "CACHE MISS"]


def test_from_base_image_and_base_change_invalidates(builder, ctx):
    write(ctx / "a.txt", "a")
    builder.images.save(Image("base", "latest", config=ImageConfig(env={"X": "1"}, working_dir="/srv")))
    text = "FROM base\nCOPY a.txt .\n"
    img, statuses = build(builder, ctx, text)
    assert img.config.env == {"X": "1"} and img.config.working_dir == "/srv"
    assert statuses == ["CACHE MISS"]
    assert build(builder, ctx, text)[1] == ["CACHE HIT"]
    builder.images.save(Image("base", "latest", config=ImageConfig(env={"X": "2"})))
    assert build(builder, ctx, text)[1] == ["CACHE MISS"]


def test_from_missing_image_is_clear_error(builder, ctx):
    with pytest.raises(BuildError, match="line 1: FROM nope.*not found"):
        build(builder, ctx, "FROM nope:1\nCMD [\"sh\"]")


def test_first_instruction_must_be_from(builder, ctx):
    with pytest.raises(BuildError, match="first instruction must be FROM"):
        build(builder, ctx, "WORKDIR /x\nFROM scratch")


def test_config_only_instructions_produce_no_layers(builder, ctx):
    img, _ = build(builder, ctx, 'FROM scratch\nWORKDIR /a\nWORKDIR b\nENV K=v\nCMD ["x"]\n')
    assert img.layers == []
    assert img.config.working_dir == "/a/b"
    assert img.config.env == {"K": "v"} and img.config.cmd == ["x"]


def test_save_load_round_trip(builder, ctx):
    write(ctx / "f.txt", "x")
    img, _ = build(builder, ctx, 'FROM scratch\nCOPY f.txt /w/\nCMD ["sh"]', ref="app:latest")
    saved = builder.images.save(img)
    loaded = builder.images.load("app:latest")
    assert loaded.digest == saved.digest and len(loaded.layers) == 1


def test_glob_copy_build(builder, ctx):
    write(ctx / "a.py", "a")
    write(ctx / "pkg" / "b.py", "b")
    write(ctx / "notes.txt", "n")
    img, _ = build(builder, ctx, "FROM scratch\nCOPY **/*.py /app/\n")
    import tarfile

    with tarfile.open(builder.layers.path(img.layers[0].digest)) as tf:
        assert tf.getnames() == ["app", "app/a.py", "app/pkg", "app/pkg/b.py"]
