# Requires Python 3.10+
"""s3bak - Unified S3 backup/restore tool

Backs up and restores configured directories or files to/from S3.

Config: ~/.config/s3bak/config.py (override: $S3BAK_CONFIG)
"""

from __future__ import annotations

import concurrent.futures
import datetime
import itertools
import os
import shlex
import shutil
import signal
import stat as stat_mod
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, NoReturn

from s3bak import manifest
from s3bak.manifest import ManifestEntry, path_match, split_excludes

PROG = "s3bak"
IS_WINDOWS = sys.platform == "win32"

# =============================================================================
# Utilities
# =============================================================================

_output_lock = threading.Lock()

# Transfer warnings (WARNED outcomes) are printed as they occur and counted;
# run() turns a warning-only run into exit code 2.
_warning_lock = threading.Lock()
_warning_count = 0


def err(msg: str) -> None:
    sys.stderr.write(f"{PROG}: {msg}\n")
    sys.stderr.flush()


def die(msg: str) -> NoReturn:
    err(msg)
    sys.exit(1)


def write_output(text: str) -> None:
    with _output_lock:
        sys.stdout.write(text)
        sys.stdout.flush()


def write_stderr(text: str) -> None:
    with _output_lock:
        sys.stderr.write(text)
        sys.stderr.flush()


def _note_warning(msg: str) -> None:
    """Print a transfer warning and count it; run() maps any warning to exit 2."""
    global _warning_count
    write_stderr(f"{msg}\n")
    with _warning_lock:
        _warning_count += 1


def echo_command(verbose: bool, args: list[str]) -> None:
    if verbose:
        write_stderr(f"+ {shlex.join(args)}\n")


def expand_home(path: str) -> str:
    # os.path.expanduser on Windows-native Python (ucrt64/mingw) resolves "~"
    # via USERPROFILE, which ignores the msys HOME. Prefer HOME when set.
    if not path.startswith("~"):
        return path
    home = os.environ.get("HOME")
    if home and (path == "~" or path.startswith("~/")):
        return home + path[1:]
    return os.path.expanduser(path)


# =============================================================================
# Manifest target resolution (restore paths)
# =============================================================================


def resolve_manifest_rel(rel_field: str, sub: str | None) -> str | None:
    """Translate manifest rel ('.' / './x/y' / 'basename') into the
    sub-relative form ('.' for self, 'x/y' for descendants, None to skip).
    """
    rel = rel_field.removeprefix("./")
    if rel_field == ".":
        rel = "."
    if sub is None:
        return rel
    if rel == sub:
        return "."
    if rel.startswith(sub + "/"):
        return rel[len(sub) + 1 :]
    return None


def manifest_target(
    entry: ManifestEntry, outpath: str, is_dir: bool, sub: str | None
) -> tuple[str, str] | None:
    """Resolve the manifest entry to (target_path, sub_rel) or None to skip."""
    rel = resolve_manifest_rel(entry.rel, sub)
    if rel is None:
        return None
    if is_dir:
        target = outpath if rel == "." else os.path.join(outpath, rel)
    else:
        target = outpath
    return target, rel


# =============================================================================
# Manifest-vs-local diff
# =============================================================================


@dataclass
class EntryDiff:
    status: str | None  # None=match, "M"=modified, "D"=missing/wrong-type
    tags: list[str]  # ["mode", "mtime", "size", "link"]
    details: list[str]  # human-readable per-field detail lines

    @property
    def is_match(self) -> bool:
        return self.status is None


def _fmt_mtime(mtime_ns: int) -> str:
    return datetime.datetime.fromtimestamp(mtime_ns / 1_000_000_000).strftime("%Y-%m-%d %H:%M:%S")


def compare_to_local(
    entry: ManifestEntry,
    target: str,
    *,
    window_ns: int,
    use_color: bool = False,
    ignore_dir_mtime: bool = False,
) -> EntryDiff:
    """Manifest record vs local filesystem state.

    The size + mtime part is the same quick check the sync's ManifestFilter
    applies (mtime within ``window_ns``), so `status` and push/pull agree on
    what counts as changed; mode is additionally compared here for the
    metadata report (the sync never transfers over a mode change).
    """
    diff = EntryDiff(status=None, tags=[], details=[])

    try:
        st: os.stat_result | None = os.lstat(target)
    except OSError:
        st = None

    if entry.sym_target is not None:
        if st is None or not stat_mod.S_ISLNK(st.st_mode):
            diff.status = "D"
            return diff
        try:
            loc_link = os.readlink(target)
        except OSError:
            loc_link = ""
        if loc_link != entry.sym_target:
            diff.status = "M"
            diff.tags.append("link")
            diff.details.append(f"link: remote={entry.sym_target} local={loc_link}")
        return diff

    if st is None:
        diff.status = "D"
        return diff

    if stat_mod.S_ISLNK(st.st_mode):
        try:
            is_dir_local = stat_mod.S_ISDIR(os.stat(target).st_mode)
        except OSError:
            is_dir_local = False
    else:
        is_dir_local = stat_mod.S_ISDIR(st.st_mode)

    if entry.is_dir != is_dir_local:
        # A directory where a regular file is expected (or vice versa) is a
        # type change, reported like a missing/wrong-type entry rather than a
        # metadata-only diff.
        diff.status = "D"
        return diff

    if not is_dir_local:
        loc_size = st.st_size
        if entry.size is not None and loc_size != entry.size:
            diff.status = "M"
            diff.tags.append("size")
            if entry.size < loc_size:
                cmp = "<"
                remote_disp = str(entry.size)
                local_disp = _color_wrap(str(loc_size), use_color)
            else:
                cmp = ">"
                remote_disp = _color_wrap(str(entry.size), use_color)
                local_disp = str(loc_size)
            diff_str = _humanize_size_diff(loc_size - entry.size)
            diff.details.append(f"size: remote={remote_disp} {cmp} local={local_disp} ({diff_str})")

    loc_mode = format(stat_mod.S_IMODE(st.st_mode), "o")
    mode_differs = loc_mode != entry.perm_str
    if mode_differs and IS_WINDOWS:
        # Windows-native Python (incl. msys2 UCRT64) reports synthetic modes
        # via os.stat: 0o666 for writable files, 0o444 for read-only - not
        # the Unix permission bits. Only the owner-write bit is meaningful.
        if (entry.perm_bits & 0o200) == (st.st_mode & 0o200):
            mode_differs = False
    if mode_differs:
        diff.status = "M"
        diff.tags.append("mode")
        diff.details.append(f"mode: remote={entry.perm_str} local={loc_mode}")

    # A directory's mtime changes whenever its children are added/removed, so
    # it is noise in `status` and is suppressed there (ignore_dir_mtime=True).
    # The restore path (_manifest_matches_local) keeps the default and still
    # detects dir mtime drift so apply_manifest can restore it.
    if ignore_dir_mtime and is_dir_local:
        return diff

    if entry.mtime_ns is not None and abs(st.st_mtime_ns - entry.mtime_ns) > window_ns:
        loc_mtime_ns = st.st_mtime_ns
        fmt_local = _fmt_mtime(loc_mtime_ns)
        fmt_remote = _fmt_mtime(entry.mtime_ns)
        diff.status = "M"
        diff.tags.append("mtime")
        if entry.mtime_ns < loc_mtime_ns:
            cmp = "<"
            remote_disp = fmt_remote
            local_disp = _color_wrap(fmt_local, use_color)
        else:
            cmp = ">"
            remote_disp = _color_wrap(fmt_remote, use_color)
            local_disp = fmt_local
        diff_str = _humanize_duration((loc_mtime_ns - entry.mtime_ns) // 1_000_000_000)
        diff.details.append(f"mtime: remote={remote_disp} {cmp} local={local_disp} ({diff_str})")

    return diff


# =============================================================================
# Config / Options
# =============================================================================


# Default quick-check mtime window (seconds). 2s absorbs every common
# filesystem's mtime granularity (FAT 2s, exFAT 10ms, NTFS 100ns), so a pull
# onto a coarser filesystem cannot loop on an unrepresentable restored mtime.
DEFAULT_MTIME_WINDOW = 2


@dataclass
class Config:
    profile: str
    prefix: str
    bucket: str
    path_prefix: str
    entries: dict[str, dict[str, Any]]
    # Max entries processed at once under --all (None = one thread per entry,
    # i.e. all at once). Consumed by run_entries, not the store.
    entry_concurrency: int | None = None
    # Quick-check mtime tolerance in seconds (0 = exact st_mtime_ns match).
    mtime_window: int = DEFAULT_MTIME_WINDOW
    store: Boto3S3Store | None = None

    @property
    def window_ns(self) -> int:
        return self.mtime_window * 1_000_000_000


@dataclass
class Opts:
    dryrun: bool = False
    delete: bool = False
    meta_only: bool = False
    data_only: bool = False
    verbose: bool = False
    checksum: bool = False
    outpath: str | None = None
    color: str = "auto"


def _config_int(ns: dict[str, Any], name: str, config_path: str, *, minimum: int) -> int | None:
    """Read an optional integer setting from the config namespace.

    Returns None when unset; dies with a clear message on a non-int (bool
    included, since `True` is an int in Python) or a value below `minimum`.
    """
    value = ns.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        kind = "a positive integer" if minimum > 0 else "a non-negative integer"
        die(f"{name} must be {kind} in {config_path} (got {value!r})")
    return value


def load_config() -> Config:
    config_path = os.environ.get("S3BAK_CONFIG")
    if not config_path:
        config_path = expand_home("~/.config/s3bak/config.py")

    if not os.path.isfile(config_path):
        config_sh = expand_home("~/.config/s3bak/config.sh")
        if os.path.isfile(config_sh):
            die(
                f"found {config_sh} but s3bak now requires config.py\n"
                f"  Please create: {config_path}"
            )
        die(
            f"config file not found: {config_path}\n\n"
            f"Create it with contents like:\n\n"
            f'  profile = "default"\n'
            f'  prefix  = "s3://my-bucket/backup"\n\n'
            f"  entries = {{\n"
            f'      "home-docs": {{"path": "/home/user/Documents"}},\n'
            f"  }}"
        )

    ns: dict[str, Any] = {}
    with open(config_path) as f:
        code = f.read()
    try:
        exec(compile(code, config_path, "exec"), ns)
    except Exception as e:
        die(f"error loading {config_path}: {e}")

    profile: str | None = ns.get("profile")
    prefix: str | None = ns.get("prefix")
    entries: dict[str, dict[str, Any]] | None = ns.get("entries")

    if not profile or not prefix:
        die(f"profile and prefix must be set in {config_path}")
    if not entries:
        die(f"no entries defined in {config_path}")
    if "all" in entries:
        err("warning: entry name 'all' conflicts with --all flag; consider renaming")
    if not prefix.startswith("s3://"):
        die(f"prefix must start with s3:// (got '{prefix}')")

    rest = prefix[5:]
    bucket = rest.split("/", 1)[0]
    path_prefix = rest.split("/", 1)[1].strip("/") if "/" in rest else ""

    if not bucket:
        die(f"could not parse bucket from prefix='{prefix}'")

    # Optional knobs (see config.example.py):
    #   max_concurrency   - parallel S3 transfer threads for cp / sync
    #   compare_workers   - parallel ETag comparisons under --checksum
    #   entry_concurrency - entries processed at once under --all
    #   mtime_window      - quick-check mtime tolerance in seconds (0 = exact)
    max_concurrency = _config_int(ns, "max_concurrency", config_path, minimum=1)
    compare_workers = _config_int(ns, "compare_workers", config_path, minimum=1)
    entry_concurrency = _config_int(ns, "entry_concurrency", config_path, minimum=1)
    mtime_window = _config_int(ns, "mtime_window", config_path, minimum=0)

    cfg = Config(
        profile=profile,
        prefix=prefix,
        bucket=bucket,
        path_prefix=path_prefix,
        entries=entries,
        entry_concurrency=entry_concurrency,
        mtime_window=DEFAULT_MTIME_WINDOW if mtime_window is None else mtime_window,
    )
    cfg.store = Boto3S3Store(
        profile,
        prefix,
        bucket,
        path_prefix,
        max_concurrency=max_concurrency,
        compare_workers=compare_workers,
    )
    return cfg


# =============================================================================
# Remote object store
# =============================================================================


@dataclass
class ObjectMeta:
    """Subset of S3 head-object response that callers use."""

    key: str
    size: int = 0
    etag: str | None = None  # dequoted S3 ETag


@dataclass
class TransferResult:
    """Result of a sync/copy operation that may print CLI output."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class Boto3S3Store:
    """Object storage backend for s3bak, built on the boto3-s3 library.

    Transfers (cp / sync) and listing go through the boto3-s3 ``S3`` API
    in-process; head-object / list-objects-v2 use the underlying boto3 client.
    Endpoint and credentials come from the AWS environment/profile, so the
    MinIO dev profile and real AWS both work without special-casing.

    `rel_key` is a path relative to the configured prefix (e.g. "bin",
    "bin/foo.txt", "bin-manifest.jsonl"). The store internally prepends
    `path_prefix` for boto3 calls and `prefix` (the s3:// URL) for cp / sync.
    """

    def __init__(
        self,
        profile: str,
        prefix: str,
        bucket: str,
        path_prefix: str,
        *,
        max_concurrency: int | None = None,
        compare_workers: int | None = None,
    ):
        self.profile = profile
        self.prefix = prefix  # full s3:// URL
        self.bucket = bucket
        self.path_prefix = path_prefix
        # Concurrency knobs (None = library default). max_concurrency tunes the
        # transfer thread pool (cp / sync); compare_workers tunes the parallel
        # ETag comparison under --checksum (see content_compare).
        self.max_concurrency = max_concurrency
        self.compare_workers = compare_workers

        # Build the S3 orchestrator and ONE boto3 client up front, here in the
        # single-threaded config-load path. boto3 client CONSTRUCTION is not
        # thread-safe, and --all runs entries - each with its own cp / sync,
        # and each sync its own transfer threads - concurrently. Every S3-side
        # location is handed to the library as an S3Storage bound to this one
        # client (see _s3_loc), so no client is ever built lazily on a worker
        # thread; head_object shares it too. A built client is safe to share
        # across threads; only construction races.
        import boto3
        from boto3_s3 import S3

        transfer_config = None
        if max_concurrency is not None:
            from boto3_s3 import TransferConfig

            transfer_config = TransferConfig(max_concurrency=max_concurrency)
        # A configured max_concurrency becomes the default TransferConfig for
        # every cp / sync (the library otherwise uses boto3's default of 10 and
        # never reads ~/.aws/config for it).
        self._s3 = S3(
            session=boto3.Session(profile_name=profile),
            transfer_config=transfer_config,
        )
        self._client = self._s3.client()

    # --- internal ----------------------------------------------------------
    def _s3_loc(self, rel_key: str = "", *, is_dir: bool = False) -> Any:
        """An ``s3://`` location for ``rel_key`` as an ``S3Storage`` bound to
        this store's one shared client.

        Passing an ``S3Storage`` (not a bare URL string) makes cp / sync / ls
        reuse the pre-built client: ``S3.resolve`` returns a ``Storage``
        unchanged, whereas a bare ``s3://`` string is resolved by building a
        fresh client per call - the thread-unsafe path this exists to avoid.
        ``is_dir`` appends the trailing ``/`` a directory sync expects.
        """
        from boto3_s3 import S3Storage

        url = self._s3_url(rel_key)
        if is_dir:
            url += "/"
        return S3Storage(url, client=self._client)

    def content_compare(self) -> Any:
        """The `--checksum` compare strategy: ETag content comparison, parallelized.

        Copies a pair only when the S3 ETag differs from the local file's
        reconstructed ETag - so a same-size, same-mtime content change is
        still transferred, and an mtime-only drift is not. This reads and
        hashes every candidate file locally, which is why it is opt-in; the
        default is the stat-only ManifestFilter. `part_size` is read from the
        same profile the uploads use, so multipart ETags reconstruct to a
        matching value.

        Wrapped in `ParallelCompare` so the per-pair local read + hash runs on
        the sync's thread pool instead of serially on its main thread; the copy
        decision is identical, only faster. `EtagComparison` is thread-safe, as
        `ParallelCompare` requires. The worker count is the configured
        `compare_workers`; when unset (None) the library defaults it to the
        transfer `max_concurrency`, else 10.
        """
        from boto3_s3 import ParallelCompare
        from boto3_s3.etagcompare import EtagComparison

        return ParallelCompare(EtagComparison(self._s3), workers=self.compare_workers)

    def _api_key(self, rel_key: str) -> str:
        return f"{self.path_prefix}/{rel_key}" if self.path_prefix else rel_key

    def _s3_url(self, rel_key: str = "") -> str:
        return f"{self.prefix}/{rel_key}" if rel_key else self.prefix

    def _transfer(self, verbose: bool, label: str, op: Callable[[Any], None]) -> TransferResult:
        """Run a boto3-s3 transfer op, collecting aws-style result lines.

        `op(on_result)` calls the S3 method with the given result callback;
        SUCCEEDED/DRYRUN items become 'upload:'/'download:'/'delete:' stdout
        lines, failures become stderr lines, and any boto3-s3 error sets
        returncode 1. on_result runs on s3transfer worker threads, so a lock
        guards the result lists.
        """
        from boto3_s3 import Boto3S3Error, OpOutcome, TransferType

        if verbose:
            write_stderr(f"+ (boto3-s3) {label}\n")
        lines: list[str] = []
        errs: list[str] = []
        lock = threading.Lock()

        def on_result(r: Any) -> None:
            if r.outcome in (OpOutcome.SUCCEEDED, OpOutcome.DRYRUN):
                pre = "(dryrun) " if r.outcome is OpOutcome.DRYRUN else ""
                if r.transfer_type is TransferType.DELETE:
                    line = f"{pre}delete: {r.key}"
                elif r.src is not None and r.dest is not None:
                    line = f"{pre}{r.transfer_type.value}: {r.src} to {r.dest}"
                else:
                    return
                with lock:
                    lines.append(line)
            elif r.outcome is OpOutcome.FAILED:
                with lock:
                    errs.append(f"{r.key}: {r.error}")
            elif r.outcome is OpOutcome.WARNED:
                _note_warning(f"warning: {r.error}" if r.error else f"warning: skipped {r.key}")
            elif r.outcome is OpOutcome.NOTICE:
                if r.error:
                    write_stderr(f"{r.error}\n")

        try:
            op(on_result)
            rc = 0
        except Boto3S3Error as e:
            rc = 1
            errs.append(str(e))
        return TransferResult(returncode=rc, stdout="\n".join(lines), stderr="\n".join(errs))

    # --- Public API --------------------------------------------------------
    def head_object(self, rel_key: str, *, verbose: bool = False) -> ObjectMeta | None:
        from botocore.exceptions import ClientError

        key = self._api_key(rel_key)
        if verbose:
            write_stderr(f"+ (boto3) head_object s3://{self.bucket}/{key}\n")
        try:
            data = self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code", "") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return ObjectMeta(
            key=rel_key,
            size=int(data.get("ContentLength", 0)),
            etag=(data.get("ETag") or "").strip('"') or None,
        )

    def needs_upload(self, rel_key: str, local_path: str, *, verbose: bool = False) -> bool:
        """True when local_path should be (re)uploaded to rel_key, by content.

        The single-object counterpart of `--checksum`: no stored object (or no
        ETag) means upload; otherwise reuse EtagComparison so the decision
        matches a dir entry's `--checksum` sync - an unchanged file is
        skipped, a same-size/same-mtime content change is not. part_size comes
        from the same profile the upload uses. The default (non---checksum)
        single-file decision is the manifest quick check, not this.
        """
        head = self.head_object(rel_key, verbose=verbose)
        if head is None or not head.etag:
            return True
        from boto3_s3 import LocalFileInfo, S3FileInfo, SyncPair, TransferType
        from boto3_s3.etagcompare import EtagComparison

        # to_native_path(key) == local_path on POSIX (identity) and on Windows
        # (a native path carries no '/'), so the local side resolves to the file.
        pair = SyncPair(
            key=rel_key,
            transfer_type=TransferType.UPLOAD,
            src=LocalFileInfo(key=local_path, size=os.path.getsize(local_path)),
            dest=S3FileInfo(key=rel_key, size=head.size, etag=head.etag),
        )
        return EtagComparison(self._s3)(pair)

    def list_top_level_lines(self, *, verbose: bool = False) -> list[str]:
        from boto3_s3 import FileKind

        if verbose:
            write_stderr(f"+ (boto3-s3) ls {self.prefix}/\n")
        names: list[str] = []
        for info in self._s3.ls(self._s3_loc(is_dir=True), recursive=False):
            if info.kind is FileKind.FILE:
                names.append(info.key.rsplit("/", 1)[-1])
        return names

    def get_object(
        self,
        rel_key: str,
        dest_path: str,
        *,
        verbose: bool = False,
        check: bool = True,
    ) -> bool:
        from boto3_s3 import NotFoundError

        if verbose:
            write_stderr(f"+ (boto3-s3) cp {self._s3_url(rel_key)} {dest_path}\n")
        try:
            self._s3.cp(self._s3_loc(rel_key), dest_path)
            return True
        except NotFoundError:
            # A genuinely-absent object is "not present"; other errors
            # (access denied, transport, config) propagate to run().
            return False

    def stream_object_to_stdout(self, rel_key: str, *, verbose: bool = False) -> int:
        from boto3_s3 import Boto3S3Error, StdioStorage

        if verbose:
            write_stderr(f"+ (boto3-s3) cp {self._s3_url(rel_key)} -\n")
        try:
            self._s3.cp(self._s3_loc(rel_key), StdioStorage())
            return 0
        except Boto3S3Error as e:
            write_stderr(f"{e}\n")
            return 1

    def sync_down(
        self,
        rel_prefix: str,
        dest_dir: str,
        *,
        compare: Any = None,
        verbose: bool = False,
    ) -> TransferResult:
        src = self._s3_loc(rel_prefix, is_dir=True)
        return self._transfer(
            verbose,
            f"sync {src} {dest_dir}/",
            lambda cb: self._s3.sync(
                src,
                f"{dest_dir}/",
                compare=compare,
                follow_symlinks=False,
                on_result=cb,
            ),
        )

    def put_file(self, rel_key: str, src_path: str, *, verbose: bool = False) -> None:
        """Upload a local file without result-line collection (manifests).
        Errors surface as Boto3S3Error, handled by run()."""
        if verbose:
            write_stderr(f"+ (boto3-s3) cp {src_path} {self._s3_url(rel_key)}\n")
        self._s3.cp(src_path, self._s3_loc(rel_key))

    def put_object(self, rel_key: str, src_path: str, *, verbose: bool = False) -> TransferResult:
        dst = self._s3_loc(rel_key)
        return self._transfer(
            verbose,
            f"cp {src_path} {self._s3_url(rel_key)}",
            lambda cb: self._s3.cp(src_path, dst, on_result=cb),
        )

    def sync_up(
        self,
        src_dir: str,
        rel_prefix: str,
        *,
        file_filter: Any = None,
        compare: Any = None,
        delete: bool = False,
        dryrun: bool = False,
        verbose: bool = False,
    ) -> TransferResult:
        """`file_filter` is the excludes predicate (manifest.exclude_filter):
        the same entry-rooted semantics the manifest walk applies, so the data
        sync and the manifest can never disagree on what an exclude means."""
        dst = self._s3_loc(rel_prefix, is_dir=True)
        return self._transfer(
            verbose,
            f"sync {src_dir} {dst}",
            # follow_symlinks=False: symlinks are not uploaded as data; the
            # manifest records them and apply_manifest recreates them on restore.
            lambda cb: self._s3.sync(
                src_dir,
                dst,
                delete=delete,
                dryrun=dryrun,
                filter=file_filter,
                compare=compare,
                follow_symlinks=False,
                on_result=cb,
            ),
        )


# =============================================================================
# Tree iteration
# =============================================================================


def iter_local_tree(outpath: str, excludes: list[str]) -> Iterator[tuple[str, bool]]:
    """Walk local tree yielding (rel_without_dot_slash, is_dir)."""
    prune_patterns, skip_patterns = split_excludes(excludes)

    for dirpath, dirnames, filenames in os.walk(outpath, followlinks=False):
        rel_dir = os.path.relpath(dirpath, outpath)
        rel_prefix = "./" if rel_dir == "." else f"./{rel_dir}/"

        dirnames.sort()
        filenames.sort()

        to_remove: list[str] = []
        for d in dirnames:
            rel = f"{rel_prefix}{d}"
            if any(path_match(rel, p) for p in prune_patterns):
                to_remove.append(d)
                continue
            yield rel[2:], True  # strip "./"
        for d in to_remove:
            dirnames.remove(d)

        for f in filenames:
            rel = f"{rel_prefix}{f}"
            if any(path_match(rel, p) for p in skip_patterns):
                continue
            yield rel[2:], False


# =============================================================================
# Manifest generation
# =============================================================================


def write_manifest_to_aws(
    cfg: Config, entry: str, target: str, excludes: list[str], verbose: bool
) -> None:
    """Walk `target` in S3 key order, stream the v3 manifest to a temp file,
    and upload it."""
    key = manifest.manifest_key(entry)
    write_stderr(f"Updating {cfg.prefix}/{key}\n")

    fd, tmp = tempfile.mkstemp(suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if os.path.isdir(target):
                manifest.write_manifest(f, manifest.walk_tree(target, excludes))
            else:
                st = os.lstat(target)
                sym = os.readlink(target) if stat_mod.S_ISLNK(st.st_mode) else None
                manifest.write_manifest(f, [(os.path.basename(target), st, sym)])
        assert cfg.store is not None
        cfg.store.put_file(key, tmp, verbose=verbose)
    finally:
        os.unlink(tmp)


def upload_manifest(cfg: Config, entry: str, target: str, excludes: list[str], opts: Opts) -> int:
    """Write the manifest to S3, then run the entry's post_hook."""
    post_hook: str | None = cfg.entries[entry].get("post_hook")

    if opts.dryrun:
        print(f"(dryrun) would update manifest: {manifest.manifest_key(entry)}")
        if post_hook:
            print(f"(dryrun) would run post_hook: {post_hook}")
        return 0

    write_manifest_to_aws(cfg, entry, target, excludes, opts.verbose)

    return _run_post_hook(post_hook, opts)


def patch_manifest_subtree(
    cfg: Config,
    entry: str,
    target_root: str,
    sub: str,
    excludes: list[str],
    opts: Opts,
) -> None:
    """Download the manifest, replace the records under `sub`, and re-upload.

    target_root/sub may be a file, a symlink, or a directory. If it does not
    exist locally, the records under `sub` are simply removed. Old and new
    records are both in sort-key order, so this is a streaming merge
    (manifest.write_patched), not a read-all + sort.
    """
    key = manifest.manifest_key(entry)
    if opts.dryrun:
        print(f"(dryrun) would patch manifest: {key} (sub={sub})")
        return

    fd_old, old_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd_old)
    fd_new, new_path = tempfile.mkstemp(suffix=".jsonl")
    try:
        have_old = download_manifest(cfg, entry, old_path, opts.verbose)
        local_sub = os.path.join(target_root, sub)
        new_entries: Iterator[tuple[str, os.stat_result, str | None]] = iter(())
        if os.path.lexists(local_sub):
            new_entries = manifest.iter_subtree(local_sub, sub, excludes)
        if not have_old:
            # First-ever manifest for this entry, born from a sub-path push:
            # record the entry root too, so the manifest keeps the dir-entry
            # shape ('.'-rooted) and the root's metadata restores on pull.
            root_record = (".", os.lstat(target_root), None)
            new_entries = itertools.chain([root_record], new_entries)
        with os.fdopen(fd_new, "w", encoding="utf-8") as out:
            manifest.write_patched(out, old_path if have_old else None, sub, new_entries)
        write_stderr(f"Updating {cfg.prefix}/{key}\n")
        assert cfg.store is not None
        cfg.store.put_file(key, new_path, verbose=opts.verbose)
    finally:
        os.unlink(old_path)
        os.unlink(new_path)


# =============================================================================
# Manifest application (restore metadata)
# =============================================================================


def _windows_collect_writable_prep(
    outpath: str, is_dir: bool, manifest_path: str, sub: str | None
) -> list[tuple[str, int]]:
    # Windows only. Walk the manifest, find existing local files that are:
    #   - regular files (not dir / not symlink)
    #   - read-only (owner write bit clear)
    # Temporarily add owner-write so `boto3-s3 sync`/`cp` can overwrite them.
    # Every read-only file is prepped, not just quick-check failures: the
    # sync's copy decision can be broader than the local quick check (remote
    # size drift; any content difference under --checksum), and prep must
    # never under-approximate what the sync may overwrite. apply_manifest
    # re-applies the recorded modes afterwards (or _windows_restore_modes on
    # the failure/--data-only paths). Returns [(path, original_mode), ...].
    targets: list[tuple[str, int]] = []
    try:
        for entry in manifest.iter_manifest(manifest_path):
            if entry.sym_target is not None or not entry.is_file:
                continue
            res = manifest_target(entry, outpath, is_dir, sub)
            if res is None:
                continue
            target, _rel = res
            try:
                st = os.lstat(target)
            except OSError:
                continue
            if not stat_mod.S_ISREG(st.st_mode):
                continue
            if st.st_mode & stat_mod.S_IWRITE:
                continue
            try:
                os.chmod(target, st.st_mode | stat_mod.S_IWRITE)
            except OSError:
                continue
            targets.append((target, st.st_mode))
    except OSError:
        pass
    return targets


def _windows_restore_modes(targets: list[tuple[str, int]]) -> None:
    for target, original_mode in targets:
        try:
            os.chmod(target, original_mode)
        except OSError:
            continue


def _apply_meta(target: str, mode: int, mtime_ns: int | None) -> bool:
    ok = True
    if mtime_ns is not None:
        try:
            os.utime(target, ns=(mtime_ns, mtime_ns))
        except PermissionError as e:
            err(f"utime failed (not owner?): {target}: {e}")
            ok = False
    try:
        os.chmod(target, mode)
    except PermissionError as e:
        err(f"chmod failed (not owner?): {target}: {e}")
        ok = False
    return ok


def apply_manifest(
    cfg: Config,
    entry: str,
    outpath: str,
    is_dir: bool,
    manifest_path: str,
    sub: str | None = None,
    verbose: bool = False,
) -> int:
    deferred_dirs: list[tuple[str, int, int | None]] = []
    errors = 0

    for m_entry in manifest.iter_manifest(manifest_path):
        res = manifest_target(m_entry, outpath, is_dir, sub)
        if res is None:
            continue
        target, _rel = res
        mode = m_entry.perm_bits

        if m_entry.sym_target is not None:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            # Clear whatever is already there. islink first, so a symlink is
            # removed as a link (never recursing into its target); a real dir
            # (e.g. left by an older follow-symlinks backup) is removed wholesale.
            if os.path.islink(target) or os.path.isfile(target):
                os.remove(target)
            elif os.path.isdir(target):
                shutil.rmtree(target)
            os.symlink(m_entry.sym_target, target)
            write_output(f"{target} -> {m_entry.sym_target}\n")
            continue

        # Symlinks are handled above; the recorded type distinguishes a
        # directory (including an empty one, which has no S3 object) from a
        # regular file that was recorded but never uploaded.
        if is_dir and m_entry.is_dir:
            if not os.path.exists(target):
                os.makedirs(target, exist_ok=True)
            deferred_dirs.append((target, mode, m_entry.mtime_ns))
            continue

        if not os.path.exists(target):
            err(f"expected file missing (sync did not place it): {target}")
            errors += 1
            continue

        write_output(f"{m_entry.perm_str} {target}\n")
        if not _apply_meta(target, mode, m_entry.mtime_ns):
            errors += 1

    deferred_dirs.sort(key=lambda x: x[0], reverse=True)
    for target, mode, mtime_ns in deferred_dirs:
        write_output(f"{format(mode, 'o')} {target}\n")
        if not _apply_meta(target, mode, mtime_ns):
            errors += 1

    return 1 if errors else 0


# =============================================================================
# S3 download helpers
# =============================================================================


def download_manifest(cfg: Config, entry: str, dest: str, verbose: bool = False) -> bool:
    assert cfg.store is not None
    return cfg.store.get_object(
        manifest.manifest_key(entry),
        dest,
        verbose=verbose,
        check=False,
    )


def _sync_compare(
    cfg: Config, opts: Opts, manifest_path: str | None, sub: str | None = None
) -> Any:
    """Build the sync `compare=` strategy: the stat-only ManifestFilter by
    default, EtagComparison under --checksum. `manifest_path=None` (nothing on
    S3 yet) yields an empty filter, so every pair transfers - which is also
    the entire v2->v3 migration story: the first push re-uploads everything
    and writes the v3 manifest."""
    assert cfg.store is not None
    if opts.checksum:
        return cfg.store.content_compare()
    entries: dict[str, ManifestEntry] = {}
    if manifest_path is not None:
        entries = manifest.load_map(manifest_path, sub=sub)
    return manifest.ManifestFilter(entries, window_ns=cfg.window_ns)


def _print_transfer_lines(stdout: str) -> bool:
    """Print the transfer-result lines, skipping progress noise. Returns True
    if any transfer line was printed.
    """
    if not stdout:
        return False
    changed = False
    for line in stdout.replace("\r", "\n").splitlines():
        line = line.strip()
        if (
            line
            and not line.startswith("Completed ")
            and not line.startswith("warning: Skipping file")
        ):
            changed = True
            write_output(f"{line}\n")
    return changed


def download_from_s3(
    cfg: Config,
    entry: str,
    outpath: str,
    is_dir: bool,
    verbose: bool,
    sub: str | None = None,
    compare: Any = None,
) -> tuple[int, bool]:
    assert cfg.store is not None
    rel = f"{entry}/{sub}" if sub else entry

    if is_dir:
        result = cfg.store.sync_down(rel, outpath, compare=compare, verbose=verbose)
        if result.returncode != 0:
            if result.stderr:
                write_stderr(result.stderr)
            return result.returncode, False
        return 0, _print_transfer_lines(result.stdout)

    # Single file: cp always transfers (we only reach here on a manifest
    # mismatch), so a successful download counts as changed -> apply_manifest
    # runs and restores mode/mtime. Matters on Windows, where apply_manifest is
    # skipped when nothing changed.
    parent = os.path.dirname(outpath)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if not cfg.store.get_object(rel, outpath, verbose=verbose):
        return 1, False
    return 0, True


# =============================================================================
# delete_extra_files
# =============================================================================


def delete_extra_files(
    outpath: str,
    check_only: bool,
    remote_files: dict[str, int],
    excludes: list[str],
) -> bool:
    extras: list[tuple[str, bool]] = []
    for rel, is_dir_entry in iter_local_tree(outpath, excludes):
        if not rel or rel == ".":
            continue
        if rel not in remote_files:
            extras.append((os.path.join(outpath, rel), is_dir_entry))

    if not extras:
        return False

    extras.sort(key=lambda x: x[0], reverse=True)

    for path, is_dir_entry in extras:
        if check_only:
            write_output(f"A {path}\n")
        else:
            try:
                if is_dir_entry and not os.path.islink(path):
                    os.rmdir(path)
                else:
                    # Files and symlinks (incl. symlinks to directories, which
                    # iter_local_tree reports as is_dir) are unlinked.
                    os.remove(path)
                write_output(f"delete: {path}\n")
            except OSError:
                pass

    return True


# =============================================================================
# Commands
# =============================================================================


def _filter_aws_output(raw: str) -> str:
    filtered: list[str] = []
    for line in raw.replace("\r", "\n").splitlines():
        line = line.strip()
        if (
            line
            and not line.startswith("Completed ")
            and not line.startswith("warning: Skipping file")
        ):
            filtered.append(line)
    return "\n".join(filtered)


def _run_post_hook(post_hook: str | None, opts: Opts) -> int:
    if not post_hook:
        return 0
    if opts.dryrun:
        print(f"(dryrun) would run post_hook: {post_hook}")
        return 0
    if opts.verbose:
        write_stderr(f"+ {post_hook}\n")
    rc = subprocess.run(["bash", "-c", post_hook]).returncode
    if rc != 0:
        err(f"post_hook failed (exit {rc}): {post_hook}")
    return rc


def _push_sub(
    cfg: Config,
    entry: str,
    post_hook: str | None,
    target_root: str,
    sub: str,
    excludes: list[str],
    opts: Opts,
) -> int:
    local_sub = os.path.join(target_root, sub)
    sub_rel = f"{entry}/{sub}"
    s3_sub_path = f"{cfg.prefix}/{sub_rel}"

    if not os.path.lexists(local_sub):
        err(f"local path does not exist: {local_sub}")
        return 1

    if opts.meta_only:
        patch_manifest_subtree(cfg, entry, target_root, sub, excludes, opts)
        return _run_post_hook(post_hook, opts)

    assert cfg.store is not None
    st = os.lstat(local_sub)

    if stat_mod.S_ISLNK(st.st_mode):
        # symlink: upload nothing, just update manifest line.
        pass
    elif os.path.isdir(local_sub):
        fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            # --checksum ignores the manifest entirely; skip the download.
            have_manifest = not opts.checksum and download_manifest(
                cfg, entry, manifest_path, opts.verbose
            )
            compare = _sync_compare(cfg, opts, manifest_path if have_manifest else None, sub=sub)
            result = cfg.store.sync_up(
                local_sub,
                sub_rel,
                file_filter=manifest.exclude_filter(excludes, sub=sub) if excludes else None,
                compare=compare,
                delete=opts.delete,
                dryrun=opts.dryrun,
                verbose=opts.verbose,
            )
        finally:
            os.unlink(manifest_path)
        if result.returncode != 0:
            write_output(result.stdout)
            if result.stderr:
                write_stderr(result.stderr)
            return result.returncode
        filtered = _filter_aws_output(result.stdout)
        if filtered:
            write_output(f"{filtered}\n")
    else:
        # Regular file: an explicit sub-path push always uploads.
        if opts.dryrun:
            print(f"(dryrun) upload: {local_sub} -> {s3_sub_path}")
        else:
            result = cfg.store.put_object(sub_rel, local_sub, verbose=opts.verbose)
            if result.returncode != 0:
                write_output(result.stdout)
                if result.stderr:
                    write_stderr(result.stderr)
                return result.returncode
            filtered = _filter_aws_output(result.stdout)
            if filtered:
                write_output(f"{filtered}\n")

    if not opts.data_only:
        patch_manifest_subtree(cfg, entry, target_root, sub, excludes, opts)
    return _run_post_hook(post_hook, opts)


def _single_file_needs_upload(cfg: Config, entry: str, target: str, opts: Opts) -> bool:
    """The single-file counterpart of the sync compare: quick check against
    the entry's one-record manifest (or EtagComparison under --checksum).

    Upload unless the manifest holds a regular-file record for exactly this
    basename (a stale dir-shaped manifest, e.g. from an entry that used to be
    a directory, must not suppress the upload), the local stat matches it, AND
    the data object actually exists on S3 - a `--meta-only` push or an S3-side
    delete leaves a manifest with no object behind it, which only this
    head-object probe can see (a dir entry self-heals via the sync listing;
    a single file has no listing)."""
    assert cfg.store is not None
    if opts.checksum:
        return cfg.store.needs_upload(entry, target, verbose=opts.verbose)
    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        if not download_manifest(cfg, entry, manifest_path, opts.verbose):
            return True
        st = os.lstat(target)
        basename = os.path.basename(target)
        for m in manifest.iter_manifest(manifest_path):
            if m.rel == basename and m.sym_target is None and m.is_file:
                if not m.matches_stat(st, cfg.window_ns):
                    return True
                return cfg.store.head_object(entry, verbose=opts.verbose) is None
        return True
    finally:
        os.unlink(manifest_path)


def cmd_push(cfg: Config, entry: str, opts: Opts, sub: str | None = None) -> int:
    entry_cfg = cfg.entries.get(entry)
    if not entry_cfg:
        err(f"no such entry: {entry}")
        return 1
    target: str = entry_cfg["path"]
    target_root = _normalize_local_path(target)
    if sub is None:
        if not os.path.lexists(target):
            err(f"target does not exist: {target}")
            return 1
        mode = os.lstat(target).st_mode
        if stat_mod.S_ISLNK(mode):
            err(f"entry path is a symlink, which is not allowed as an entry: {target}")
            return 1
        if not (stat_mod.S_ISREG(mode) or stat_mod.S_ISDIR(mode)):
            err(f"entry path must be a regular file or directory: {target}")
            return 1

    excludes: list[str] = entry_cfg.get("excludes", [])

    # Hook contract: pre_hook runs before every push attempt. post_hook is
    # deliberately asymmetric - it runs only after a push that did work, i.e.
    # that transferred data and/or refreshed the manifest (see upload_manifest,
    # the data-only branch below, and _push_sub), or whenever --meta-only is
    # given (which always refreshes the manifest and runs the hook). A pure
    # no-op push runs no post_hook on purpose, so side-effecting hooks (e.g.
    # rclone) do not fire when nothing changed; use --meta-only to run the hook
    # on demand. By design, not a bug.
    pre_hook: str | None = entry_cfg.get("pre_hook")
    if pre_hook:
        if opts.dryrun:
            print(f"(dryrun) would run pre_hook: {pre_hook}")
        else:
            if opts.verbose:
                write_stderr(f"+ {pre_hook}\n")
            st = subprocess.run(["bash", "-c", pre_hook]).returncode
            if st != 0:
                return st

    if sub is not None:
        post_hook_sub: str | None = entry_cfg.get("post_hook")
        return _push_sub(cfg, entry, post_hook_sub, target_root, sub, excludes, opts)

    if entry.endswith(".git") and opts.meta_only:
        err(f"skipping manifest for {entry} (.git suffix convention)")
        return 0

    # --meta-only refreshes the manifest and runs the post_hook even with no data
    # change: the supported way to re-run the post_hook on demand (intended).
    if opts.meta_only:
        return upload_manifest(cfg, entry, target, excludes, opts)

    results = ""
    assert cfg.store is not None

    if os.path.isdir(target):
        fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            # --checksum ignores the manifest entirely; skip the download.
            have_manifest = not opts.checksum and download_manifest(
                cfg, entry, manifest_path, opts.verbose
            )
            compare = _sync_compare(cfg, opts, manifest_path if have_manifest else None)
            result = cfg.store.sync_up(
                target,
                entry,
                file_filter=manifest.exclude_filter(excludes) if excludes else None,
                compare=compare,
                delete=True,
                dryrun=opts.dryrun,
                verbose=opts.verbose,
            )
        finally:
            os.unlink(manifest_path)
        if result.returncode != 0:
            write_output(result.stdout)
            if result.stderr:
                write_stderr(result.stderr)
            return result.returncode
        results = _filter_aws_output(result.stdout)
    elif _single_file_needs_upload(cfg, entry, target, opts):
        # Single-file entry that fails the quick check against its manifest
        # (or the --checksum ETag comparison), or was never pushed: upload it.
        if opts.dryrun:
            # Set results only; the shared writer below emits it (and the truthy
            # results drives the dryrun manifest line). Printing here too would
            # double the line.
            results = f"(dryrun) upload: {target} -> {cfg.prefix}/{entry}"
        else:
            result = cfg.store.put_object(entry, target, verbose=opts.verbose)
            if result.returncode != 0:
                write_output(result.stdout)
                if result.stderr:
                    write_stderr(result.stderr)
                return result.returncode
            results = _filter_aws_output(result.stdout)

    if results:
        write_output(f"{results}\n")

    # Refresh the manifest only when file data was actually transferred. The
    # default compare is the manifest quick check (size + mtime within the
    # window), so a change it cannot see - mode/owner/group, or an mtime drift
    # inside the window - transfers nothing and so does not refresh the
    # manifest; `status` keeps showing that diff until you run `push
    # --meta-only` (handled above). Deliberate spec choice, not a bug. Note
    # --meta-only asserts "S3 matches local" without making it true: any
    # never-pushed local edit becomes invisible to the quick check afterwards,
    # so it is a metadata refresh, never a substitute for a real push.
    if results and not opts.data_only:
        st = upload_manifest(cfg, entry, target, excludes, opts)
        if st != 0:
            return st

    if results and opts.data_only:
        post_hook: str | None = entry_cfg.get("post_hook")
        return _run_post_hook(post_hook, opts)

    return 0


def _entry_kind_from_manifest(manifest_path: str) -> str:
    """Return 'dir' or 'file' from the first record's rel shape: a dir-entry
    manifest is '.'-rooted ('.' / './...'), a single-file manifest holds one
    bare basename. Shape, not type bits, so a dir-entry manifest whose first
    record happens to be a file (possible after a sub-path push created it)
    still classifies as a directory entry. Returns 'file' for an empty
    manifest so callers fail fast."""
    for entry in manifest.iter_manifest(manifest_path):
        return "dir" if entry.rel == "." or entry.rel.startswith("./") else "file"
    return "file"


def _sub_kind_from_manifest(manifest_path: str, sub: str) -> str:
    """Return 'file', 'dir', 'symlink', or 'missing' for sub from the manifest.

    A descendant under sub proves it is a directory; otherwise the recorded
    type decides (an empty directory has no descendants and no S3 object, but
    its type is recorded). 'symlink' covers every non-file, non-dir record:
    there is no data object to download - apply_manifest recreates it from
    the manifest alone."""
    self_entry: ManifestEntry | None = None
    for entry in manifest.iter_manifest(manifest_path):
        rel = entry.rel.removeprefix("./")
        if rel == sub:
            self_entry = entry
        elif rel.startswith(sub + "/"):
            return "dir"
    if self_entry is None:
        return "missing"
    if self_entry.is_dir:
        return "dir"
    return "file" if self_entry.is_file else "symlink"


def _manifest_matches_local(
    manifest_path: str, outpath: str, is_dir: bool, sub: str | None, window_ns: int
) -> bool:
    """True iff every manifest record matches the local filesystem.

    Returning True means 'boto3-s3 sync' would copy nothing AND apply_manifest
    would change nothing - so both can be skipped.
    """
    for entry in manifest.iter_manifest(manifest_path):
        res = manifest_target(entry, outpath, is_dir, sub)
        if res is None:
            continue
        target, _rel = res
        if not compare_to_local(entry, target, window_ns=window_ns).is_match:
            return False
    return True


def cmd_pull(cfg: Config, entry: str, opts: Opts, sub: str | None = None) -> int:
    outpath: str | None = opts.outpath
    entry_cfg = cfg.entries.get(entry)
    if outpath is None:
        if not entry_cfg:
            err(f"no such entry in config: {entry}")
            err("use -o <path> to specify the output path")
            return 1
        base_path: str = entry_cfg["path"]
        outpath = os.path.join(base_path, sub) if sub else base_path

    if outpath.endswith("/"):
        tail = sub if sub else entry
        outpath = os.path.join(outpath, tail)

    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        # 1. Fetch the manifest first; its content tells us file/dir
        #    without any extra head-object calls.
        if not download_manifest(cfg, entry, manifest_path, opts.verbose):
            if sub is not None:
                err(f"not found on S3: {entry}/{sub}")
            else:
                err(f"entry not found on S3: {entry}")
            return 1

        entry_is_dir = _entry_kind_from_manifest(manifest_path) == "dir"

        if sub is not None:
            if not entry_is_dir:
                err(f"sub path not allowed for single-file entry: {entry}")
                return 1
            kind = _sub_kind_from_manifest(manifest_path, sub)
            if kind == "missing":
                err(f"not found on S3: {entry}/{sub}")
                return 1
            is_dir = kind == "dir"
            has_data = kind != "symlink"  # a symlink has no data object
        else:
            is_dir = entry_is_dir
            has_data = True

        # 2. If everything in the manifest already matches local, both
        #    the s3 sync/cp and apply_manifest are no-ops. Skip them. Not
        #    under --checksum: this gate is the same stat quick check whose
        #    blind spot --checksum exists to cover, so it must not stand
        #    between the user and the content comparison.
        manifest_matches = _manifest_matches_local(
            manifest_path, outpath, is_dir, sub, cfg.window_ns
        )
        if manifest_matches and not opts.checksum:
            if not opts.meta_only and opts.delete and is_dir:
                excludes: list[str] = entry_cfg.get("excludes", []) if entry_cfg else []
                remote_files = _read_manifest_files(manifest_path, sub=sub)
                delete_extra_files(outpath, False, remote_files, excludes)
            return 0

        # 3. Normal path: prep, then sync (dir) or cp (file).
        prep: list[tuple[str, int]] = []
        if IS_WINDOWS and not opts.meta_only:
            prep = _windows_collect_writable_prep(outpath, is_dir, manifest_path, sub)

        changed = False
        if not opts.meta_only and has_data:
            # The compare only matters for the dir sync; a single-file cp
            # always transfers (we only reach it on a manifest mismatch).
            compare = _sync_compare(cfg, opts, manifest_path, sub=sub) if is_dir else None
            rc, changed = download_from_s3(
                cfg, entry, outpath, is_dir, opts.verbose, sub=sub, compare=compare
            )
            if rc != 0:
                if IS_WINDOWS:
                    _windows_restore_modes(prep)
                return rc

        # 4. Apply manifest metadata (mode, mtime, symlinks): objectless or
        #    metadata-only diffs (empty dirs, symlinks, mode/mtime) have nothing
        #    to download yet still need applying. apply_manifest sets the modes
        #    itself, so the writable prep needs no separate restore. Skipped
        #    with --data-only, and after a --checksum pass over an
        #    already-clean tree (nothing transferred, metadata matches).
        if opts.data_only:
            if IS_WINDOWS and not opts.meta_only:
                _windows_restore_modes(prep)
            st = 0
        elif manifest_matches and not changed:
            if IS_WINDOWS and not opts.meta_only:
                _windows_restore_modes(prep)
            st = 0
        else:
            st = apply_manifest(
                cfg,
                entry,
                outpath,
                is_dir,
                manifest_path,
                sub=sub,
                verbose=opts.verbose,
            )

        if not opts.meta_only and opts.delete and is_dir:
            excludes = entry_cfg.get("excludes", []) if entry_cfg else []
            remote_files = _read_manifest_files(manifest_path, sub=sub)
            delete_extra_files(outpath, False, remote_files, excludes)

        return st
    finally:
        os.unlink(manifest_path)


def _read_manifest_files(manifest_path: str, sub: str | None = None) -> dict[str, int]:
    remote_files: dict[str, int] = {}
    for entry in manifest.iter_manifest(manifest_path):
        rel = entry.rel.removeprefix("./")
        if sub is not None:
            if rel == sub:
                continue
            if not rel.startswith(sub + "/"):
                continue
            rel = rel[len(sub) + 1 :]
        remote_files[rel] = 1
    return remote_files


def cmd_show(cfg: Config, entry: str, opts: Opts, file: str | None = None) -> int:
    if entry not in cfg.entries:
        err(f"no such entry: {entry}")
        return 1

    if file:
        file = file.lstrip("./")
        rel = f"{entry}/{file}"
    else:
        rel = entry

    assert cfg.store is not None
    return cfg.store.stream_object_to_stdout(rel, verbose=opts.verbose)


_ANSI_GREEN = "\033[1;32m"
_ANSI_RESET = "\033[0m"


def _resolve_use_color(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _color_wrap(s: str, use_color: bool) -> str:
    return f"{_ANSI_GREEN}{s}{_ANSI_RESET}" if use_color else s


def _diff_color_flag(color_mode: str) -> str:
    return f"--color={'always' if _resolve_use_color(color_mode) else 'never'}"


def _humanize_size_diff(diff_bytes: int) -> str:
    diff = abs(diff_bytes)
    if diff < 1024:
        return f"+{diff} bytes"
    for unit, threshold in (
        ("TB", 1024**4),
        ("GB", 1024**3),
        ("MB", 1024**2),
        ("KB", 1024),
    ):
        if diff >= threshold:
            return f"+{diff} bytes (+{diff / threshold:.2f} {unit})"
    return f"+{diff} bytes"


def _humanize_duration(diff_sec: int) -> str:
    diff = abs(diff_sec)
    if diff == 0:
        return "+0s"
    days, rem = divmod(diff, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds:
        parts.append(f"{seconds}s")
    return "+" + " ".join(parts[:2])


def format_diff_block(diff: EntryDiff, target: str, verbose: bool) -> str | None:
    if diff.is_match:
        return None
    if diff.status == "D":
        block = f"D {target}\n"
    else:
        block = f"M {target}\t{', '.join(diff.tags)}\n"
    if verbose:
        for d in diff.details:
            block += f"      {d}\n"
    return block


def check_metadata(
    target: str,
    entry: ManifestEntry,
    verbose: bool,
    window_ns: int,
    use_color: bool = False,
    ignore_dir_mtime: bool = False,
) -> str | None:
    diff = compare_to_local(
        entry, target, window_ns=window_ns, use_color=use_color, ignore_dir_mtime=ignore_dir_mtime
    )
    return format_diff_block(diff, target, verbose)


def cmd_status(cfg: Config, entry: str, opts: Opts, sub: str | None = None) -> int:
    entry_cfg = cfg.entries.get(entry)
    if not entry_cfg:
        err(f"no such entry: {entry}")
        return 1
    base_path: str = entry_cfg["path"]
    outpath = os.path.join(base_path, sub) if sub else base_path

    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        if not download_manifest(cfg, entry, manifest_path, opts.verbose):
            return 1
        if sub is not None and _sub_kind_from_manifest(manifest_path, sub) == "missing":
            err(f"not found on S3: {entry}/{sub}")
            return 1

        is_dir = os.path.isdir(outpath)
        excludes: list[str] = entry_cfg.get("excludes", [])
        use_color = _resolve_use_color(opts.color)

        remote_files: dict[str, int] = {}
        for entry_obj in manifest.iter_manifest(manifest_path):
            res = manifest_target(entry_obj, outpath, is_dir, sub)
            if res is None:
                continue
            target, rel = res
            if is_dir:
                remote_files[rel] = 1
            block = check_metadata(
                target,
                entry_obj,
                opts.verbose,
                cfg.window_ns,
                use_color=use_color,
                ignore_dir_mtime=True,
            )
            if block:
                write_output(block)

        if is_dir:
            delete_extra_files(outpath, True, remote_files, excludes)

        return 0
    finally:
        os.unlink(manifest_path)


def diff_single_file(cfg: Config, rel_key: str, label: str, localfile: str, opts: Opts) -> int:
    fd, tmppath = tempfile.mkstemp()
    os.close(fd)
    try:
        assert cfg.store is not None
        if not cfg.store.get_object(rel_key, tmppath, verbose=opts.verbose, check=True):
            return 1
        cmd = [
            "diff",
            _diff_color_flag(opts.color),
            "-ru",
            tmppath,
            localfile,
            "--label",
            f"a/{label}",
            "--label",
            f"b/{label}",
        ]
        echo_command(opts.verbose, cmd)
        result = subprocess.run(cmd)
        return 0 if result.returncode == 0 else 1
    finally:
        os.unlink(tmppath)


def diff_backup(cfg: Config, entry: str, outpath: str, opts: Opts) -> int:
    excludes: list[str] = cfg.entries[entry].get("excludes", [])
    tmpdir = tempfile.mkdtemp()
    has_diff = 0

    try:
        assert cfg.store is not None
        result = cfg.store.sync_down(entry, tmpdir, verbose=opts.verbose)
        if result.returncode != 0:
            if result.stderr:
                write_stderr(result.stderr)
            return result.returncode

        backup_files: set[str] = set()
        for dirpath, _, filenames in os.walk(tmpdir):
            for f in sorted(filenames):
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, tmpdir)
                backup_files.add(rel)
                local = os.path.join(outpath, rel)
                if not os.path.exists(local):
                    cmd = [
                        "diff",
                        _diff_color_flag(opts.color),
                        "-u",
                        full,
                        "/dev/null",
                        "--label",
                        f"a/{rel}",
                        "--label",
                        f"b/{rel}",
                    ]
                    echo_command(opts.verbose, cmd)
                    subprocess.run(cmd)
                    has_diff = 1
                    continue
                cmd = [
                    "diff",
                    _diff_color_flag(opts.color),
                    "-u",
                    full,
                    local,
                    "--label",
                    f"a/{rel}",
                    "--label",
                    f"b/{rel}",
                ]
                echo_command(opts.verbose, cmd)
                r = subprocess.run(cmd)
                if r.returncode != 0:
                    has_diff = 1

        for rel, is_dir_entry in iter_local_tree(outpath, excludes):
            if is_dir_entry:
                continue
            if not os.path.isfile(os.path.join(outpath, rel)):
                continue
            if rel in backup_files:
                continue
            cmd = [
                "diff",
                _diff_color_flag(opts.color),
                "-u",
                "/dev/null",
                os.path.join(outpath, rel),
                "--label",
                f"a/{rel}",
                "--label",
                f"b/{rel}",
            ]
            echo_command(opts.verbose, cmd)
            subprocess.run(cmd)
            has_diff = 1

        return has_diff
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def cmd_diff(cfg: Config, entry: str, opts: Opts, file: str | None = None) -> int:
    entry_cfg = cfg.entries.get(entry)
    if not entry_cfg:
        err(f"no such entry: {entry}")
        return 1
    outpath: str = entry_cfg["path"]

    if file:
        file = file.lstrip("./")
        return diff_single_file(
            cfg,
            f"{entry}/{file}",
            f"{entry}/{file}",
            f"{outpath}/{file}",
            opts,
        )

    is_dir = os.path.isdir(outpath)
    if not is_dir:
        return diff_single_file(cfg, entry, entry, outpath, opts)

    return diff_backup(cfg, entry, outpath, opts)


def cmd_list(cfg: Config, opts: Opts) -> int:
    for key in sorted(cfg.entries.keys()):
        path = cfg.entries[key]["path"]
        write_output(f"{key:<20s} {path}\n")
    return 0


def show_entry_files(manifest_path: str, sub: str | None = None) -> None:
    for entry in manifest.iter_manifest(manifest_path):
        if sub is not None:
            rel = entry.rel.removeprefix("./")
            if rel != sub and not rel.startswith(sub + "/"):
                continue
        display = entry.rel
        if entry.sym_target:
            display = f"{entry.rel} -> {entry.sym_target}"
        when = "" if entry.mtime_ns is None else _fmt_mtime(entry.mtime_ns)
        size = "" if entry.size is None else str(entry.size)
        write_output(
            f"{format(entry.mode, 'o'):<6s} {entry.owner:<8s} {entry.group:<8s} "
            f"{size:>8s}  {when}  {display}\n"
        )


def cmd_ls_remote(cfg: Config, opts: Opts, entry: str | None = None, sub: str | None = None) -> int:
    assert cfg.store is not None
    if entry is None:
        for line in cfg.store.list_top_level_lines(verbose=opts.verbose):
            if line.endswith(manifest.MANIFEST_SUFFIX):
                parts = line.split()
                if parts:
                    name = parts[-1].removesuffix(manifest.MANIFEST_SUFFIX)
                    write_output(f"{name}\n")
        return 0

    if entry not in cfg.entries:
        err(f"no such entry: {entry}")
        return 1

    fd, manifest_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        if not download_manifest(cfg, entry, manifest_path, opts.verbose):
            return 1
        if sub is not None and _sub_kind_from_manifest(manifest_path, sub) == "missing":
            err(f"not found on S3: {entry}/{sub}")
            return 1
        show_entry_files(manifest_path, sub=sub)
        return 0
    finally:
        os.unlink(manifest_path)


# =============================================================================
# Parallel runner
# =============================================================================


def run_entries(
    fn: Callable[[Config, str, Opts], int],
    cfg: Config,
    entries: list[str],
    opts: Opts,
) -> int:
    if not entries:
        return 0
    if len(entries) == 1:
        return fn(cfg, entries[0], opts)

    # One thread per entry by default; cap at entry_concurrency when configured.
    workers = len(entries)
    if cfg.entry_concurrency is not None:
        workers = min(workers, cfg.entry_concurrency)

    agg = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fn, cfg, e, opts): e for e in entries}
        for future in concurrent.futures.as_completed(futures):
            try:
                st = future.result()
                if st and not agg:
                    agg = st
            except Exception as exc:
                entry = futures[future]
                err(f"{entry}: {exc}")
                if not agg:
                    agg = 1
    return agg


# =============================================================================
# Usage
# =============================================================================


def print_usage(status: int = 1) -> NoReturn:
    config_path = os.environ.get("S3BAK_CONFIG") or expand_home("~/.config/s3bak/config.py")
    text = f"""\
Usage: s3bak <command> [options] [args]

Commands:
  push <entry|path>...         Back up entries or sub-paths to S3
  pull <entry|path>            Restore an entry or sub-path (use --all for every entry)
  show <entry|path>            Print a single file from the backup to stdout
  status <entry|path>...       Compare local vs backup (metadata only)
  diff <entry|path>            Show content diff between backup and local
  list                         List locally configured entries
  ls-remote [entry|path]       List S3 entries, or files under an entry/sub-path
  help                         Show this help

Options:
  --all            Apply the command to all configured entries
  --dryrun         Show what would happen without changing anything (push)
  --delete         After restore, delete local files not in backup (pull only)
  --meta-only      Sync only metadata (the manifest), skip file data (push/pull)
  --data-only      Sync only file data, leave manifest/local-meta untouched (push/pull)
  --checksum       Compare by content (ETag) instead of the manifest quick
                   check; reads every candidate file (push/pull)
  -o, --output <path>  Restore destination for pull (default: entry's configured path)
  -v, --verbose    Verbose output (details per field in status)
  --color[=WHEN]   Colorize status (verbose) and diff output
                   (WHEN: auto|always|never; default auto).
                   --color alone == --color=always. Honors NO_COLOR env var.
  --no-color       Disable color (same as --color=never)
  -h, --help       Show this help

status letters (push-oriented: what would change on the backup):
  M <path>         modified (metadata differs between local and backup)
  A <path>         only locally, not in backup   (push would add)
  D <path>         only in backup, not locally   (push would delete)

Config file: {config_path}

Examples:
  # push: back up one or more entries (or sub-paths)
  s3bak push bin .bash.d               # push selected entries
  s3bak push --all                     # push every configured entry
  s3bak push --all --dryrun            # preview without uploading
  s3bak push --meta-only bin           # upload metadata (the manifest) only
  s3bak push --meta-only --all         # upload metadata for all entries
  s3bak push --data-only bin           # upload data only, leave manifest unchanged
  s3bak push bin/s3bak                 # single file inside the bin entry
  s3bak push ~/bin/s3bak               # same, via ~ expansion
  s3bak push bin/subdir                # only the sub-directory

  # pull: restore from the backup (single entry/path; use --all for every entry)
  s3bak pull bin                       # restore to the configured path
  s3bak pull bin -o /tmp/restore       # restore to an alternative path
  s3bak pull bin --delete              # also remove local files not in backup
  s3bak pull --all                     # restore every entry in parallel
  s3bak pull --meta-only bin           # restore metadata only (no file download)
  s3bak pull --data-only bin           # restore file data only (no mode/mtime applied)
  s3bak pull bin/s3bak                 # restore a single file
  s3bak pull bin/subdir -o /tmp/restore # restore a sub-tree elsewhere

  # show: print a single backed-up file to stdout
  s3bak show wsl.conf                  # single-file entry (no slash = entry name)
  s3bak show bin/s3bak                 # local path, CWD-relative
  s3bak show ~/bin/s3bak               # local path with ~ expansion
  s3bak show /home/me/bin/s3bak | less # absolute local path

  # status: compare local vs backup (metadata only, both directions)
  s3bak status bin                     # M/A/D summary for one entry
  s3bak status --all                   # status of every entry
  s3bak status -v bin                  # verbose per-field differences
  s3bak status bin/s3bak               # status of a single sub-path

  # diff: content diff between backup and local
  s3bak diff bin                       # diff the whole entry
  s3bak diff bin/s3bak                 # single-file diff, CWD-relative local path
  s3bak diff ~/bin/s3bak               # single-file diff with ~ expansion

  # list: locally configured entries (no S3 access)
  s3bak list

  # ls-remote: what is on S3
  s3bak ls-remote                      # list entries stored on S3
  s3bak ls-remote bin                  # list files recorded in bin's manifest
  s3bak ls-remote bin/subdir           # list manifest lines under a sub-path
"""
    sys.stderr.write(text)
    sys.exit(status)


# =============================================================================
# Main
# =============================================================================


def _normalize_local_path(arg: str) -> str:
    expanded = expand_home(arg) if arg.startswith("~") else arg
    return os.path.abspath(expanded)


def _resolve_one_arg(cfg: Config, arg: str) -> tuple[str, str | None]:
    # No '/': match strictly as an entry name.
    # Contains '/': treat as a local path made absolute against CWD/HOME,
    #   then find which entry's path contains it, preferring the longest prefix.
    if "/" not in arg:
        if arg in cfg.entries:
            return arg, None
        die(f"no such entry: {arg}")

    local = _normalize_local_path(arg)
    best_name: str | None = None
    best_path: str = ""
    best_file: str | None = None
    for name, entry_cfg in cfg.entries.items():
        raw_path: str = entry_cfg["path"]
        entry_path = _normalize_local_path(raw_path)
        if local == entry_path:
            candidate_file: str | None = None
        elif local.startswith(entry_path + "/"):
            candidate_file = local[len(entry_path) + 1 :]
        else:
            continue
        if best_name is None or len(entry_path) > len(best_path):
            best_name = name
            best_path = entry_path
            best_file = candidate_file

    if best_name is None:
        die(f"no such entry for path: {arg}")
    return best_name, best_file


def resolve_entry_file(cfg: Config, positional: list[str], cmd: str) -> tuple[str, str | None]:
    if len(positional) != 1:
        die(f"{cmd} takes <entry> or <path>")
    return _resolve_one_arg(cfg, positional[0])


def resolve_entry_files(
    cfg: Config, positional: list[str], cmd: str
) -> list[tuple[str, str | None]]:
    if not positional:
        die(f"{cmd} requires at least one entry or path")
    return [_resolve_one_arg(cfg, arg) for arg in positional]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print_usage()

    subcmd = args[0]
    if subcmd in ("help", "-h", "--help"):
        print_usage(0)

    cfg = load_config()

    opt_all = False
    opt_dryrun = False
    opt_delete = False
    opt_meta_only = False
    opt_data_only = False
    opt_verbose = False
    opt_checksum = False
    opt_outpath: str | None = None
    opt_color: str = "auto"
    positional: list[str] = []

    def take_value(flag: str, idx: int) -> tuple[str, int]:
        # Support both --flag=value and --flag value
        if "=" in flag:
            return flag.split("=", 1)[1], idx
        if idx + 1 >= len(args):
            die(f"{flag} requires a value")
        return args[idx + 1], idx + 1

    i = 1
    while i < len(args):
        a = args[i]
        if a == "--all":
            opt_all = True
        elif a in ("--dryrun", "--dry-run"):
            opt_dryrun = True
        elif a == "--delete":
            opt_delete = True
        elif a == "--meta-only":
            opt_meta_only = True
        elif a == "--data-only":
            opt_data_only = True
        elif a in ("-v", "--verbose"):
            opt_verbose = True
        elif a == "--checksum":
            opt_checksum = True
        elif a in ("-o", "--output", "--outpath") or a.startswith(("--output=", "--outpath=")):
            opt_outpath, i = take_value(a, i)
        elif a == "--color":
            opt_color = "always"
        elif a.startswith("--color="):
            val = a.split("=", 1)[1]
            if val not in ("auto", "always", "never"):
                die(f"invalid --color value: {val} (use auto|always|never)")
            opt_color = val
        elif a == "--no-color":
            opt_color = "never"
        elif a in ("-h", "--help"):
            print_usage(0)
        elif a == "--":
            positional.extend(args[i + 1 :])
            break
        elif a.startswith("-"):
            die(f"unknown option: {a}")
        else:
            positional.append(a)
        i += 1

    opts = Opts(
        dryrun=opt_dryrun,
        delete=opt_delete,
        meta_only=opt_meta_only,
        data_only=opt_data_only,
        verbose=opt_verbose,
        checksum=opt_checksum,
        outpath=opt_outpath,
        color=opt_color,
    )

    if opt_all and positional:
        die("--all cannot be combined with explicit entries")

    if opt_meta_only and opt_data_only:
        die("--meta-only and --data-only are mutually exclusive")

    if opt_checksum and subcmd not in ("push", "pull"):
        die("--checksum only applies to push and pull")

    if subcmd == "push":
        if opt_all:
            entries = sorted(cfg.entries.keys())
            sub_by_entry: dict[str, str | None] = {e: None for e in entries}
        else:
            resolved = resolve_entry_files(cfg, positional, "push")
            seen: set[str] = set()
            for e, _s in resolved:
                if e in seen:
                    die(
                        f"duplicate entry in push: {e} "
                        f"(parallel push of the same entry is not supported)"
                    )
                seen.add(e)
            entries = [e for e, _ in resolved]
            sub_by_entry = {e: s for e, s in resolved}

        def _push_one(cfg_: Config, entry_: str, opts_: Opts) -> int:
            return cmd_push(cfg_, entry_, opts_, sub=sub_by_entry.get(entry_))

        return run_entries(_push_one, cfg, entries, opts)

    elif subcmd == "pull":
        if opt_all:
            if opts.outpath:
                die("--all cannot be combined with -o/--output")
            return run_entries(cmd_pull, cfg, sorted(cfg.entries.keys()), opts)
        entry, sub = resolve_entry_file(cfg, positional, "pull")
        return cmd_pull(cfg, entry, opts, sub=sub)

    elif subcmd == "status":
        if opt_delete:
            die("status does not support --delete (use 'pull --delete' to remove local extras)")
        if opt_meta_only:
            die("status does not support --meta-only")
        if opt_data_only:
            die("status does not support --data-only")
        if opt_all:
            entries = sorted(cfg.entries.keys())
            status_sub_by_entry: dict[str, str | None] = {e: None for e in entries}
        else:
            resolved = resolve_entry_files(cfg, positional, "status")
            entries = [e for e, _ in resolved]
            status_sub_by_entry = {}
            for e, s in resolved:
                if e in status_sub_by_entry and status_sub_by_entry[e] != s:
                    die(f"conflicting sub paths for entry {e}")
                status_sub_by_entry[e] = s

        def _status_one(cfg_: Config, entry_: str, opts_: Opts) -> int:
            return cmd_status(cfg_, entry_, opts_, sub=status_sub_by_entry.get(entry_))

        return run_entries(_status_one, cfg, entries, opts)

    elif subcmd == "diff":
        if opt_all:
            die("diff does not support --all")
        if opt_meta_only:
            die("diff does not support --meta-only")
        if opt_data_only:
            die("diff does not support --data-only")
        entry, file = resolve_entry_file(cfg, positional, "diff")
        return cmd_diff(cfg, entry, opts, file)

    elif subcmd == "show":
        if opt_all:
            die("show does not support --all")
        if opt_meta_only:
            die("show does not support --meta-only")
        if opt_data_only:
            die("show does not support --data-only")
        entry, file = resolve_entry_file(cfg, positional, "show")
        return cmd_show(cfg, entry, opts, file)

    elif subcmd == "list":
        if opt_all:
            die("list does not support --all")
        if opt_meta_only:
            die("list does not support --meta-only")
        if opt_data_only:
            die("list does not support --data-only")
        if positional:
            die("list takes no arguments (use 'ls-remote <entry>' for manifest contents)")
        return cmd_list(cfg, opts)

    elif subcmd == "ls-remote":
        if opt_all:
            die("ls-remote does not support --all")
        if opt_meta_only:
            die("ls-remote does not support --meta-only")
        if opt_data_only:
            die("ls-remote does not support --data-only")
        if len(positional) > 1:
            die("ls-remote takes at most one entry or path")
        if not positional:
            return cmd_ls_remote(cfg, opts, None, None)
        entry, sub = _resolve_one_arg(cfg, positional[0])
        return cmd_ls_remote(cfg, opts, entry, sub)

    else:
        err(f"unknown command: {subcmd}")
        print_usage()


def _sdk_errors() -> tuple[type[BaseException], ...]:
    """The boto3-s3 / botocore error types, imported lazily so `help` / `list`
    stay SDK-free (the except clause only evaluates this on an error). Returns an
    empty tuple if the SDK is unimportable, so matching never masks the original
    error with an ImportError."""
    try:
        from boto3_s3 import Boto3S3Error
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        return ()
    return (Boto3S3Error, BotoCoreError, ClientError)


def run() -> int:
    """Console entry point: install signal handling and translate exceptions
    into exit codes. This is what the ``s3bak`` command invokes."""
    global _warning_count
    _warning_count = 0
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
    try:
        rc = main() or 0
    except subprocess.CalledProcessError as e:
        cmd_str = shlex.join(e.cmd) if isinstance(e.cmd, list) else str(e.cmd)
        err(f"command failed: {cmd_str}")
        return e.returncode or 1
    except BrokenPipeError:
        return 141
    except manifest.ManifestError as e:
        err(str(e))
        return 1
    except _sdk_errors() as e:
        err(str(e))
        return 1
    # A run that only warned (skipped files etc.) but hit no hard error exits 2
    # (aws-style), after the manifest update has completed.
    if rc == 0 and _warning_count > 0:
        return 2
    return rc


if __name__ == "__main__":
    sys.exit(run())
