"""Core data types shared by every Docksmith subsystem."""

from __future__ import annotations

from dataclasses import dataclass, field


class DocksmithError(Exception):
    """Base class for all user-facing Docksmith errors."""


class ParseError(DocksmithError):
    """A Docksmithfile could not be parsed."""


class BuildError(DocksmithError):
    """A build step failed."""


class ImageNotFound(DocksmithError):
    """The requested image does not exist in the local store."""


class IsolationError(DocksmithError):
    """The isolated process could not be started."""


@dataclass
class Instruction:
    """One parsed Docksmithfile instruction.

    ``raw`` is the instruction exactly as written (used for cache keys),
    ``text`` is everything after the opcode (used as the RUN shell command),
    ``args`` is the tokenised / decoded argument list.
    """

    op: str
    args: list[str]
    raw: str = ""
    text: str = ""
    line: int = 0

    def __str__(self) -> str:
        if self.raw.strip():
            return self.raw.strip()
        if not self.op:
            return ""
        return f"{self.op} {' '.join(self.args)}".strip()


@dataclass
class ImageConfig:
    cmd: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    working_dir: str = ""


@dataclass
class ImageLayer:
    digest: str
    size: int
    created_by: str


@dataclass
class Image:
    name: str
    tag: str
    digest: str = ""
    created: str = ""
    config: ImageConfig = field(default_factory=ImageConfig)
    layers: list[ImageLayer] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.name}:{self.tag}"

    @property
    def short_id(self) -> str:
        """First 12 hex characters of the manifest digest (``docksmith images`` ID)."""
        return self.digest.split(":", 1)[-1][:12] if self.digest else "<none>"


@dataclass
class BuildStep:
    """Progress record emitted after each build instruction."""

    index: int
    total: int
    instruction: str
    op: str
    cache_status: str = ""  # "CACHE HIT" / "CACHE MISS" for COPY and RUN only
    duration: float = 0.0
