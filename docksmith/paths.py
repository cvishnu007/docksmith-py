"""Location of Docksmith's on-disk state (``~/.docksmith`` by default).

Layout::

    ~/.docksmith/
      images/   one JSON manifest per image
      layers/   content-addressed tar files named by digest
      cache/    index mapping cache keys to layer digests
      tmp/      scratch root filesystems (build + run), always cleaned up
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def default_root() -> Path:
    override = os.environ.get("DOCKSMITH_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".docksmith"


@dataclass(frozen=True)
class StatePaths:
    root: Path

    @classmethod
    def default(cls) -> "StatePaths":
        return cls(default_root())

    @property
    def images(self) -> Path:
        return self.root / "images"

    @property
    def layers(self) -> Path:
        return self.root / "layers"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    def ensure(self) -> "StatePaths":
        for d in (self.images, self.layers, self.cache, self.tmp):
            d.mkdir(parents=True, exist_ok=True)
        return self
