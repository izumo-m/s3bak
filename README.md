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

To poke at s3bak against a real S3-compatible endpoint, `scripts/` brings up a
local MinIO stack: `scripts/compose-up.sh && source scripts/minio-env.sh`.

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
  diff        Show content differences
  list        List locally configured entries
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
s3bak status bin              # M/A/D summary for one entry
s3bak pull bin home-docs      # restore selected entries in parallel
s3bak pull bin -o /tmp/out    # restore the bin entry to /tmp/out
s3bak pull bin --delete --dry-run  # preview a mirror restore
s3bak push --all --delete --yes    # unattended mirror (e.g. cron)
s3bak push bin/subdir         # entry-rooted syntax; independent of CWD
s3bak verify --all            # check manifests against stored objects
s3bak verify --all --checksum # also compare local content to S3 ETags
s3bak ls-remote               # list entries stored on S3
```

The `status` letters are push-oriented (what a push would change on the backup):
`M` modified, `A` only local (push would add), `D` only in backup
(`push --delete` would remove; a plain push keeps it).

## Design

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
