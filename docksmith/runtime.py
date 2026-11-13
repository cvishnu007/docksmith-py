"""Container runtime: assemble an image's rootfs and run a process inside it."""

from __future__ import annotations

from .fsutil import ensure_dev_nodes, ensure_dir_in_root, make_tempdir, rmtree
from .isolation import check_supported, run_isolated
from .layer import LayerStore
from .models import DocksmithError, Image
from .paths import StatePaths


def assemble_rootfs(image: Image, layers: LayerStore, rootfs: str) -> None:
    """Extract every layer of ``image`` in order (later layers win) into ``rootfs``."""
    for layer in image.layers:
        layers.extract(layer.digest, rootfs)


class Runtime:
    def __init__(self, paths: StatePaths, layers: LayerStore):
        self.paths = paths
        self.layers = layers

    def run(self, image: Image, cmd: list[str] | None = None, env_overrides: dict[str, str] | None = None) -> int:
        command = list(cmd) if cmd else list(image.config.cmd)
        if not command:
            raise DocksmithError(
                f"no command specified: image {image.ref} has no CMD and no command was given"
            )
        env = dict(image.config.env)
        env.update(env_overrides or {})
        workdir = image.config.working_dir or "/"

        check_supported()
        rootfs = make_tempdir(self.paths.tmp, "rootfs-")
        try:
            assemble_rootfs(image, self.layers, rootfs)
            ensure_dev_nodes(rootfs)
            ensure_dir_in_root(rootfs, workdir)
            return run_isolated(rootfs, command, env, workdir)
        finally:
            rmtree(rootfs)
