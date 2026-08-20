# s3bak

Unified S3 backup/restore tool.

`s3bak` backs up and restores configured directories or files to/from S3. It
uses [boto3-s3](https://pypi.org/project/boto3-s3/) (an aws-s3-compatible
library built on boto3) for transfers and boto3 for object inspection, and keeps
a metadata manifest alongside the data so it can report exactly what a push or
pull would change.

## Requirements

- Python **3.10+**
- An AWS profile name in `config.py`, with usable credentials for that profile
- A `diff` executable for the `s3bak diff` command (GNU diff enables color)

s3bak depends on [boto3-s3](https://pypi.org/project/boto3-s3/) (installed
automatically), which brings in boto3. No separate AWS CLI install is required.

## Install

With [uv](https://docs.astral.sh/uv/):

```sh
# Install the `s3bak` command as a uv tool
uv tool install git+https://github.com/izumo-m/s3bak

# ...or run it without installing
uvx --from git+https://github.com/izumo-m/s3bak s3bak --help
```

For local development:

```sh
git clone git@github.com:izumo-m/s3bak.git
cd s3bak
uv sync
uv run s3bak --help
uv run pytest        # hermetic test suite (uses moto; no AWS/Docker needed)
```

To install the `s3bak` command from this working tree instead of GitHub (e.g.
to try out local changes without a venv):

```sh
uv tool install .
```

To poke at s3bak against a real S3-compatible endpoint, `scripts/` brings up a
local MinIO stack: `scripts/compose-up.sh && source scripts/minio-env.sh`.

## Update

```sh
uv tool upgrade s3bak --reinstall    # installed from GitHub
uv tool install . --reinstall        # installed from this working tree
```

The GitHub install tracks the default branch, not a pinned commit, so
`--reinstall` is required — otherwise `uv` may treat the cached clone as
already up to date and skip fetching new commits.

## Configuration

s3bak reads `~/.config/s3bak/config.py` (override with `$S3BAK_CONFIG`). It is
plain Python, executed at startup, so build paths from `HOME` - entry paths are
used as-is and `~` is not expanded. See
[`config.example.py`](config.example.py) for a fully commented template.
Entry names must be non-empty, single path components and cannot end in
`-manifest.jsonl`.
Minimal example:

```python
import os

HOME = os.environ["HOME"]  # fail loudly if unset; on Windows use USERPROFILE

profile = "default"
prefix = "s3://my-bucket/backup"

entries = {
    "bin": {"path": f"{HOME}/bin"},
    "home-docs": {"path": f"{HOME}/Documents", "excludes": ["*.tmp"]},
}
```

Per-entry keys: `path` (required), `excludes`, `pre_hook`, `post_hook`,
`mtime_window`.

The optional top-level `groups` names sets of entries — `groups = {"nightly":
["bin", "home-docs"]}` — and a group can be typed wherever a command takes
several entry names. Groups may nest, and no group name reaches S3.

Hooks are non-empty argument lists whose first item is the executable. s3bak
runs them directly without a command shell, so shell parsing, expansion,
pipelines, and redirection are unavailable. Put complex work in a standalone
executable or script instead.

## Usage

```
Usage: s3bak <command> [options] [args]

Commands:
  push        Back up entries or sub-paths to S3
  pull        Restore entries or sub-paths from S3
  show        Print a backed-up file
  status      Compare local files with the backup
  verify      Verify backup integrity on S3
  hook        Run an entry's pre_hook or post_hook on demand
  diff        Show content differences
  list        List locally configured entries and groups
  ls-remote   List entries or files stored on S3

Global options:
  --help      Show this help
  --version   Show the program version
```

Run `s3bak <command> --help` for the selected command's arguments, options, and
examples.

### Examples

```sh
s3bak push --all              # back up every configured entry
s3bak push --all --dry-run    # preview without uploading
s3bak push nightly            # back up every entry in a configured group
s3bak status bin              # what a push would change (M/A)
s3bak status --delete bin     # also list what push --delete would offer (D)
s3bak pull bin home-docs      # restore selected entries in parallel
s3bak pull bin -o /tmp/out    # restore the bin entry to /tmp/out
s3bak pull bin --delete --dry-run  # preview a mirror restore
s3bak push --all --delete --yes    # unattended mirror (e.g. cron)
s3bak push bin/subdir         # entry-rooted syntax; independent of CWD
s3bak verify --all            # check manifests against stored objects
s3bak verify --all --checksum # also compare local content to S3 ETags
s3bak ls-remote               # list entries stored on S3
```

The `status` letters are push-oriented — each variant previews its push. What
each one means, and every other command's output, is the manual's
[command reference](docs/manual/05-command-reference.md).

## Documentation

The [**user manual**](docs/manual/README.md) is the complete guide and the
authoritative description of what s3bak does: configuration, change detection,
every command, `--delete`, the operating routine, recovery, and platform
notes. Start at [chapter 1](docs/manual/01-introduction.md), or
[chapter 2](docs/manual/02-getting-started.md) to set a backup up.

For how s3bak is built and why:

- [`docs/overview.md`](docs/overview.md) — project goals and the design document
  index.
- [`docs/storage.md`](docs/storage.md) — the S3 key layout and storage model.
- [`docs/manifest.md`](docs/manifest.md) — the JSONL manifest format and its
  streaming invariants.
- [`docs/sync.md`](docs/sync.md) — comparison, transfer, concurrency, and the
  push / pull pipelines.
- [`docs/cli.md`](docs/cli.md) — argument resolution, results, and exit codes.
- [`docs/architecture.md`](docs/architecture.md) — module boundaries,
  dependency direction, and S3 client lifetime.

## License

[MIT](LICENSE)
