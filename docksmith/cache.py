"""Deterministic build cache: cache key computation and the key -> layer index."""

from __future__ import annotations

import hashlib
import os


def encode_env(env: dict[str, str]) -> str:
    """``KEY=value`` pairs in lexicographically sorted key order, newline separated."""
    return "\n".join(f"{k}={env[k]}" for k in sorted(env))


def cache_key(
    prev_digest: str,
    raw_instruction: str,
    workdir: str,
    env: dict[str, str],
    copy_sources_hash: str | None = None,
) -> str:
    """Hash of every input that can influence a COPY/RUN layer.

    * ``prev_digest`` - digest of the previous layer, or the base image's manifest
      digest for the first layer-producing step (so changing FROM invalidates all).
    * ``raw_instruction`` - the instruction exactly as written.
    * ``workdir`` - current WORKDIR ("" when never set).
    * ``env`` - accumulated ENV, serialised in sorted key order ("" when empty).
    * ``copy_sources_hash`` - COPY only: hash over every source file's bytes.
    """
    h = hashlib.sha256()
    for part in (prev_digest or "", raw_instruction, workdir or "", encode_env(env or {}),
                 copy_sources_hash or ""):
        h.update(part.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
    return h.hexdigest()


class BuildCache:
    """``cache/`` directory: one ``<key>.digest`` file per entry holding a layer digest."""

    def __init__(self, cache_dir: str | os.PathLike):
        self.dir = os.fspath(cache_dir)

    def _path(self, key: str) -> str:
        key = key.strip()
        if not key or any(c not in "0123456789abcdef" for c in key):
            raise ValueError(f"invalid cache key {key!r}")
        return os.path.join(self.dir, f"{key}.digest")

    def get(self, key: str) -> str | None:
        try:
            with open(self._path(key), encoding="utf-8") as f:
                value = f.read().strip()
        except (FileNotFoundError, ValueError):
            return None
        return value or None

    def put(self, key: str, layer_digest: str) -> None:
        os.makedirs(self.dir, exist_ok=True)
        path = self._path(key)
        tmp = f"{path}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(layer_digest.strip())
        os.replace(tmp, path)
