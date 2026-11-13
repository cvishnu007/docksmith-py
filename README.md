# Docksmith (Python)

A simplified Docker-like **image builder + container runtime**, written in pure
Python (standard library only). This is a complete Python rewrite of the Go
project [`docksmith-engine`](https://github.com/cvishnu007/docksmith-engine), with the gaps
against the Docksmith spec filled in (see [What changed vs. the Go version](#what-changed-vs-the-go-version)).

> **New here?** [GUIDE.md](GUIDE.md) explains the whole project from scratch: concepts,
> every module and function, how they connect, and every command.

Docksmith reads a `Docksmithfile`, builds content-addressed delta layers with a
deterministic build cache, and runs containers in an isolated root filesystem
using Linux namespaces + `pivot_root` (falling back to `chroot`). No daemon, no
Docker/runc. All state lives in `~/.docksmith/`.

```text
CLI ─► Parser ─► Builder ─┬─► Cache  (cache/  key → layer digest)
                          ├─► Layers (layers/ sha256_<hex>.tar, immutable deltas)
                          ├─► Images (images/ <name>__<tag>.json manifests)
                          └─► Isolation ◄── Runtime (`docksmith run`)
                              (one primitive, used by RUN and by run)
```

## Requirements

- **Linux** (or a Linux VM). `RUN` steps and `docksmith run` need **root** (`sudo`).
  Parsing, `COPY`-only builds, `images` and `rmi` work unprivileged.
- **Python 3.12+**. No third-party packages.
- Fully **offline** after the one-time base image import.

Run it straight from the checkout (`python3 -m docksmith …`), or install it into a virtualenv:

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install .
```

```bash
sudo .venv/bin/docksmith --help
```

Use `sudo` for both build and run so everything lives in root's `~/.docksmith/`.
Set `DOCKSMITH_HOME=/some/dir` to put the state somewhere else.

## Demo

### 0) One-time base image import

Option A — build a minimal base from the host's own userland (sh, coreutils, grep,
sed, awk, … plus their shared libraries). Needs no network at all:

```bash
sudo python3 -m docksmith base base:latest
```

Option B — import a real distro rootfs downloaded once (Alpine minirootfs):

```bash
sudo sh scripts/fetch-alpine.sh alpine:3.20
```

Any rootfs tarball works with `docksmith import <rootfs.tar[.gz|.xz]> <name:tag>`.

### 1) Cold build: every COPY/RUN is a `[CACHE MISS]`

```bash
sudo python3 -m docksmith build -t sample:latest sample-app
```

```text
Step 1/6 : FROM base:latest
Step 2/6 : WORKDIR /app
Step 3/6 : COPY . /app [CACHE MISS] 0.05s
Step 4/6 : ENV MESSAGE=Hello
Step 5/6 : RUN echo "built at build time" > /app/build.txt && chmod +x /app/app/main.sh [CACHE MISS] 0.01s
Step 6/6 : CMD ["sh", "/app/app/main.sh"]
Successfully built sha256:71e9f0b186c7 sample:latest (0.07s)
```

### 2) Warm build: `[CACHE HIT]`, identical digest

Run the same build command again. Both steps report `[CACHE HIT]` and the manifest digest is the same.

### 3) Edit a source file, then rebuild

The changed `COPY` and every step after it report `[CACHE MISS]`.

```bash
echo change >> sample-app/app/data.txt
```

```bash
sudo python3 -m docksmith build -t sample:latest sample-app
```

### 4) List images

```bash
sudo python3 -m docksmith images
```

```text
NAME     TAG      ID             CREATED
base     latest   f412b804742e   2026-11-13T21:21:36Z
sample   latest   64f5c9a0d0b9   2026-11-13T21:21:36Z
```

### 5) Run the image

```bash
sudo python3 -m docksmith run sample:latest
```

```text
[run] image=sample:latest cmd=sh /app/app/main.sh
Hello from Docksmith sample app!
Working directory: /app
MESSAGE=Hello
Hostname: docksmith
Build artifact: built at build time
Exit code: 0
```

### 6) Override ENV

```bash
sudo python3 -m docksmith run -e MESSAGE="Hi Professor" sample:latest
```

### 7) Isolation proof: writes inside the container never reach the host

```bash
sudo python3 -m docksmith run sample:latest sh -c 'echo hacked > /test.txt; ls /'
```

```bash
ls /test.txt
```

The second command prints `ls: cannot access '/test.txt': No such file or directory`.

### 8) Remove the image and its layer files

```bash
sudo python3 -m docksmith rmi sample:latest
```

Add `--no-cache` to `build` to skip every cache lookup and write. Layers are still written.

## CLI reference

| Command | Behaviour |
|---|---|
| `docksmith build -t name[:tag] [-f file] [--no-cache] [context]` | Build from `<context>/Docksmithfile`. Prints each step; COPY/RUN show `[CACHE HIT\|MISS]` and duration. Ends with the digest and total time. |
| `docksmith run [-e KEY=VALUE]... name[:tag] [cmd ...]` | Assemble the rootfs, run in the foreground, print and return the exit code. `cmd` overrides `CMD`; `-e` overrides image ENV (repeatable). |
| `docksmith images` | Name, Tag, ID (first 12 hex of the digest), Created. |
| `docksmith rmi name[:tag]...` | Delete the manifest and all its layer files (no ref-counting, per spec). |
| `docksmith base [name[:tag]]` | Create a base image from the host userland (offline). Defaults to `base:latest`. |
| `docksmith import <tarball> name[:tag]` | Import a rootfs tarball as a single-layer base image. |

Tags default to `latest` everywhere (`FROM base` means `FROM base:latest`).

## Docksmithfile

| Instruction | Behaviour |
|---|---|
| `FROM <image>[:<tag>]` / `FROM scratch` | Start from a local image's layers and config. Must be the first instruction. Unknown images fail with a clear error. |
| `COPY <src>... <dest>` | Copy from the build context. Supports `*`, `?`, `[...]` and `**` globs. Missing directories are created. Produces a layer. |
| `RUN <command>` | Runs `/bin/sh -c "<command>"` **inside** the image filesystem, isolated with the same primitive as `docksmith run`. Produces a layer. |
| `WORKDIR <path>` | Sets the working directory (relative paths resolve against the current one). It is created silently before the next COPY/RUN and is not stored in a layer. |
| `ENV KEY=value [KEY2=value2 ...]` | Adds to the image env and to the env of later RUN steps. Quoted values may contain spaces. |
| `CMD ["exec", "arg"]` | Default command. JSON array form only. |

Also supported: `#` comments, blank lines, CRLF endings and `\` line continuations.
Any other instruction fails with `line N: unknown instruction X`.

**COPY semantics.** These match Docker:

- A directory source copies its *contents*, so `COPY . /app` puts the files directly in `/app`.
- A single file goes to `<dest>` exactly, unless `<dest>` ends in `/`, is `.`, or is an existing directory.
- Glob matches go into `<dest>` and keep their path relative to the pattern's fixed prefix. `COPY src/**/*.py /app/` keeps the sub-directories under `src`.
- `Docksmithfile`, `.docksmith/`, OS junk files and the patterns in a `.docksmithignore` file are never copied.

## How it works

**Layers.** Every COPY/RUN snapshots the working rootfs before and after the step. The
layer is a tar of only the added or modified paths, plus parent directories. Paths the
step deleted become OCI-style whiteout entries (`.wh.<name>`). A tar's digest is the
SHA-256 of its bytes, and it is stored once as `layers/sha256_<hex>.tar`. Layers are
reproducible byte for byte: entries are sorted, mtimes are 0, owners are root, and the
format is PAX. Extraction is confined to the root: `..` entries are rejected, and
symlinks are resolved *inside* the rootfs, so a layer can't write onto the host.

**Cache key.** The key is the SHA-256 of these parts, joined:

- the previous layer digest (or, for the first layer step, the base image's manifest digest);
- the raw instruction text;
- the current WORKDIR (`""` if unset);
- the ENV as `KEY=value` lines in sorted key order;
- for COPY only, a hash of every source file's path, mode and SHA-256.

A hit needs the key in `cache/` **and** the layer file on disk. After the first miss,
every later step is a miss too. `--no-cache` skips both lookups and writes. Cache hits
don't touch the filesystem: the working rootfs is assembled lazily, only when a step
actually has to run.

**Manifest.** The JSON is serialised with `"digest": ""`. Its SHA-256 becomes the
`digest`, and the file is then written with that value. `created` is set at the first
build and kept on any rebuild with identical content, so the digest stays the same
across rebuilds.

**Isolation** (`docksmith/isolation.py`). The same function runs `RUN` and `docksmith run`.
Between `fork` and `exec`, the child:

1. calls `unshare(CLONE_NEWNS|CLONE_NEWUTS|CLONE_NEWIPC)`;
2. makes all mounts private;
3. bind-mounts the rootfs onto itself;
4. calls `pivot_root(".", ".")` and detaches the old root, so the host filesystem is
   unreachable rather than merely hidden as it is with plain `chroot`;
5. sets the hostname to `docksmith`, then `chdir`s to `WorkingDir` and execs the command
   with only the image env plus `-e` overrides.

If namespaces aren't allowed (some sandboxes), it falls back to `chroot(2)`.
`DOCKSMITH_ISOLATION=chroot` forces the fallback. A minimal `/dev` (`null`, `zero`,
`random`, `urandom`) is created in the rootfs; `/dev`, `/proc` and `/sys` never go
into layers. Container rootfs directories live in `~/.docksmith/tmp/` and are always
deleted afterwards.

## Tests

```bash
python3 -m pytest -q
```

```bash
sudo python3 -m pytest -q
```

The first command runs the unit tests unprivileged. The second also runs the root-only
end-to-end isolation tests: RUN inside the image, whiteouts, env/workdir/exit codes,
host-write isolation, rootfs import, and the full CLI demo flow. CI
(`.github/workflows/tests.yml`) runs both.

## Project layout

```text
docksmith/
  cli.py         argparse CLI (build, run, images, rmi, base, import)
  parser.py      Docksmithfile parser
  builder.py     build engine: cache keys, cascade, lazy rootfs, step execution
  copyops.py     COPY source resolution (globs, .docksmithignore), hashing, copying
  cache.py       cache key + cache index
  layer.py       deterministic delta tars, whiteouts, safe extraction, layer store
  image.py       manifest format/digest + image store
  isolation.py   namespaces + pivot_root / chroot process isolation
  runtime.py     `docksmith run`
  baseimage.py   `base` (host userland) and `import` (rootfs tarball)
  fsutil.py      root-confined path resolution, snapshots
sample-app/      sample app using all six instructions
scripts/         one-time Alpine download + import
tests/           pytest suite
```

## What changed vs. the Go version

The Go source maps to Python module by module: `cmd/` → `cli.py`, `internal/parser` →
`parser.py`, `internal/builder` → `builder.py`, `internal/cache` → `cache.py` +
`copyops.py`, `internal/layer` → `layer.py` + `copyops.py`, `internal/image` → `image.py`,
`internal/runtime` → `runtime.py`, `internal/isolation` → `isolation.py`, and
`internal/provision` → `baseimage.py`. Every Go test has a Python equivalent.

These spec gaps and bugs from the Go version are fixed:

| Area | Go version | Python version |
|---|---|---|
| COPY globs (`*`, `**`) | not supported | supported, in both the copy and the cache key |
| `FROM base` (no tag) | failed (`invalid name:tag`) | defaults to `:latest` |
| `RUN` | args were exec'd directly, so `>`, `&&` and `\|` didn't work | runs through `/bin/sh -c`, as the spec says |
| RUN layer contents | host binaries were copied in *after* the snapshot, so every RUN layer held ~20 MB of `/usr/bin` + libc | the base userland is a real base-image layer; RUN layers hold only true deltas |
| Base image | `docksmith base` wrote an empty manifest; every build/run copied host binaries in | `base` creates a real, deterministic base layer; `import` loads any rootfs tarball (e.g. Alpine) |
| WORKDIR creation | ended up inside the next layer | created silently before the snapshot (spec 3) |
| Deleted files | reappeared at run time (no whiteouts) | whiteouts make deletions persist |
| Symlinks | rejected on extract; COPY followed them | preserved; resolved safely inside the rootfs |
| `images` | `NAME TAG ID`, full digest, no Created | `NAME TAG ID CREATED` with the 12-char ID (spec 7) |
| Build summary | no total time | `Successfully built sha256:… name:tag (N.NNs)` |
| Manifest digest | `created` excluded from the hash | hashed exactly as the spec describes; `created` preserved so rebuilds give identical digests |
| Isolation | bare `chroot` (escapable by root) | mount/UTS/IPC namespaces + `pivot_root`, with `chroot` fallback |
| `run` exit code | non-zero exit was turned into an error | prints `Exit code: N` and exits with N |
| Extras | — | `.docksmithignore`, `\` continuations, `-f`, `rmi` with several images, `/dev` nodes, clear non-root errors |

The unused mock modules, the prebuilt `demo/app` binary and the stray root `Docksmithfile` were not carried over.
