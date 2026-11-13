"""Build engine: executes a parsed Docksmithfile into a layered image."""

from __future__ import annotations

import os
import time
from typing import Callable

from .cache import BuildCache, cache_key
from .copyops import apply_copy, hash_sources, resolve_container_path
from .fsutil import Snapshotter, ensure_dev_nodes, ensure_dir_in_root, make_tempdir, resolve_in_root, rmtree
from .image import ImageStore
from .isolation import check_supported, run_isolated
from .layer import LayerStore, build_layer_tar
from .fsutil import diff_snapshots
from .models import BuildError, BuildStep, DocksmithError, Image, ImageConfig, ImageLayer, Instruction
from .paths import StatePaths
from .refs import format_ref, parse_ref

ProgressFn = Callable[[BuildStep], None]


class _Rootfs:
    """Working filesystem for one build stage, materialised lazily.

    Cache hits only record layer digests; the tree is assembled (by extracting
    the pending layers) the first time a step actually has to execute.
    """

    def __init__(self, tmp_base: str, layers: LayerStore):
        self.tmp_base = tmp_base
        self.layers = layers
        self.path: str | None = None
        self.applied = 0

    def materialise(self, image_layers: list[ImageLayer]) -> str:
        if self.path is None:
            self.path = make_tempdir(self.tmp_base, "build-")
            self.applied = 0
        for layer in image_layers[self.applied:]:
            self.layers.extract(layer.digest, self.path)
        self.applied = len(image_layers)
        return self.path

    def discard(self) -> None:
        if self.path is not None:
            rmtree(self.path)
        self.path = None
        self.applied = 0


class Builder:
    def __init__(self, paths: StatePaths, images: ImageStore, layers: LayerStore, cache: BuildCache):
        self.paths = paths
        self.images = images
        self.layers = layers
        self.cache = cache

    @classmethod
    def from_paths(cls, paths: StatePaths) -> "Builder":
        layers = LayerStore(paths.layers)
        return cls(paths, ImageStore(paths.images, layers), layers, BuildCache(paths.cache))

    def build(
        self,
        instructions: list[Instruction],
        ref: str,
        context: str,
        *,
        no_cache: bool = False,
        on_step: ProgressFn | None = None,
    ) -> Image:
        """Run every instruction; returns the (unsaved) image. Only COPY and RUN create layers."""
        if not instructions:
            raise BuildError("no instructions provided")
        name, tag = parse_ref(ref)
        ctx = os.path.abspath(context)
        if not os.path.isdir(ctx):
            raise BuildError(f"build context {context!r} is not a directory")
        if instructions[0].op != "FROM":
            raise BuildError(f"line {instructions[0].line}: the first instruction must be FROM")

        rootfs = _Rootfs(str(self.paths.tmp), self.layers)
        snap = Snapshotter()
        layers: list[ImageLayer] = []
        env: dict[str, str] = {}
        cmd: list[str] = []
        workdir = ""  # "" means never set (cache key uses the empty string)
        prev_digest = ""
        cascade = False
        total = len(instructions)

        try:
            for idx, inst in enumerate(instructions, start=1):
                started = time.perf_counter()
                status = ""
                op = inst.op

                if op == "FROM":
                    rootfs.discard()
                    layers, env, cmd, workdir, prev_digest, cascade = [], {}, [], "", "", False
                    source = inst.args[0]
                    if source.lower() != "scratch":
                        try:
                            base = self.images.load(source)
                        except DocksmithError as exc:
                            raise BuildError(
                                f"line {inst.line}: FROM {source}: {exc}. Import a base image first "
                                f"(docksmith base {format_ref(*parse_ref(source))} or docksmith import <rootfs.tar> <name:tag>)"
                            ) from None
                        for layer in base.layers:
                            if not self.layers.exists(layer.digest):
                                raise BuildError(
                                    f"line {inst.line}: base image {base.ref} is broken: layer {layer.digest} is missing"
                                )
                        layers = list(base.layers)
                        env = dict(base.config.env)
                        cmd = list(base.config.cmd)
                        workdir = base.config.working_dir or ""
                        prev_digest = base.digest

                elif op == "WORKDIR":
                    workdir = resolve_container_path(workdir or "/", inst.args[0])

                elif op == "ENV":
                    for token in inst.args:
                        key, _, value = token.partition("=")
                        env[key.strip()] = value

                elif op == "CMD":
                    cmd = list(inst.args)

                elif op in ("COPY", "RUN"):
                    copy_hash = hash_sources(ctx, inst.args[:-1]) if op == "COPY" else None
                    key = cache_key(prev_digest, inst.raw, workdir, env, copy_hash)

                    digest = None
                    if not no_cache and not cascade:
                        cached = self.cache.get(key)
                        if cached and self.layers.exists(cached):
                            digest = cached
                    if digest is not None:
                        status = "CACHE HIT"
                        size = self.layers.size(digest)
                    else:
                        status = "CACHE MISS"
                        cascade = True
                        digest, size = self._execute(inst, rootfs, layers, snap, ctx, workdir, env)
                        if not no_cache:
                            self.cache.put(key, digest)

                    layers.append(ImageLayer(digest=digest, size=size, created_by=str(inst)))
                    prev_digest = digest
                    if status == "CACHE MISS":
                        rootfs.applied = len(layers)  # tree already contains this step's changes
                else:  # pragma: no cover - parser rejects unknown instructions
                    raise BuildError(f"line {inst.line}: unsupported instruction {op}")

                if on_step:
                    on_step(BuildStep(idx, total, str(inst), op, status, time.perf_counter() - started))
        finally:
            rootfs.discard()

        return Image(
            name=name,
            tag=tag,
            config=ImageConfig(cmd=cmd, env=env, working_dir=workdir),
            layers=layers,
        )

    def _execute(self, inst: Instruction, rootfs: _Rootfs, layers: list[ImageLayer], snap: Snapshotter,
                 ctx: str, workdir: str, env: dict[str, str]) -> tuple[str, int]:
        if inst.op == "RUN":
            check_supported()
        root = rootfs.materialise(layers)
        if workdir:
            # Silent WORKDIR creation: happens before the snapshot, so it is not part of the delta.
            ensure_dir_in_root(root, workdir)
        if inst.op == "RUN":
            ensure_dev_nodes(root)
            if not os.path.lexists(resolve_in_root(root, "/bin/sh", follow_last=True)):
                raise BuildError(
                    f"line {inst.line}: RUN needs /bin/sh inside the image, but the base image has none"
                )
        before = snap.snapshot(root)

        if inst.op == "COPY":
            try:
                apply_copy(root, ctx, inst.args, workdir or "/")
            except OSError as exc:
                raise BuildError(f"line {inst.line}: COPY failed: {exc}") from None
        else:
            code = run_isolated(root, ["/bin/sh", "-c", inst.text], env, workdir or "/")
            if code != 0:
                raise BuildError(f"line {inst.line}: RUN {inst.text!r} exited with code {code}")

        after = snap.snapshot(root)
        changed, deleted = diff_snapshots(before, after)
        return self.layers.put(build_layer_tar(root, changed, deleted))
