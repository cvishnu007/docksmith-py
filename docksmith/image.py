"""Image manifests and the local image store (``images/``).

Manifest digest (spec 4.1): serialise the manifest with ``"digest": ""``, take
the SHA-256 of those bytes, then write the file with ``"digest": "sha256:<hex>"``.
``created`` is part of the hashed form, so it is set once at first build and
preserved whenever an identical image is rebuilt - keeping the digest stable.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os

from .layer import LayerStore
from .models import DocksmithError, Image, ImageConfig, ImageLayer, ImageNotFound
from .refs import format_ref, parse_ref

DIGEST_PREFIX = "sha256:"


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def env_to_list(env: dict[str, str]) -> list[str]:
    return [f"{k}={env[k]}" for k in sorted(env)]


def env_from_list(items: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in items or []:
        k, sep, v = item.partition("=")
        if sep and k:
            env[k] = v
    return env


def manifest_dict(img: Image, digest: str) -> dict:
    return {
        "name": img.name,
        "tag": img.tag,
        "digest": digest,
        "created": img.created,
        "config": {
            "Env": env_to_list(img.config.env),
            "Cmd": list(img.config.cmd or []),
            "WorkingDir": img.config.working_dir or "",
        },
        "layers": [
            {"digest": l.digest, "size": l.size, "createdBy": l.created_by} for l in img.layers
        ],
    }


def serialize_manifest(m: dict) -> bytes:
    return json.dumps(m, indent=2, ensure_ascii=False).encode("utf-8")


def compute_manifest_digest(img: Image) -> str:
    raw = serialize_manifest(manifest_dict(img, ""))
    return DIGEST_PREFIX + hashlib.sha256(raw).hexdigest()


def image_from_manifest(data: dict) -> Image:
    cfg = data.get("config") or {}
    return Image(
        name=data.get("name", ""),
        tag=data.get("tag", ""),
        digest=data.get("digest", ""),
        created=data.get("created", ""),
        config=ImageConfig(
            cmd=list(cfg.get("Cmd") or []),
            env=env_from_list(cfg.get("Env") or []),
            working_dir=cfg.get("WorkingDir") or "",
        ),
        layers=[
            ImageLayer(digest=l.get("digest", ""), size=int(l.get("size", 0)), created_by=l.get("createdBy", ""))
            for l in data.get("layers") or []
        ],
    )


def _same_content(a: Image, b: Image) -> bool:
    return manifest_dict(a, "") | {"created": ""} == manifest_dict(b, "") | {"created": ""}


class ImageStore:
    def __init__(self, images_dir: str | os.PathLike, layers: LayerStore):
        self.dir = os.fspath(images_dir)
        self.layers = layers

    def _path(self, name: str, tag: str) -> str:
        safe = name.replace("/", "_") + "__" + tag.replace("/", "_") + ".json"
        return os.path.join(self.dir, safe)

    def save(self, img: Image) -> Image:
        """Write ``img``'s manifest; returns it with ``digest`` and ``created`` filled in."""
        if not img.name.strip() or not img.tag.strip():
            raise DocksmithError("image: name and tag are required")
        os.makedirs(self.dir, exist_ok=True)
        path = self._path(img.name, img.tag)

        created = img.created
        try:
            existing = self._read(path)
        except (FileNotFoundError, DocksmithError):
            existing = None
        if existing is not None and existing.created and _same_content(existing, img):
            created = existing.created  # identical rebuild: keep timestamp -> same digest
        if not created:
            created = now_iso()

        out = Image(name=img.name, tag=img.tag, created=created, config=img.config, layers=list(img.layers))
        out.digest = compute_manifest_digest(out)
        tmp = f"{path}.tmp-{os.getpid()}"
        with open(tmp, "wb") as f:
            f.write(serialize_manifest(manifest_dict(out, out.digest)))
        os.replace(tmp, path)
        return out

    def _read(self, path: str) -> Image:
        with open(path, "rb") as f:
            raw = f.read()
        try:
            return image_from_manifest(json.loads(raw))
        except (ValueError, AttributeError, TypeError) as exc:
            raise DocksmithError(f"image: corrupt manifest {path}: {exc}") from None

    def exists(self, ref: str) -> bool:
        name, tag = parse_ref(ref)
        return os.path.isfile(self._path(name, tag))

    def load(self, ref: str) -> Image:
        name, tag = parse_ref(ref)
        try:
            return self._read(self._path(name, tag))
        except FileNotFoundError:
            raise ImageNotFound(f"image {format_ref(name, tag)} not found in local store") from None

    def list(self) -> list[Image]:
        try:
            names = sorted(os.listdir(self.dir))
        except FileNotFoundError:
            return []
        out: list[Image] = []
        for n in names:
            if not n.endswith(".json"):
                continue
            try:
                out.append(self._read(os.path.join(self.dir, n)))
            except (OSError, DocksmithError):
                continue
        out.sort(key=lambda i: (i.name, i.tag))
        return out

    def remove(self, ref: str) -> Image:
        """Delete the manifest and every layer file it references (no ref-counting)."""
        img = self.load(ref)
        for layer in img.layers:
            self.layers.remove(layer.digest)
        os.unlink(self._path(img.name, img.tag))
        return img

    @staticmethod
    def verify_digest(path: str) -> bool:
        with open(path, "rb") as f:
            data = json.loads(f.read())
        claimed = data.get("digest", "")
        data["digest"] = ""
        return claimed == DIGEST_PREFIX + hashlib.sha256(serialize_manifest(data)).hexdigest()
