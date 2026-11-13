# Docksmith — The Complete Guide

This guide explains Docksmith **from scratch**: the ideas behind it, how every piece
of the code works, how the pieces connect, and every command you need to run it.
If you only want the quick demo, see [README.md](README.md).

## Contents

1. [What Docksmith is](#1-what-docksmith-is)
2. [Core concepts](#2-core-concepts)
3. [Installing and running](#3-installing-and-running)
4. [Command reference](#4-command-reference)
5. [Writing a Docksmithfile](#5-writing-a-docksmithfile)
6. [On-disk state](#6-on-disk-state)
7. [Architecture: how everything connects](#7-architecture-how-everything-connects)
8. [A full walkthrough of one build and one run](#8-a-full-walkthrough-of-one-build-and-one-run)
9. [Module and function reference](#9-module-and-function-reference)
10. [Tests](#10-tests)
11. [Troubleshooting](#11-troubleshooting)
12. [Glossary](#12-glossary)

---

## 1. What Docksmith is

Docksmith is a small Docker-like system with three parts:

1. **A build system.** It reads a `Docksmithfile` (like a `Dockerfile`) and turns it
   into an **image**: a stack of filesystem layers plus some configuration
   (environment variables, working directory, default command).
2. **A build cache.** Rebuilding an unchanged project reuses earlier work instead of
   redoing it. A cache key decides whether a step's earlier result can be reused.
3. **A container runtime.** It unpacks an image into a temporary directory and runs a
   process inside it. The process sees that directory as `/` and cannot reach the
   host's files.

It is a single command-line program: no background daemon, no Docker, no `runc`. It
uses only Linux kernel features (namespaces, `pivot_root`, `chroot`) and the Python
standard library. All of its state is in one folder, `~/.docksmith/`.

Out of scope (by design): networking, registries (`push`/`pull`), resource limits,
multi-stage `COPY --from`, volumes, detached containers.

---

## 2. Core concepts

### Image

An image is a recipe for a root filesystem plus a config. On disk it is a JSON
**manifest** that lists:

- `layers`: the layer tars to unpack, bottom to top;
- `config.Env`: environment variables for every container;
- `config.Cmd`: the default command;
- `config.WorkingDir`: the directory the command starts in.

### Layer

A layer is a `.tar` file containing **only what one build step changed** (a *delta*),
not a full copy of the filesystem. Stacking the layers in order rebuilds the full
filesystem; later layers overwrite earlier ones at the same path.

If a step **deletes** a file, the layer records that with a *whiteout*: an empty
entry named `.wh.<filename>`. When it is unpacked, the file is deleted.

Only `COPY` and `RUN` make layers. `WORKDIR`, `ENV` and `CMD` only change the config.
`FROM` reuses another image's layers.

### Content addressing

A layer's name is the SHA-256 hash of its bytes: `sha256:<64 hex chars>`, stored as
`layers/sha256_<hex>.tar`. Identical content always gives the identical name, so
the same layer is stored only once, and a name always means exactly one content.
The image manifest has a digest too (the hash of the manifest itself).

### Determinism

For the cache to work, building the same inputs twice must produce **byte-for-byte
identical** tars. Docksmith guarantees this by:

- adding tar entries in sorted order;
- setting every timestamp to 0 (1970-01-01);
- setting every owner to root (uid/gid 0, no user/group names);
- using a fixed tar format (PAX).

### Build cache

Before each `COPY` or `RUN`, Docksmith computes a **cache key**, a hash of everything
that could affect that step's result:

| Input | Why it matters |
|---|---|
| Previous layer's digest (or the base image's manifest digest for the first layer step) | A different starting filesystem means a different result. This also makes a changed `FROM` image invalidate everything. |
| The instruction text, exactly as written | `RUN echo a` ≠ `RUN echo b`. |
| Current `WORKDIR` (`""` if never set) | Relative paths resolve differently. |
| Current `ENV`, sorted `KEY=value` lines | Commands can read env vars. |
| `COPY` only: a hash of every source file (path, mode, content) | Editing a source file must invalidate the step. |

If the key is in the cache index **and** the layer file still exists, the step is a
**`[CACHE HIT]`**: the stored layer is reused and nothing runs. Otherwise it is a
**`[CACHE MISS]`**: the step runs, the new layer is stored, and the index is updated.

**Cascade:** once one step misses, every later step misses too. Its starting
filesystem has changed, so later results can't be trusted.

### Isolation

A container process must not be able to read or write anything outside its own
root filesystem. Docksmith uses one function, `run_isolated`, for **both** `RUN`
during a build and `docksmith run`. In the child process, before the command starts:

1. `unshare` creates private mount, hostname (UTS) and IPC namespaces.
2. All mounts are made private, so nothing leaks back to the host.
3. The rootfs is bind-mounted onto itself and becomes `/` via `pivot_root`.
4. The old host root is detached. From then on the host filesystem is
   **unreachable**, not just hidden.
5. The hostname is set to `docksmith`, the process moves to the working
   directory, and the command runs with only the image's environment.

If the kernel or a sandbox forbids namespaces, it falls back to plain `chroot`.

---

## 3. Installing and running

### Requirements

- Linux (a VM is fine on macOS/Windows).
- Python **3.12 or newer**. Check with `python3 --version`.
- `sudo`/root for anything that runs a process in isolation (`RUN` steps and
  `docksmith run`). Parsing, `COPY`-only builds, `images` and `rmi` work without root.

### Get the code

```bash
git clone https://github.com/cvishnu007/docksmith-py.git
```

```bash
cd docksmith-py
```

### Option A: run straight from the checkout (no install)

```bash
sudo python3 -m docksmith --help
```

### Option B: install the `docksmith` command (in a virtualenv)

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install .
```

```bash
sudo .venv/bin/docksmith --help
```

The examples below use `python3 -m docksmith`. If you installed it, just type
`docksmith` instead.

> **Always use the same user for build and run.** With `sudo`, state goes to root's
> `~/.docksmith/`; without it, to yours. Set `DOCKSMITH_HOME=/path` to choose
> another location. When using `sudo` with an environment variable, pass it
> explicitly, e.g. `sudo DOCKSMITH_HOME=/srv/ds python3 -m docksmith images`.

### The full demo, step by step

**Step 0: create a base image (once).** Every `FROM` needs an image in the local
store, and nothing is downloaded during builds. Pick one option.

Option A builds `base:latest` from the host's own programs (`sh`, coreutils, `grep`,
`sed`, `awk`, …) and their shared libraries. No network needed:

```bash
sudo python3 -m docksmith base
```

Option B downloads Alpine Linux once and imports it as `alpine:3.20`:

```bash
sudo sh scripts/fetch-alpine.sh
```

**Step 1: cold build.** Every `COPY` and `RUN` is a `[CACHE MISS]`:

```bash
sudo python3 -m docksmith build -t sample:latest sample-app
```

**Step 2: warm build.** Every step is a `[CACHE HIT]`, and the digest is identical:

```bash
sudo python3 -m docksmith build -t sample:latest sample-app
```

**Step 3: change a source file and rebuild.** The `COPY` and everything after it miss:

```bash
echo "edited" >> sample-app/app/data.txt
```

```bash
sudo python3 -m docksmith build -t sample:latest sample-app
```

**Step 4: list images:**

```bash
sudo python3 -m docksmith images
```

**Step 5: run the default command:**

```bash
sudo python3 -m docksmith run sample:latest
```

**Step 6: override an environment variable:**

```bash
sudo python3 -m docksmith run -e MESSAGE="Hi Professor" sample:latest
```

**Step 7: isolation proof.** Write a file inside the container:

```bash
sudo python3 -m docksmith run sample:latest sh -c 'echo hacked > /test.txt; ls /'
```

Then check the host. This must fail with "No such file or directory":

```bash
ls /test.txt
```

**Step 8: remove the image and its layer files:**

```bash
sudo python3 -m docksmith rmi sample:latest
```

**Force a full rebuild** without using or writing the cache:

```bash
sudo python3 -m docksmith build -t sample:latest --no-cache sample-app
```

---

## 4. Command reference

| Command | What it does |
|---|---|
| `docksmith build -t NAME[:TAG] [-f FILE] [--no-cache] [CONTEXT]` | Parses `CONTEXT/Docksmithfile` (or `-f FILE`) and runs every step. `CONTEXT` defaults to `.`. Prints `Step i/n : <instruction>`; `COPY`/`RUN` lines also show `[CACHE HIT]` or `[CACHE MISS]` and the step time. Ends with `Successfully built sha256:<12 hex> NAME:TAG (<total>s)`. |
| `docksmith run [-e KEY=VALUE]... NAME[:TAG] [CMD ...]` | Unpacks the image, runs `CMD` (or the image's default `Cmd`) in the foreground, prints `Exit code: N`, and exits with N. `-e` adds or overrides env vars and can be repeated. Everything after the image name is the command. |
| `docksmith images` | Table of `NAME  TAG  ID  CREATED`. `ID` is the first 12 hex characters of the manifest digest. |
| `docksmith rmi NAME[:TAG]...` | Deletes each image's manifest **and all of its layer files**. There is no reference counting: another image that shares a layer (including the base image) becomes broken. This is intended spec behaviour. |
| `docksmith base [NAME[:TAG]]` | Creates a base image (default `base:latest`) from the host's programs. Offline. |
| `docksmith import TARBALL NAME[:TAG]` | Imports any root filesystem tarball (`.tar`, `.tar.gz`, `.tar.xz`) as a single-layer base image. |
| `docksmith --version` | Prints the version. |

Rules shared by all commands:

- A missing tag means `latest`: `sample` is `sample:latest`, and `FROM base` is `FROM base:latest`.
- Exit status is 0 on success and 1 on any error (printed as `Error: …` on stderr).
  For `run`, it is the container's own exit code. A container killed by signal N
  gives 128 + N.

---

## 5. Writing a Docksmithfile

```dockerfile
# Comments start with '#'. Blank lines are ignored.
FROM base:latest                 # must be the first instruction
WORKDIR /app                     # sets the working dir (created silently)
COPY . /app                      # copy the build context into /app  -> layer
ENV MESSAGE=Hello                # env for later RUN steps and for containers
RUN echo built > /app/build.txt && \
    chmod +x /app/app/main.sh    # runs /bin/sh -c "..." inside the image -> layer
CMD ["sh", "/app/app/main.sh"]   # default command (JSON array only)
```

| Instruction | Rules |
|---|---|
| `FROM image[:tag]` or `FROM scratch` | Exactly one argument. Must come first. A later `FROM` starts a fresh stage and discards everything before it. `scratch` means an empty filesystem. |
| `COPY src... dest` | At least one source and one destination, relative to the build context. Supports globs `*`, `?`, `[abc]`, `**`. Sources may not leave the context (`../x` is rejected). |
| `RUN command` | The rest of the line is passed to `/bin/sh -c`, so `&&`, `\|`, `>` and `$VAR` all work. A non-zero exit fails the build. |
| `WORKDIR path` | Relative paths are joined to the current workdir. |
| `ENV KEY=value [K2=v2 ...]` | Each token must contain `=`. Quote values with spaces: `ENV MSG="hello world"`. |
| `CMD ["exec", "arg", ...]` | Must be a non-empty JSON array of strings. |

Other syntax:

- A line ending in `\` continues on the next line.
- Windows (CRLF) line endings are accepted.
- Unknown instructions (including Docker's `EXPOSE`, `ADD`, …) fail immediately
  with `line N: unknown instruction X`.

### Where COPY puts files

| Source | Destination | Result |
|---|---|---|
| A directory (incl. `.`) | `/app` | The directory's **contents** go into `/app`. |
| One file `f.txt` | `/opt/x.txt` (no trailing `/`, not an existing dir) | Copied to exactly `/opt/x.txt`. |
| One file `f.txt` | `/opt/` or `.` or an existing dir | Copied to `/opt/f.txt`. |
| Several sources | `/dir/` | Each goes into `/dir/`. |
| Glob `*.txt` | `/dir/` | Each match goes into `/dir/` by name. |
| Glob `src/**/*.py` | `/dir/` | Matches keep their path below `src/`, e.g. `/dir/pkg/b.py`. |

These are never copied:

- `Docksmithfile`
- `.docksmith/`
- `.docksmithignore`
- `.DS_Store`, `Thumbs.db`, `__MACOSX`, and `._*` files
- anything matching a pattern in a `.docksmithignore` file in the context (one
  pattern per line, e.g. `*.env` or `build`)

---

## 6. On-disk state

```text
~/.docksmith/                    (or $DOCKSMITH_HOME)
├── images/
│   ├── base__latest.json        one manifest per image: <name>__<tag>.json
│   └── sample__latest.json
├── layers/
│   ├── sha256_d18175e8….tar     immutable layer tars, named by their SHA-256
│   └── sha256_90def91c….tar
├── cache/
│   └── 3f2a…e1.digest           one file per cache key; contains a layer digest
└── tmp/                         scratch root filesystems (always cleaned up)
```

Example manifest (`images/sample__latest.json`):

```json
{
  "name": "sample",
  "tag": "latest",
  "digest": "sha256:64f5c9a0d0b9d0946e44a863a24d4ed6fe97a00d2d1041c3837478bab4fbef10",
  "created": "2026-11-13T21:21:36Z",
  "config": {
    "Env": ["MESSAGE=Hello", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"],
    "Cmd": ["sh", "/app/app/main.sh"],
    "WorkingDir": "/app"
  },
  "layers": [
    { "digest": "sha256:d181…", "size": 19804160, "createdBy": "BASE host userland (56 binaries)" },
    { "digest": "sha256:90de…", "size": 10240,    "createdBy": "COPY . /app" },
    { "digest": "sha256:525a…", "size": 10240,    "createdBy": "RUN echo \"built at build time\" > /app/build.txt && …" }
  ]
}
```

- `size` is the size of the tar file on disk.
- `digest` is computed by serialising this exact JSON with `"digest": ""`, hashing
  those bytes with SHA-256, then writing the file with the real value.

Inspect a layer with standard tools:

```bash
sudo tar -tvf ~/.docksmith/layers/sha256_<hex>.tar
```

---

## 7. Architecture: how everything connects

### Module map

```text
                         ┌──────────────┐
          user ───────►  │   cli.py     │  argparse; one cmd_* function per command
                         └──────┬───────┘
          ┌──────────────┬──────┼───────────────┬────────────────┐
          ▼              ▼      ▼               ▼                ▼
     parser.py      builder.py  runtime.py   image.py       baseimage.py
   (Docksmithfile   (build      (docksmith   (manifests,    (base / import)
    → Instructions)  engine)     run)         ImageStore)
                      │  │  │       │  │          │
          ┌───────────┘  │  └────┐  │  └──────┐   │
          ▼              ▼       ▼  ▼         ▼   ▼
      cache.py      copyops.py  isolation.py  layer.py ◄── (images reference layers)
   (cache keys,    (COPY plan,  (the ONE      (deterministic tars,
    BuildCache)     globs, hash) isolation     whiteouts, safe extract,
                                 primitive)    LayerStore)
                         │                        │
                         └──────► fsutil.py ◄─────┘
                    (root-confined path resolution, snapshots, diff,
                     /dev nodes, temp dirs)

   Shared by all: models.py (data classes + errors), refs.py (name:tag),
                  paths.py (where ~/.docksmith lives)
```

Dependencies only point downward: low-level modules never import high-level ones.
`isolation.py` is the single isolation primitive, called from `builder.py` (for `RUN`)
and `runtime.py` (for `docksmith run`), as the spec requires.

### Build flow

```text
cli.cmd_build
 ├─ refs.parse_ref("sample:latest")                 validate name:tag
 ├─ parser.parse_file("sample-app/Docksmithfile")   → [Instruction, ...]
 ├─ Builder.from_paths(StatePaths)                   wires LayerStore, ImageStore, BuildCache
 ├─ Builder.build(instructions, ref, context, on_step=print_step)
 │    for each instruction:
 │      FROM    → ImageStore.load(base); copy its layers/env/cmd/workdir; prev = base digest
 │      WORKDIR → copyops.resolve_container_path
 │      ENV     → update env dict
 │      CMD     → set cmd
 │      COPY/RUN:
 │        copyops.hash_sources (COPY only)
 │        cache.cache_key(prev, raw, workdir, env, copy_hash)
 │        hit?  BuildCache.get(key) and LayerStore.exists(digest)  → reuse layer
 │        miss: Builder._execute
 │               _Rootfs.materialise   (extract pending layers into tmp dir)
 │               fsutil.ensure_dir_in_root(workdir)       (silent, before snapshot)
 │               Snapshotter.snapshot  → before
 │               COPY: copyops.apply_copy
 │               RUN:  isolation.run_isolated(["/bin/sh","-c",text])
 │               Snapshotter.snapshot  → after
 │               fsutil.diff_snapshots → changed, deleted
 │               layer.build_layer_tar → bytes → LayerStore.put → digest
 │               BuildCache.put(key, digest)  (unless --no-cache)
 │        prev = digest; on_step(BuildStep) → cli.print_step
 │    → Image(config, layers)   (no digest yet)
 ├─ ImageStore.save(image)      keeps "created" if content unchanged; computes digest
 └─ print "Successfully built sha256:<12> name:tag (Ns)"
```

### Run flow

```text
cli.cmd_run
 ├─ parse -e KEY=VALUE pairs
 ├─ ImageStore.load("sample:latest")
 ├─ pick command: CLI args, else image Cmd, else error "no CMD"
 └─ Runtime.run(image, cmd, env)
      ├─ isolation.check_supported()             Linux + root?
      ├─ fsutil.make_tempdir(~/.docksmith/tmp)
      ├─ assemble_rootfs → LayerStore.extract for each layer, in order
      ├─ fsutil.ensure_dev_nodes                 /dev/null etc.
      ├─ fsutil.ensure_dir_in_root(WorkingDir)
      ├─ isolation.run_isolated(rootfs, cmd, image env + overrides, WorkingDir)
      │     child: unshare → private mounts → bind → pivot_root → detach old root
      │            → sethostname → chdir(workdir) → exec cmd
      └─ finally: fsutil.rmtree(rootfs)          always cleaned up
 → print "Exit code: N"; exit with N
```

### Base image flow

```text
cli.cmd_base   → baseimage.create_host_base
                   _populate_host_userland  (copy host binaries + ldd libraries
                                             into a merged-/usr layout, /etc files)
                   _save_single_layer_image (snapshot → tar → LayerStore.put →
                                             ImageStore.save with PATH env, Cmd /bin/sh)
cli.cmd_import → baseimage.import_rootfs
                   layer.extract_tar(tarball) into tmp dir → _save_single_layer_image
```

---

## 8. A full walkthrough of one build and one run

This is what happens for `sudo python3 -m docksmith build -t sample:latest sample-app`
on a **cold** cache, with `base:latest` already created.

1. **`cli.main`** sets `umask 022` (so directories get the same permissions on every
   build), parses arguments, and calls **`cmd_build`**.
2. **`parse_ref`** checks `sample:latest`. **`parse_file`** reads
   `sample-app/Docksmithfile`. **`_logical_lines`** joins the `\` continuation, and
   **`_parse_instruction`** produces six `Instruction` objects.
3. **`Builder.build`** checks that the first instruction is `FROM`, then loops.
4. `Step 1/6 : FROM base:latest`: **`ImageStore.load`** reads
   `images/base__latest.json`. The builder copies its layer list (one host-userland
   layer), its env (`PATH=…`), its `Cmd` and its `WorkingDir`. It sets
   `prev_digest = <base manifest digest>`. Nothing is extracted yet.
5. `Step 2/6 : WORKDIR /app`: `workdir = "/app"`. Config only.
6. `Step 3/6 : COPY . /app`:
   - **`hash_sources`** runs **`plan_copy`** over the context (ignoring
     `Docksmithfile`) and hashes every file's path, mode and contents.
   - **`cache_key`** combines `prev_digest`, `"COPY . /app"`, `"/app"`, the env and
     that hash.
   - **`BuildCache.get(key)`** finds nothing → `[CACHE MISS]`, and cascade is now on.
   - **`_execute`** → **`_Rootfs.materialise`** makes `~/.docksmith/tmp/build-XXXX/`
     and extracts the base layer into it.
   - **`ensure_dir_in_root`** creates `/app` *before* the snapshot, so the empty
     directory isn't part of the delta.
   - **`Snapshotter.snapshot`** records every path's type, mode and SHA-256.
   - **`apply_copy`** copies `app/main.sh` and `app/data.txt` into `/app/app/`.
   - A second snapshot and **`diff_snapshots`** give `changed = [app, app/app, app/app/data.txt, app/app/main.sh]`.
   - **`build_layer_tar`** writes those entries sorted, zero-timestamped and
     root-owned. **`LayerStore.put`** hashes the bytes and stores
     `layers/sha256_<hex>.tar`.
   - **`BuildCache.put(key, digest)`** records the result.
   - `prev_digest` becomes this layer's digest, and **`print_step`** prints the line.
7. `Step 4/6 : ENV MESSAGE=Hello`: `env["MESSAGE"] = "Hello"`.
8. `Step 5/6 : RUN echo … && chmod …`:
   - Its key includes the COPY layer digest and the new env.
   - Cascade forces a miss. The rootfs already holds the base and COPY layers, so
     nothing is extracted again.
   - **`ensure_dev_nodes`** adds `/dev/null` etc.
   - After the before-snapshot, **`run_isolated`** runs
     `/bin/sh -c 'echo … > /app/build.txt && chmod +x /app/app/main.sh'` inside the
     rootfs.
   - The delta is `app/app/main.sh` (its mode changed) and `app/build.txt` (new).
9. `Step 6/6 : CMD [...]`: sets `cmd`.
10. The **`finally`** block deletes the temporary rootfs. `build` returns an `Image`
    with three layers.
11. **`ImageStore.save`** finds no existing manifest, so it sets `created` to now,
    computes the digest over the JSON with `"digest": ""`, and writes the file.
12. The CLI prints `Successfully built sha256:… sample:latest (0.07s)`.

On a **warm** build, steps 6 and 8 compute the same keys and find them in `cache/`,
and the layer files exist, so both are `[CACHE HIT]`. No rootfs is ever created.
`ImageStore.save` sees identical content and keeps the old `created`, so the digest
is identical.

For **`run sample:latest`**:

- **`Runtime.run`** extracts the three layers into a fresh temp dir, adds `/dev`
  nodes, and makes sure `/app` exists.
- **`run_isolated`** forks. The child enters new namespaces, pivots into the temp
  dir, sets the hostname, `chdir`s to `/app`, and execs `sh /app/app/main.sh` with
  `MESSAGE=Hello`, `PATH=…`, `HOME=/root` and `HOSTNAME=docksmith`.
- The parent waits, prints `Exit code: 0`, and the temp dir is deleted. Anything the
  container wrote is gone with it.

---

## 9. Module and function reference

### `docksmith/__main__.py`

Lets `python3 -m docksmith` work. It calls `cli.main()` and exits with its return
value.

### `docksmith/__init__.py`

Defines `__version__`.

### `docksmith/models.py`: shared data types

| Name | Purpose |
|---|---|
| `DocksmithError` | Base class for all user-facing errors. The CLI prints these as `Error: …` and exits with status 1. |
| `ParseError`, `BuildError`, `ImageNotFound`, `IsolationError` | Specific error kinds (all subclasses of `DocksmithError`). |
| `Instruction` | One parsed line. Fields: `op` (`"COPY"`), `args` (token list), `raw` (exact text, used in cache keys), `text` (everything after the opcode, used as the RUN shell command), `line` (line number for errors). |
| `Instruction.__str__` | The instruction as printed in step lines and `createdBy`. |
| `ImageConfig` | `cmd`, `env`, `working_dir`. |
| `ImageLayer` | `digest`, `size`, `created_by`. |
| `Image` | `name`, `tag`, `digest`, `created`, `config`, `layers`. |
| `Image.ref` | `"name:tag"`. |
| `Image.short_id` | First 12 hex characters of the digest (the `images` ID column). |
| `BuildStep` | Progress record passed to the step callback: `index`, `total`, `instruction`, `op`, `cache_status`, `duration`. |

### `docksmith/refs.py`: image references

| Function | Purpose |
|---|---|
| `parse_ref(ref)` | Splits `name[:tag]` into `(name, tag)`. The tag defaults to `latest`. Rejects empty parts, spaces and invalid characters. |
| `format_ref(name, tag)` | Joins them back as `name:tag`. |

### `docksmith/paths.py`: where state lives

| Name | Purpose |
|---|---|
| `default_root()` | `$DOCKSMITH_HOME` if set, else `~/.docksmith`. |
| `StatePaths` | Immutable holder of the root path. |
| `StatePaths.default()` | A `StatePaths` at `default_root()`. |
| `.images` / `.layers` / `.cache` / `.tmp` | The four sub-directories. |
| `.ensure()` | Creates the four sub-directories and returns `self`. |

### `docksmith/parser.py`: Docksmithfile → instructions

| Function | Purpose |
|---|---|
| `parse_file(path)` | Reads the file (clear errors if it is missing or unreadable) and calls `parse_string`. |
| `parse_string(content)` | Skips blank lines and comments, parses every logical line, and errors if nothing is left. |
| `_logical_lines(content)` | Yields `(start_line, text)`. Strips `\r` and joins lines ending in `\`, so error line numbers point at where the instruction starts. |
| `_parse_instruction(raw, trimmed, line)` | Splits off the opcode and validates it against the six supported ones (out-of-scope Docker instructions get a hint). Decodes `CMD` as JSON and tokenises the rest with `split_args`. Checks per-instruction argument rules and returns an `Instruction`. |
| `split_args(s)` | Shell-like tokeniser: whitespace separates tokens, `'single'` and `"double"` quotes group words, `\` escapes the next character. An unclosed quote is an error. |

### `docksmith/fsutil.py`: safe filesystem helpers

| Name | Purpose |
|---|---|
| `RUNTIME_DIRS` | `{"dev", "proc", "sys"}`. Top-level directories that are never part of a layer. |
| `resolve_in_root(root, path, follow_last=False)` | **Key security helper.** Resolves a container path as if `root` were `/`. Symlinks are followed, but absolute link targets are re-rooted at `root`, and `..` can never climb above `root`, so the result is always inside `root`. Every write into a rootfs goes through it, so a malicious symlink like `/evil → /etc` cannot make Docksmith write to the host. |
| `remove_path(path)` | Deletes a file, symlink or directory tree if it exists. |
| `rmtree(path)` | `shutil.rmtree` that also handles read-only directories. |
| `make_tempdir(base, prefix)` | A unique temp directory under `base` (always under `~/.docksmith/tmp`, so it is on the same disk and is never `noexec`). |
| `sha256_file(path)` | Streams a file through SHA-256. |
| `Snapshotter` | Takes snapshots of a rootfs. |
| `Snapshotter.snapshot(root)` | Returns `{relative_path: (kind, mode, content_id)}` for every file (`"f"`, content = SHA-256), directory (`"d"`) and symlink (`"l"`, content = link target). It skips `/dev`, `/proc` and `/sys`, plus sockets and devices. Hashes are cached by stat signature, so the second snapshot of a step only re-hashes files that changed. |
| `diff_snapshots(before, after)` | Returns `(changed, deleted)`. `changed` is every new or modified path. `deleted` is only the top-most removed paths (a removed directory implies its children). |
| `ensure_dev_nodes(rootfs)` | Creates `/dev/null`, `/dev/zero`, `/dev/random`, `/dev/urandom` (with `mknod`, needs root), `/dev/shm` and `/dev/pts`. Never part of a layer. |
| `ensure_dir_in_root(rootfs, path)` | `mkdir -p` inside the rootfs, via `resolve_in_root`. |
| `is_within(child, parent)` | Real-path containment check. Used to keep COPY sources inside the build context. |

### `docksmith/layer.py`: layers

| Name | Purpose |
|---|---|
| `LayerError` | Error for bad digests, bad tar paths or missing layers. |
| `digest_hex(digest)` | Validates `sha256:<64 lowercase hex>` and returns the hex part. |
| `sha256_digest(data)` | `"sha256:" + sha256(data)`. |
| `_parents(rel)` | Yields every parent directory of a relative path (`a/b/c` → `a/b`, `a`). |
| `build_layer_tar(root, changed, deleted)` | **The deterministic tar writer.** It collects the changed paths, each one's parent directories (so permissions are preserved), and a `.wh.<name>` whiteout for each deleted path. Entries are written **sorted**, with mtime 0, uid/gid 0, empty user/group names, PAX format, and the real permission bits. Files, directories and symlinks are supported. |
| `_normalise_member_name(name)` | Cleans a tar member name (drops `./` and leading `/`). Raises on any `..` that would escape. |
| `extract_tar(source, root)` | **The safe extractor**, used for both layers and imported tarballs. Each member's parent is resolved with `resolve_in_root`. Whiteouts (`.wh.x`) delete `x`, and opaque whiteouts (`.wh..wh..opq`) empty the directory. Otherwise it creates the directory, file, symlink or hard link, replacing whatever was there (later layers win). Files are opened with `O_NOFOLLOW`. Devices and FIFOs are skipped. |
| `LayerStore(layers_dir)` | The `layers/` directory. |
| `LayerStore.path(digest)` | `layers/sha256_<hex>.tar`. |
| `LayerStore.exists(digest)` / `.size(digest)` | Existence check (part of the cache-hit test) and the size used in manifests. |
| `LayerStore.put(data)` | Stores tar bytes under their SHA-256. If the file already exists it is **not rewritten** (layers are immutable). It writes to a temp file then renames atomically, and makes the file read-only. Returns `(digest, size)`. |
| `LayerStore.extract(digest, root)` | Unpacks one layer, with a clear error if the file was deleted (e.g. by `rmi`). |
| `LayerStore.remove(digest)` | Deletes a layer file. Returns whether it existed. |

### `docksmith/cache.py`: build cache

| Name | Purpose |
|---|---|
| `encode_env(env)` | `KEY=value` lines in sorted key order (`""` if empty), so env order never changes the key. |
| `cache_key(prev_digest, raw, workdir, env, copy_sources_hash=None)` | SHA-256 over those five parts, each followed by a NUL separator so parts can't run into each other. Returns hex. |
| `BuildCache(cache_dir)` | The `cache/` index. |
| `BuildCache.get(key)` | The layer digest stored for `key`, or `None`. |
| `BuildCache.put(key, digest)` | Writes `cache/<key>.digest` atomically. |

### `docksmith/copyops.py`: COPY

| Name | Purpose |
|---|---|
| `has_glob(s)` | True if `s` contains `*`, `?` or `[`. |
| `_Ignore(ctx)` | Loads `.docksmithignore`. Calling it with a context-relative path returns True for always-ignored names (`Docksmithfile`, `.docksmith`, OS junk) and for paths matching a pattern (checked against every path prefix and name). |
| `SourceEntry` | One item to copy: `placement` (path under the destination), `ctx_rel` (path in the context), `abs_path`, `kind` (`f`/`d`/`l`), `mode`. |
| `CopyPlan` | The sorted list of `SourceEntry` plus `single_file` (one plain file source, eligible for "copy to exactly this path"). |
| `_kind(stat)` | Maps a stat result to `f`/`d`/`l`, or `None` for unsupported types. |
| `_static_prefix(pattern)` | The leading directory part of a glob with no wildcards (`src/**/*.py` → `src`). Matches keep their path relative to it. |
| `_clean_rel(src)` | Normalises a source argument relative to the context (`/a/../b` → `b`, empty → `.`). |
| `plan_copy(ctx, sources)` | **Resolves all sources into a `CopyPlan`.** Globs are expanded with `glob.glob(..., recursive=True)` (an error if nothing matches). Directories are walked recursively, and ignored files are skipped. Sources outside the context are rejected. Later sources override earlier ones at the same placement. Used by **both** the cache hash and the copy, so they can never disagree. |
| `_inside(abs_path, ctx)` | Containment check. For symlinks it checks where the link *is*, not where it points, because links are copied as links and never followed. |
| `_join(a, b)` | Joins relative POSIX paths, treating `""` and `.` as empty. |
| `hash_sources(ctx, sources)` | SHA-256 over every planned entry: type, placement, context path, mode, and content hash (or link target). This is the COPY part of the cache key. |
| `resolve_container_path(workdir, p)` | Resolves `p` against the current workdir into a normalised absolute container path. Used by `WORKDIR` and the COPY destination. |
| `_copy_entry(entry, target)` | Copies one entry: makes the directory, re-creates the symlink, or copies the file and sets its mode. Anything conflicting at `target` is replaced. |
| `apply_copy(rootfs, ctx, args, workdir)` | **Executes COPY.** It builds the plan and resolves the destination inside the rootfs. It then either copies a single file to an exact path, or makes sure the destination directory exists and places every entry under it. All paths go through `resolve_in_root`. |

### `docksmith/image.py`: manifests and the image store

| Name | Purpose |
|---|---|
| `now_iso()` | Current UTC time as `YYYY-MM-DDTHH:MM:SSZ`. |
| `env_to_list(env)` / `env_from_list(items)` | Convert between the `{"K": "v"}` dict and the manifest's sorted `["K=v"]` list. |
| `manifest_dict(img, digest)` | The manifest as an ordered dict (`name, tag, digest, created, config{Env,Cmd,WorkingDir}, layers[{digest,size,createdBy}]`). |
| `serialize_manifest(m)` | Canonical bytes: `json.dumps(indent=2)`, UTF-8. Hashing and writing both use exactly this. |
| `compute_manifest_digest(img)` | SHA-256 of `serialize_manifest(manifest_dict(img, ""))`, following spec §4.1. |
| `image_from_manifest(data)` | Parsed JSON → `Image`. |
| `_same_content(a, b)` | True if two images differ at most in `digest` and `created`. |
| `ImageStore(images_dir, layers)` | The `images/` directory. It also holds the `LayerStore` so `remove` can delete layers. |
| `ImageStore._path(name, tag)` | `images/<name>__<tag>.json` (a `/` in a name becomes `_`). |
| `ImageStore.save(img)` | If a manifest for this name:tag exists with identical content, it **reuses its `created`**; otherwise `created` is now. It then computes the digest and writes atomically. Returns the saved image (with digest). This is why identical rebuilds give identical digests. |
| `ImageStore._read(path)` | Loads and parses one manifest, with a clear error if it is corrupt. |
| `ImageStore.exists(ref)` / `.load(ref)` | Existence check and loading. `load` raises `ImageNotFound` with a clear message. |
| `ImageStore.list()` | All readable manifests, sorted by name then tag. |
| `ImageStore.remove(ref)` | Deletes every referenced layer file, then the manifest. Returns `(image, layers_actually_deleted)`. No reference counting (spec). |
| `ImageStore.verify_digest(path)` | Re-computes a manifest file's digest and checks it matches the stored one (used in tests). |

### `docksmith/isolation.py`: the isolation primitive

| Name | Purpose |
|---|---|
| `DEFAULT_PATH`, `HOSTNAME` | Default `PATH` and the container hostname (`docksmith`). |
| `_libc`, `_CLONE_*`, `_MS_*`, `_MNT_DETACH`, `_SYS_PIVOT_ROOT` | `ctypes` handle to libc and the kernel constants and syscall numbers (per CPU architecture) used below. |
| `_check(ret, what)` | Turns a failed libc call into an `OSError` with `errno`. |
| `_pivot_into(rootfs)` | **Runs in the child process.** `unshare(NEWNS\|NEWUTS\|NEWIPC)` → make `/` recursively private → bind-mount the rootfs onto itself → `chdir` into it → `pivot_root(".", ".")` → `umount2(".", MNT_DETACH)` to drop the host root. If that unmount fails the child exits rather than run with the host visible. Then `chroot(".")`, `chdir("/")` and `sethostname`. |
| `_make_preexec(rootfs, workdir)` | Builds the function `subprocess` runs between `fork` and `exec`. It restores default signal handling, tries `_pivot_into`, falls back to `chroot(rootfs)` if that fails (or if `DOCKSMITH_ISOLATION=chroot`), then `chdir`s to the workdir. |
| `process_env(env)` | The process environment: defaults (`PATH`, `HOME=/root`, `HOSTNAME`) overridden by the image env and `-e` values. Nothing from the host leaks in. |
| `check_supported()` | Raises a clear `IsolationError` if not on Linux or not root. |
| `run_isolated(rootfs, cmd, env, workdir)` | **The one primitive.** It starts `cmd` with the preexec hook and waits (Ctrl-C reaches the container, and cleanup waits for it to exit). Returns the exit code (128 + N if killed by signal N). Launch failures become clear errors (`executable 'x' not found inside the image filesystem`). |

### `docksmith/runtime.py`: `docksmith run`

| Name | Purpose |
|---|---|
| `assemble_rootfs(image, layers, rootfs)` | Extracts every layer in order. Later layers overwrite earlier ones, and whiteouts delete files. |
| `Runtime(paths, layers)` | Holds state paths and the layer store. |
| `Runtime.run(image, cmd=None, env_overrides=None)` | Picks the command (the argument, else the image `Cmd`, else a "no CMD" error) and merges the env (image env, then overrides). It then: checks support → makes a temp rootfs → assembles it → adds `/dev` nodes → creates `WorkingDir` (default `/`) → `run_isolated` → **always** deletes the temp rootfs. Returns the exit code. |

### `docksmith/builder.py`: the build engine

| Name | Purpose |
|---|---|
| `_Rootfs` | The working filesystem of the current build stage, **created lazily**. |
| `_Rootfs.materialise(layers)` | Creates the temp dir on first use and extracts only the layers not yet applied (`self.applied` tracks how many). Because cache hits never call this, a fully cached build never unpacks anything. |
| `_Rootfs.discard()` | Deletes the temp dir. Called on `FROM` and at the end of the build (even on errors). |
| `Builder(paths, images, layers, cache)` | The engine, with its stores injected. |
| `Builder.from_paths(paths)` | Convenience constructor that wires the stores for a `StatePaths`. |
| `Builder.build(instructions, ref, context, no_cache=False, on_step=None)` | **The main loop.** It tracks `layers`, `env`, `cmd`, `workdir`, `prev_digest` and `cascade`. `FROM` loads the base (or `scratch`) and resets all state. `WORKDIR`, `ENV` and `CMD` update state. `COPY`/`RUN` compute the cache key, try a hit (key in the index and layer on disk, unless `no_cache` or `cascade`), and otherwise call `_execute` and record the result. After each instruction it calls `on_step(BuildStep(...))`. Returns an unsaved `Image`. |
| `Builder._execute(inst, rootfs, layers, snap, ctx, workdir, env)` | Runs one COPY/RUN for real. It materialises the rootfs and silently creates the workdir. For RUN it also adds `/dev` nodes and checks for `/bin/sh`. Then: snapshot → `apply_copy` or `run_isolated(["/bin/sh","-c",text])` (non-zero exit = `BuildError`) → snapshot → diff → `build_layer_tar` → `LayerStore.put`. Returns `(digest, size)`. |

### `docksmith/baseimage.py`: creating base images

| Name | Purpose |
|---|---|
| `HOST_BINARIES` | The programs copied by `docksmith base` (shell, coreutils, grep/sed/awk, find, tar, gzip, ps, …). Missing ones are skipped. |
| `_rootfs_path(host_path)` | Maps a host path into a merged-`/usr` layout (`/lib/x/libc.so.6` → `/usr/lib/x/libc.so.6`), so it works on hosts with or without merged `/usr`. |
| `_copy_host_file(rootfs, host_path, dest, mode)` | Copies the real file (symlinks resolved) into the rootfs at `dest`. |
| `_shared_libs(binary)` | Runs `ldd` and returns the absolute paths of every needed library and the dynamic loader. |
| `_populate_host_userland(rootfs)` | Builds the base tree: `usr/{bin,sbin,lib,lib64}` with `bin`/`sbin`/`lib`/`lib64` symlinks into it, `etc`, `root`, `tmp` (mode 1777), the binaries and their libraries, plus `/etc/passwd`, `/etc/group`, `/etc/hostname` and `/etc/os-release`. Fails clearly if no shell was found. |
| `_save_single_layer_image(paths, rootfs, ref, created_by, cmd)` | Snapshots the whole tree into one deterministic layer and saves an image with `PATH` set, `WorkingDir /`, and `Cmd ["/bin/sh"]` (if a shell exists). |
| `create_host_base(paths, ref)` | `docksmith base`: temp dir → populate → save → clean up. Returns `(image, binary_count)`. |
| `import_rootfs(paths, tarball, ref)` | `docksmith import`: safely extracts the tarball (any compression) → save → clean up. |

### `docksmith/cli.py`: the command line

| Name | Purpose |
|---|---|
| `_short(digest)` | `sha256:` + the first 12 hex characters, for output. |
| `print_step(step)` | Prints `Step i/n : <instruction>`, plus ` [CACHE HIT\|MISS] 0.00s` for COPY/RUN. FROM, WORKDIR, ENV and CMD never show cache status. |
| `cmd_build(args, paths)` | Validates the tag and context, parses, builds with `print_step`, saves, and prints the summary with total time. |
| `cmd_run(args, paths)` | Parses `-e` pairs, loads the image, picks the command, prints `[run] image=… cmd=…`, runs, prints `Exit code: N`, and returns N. |
| `cmd_images(args, paths)` | Prints the aligned `NAME TAG ID CREATED` table. |
| `cmd_rmi(args, paths)` | Removes each named image. A missing image is an error but doesn't stop the rest. |
| `cmd_base(args, paths)` / `cmd_import(args, paths)` | Create base images. |
| `build_parser()` | Defines all sub-commands and flags with `argparse`. `run` uses `REMAINDER`, so everything after the image name is the container command. |
| `main(argv=None)` | Entry point: sets `umask 022`, parses arguments, and dispatches. Turns any `DocksmithError` into `Error: …` with exit status 1, and Ctrl-C into exit status 130. |

---

## 10. Tests

Run the unit tests unprivileged:

```bash
python3 -m pip install pytest
```

```bash
python3 -m pytest -q
```

Run the full suite, including the root-only isolation tests:

```bash
sudo python3 -m pytest -q
```

Force the `chroot` fallback to test it as well:

```bash
sudo DOCKSMITH_ISOLATION=chroot python3 -m pytest -q
```

| File | What it covers |
|---|---|
| `tests/test_parser.py` | All instructions, comments, blank lines, CRLF, continuations, JSON `CMD`, `ENV` forms, error messages with line numbers, quoting. |
| `tests/test_cache_and_copy.py` | Cache-key properties (env order, whitespace, each component), cache index round trip, COPY hashing, `*`/`**` globs, `.docksmithignore`, context escapes, COPY destination rules, and symlink escapes during COPY. |
| `tests/test_layer_and_image.py` | Deterministic sorted zero-timestamp tars, deltas and whiteouts, whiteout extraction, `..` and symlink-escape rejection on extract, immutable content-addressed storage, spec-exact manifest digest, `created` preservation, list/remove, default tag. |
| `tests/test_builder.py` | Deterministic layer digests, HIT/MISS and cascade, every invalidation trigger (source file, instruction text, ENV, WORKDIR, FROM base, missing layer file), `--no-cache`, config-only instructions, first-instruction-must-be-FROM, save/load, glob builds. |
| `tests/test_isolation_e2e.py` (root) | RUN executes inside the image and its layer holds only the delta, failing RUN stops the build, deletions persist, env/workdir/overrides/exit codes, **container writes never reach the host and host files are invisible**, temp dirs cleaned up, rootfs import with symlinks and hardlinks, and the full CLI demo flow. |

CI (`.github/workflows/tests.yml`) runs the suite both unprivileged and as root on
every push.

---

## 11. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Error: process isolation requires root … re-run with sudo` | `RUN` and `run` need root. Use `sudo`, and use it for every command so the state directory is the same. |
| `FROM base: image base:latest not found` | Create a base first: `sudo python3 -m docksmith base` (or `import`). Make sure you are the same user (sudo vs not). |
| `images` shows nothing after a `sudo` build | Without `sudo` you are looking at your own `~/.docksmith`. Run `sudo python3 -m docksmith images`. |
| `base image … is broken: layer … is missing` | `rmi` deleted a layer shared with the base image (no reference counting, per spec). Re-run `docksmith base` / `import`. |
| `RUN needs /bin/sh inside the image` | You built `FROM scratch` (or from a rootfs without a shell) and used `RUN`. Use a base image with a shell. |
| `executable 'x' not found inside the image filesystem` | The command isn't in the image. Only programs present in its layers exist inside the container. |
| `set: Illegal option -` or `^M` in output | Your script has Windows (CRLF) line endings. Convert it with `sed -i 's/\r$//' file.sh`. `.gitattributes` keeps `*.sh` as LF in git. |
| Network commands fail in `RUN` | By design: builds are offline. Put dependencies in the build context. |
| Python errors about `onexc` or `include_hidden` | Python is older than 3.12. Upgrade. |

---

## 12. Glossary

| Term | Meaning |
|---|---|
| **Build context** | The directory passed to `build`. `COPY` can only read from it. |
| **Cache key** | Hash of everything that affects one COPY/RUN step. |
| **Cascade** | After the first cache miss, every later step is a miss. |
| **Delta layer** | A tar of only what one step added, changed or deleted. |
| **Digest** | `sha256:<hex>` hash identifying a layer or a manifest. |
| **Manifest** | The JSON file describing an image. |
| **Namespace** | A Linux kernel feature that gives a process a private view of something (mounts, hostname, …). |
| **`pivot_root`** | Syscall that swaps the root filesystem of a mount namespace. Unlike `chroot`, the old root can then be detached entirely. |
| **Rootfs** | The directory a container sees as `/`. |
| **Whiteout** | Tar entry `.wh.<name>` meaning "delete `<name>`" when the layer is applied. |
