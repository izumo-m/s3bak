"""Windows-only restore correctness, pinned with monkeypatch-based unit tests
since the suite only runs on Linux/macOS CI.

Covers: a directory junction fooling the ancestor guards and the pre-sync
directory-conflict cleanup (both lstat as an ordinary directory, since
Windows does not model a junction as a symlink); is_junction itself; the
writable prep being scoped to the manifest's own recorded type instead of
local reality, and skipped entirely when a conflicting root is repaired via
a staged swap; a symlink's file-vs-directory kind probe running before its
target directory exists, and the deferred placement's own re-settle of its
parent directory's mtime; the writable prep surviving both a metadata-apply
failure and an exception raised out of the pull; and windows_restore_modes
itself restoring a prepped path's original mode.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from s3bak import commands, console, restore

MTIME_NS = 1_600_000_000 * 1_000_000_000


def _line(
    mode: str,
    rel: str,
    *,
    size: int | None = None,
    link: str | None = None,
    mtime_ns: int = MTIME_NS,
) -> str:
    obj: dict[str, object] = {"path": rel, "mode": mode, "owner": "o", "group": "g"}
    if size is not None:
        obj["size"] = size
    if link is not None:
        obj["link"] = link
    obj["mtime_ns"] = mtime_ns
    return json.dumps(obj)


def _write_manifest(path, lines: list[str]) -> None:
    path.write_text("\n".join(['{"s3bak_manifest":3}', *lines]) + "\n")


class _FakeReparseStat:
    """Wraps a real ``os.stat_result``, adding the Windows-only
    ``st_reparse_tag`` attribute so an ordinary directory can stand in for a
    junction in a Linux unit test."""

    def __init__(self, real: os.stat_result, reparse_tag: int = 0xA0000003):
        self._real = real
        self.st_reparse_tag = reparse_tag

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def test_is_junction_true_only_for_a_mount_point_reparse_tag(tmp_path):
    real_dir = tmp_path / "d"
    real_dir.mkdir()
    st = os.lstat(real_dir)

    # An ordinary directory has no st_reparse_tag attribute at all (POSIX, and
    # non-reparse-point directories on Windows too) - getattr's default keeps
    # this False rather than raising.
    assert console.is_junction(st) is False

    # The default fake tag matches IO_REPARSE_TAG_MOUNT_POINT (a junction).
    assert console.is_junction(_FakeReparseStat(st)) is True

    # A different reparse tag (e.g. a symlink reparse point, 0xA000000C) is a
    # reparse point but NOT a mount point, so this must stay False - is_junction
    # is specifically the mount-point tag, not "any reparse point".
    assert console.is_junction(_FakeReparseStat(st, reparse_tag=0xA000000C)) is False


# --- W-F1: a Windows directory junction lstats as an ordinary directory -----


def test_ancestor_block_reason_treats_junction_as_structural(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    junction = root / "junc"
    junction.mkdir()  # stands in for a junction: a real dir, faked reparse tag

    real_lstat = os.lstat

    def fake_lstat(path, *a, **k):
        st = real_lstat(path, *a, **k)
        if os.path.abspath(path) == str(junction):
            return _FakeReparseStat(st)
        return st

    monkeypatch.setattr(os, "lstat", fake_lstat)

    assert commands._ancestor_block_reason(str(root), "junc/leaf") == "structural"


def test_reject_symlinked_sub_ancestors_rejects_junction(tmp_path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    junction = base / "junc"
    junction.mkdir()

    real_lstat = os.lstat

    def fake_lstat(path, *a, **k):
        st = real_lstat(path, *a, **k)
        if os.path.abspath(path) == str(junction):
            return _FakeReparseStat(st)
        return st

    monkeypatch.setattr(os, "lstat", fake_lstat)

    msg = commands._reject_symlinked_sub_ancestors(str(base), "junc/leaf")
    assert msg is not None
    assert "junction" in msg


def test_prepare_dir_conflicts_clears_junction_at_recorded_directory(tmp_path, monkeypatch):
    outpath = tmp_path / "out"
    outpath.mkdir()
    junction = outpath / "d"
    junction.mkdir()  # stands in for a junction pointing elsewhere

    manifest_path = tmp_path / "m.jsonl"
    _write_manifest(manifest_path, [_line("40755", "."), _line("40755", "./d")])

    real_lstat = os.lstat

    def fake_lstat(path, *a, **k):
        st = real_lstat(path, *a, **k)
        if os.path.abspath(path) == str(junction):
            return _FakeReparseStat(st)
        return st

    monkeypatch.setattr(os, "lstat", fake_lstat)

    rmdir_calls: list[str] = []
    real_rmdir = os.rmdir

    def spy_rmdir(path, *a, **k):
        rmdir_calls.append(path)
        return real_rmdir(path, *a, **k)

    monkeypatch.setattr(os, "rmdir", spy_rmdir)

    errors = restore.prepare_dir_conflicts(str(outpath), str(manifest_path), None)

    assert errors == 0
    # Only os.path.islink() would have missed this (a junction is not a
    # symlink); reaching the rmdir+makedirs replacement proves it was
    # recognized instead of silently passed through as an ordinary directory.
    assert rmdir_calls == [str(junction)]
    assert os.path.isdir(junction)
    assert not os.path.islink(junction)


# --- W-F5: a symlink's kind probe running before its target dir exists -----


def _empty_dir_symlink_manifest(path) -> None:
    # "a-link" sorts before "z-empty" (alphabetical, and a directory record
    # sorts at "name/" - either way "a" < "z"), so a-link's record is always
    # processed while z-empty (an empty, data-object-less directory) does not
    # exist locally yet.
    _write_manifest(
        path,
        [
            _line("40755", "."),
            _line("120777", "./a-link", link="z-empty"),
            _line("40755", "./z-empty"),
        ],
    )


def test_apply_manifest_defers_symlink_kind_probe_until_target_dir_exists_on_windows(
    tmp_path, monkeypatch
):
    manifest_path = tmp_path / "m.jsonl"
    _empty_dir_symlink_manifest(manifest_path)
    outpath = tmp_path / "out"

    monkeypatch.setattr(restore, "IS_WINDOWS", True)

    events: list[tuple[str, ...]] = []
    real_symlink = os.symlink
    real_makedirs = os.makedirs

    def spy_symlink(src, dst, target_is_directory=False):
        events.append(("symlink", os.path.basename(dst), target_is_directory))
        return real_symlink(src, dst, target_is_directory=target_is_directory)

    def spy_makedirs(name, *a, **k):
        if os.path.basename(str(name).rstrip(os.sep)) == "z-empty":
            events.append(("makedirs", "z-empty"))
        return real_makedirs(name, *a, **k)

    monkeypatch.setattr(os, "symlink", spy_symlink)
    monkeypatch.setattr(os, "makedirs", spy_makedirs)

    st = restore.apply_manifest(str(outpath), True, str(manifest_path), window_ns=0)

    assert st == 0
    assert os.path.islink(outpath / "a-link")
    assert os.path.isdir(outpath / "z-empty")
    symlink_events = [e for e in events if e[0] == "symlink"]
    makedirs_events = [e for e in events if e[0] == "makedirs"]
    assert len(symlink_events) == 1
    assert len(makedirs_events) == 1
    assert symlink_events[0][2] is True  # target_is_directory: probed once z-empty existed
    # The symlink was placed AFTER z-empty's directory was created - proof its
    # placement was deferred to stream end rather than attempted inline while
    # the target was still unresolved.
    assert events.index(symlink_events[0]) > events.index(makedirs_events[0])


def test_apply_manifest_does_not_defer_symlink_placement_on_posix(tmp_path, monkeypatch):
    manifest_path = tmp_path / "m.jsonl"
    _empty_dir_symlink_manifest(manifest_path)
    outpath = tmp_path / "out"

    monkeypatch.setattr(restore, "IS_WINDOWS", False)

    events: list[tuple[str, ...]] = []
    real_symlink = os.symlink
    real_makedirs = os.makedirs

    def spy_symlink(src, dst, target_is_directory=False):
        events.append(("symlink", os.path.basename(dst), target_is_directory))
        return real_symlink(src, dst, target_is_directory=target_is_directory)

    def spy_makedirs(name, *a, **k):
        if os.path.basename(str(name).rstrip(os.sep)) == "z-empty":
            events.append(("makedirs", "z-empty"))
        return real_makedirs(name, *a, **k)

    monkeypatch.setattr(os, "symlink", spy_symlink)
    monkeypatch.setattr(os, "makedirs", spy_makedirs)

    st = restore.apply_manifest(str(outpath), True, str(manifest_path), window_ns=0)

    assert st == 0
    assert os.path.islink(outpath / "a-link")
    assert os.path.isdir(outpath / "z-empty")
    symlink_events = [e for e in events if e[0] == "symlink"]
    makedirs_events = [e for e in events if e[0] == "makedirs"]
    assert len(symlink_events) == 1
    # Placed inline, before z-empty existed: the probe found nothing, so
    # target_is_directory is False (POSIX ignores the flag either way) and the
    # placement precedes z-empty's own makedirs - unchanged, undeferred order.
    assert symlink_events[0][2] is False
    assert events.index(symlink_events[0]) < events.index(makedirs_events[0])


# --- W-F6: the writable prep is scoped to local state, not manifest type ---


def test_windows_collect_writable_prep_uses_local_state_not_manifest_type(tmp_path):
    outpath = tmp_path / "out"
    outpath.mkdir()
    dir_conflict = outpath / "d"
    dir_conflict.write_text("stale file where a directory belongs")
    os.chmod(dir_conflict, stat.S_IREAD)
    sym_conflict = outpath / "s"
    sym_conflict.write_text("stale file where a symlink belongs")
    os.chmod(sym_conflict, stat.S_IREAD)

    manifest_path = tmp_path / "m.jsonl"
    _write_manifest(
        manifest_path,
        [
            _line("40755", "."),
            _line("40755", "./d"),
            _line("120777", "./s", link="elsewhere"),
        ],
    )

    dir_mode = os.lstat(dir_conflict).st_mode
    sym_mode = os.lstat(sym_conflict).st_mode

    prep = restore.windows_collect_writable_prep(str(outpath), True, str(manifest_path), None)

    # Before the fix, the manifest's own type (dir/symlink, never a file)
    # skipped both records outright, leaving prep empty.
    assert dict(prep) == {str(dir_conflict): dir_mode, str(sym_conflict): sym_mode}
    assert os.lstat(dir_conflict).st_mode & stat.S_IWRITE
    assert os.lstat(sym_conflict).st_mode & stat.S_IWRITE


# --- W-F7: a failed apply_manifest must still restore the writable prep ----


def test_windows_pull_restores_writable_prep_when_apply_manifest_fails(ws, monkeypatch):
    monkeypatch.setattr(commands, "IS_WINDOWS", True)
    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "out"
    dest.mkdir()
    target = dest / "a.txt"
    target.write_text("stale content, different size than the backup")
    os.chmod(target, stat.S_IREAD)  # read-only: windows_collect_writable_prep must find it

    collected: list[list[tuple[str, int]]] = []
    real_collect = commands.windows_collect_writable_prep

    def spy_collect(*args, **kwargs):
        prep = real_collect(*args, **kwargs)
        collected.append(prep)
        return prep

    restored: list[list[tuple[str, int]]] = []
    monkeypatch.setattr(commands, "windows_collect_writable_prep", spy_collect)
    monkeypatch.setattr(commands, "windows_restore_modes", lambda prep: restored.append(list(prep)))
    # apply_manifest ran (unlike the download-failure / --data-only paths,
    # which already restored the prep before this fix) and reported a
    # metadata-apply failure - the exact gap this fix closes.
    monkeypatch.setattr(commands, "apply_manifest", lambda *a, **k: 1)

    res = ws.run("pull", "data", "-o", str(dest))

    assert res.rc == 1
    assert len(collected) == 1 and collected[0], "prep must have found the read-only file"
    assert len(restored) == 1, "windows_restore_modes must run even when apply_manifest returns 1"
    assert restored[0] == collected[0]


def test_windows_pull_does_not_re_restore_prep_after_a_clean_apply(ws, monkeypatch):
    # The mirror image of the test above: once apply_manifest succeeds, it has
    # already re-chmod'd every prepped path to its recorded mode itself: a
    # blanket restore here would revert a legitimate mode change back to the
    # stale pre-pull value. windows_restore_modes must not run a second time
    # after a clean apply.
    monkeypatch.setattr(commands, "IS_WINDOWS", True)
    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "out"
    dest.mkdir()
    target = dest / "a.txt"
    target.write_text("stale content, different size than the backup")
    os.chmod(target, stat.S_IREAD)

    restored: list[list[tuple[str, int]]] = []
    monkeypatch.setattr(commands, "windows_restore_modes", lambda prep: restored.append(list(prep)))

    ws.run("pull", "data", "-o", str(dest), expect_rc=0)

    assert restored == []


def test_windows_pull_restores_writable_prep_when_apply_manifest_raises(ws, monkeypatch):
    # The mirror image of the return-1 test above, on the OTHER exit the
    # outer finally must cover: an exception escaping the try block (an S3
    # error, a local I/O failure, SIGINT, ...) rather than an ordinary
    # non-zero return. cmd_pull's `except BaseException: ... raise` still lets
    # the finally run before the exception propagates - this pins that the
    # writable prep is restored there too, not only on a clean return path.
    monkeypatch.setattr(commands, "IS_WINDOWS", True)
    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "out"
    dest.mkdir()
    target = dest / "a.txt"
    target.write_text("stale content, different size than the backup")
    os.chmod(target, stat.S_IREAD)  # read-only: windows_collect_writable_prep must find it

    collected: list[list[tuple[str, int]]] = []
    real_collect = commands.windows_collect_writable_prep

    def spy_collect(*args, **kwargs):
        prep = real_collect(*args, **kwargs)
        collected.append(prep)
        return prep

    restored: list[list[tuple[str, int]]] = []
    monkeypatch.setattr(commands, "windows_collect_writable_prep", spy_collect)
    monkeypatch.setattr(commands, "windows_restore_modes", lambda prep: restored.append(list(prep)))

    def _raise_apply_manifest(*args, **kwargs):
        raise RuntimeError("boom: simulated apply_manifest crash")

    monkeypatch.setattr(commands, "apply_manifest", _raise_apply_manifest)

    # A single resolved entry calls cmd_pull directly (cli.run_entries), so the
    # exception is not converted to an exit code here - it propagates all the
    # way out of cli.main, same as test_security.py's own symlink-privilege
    # exception test.
    with pytest.raises(RuntimeError, match="boom"):
        ws.run("pull", "data", "-o", str(dest))

    assert len(collected) == 1 and collected[0], "prep must have found the read-only file"
    assert len(restored) == 1, "windows_restore_modes must run even when apply_manifest raises"
    assert restored[0] == collected[0]


def test_windows_pull_skips_writable_prep_when_swapping_a_conflicting_root(ws, monkeypatch):
    # A conflicting root type (a plain file sitting where the manifest records
    # a directory) is repaired by downloading into a fresh stage directory and
    # swapping it in, never by mutating outpath in place - so the writable
    # prep, which walks the EXISTING outpath, must not run at all: a fresh
    # stage has no stale read-only files, and outpath itself is about to be
    # replaced wholesale, not written into.
    monkeypatch.setattr(commands, "IS_WINDOWS", True)
    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "out"
    dest.write_text("a plain file where the manifest records a directory")

    collected: list[list[tuple[str, int]]] = []
    real_collect = commands.windows_collect_writable_prep

    def spy_collect(*args, **kwargs):
        prep = real_collect(*args, **kwargs)
        collected.append(prep)
        return prep

    monkeypatch.setattr(commands, "windows_collect_writable_prep", spy_collect)

    res = ws.run("pull", "data", "-o", str(dest))

    assert res.rc == 0
    assert collected == [], "the writable prep must not walk a root about to be swapped wholesale"
    assert dest.is_dir()
    assert (dest / "a.txt").read_text() == "hello"


# --- windows_restore_modes itself: the chmod-back, not just that it ran -----


def test_windows_restore_modes_restores_the_original_mode(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x")
    original_mode = os.lstat(target).st_mode
    os.chmod(target, original_mode | stat.S_IWRITE)  # simulate the prep's write-bit add

    restore.windows_restore_modes([(str(target), original_mode)])

    assert os.lstat(target).st_mode == original_mode


def test_windows_restore_modes_skips_a_missing_path_and_still_restores_the_rest(tmp_path):
    # One prepped path can vanish before the restore pass (e.g. a manifest
    # record whose data sync removed and replaced it); that must not abort the
    # restore of every OTHER prepped path still queued behind it.
    missing = str(tmp_path / "gone.txt")
    present = tmp_path / "present.txt"
    present.write_text("x")
    original_mode = os.lstat(present).st_mode
    os.chmod(present, original_mode | stat.S_IWRITE)

    restore.windows_restore_modes([(missing, 0o100644), (str(present), original_mode)])

    assert os.lstat(present).st_mode == original_mode


# --- the deferred Windows symlink placement re-settles its parent dir ------


def test_apply_manifest_resettles_parent_dir_mtime_after_deferred_windows_symlink_placement(
    tmp_path, monkeypatch
):
    # Placing the deferred symlink (see the kind-probe tests above) adds a new
    # directory entry under the restore root, which bumps the root's own mtime
    # again - AFTER the root's directory frame was already popped once at
    # end-of-stream. `resettle` is what re-applies the root's recorded (stale)
    # mtime a second time, after that placement; without it the root would be
    # left at whatever mtime the symlink creation happened to leave it at,
    # never matching the manifest again under an exact (window_ns=0) compare.
    manifest_path = tmp_path / "m.jsonl"
    _empty_dir_symlink_manifest(manifest_path)
    outpath = tmp_path / "out"

    monkeypatch.setattr(restore, "IS_WINDOWS", True)

    st = restore.apply_manifest(str(outpath), True, str(manifest_path), window_ns=0)

    assert st == 0
    # Exact match against the manifest's recorded (deliberately stale) MTIME_NS
    # - only possible if the root was settled a second time after the deferred
    # symlink placement dirtied it, not just once at the first pop.
    assert os.lstat(outpath).st_mtime_ns == MTIME_NS
