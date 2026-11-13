"""``docksmith`` command-line interface."""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import __version__
from .baseimage import create_host_base, import_rootfs
from .builder import Builder
from .layer import LayerStore
from .image import ImageStore
from .models import BuildStep, DocksmithError
from .parser import parse_file
from .paths import StatePaths
from .refs import format_ref, parse_ref
from .runtime import Runtime


def _short(digest: str) -> str:
    return "sha256:" + digest.split(":", 1)[-1][:12] if digest else "sha256:<unknown>"


def print_step(step: BuildStep) -> None:
    line = f"Step {step.index}/{step.total} : {step.instruction}"
    if step.op in ("COPY", "RUN") and step.cache_status:
        line += f" [{step.cache_status}] {step.duration:.2f}s"
    print(line, flush=True)


def cmd_build(args, paths: StatePaths) -> int:
    name, tag = parse_ref(args.tag)
    context = args.context
    if not os.path.isdir(context):
        raise DocksmithError(f"build context {context!r} is not a directory")
    dockerfile = args.file or os.path.join(context, "Docksmithfile")
    instructions = parse_file(dockerfile)

    start = time.perf_counter()
    paths.ensure()
    builder = Builder.from_paths(paths)
    image = builder.build(instructions, format_ref(name, tag), context, no_cache=args.no_cache, on_step=print_step)
    saved = builder.images.save(image)
    print(f"Successfully built {_short(saved.digest)} {saved.ref} ({time.perf_counter() - start:.2f}s)")
    return 0


def cmd_run(args, paths: StatePaths) -> int:
    env: dict[str, str] = {}
    for item in args.env or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise DocksmithError(f"-e expects KEY=VALUE, got {item!r}")
        env[key.strip()] = value
    layers = LayerStore(paths.layers)
    image = ImageStore(paths.images, layers).load(args.image)
    command = list(args.cmd or [])
    if command and command[0] == "--":
        command = command[1:]
    effective = command or image.config.cmd
    if not effective:
        raise DocksmithError(f"no command specified: image {image.ref} has no CMD and no command was given")
    print(f"[run] image={image.ref} cmd={' '.join(effective)}", flush=True)
    code = Runtime(paths, layers).run(image, command, env)
    print(f"Exit code: {code}")
    return code


def cmd_images(args, paths: StatePaths) -> int:
    images = ImageStore(paths.images, LayerStore(paths.layers)).list()
    rows = [("NAME", "TAG", "ID", "CREATED")]
    rows += [(i.name, i.tag, i.short_id, i.created or "-") for i in images]
    widths = [max(len(r[c]) for r in rows) for c in range(3)]
    for r in rows:
        print("   ".join(r[c].ljust(widths[c]) for c in range(3)) + "   " + r[3])
    if not images:
        print("(no images - create one with 'docksmith base' or 'docksmith import')")
    return 0


def cmd_rmi(args, paths: StatePaths) -> int:
    store = ImageStore(paths.images, LayerStore(paths.layers))
    status = 0
    for ref in args.images:
        try:
            img = store.remove(ref)
        except DocksmithError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            status = 1
            continue
        print(f"Removed image {img.ref} ({_short(img.digest)}, {len(img.layers)} layer file(s) deleted)")
    return status


def cmd_base(args, paths: StatePaths) -> int:
    img, count = create_host_base(paths, args.ref)
    print(f"Imported base image {img.ref} ({_short(img.digest)}, {count} host binaries)")
    return 0


def cmd_import(args, paths: StatePaths) -> int:
    img = import_rootfs(paths, args.tarball, args.ref)
    print(f"Imported base image {img.ref} from {args.tarball} ({_short(img.digest)})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="docksmith",
        description="A simplified Docker-like image builder and container runtime.",
    )
    p.add_argument("--version", action="version", version=f"docksmith {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    b = sub.add_parser("build", help="build an image from a Docksmithfile")
    b.add_argument("-t", "--tag", required=True, metavar="NAME[:TAG]", help="image name and tag")
    b.add_argument("-f", "--file", help="path to the Docksmithfile (default: <context>/Docksmithfile)")
    b.add_argument("--no-cache", action="store_true", help="skip all cache lookups and writes")
    b.add_argument("context", nargs="?", default=".", help="build context directory (default: .)")
    b.set_defaults(func=cmd_build)

    r = sub.add_parser("run", help="run a container from an image")
    r.add_argument("-e", "--env", action="append", metavar="KEY=VALUE", help="set/override env (repeatable)")
    r.add_argument("image", metavar="NAME[:TAG]")
    r.add_argument("cmd", nargs=argparse.REMAINDER, help="command overriding the image CMD")
    r.set_defaults(func=cmd_run)

    i = sub.add_parser("images", help="list images")
    i.set_defaults(func=cmd_images)

    rm = sub.add_parser("rmi", help="remove image(s) and their layer files")
    rm.add_argument("images", nargs="+", metavar="NAME[:TAG]")
    rm.set_defaults(func=cmd_rmi)

    bs = sub.add_parser("base", help="create a base image from the host's userland (offline)")
    bs.add_argument("ref", nargs="?", default="base:latest", metavar="NAME[:TAG]")
    bs.set_defaults(func=cmd_base)

    im = sub.add_parser("import", help="import a root filesystem tarball as a base image")
    im.add_argument("tarball", help="rootfs tar / tar.gz / tar.xz (e.g. alpine-minirootfs)")
    im.add_argument("ref", metavar="NAME[:TAG]")
    im.set_defaults(func=cmd_import)
    return p


def main(argv: list[str] | None = None) -> int:
    os.umask(0o022)  # deterministic modes for directories created during builds
    args = build_parser().parse_args(argv)
    try:
        return args.func(args, StatePaths.default())
    except DocksmithError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
