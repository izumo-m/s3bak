# Requires Python 3.10+
"""The command layer: one ``cmd_*`` per subcommand plus their private helpers.

Orchestrates the lower layers - store (S3), syncops (manifest<->S3),
restore (local filesystem), compare (status/diff) - into the push / pull /
status / diff / show / list / ls-remote behaviours. ``cli.py`` parses argv and
dispatches here.
"""

from __future__ import annotations

import os
import shutil
import stat as stat_mod
import subprocess
import tempfile

from s3bak import manifest
from s3bak.compare import (
    _diff_color_flag,
    _fmt_mtime,
    _resolve_use_color,
    check_metadata,
    compare_to_local,
)
from s3bak.config import Config, Opts
from s3bak.console import (
    IS_WINDOWS,
    echo_command,
    err,
    normalize_local_path,
    write_output,
    write_stderr,
)
from s3bak.manifest import ManifestEntry
from s3bak.restore import (
    apply_manifest,
    delete_extra_files,
    iter_local_tree,
    manifest_target,
    windows_collect_writable_prep,
    windows_restore_modes,
)
from s3bak.syncops import (
    download_from_s3,
    download_manifest,
    patch_manifest_subtree,
    sync_compare,
    write_manifest_to_aws,
)


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
            compare = sync_compare(cfg, opts, manifest_path if have_manifest else None, sub=sub)
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
    target_root = normalize_local_path(target)
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
            compare = sync_compare(cfg, opts, manifest_path if have_manifest else None)
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
            prep = windows_collect_writable_prep(outpath, is_dir, manifest_path, sub)

        changed = False
        if not opts.meta_only and has_data:
            # The compare only matters for the dir sync; a single-file cp
            # always transfers (we only reach it on a manifest mismatch).
            compare = sync_compare(cfg, opts, manifest_path, sub=sub) if is_dir else None
            rc, changed = download_from_s3(
                cfg, entry, outpath, is_dir, opts.verbose, sub=sub, compare=compare
            )
            if rc != 0:
                if IS_WINDOWS:
                    windows_restore_modes(prep)
                return rc

        # 4. Apply manifest metadata (mode, mtime, symlinks): objectless or
        #    metadata-only diffs (empty dirs, symlinks, mode/mtime) have nothing
        #    to download yet still need applying. apply_manifest sets the modes
        #    itself, so the writable prep needs no separate restore. Skipped
        #    with --data-only, and after a --checksum pass over an
        #    already-clean tree (nothing transferred, metadata matches).
        if opts.data_only:
            if IS_WINDOWS and not opts.meta_only:
                windows_restore_modes(prep)
            st = 0
        elif manifest_matches and not changed:
            if IS_WINDOWS and not opts.meta_only:
                windows_restore_modes(prep)
            st = 0
        else:
            st = apply_manifest(outpath, is_dir, manifest_path, sub=sub)

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
