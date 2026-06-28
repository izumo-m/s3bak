# s3bak

Unified S3 backup/restore tool.

`s3bak` backs up and restores configured directories or files to/from S3. It is
a single, dependency-free Python program that drives the official `aws` CLI, and
keeps a metadata manifest alongside the data so it can report exactly what a
push or pull would change.

## Requirements

- Python **3.10+**
- The [AWS CLI](https://docs.aws.amazon.com/cli/) (`aws`) installed and on `PATH`,
  with a configured profile

There are no third-party Python dependencies.

## Install

With [uv](https://docs.astral.sh/uv/):

```sh
# Install the `s3bak` command as a uv tool
uv tool install git+https://github.com/izumo-m/s3bak

# ...or run it without installing
uvx --from git+https://github.com/izumo-m/s3bak s3bak help
```

For local development:

```sh
git clone git@github.com:izumo-m/s3bak.git
cd s3bak
uv sync
uv run s3bak help
```

## Configuration

s3bak reads `~/.config/s3bak/config.py` (override with `$S3BAK_CONFIG`). It is
plain Python, executed at startup, so build paths from `HOME` - entry paths are
used as-is and `~` is not expanded. See
[`config.example.py`](config.example.py) for a fully commented template.
Minimal example:

```python
import os

HOME = os.environ.get("HOME", "")

profile = "default"
prefix = "s3://my-bucket/backup"

entries = {
    "bin": {"path": f"{HOME}/bin"},
    "home-docs": {"path": f"{HOME}/Documents", "excludes": ["*.tmp"]},
}
```

Per-entry keys: `path` (required), `excludes`, `pre_hook`, `post_hook`.

## Usage

```
Usage: s3bak <command> [options] [args]

Commands:
  push <entry|path>...     Back up entries or sub-paths to S3
  pull <entry|path>        Restore an entry or sub-path (use --all for every entry)
  show <entry|path>        Print a single file from the backup to stdout
  status <entry|path>...   Compare local vs backup (metadata only)
  diff <entry|path>        Show content diff between backup and local
  list                     List locally configured entries
  ls-remote [entry|path]   List S3 entries, or files under an entry/sub-path
  help                     Show this help
```

Common options: `--all`, `--dryrun` (push), `--delete` (pull), `--meta-only`,
`--data-only`, `-o/--output <path>` (pull), `-v/--verbose`, `--color[=WHEN]`.

Run `s3bak help` for the full option list and worked examples.

### Examples

```sh
s3bak push --all              # back up every configured entry
s3bak push --all --dryrun     # preview without uploading
s3bak status bin              # M/A/D summary for one entry
s3bak pull bin -o /tmp/out    # restore the bin entry to /tmp/out
s3bak ls-remote               # list entries stored on S3
```

The `status` letters are push-oriented (what a push would change on the backup):
`M` modified, `A` only local (push would add), `D` only in backup (push would delete).

## License

[MIT](LICENSE)
