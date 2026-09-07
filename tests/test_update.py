"""push -u / pull -u: the newer side wins (docs/sync.md).

The record's mtime orders the two sides of every pair; a tie with a
difference is a conflict, warned and left alone. Two machines sharing one
entry are simulated by pointing the same entry name at two local trees."""

from __future__ import annotations

import os
import shutil
import stat

import pytest

from s3bak.compare import SYMLINK_MTIME_SUPPORTED
from s3bak.console import console

OLD = 1_600_000_000  # 2020: older than any record written by the suite
NEW = 2_000_000_000  # 2033: newer than any record written by the suite
# cli.main (what the suite drives) leaves the warning -> exit 2 mapping to
# cli.run, so a conflict is observed as its warning line plus the counter.
CONFLICT = "warning: conflict - "


def _mtime_ns(p) -> int:
    return os.lstat(p).st_mtime_ns


def _mode(p) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def _manifest_body(ws, entry: str) -> str:
    key = f"{ws.prefix}/{entry}-manifest.jsonl"
    return ws.s3.get_object(Bucket=ws.bucket, Key=key)["Body"].read().decode()


def _object_body(ws, key: str) -> str:
    return ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/{key}")["Body"].read().decode()


def _rewrite(p, content: str, mtime: int | None) -> None:
    """Replace a file's content and pin its mtime (seconds); ``None`` keeps
    the mtime the file had before the write - the tie case."""
    before = _mtime_ns(p)
    p.write_text(content)
    if mtime is None:
        os.utime(p, ns=(before, before))
    else:
        os.utime(p, (mtime, mtime))


def _pushed_tree(ws):
    a = ws.write("data/a.txt", "alpha")
    b = ws.write("data/sub/b.txt", "beta")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    return a, b


# --- push -u -----------------------------------------------------------------


def test_push_update_keeps_the_record_when_the_backup_is_newer(ws):
    a, _b = _pushed_tree(ws)
    body = _manifest_body(ws, "data")
    _rewrite(a, "older edit", OLD)

    res = ws.run("push", "-u", "data", expect_rc=0)

    assert "upload:" not in res.out
    assert _object_body(ws, "data/a.txt") == "alpha"
    assert _manifest_body(ws, "data") == body  # the record still describes the object
    assert "skip" not in res.err  # a quiet push means nothing to do


def test_push_update_reports_a_skip_under_verbose(ws):
    a, _b = _pushed_tree(ws)
    _rewrite(a, "older edit", OLD)

    res = ws.run("push", "-u", "-v", "data", expect_rc=0)

    assert "skip (backup is newer): " in res.err
    assert "a.txt" in res.err


def test_push_update_uploads_a_newer_local_file(ws):
    a, _b = _pushed_tree(ws)
    _rewrite(a, "newer edit", NEW)

    res = ws.run("push", "-u", "data", expect_rc=0)

    assert "upload:" in res.out
    assert _object_body(ws, "data/a.txt") == "newer edit"
    assert f'"mtime_ns":{NEW * 1_000_000_000}' in _manifest_body(ws, "data")


def test_push_update_tie_with_a_size_difference_is_a_conflict(ws):
    a, _b = _pushed_tree(ws)
    body = _manifest_body(ws, "data")
    _rewrite(a, "alpha plus", None)
    warned = console.warning_count()

    res = ws.run("push", "-u", "data", expect_rc=0)

    assert f"{CONFLICT}same mtime, size differs; skipped (touch the copy to keep): " in res.err
    assert console.warning_count() == warned + 1  # what cli.run turns into exit 2
    assert "upload:" not in res.out
    assert _object_body(ws, "data/a.txt") == "alpha"
    assert _manifest_body(ws, "data") == body


def test_push_update_mode_only_tie_is_a_conflict(ws):
    a, _b = _pushed_tree(ws)
    body = _manifest_body(ws, "data")
    os.chmod(a, 0o600 if _mode(a) != 0o600 else 0o640)

    res = ws.run("push", "-u", "data", expect_rc=0)

    assert f"{CONFLICT}same mtime, mode differs" in res.err
    assert _manifest_body(ws, "data") == body  # a plain push would refresh it


def test_push_update_directory_record_follows_the_newer_side(ws):
    _pushed_tree(ws)
    sub = ws.root / "data" / "sub"
    body = _manifest_body(ws, "data")

    os.utime(sub, (OLD, OLD))
    ws.run("push", "-u", "data", expect_rc=0)
    assert _manifest_body(ws, "data") == body  # the newer record is kept

    os.utime(sub, (NEW, NEW))
    ws.run("push", "-u", "data", expect_rc=0)
    assert f'"path":"./sub","mode":"{_mode(sub) | stat.S_IFDIR:o}"' in _manifest_body(ws, "data")
    assert f'"mtime_ns":{NEW * 1_000_000_000}' in _manifest_body(ws, "data")


def test_push_update_object_that_drifted_from_its_record_is_a_conflict(ws):
    _pushed_tree(ws)
    body = _manifest_body(ws, "data")
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt", Body=b"written around s3bak")

    res = ws.run("push", "-u", "data", expect_rc=0)

    assert f"{CONFLICT}stored object does not match the record" in res.err
    assert _object_body(ws, "data/a.txt") == "written around s3bak"  # a plain push re-uploads
    assert _manifest_body(ws, "data") == body


def test_push_update_still_uploads_a_new_local_file(ws):
    _pushed_tree(ws)
    ws.write("data/new.txt", "fresh")

    res = ws.run("push", "-u", "data", expect_rc=0)

    assert "upload:" in res.out
    assert "data/new.txt" in ws.keys()


def test_push_update_uploads_over_an_unrecorded_object(ws):
    # No record means no mtime to order by. The local file is the only copy
    # the operator holds, and the object is what bucket versioning keeps, so
    # push takes the local side (the converse pull is a conflict, below).
    _pushed_tree(ws)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/extra.txt", Body=b"around")
    ws.write("data/extra.txt", "local copy")

    ws.run("push", "-u", "data", expect_rc=0)

    assert _object_body(ws, "data/extra.txt") == "local copy"


def test_push_update_named_file_subpath_is_unconditional(ws):
    a, _b = _pushed_tree(ws)
    _rewrite(a, "older edit", OLD)

    res = ws.run("push", "-u", "data/a.txt", expect_rc=0)

    assert "upload:" in res.out
    assert _object_body(ws, "data/a.txt") == "older edit"


def test_push_update_with_checksum(ws):
    a, b = _pushed_tree(ws)
    os.utime(a, (NEW, NEW))  # content-equal, newer: re-record only
    _rewrite(b, "beta edited", OLD)  # content differs, record newer: kept

    res = ws.run("push", "-u", "--checksum", "data", expect_rc=0)

    assert "upload:" not in res.out
    assert f'"mtime_ns":{NEW * 1_000_000_000}' in _manifest_body(ws, "data")
    assert _object_body(ws, "data/sub/b.txt") == "beta"


def test_push_update_with_checksum_content_tie_is_a_conflict(ws):
    a, _b = _pushed_tree(ws)
    _rewrite(a, "delta", None)  # same size, same mtime, other content

    res = ws.run("push", "-u", "--checksum", "data", expect_rc=0)

    assert f"{CONFLICT}same mtime, content differs" in res.err
    assert _object_body(ws, "data/a.txt") == "alpha"


def test_push_update_single_file_entry(ws):
    f = ws.write("one.conf", "v1")
    ws.config({"one.conf": {"path": str(f)}})
    ws.run("push", "one.conf", expect_rc=0)
    body = _manifest_body(ws, "one.conf")

    _rewrite(f, "v0", OLD)
    res = ws.run("push", "-u", "one.conf", expect_rc=0)
    assert "upload:" not in res.out
    assert _manifest_body(ws, "one.conf") == body

    _rewrite(f, "v2", NEW)
    res = ws.run("push", "-u", "one.conf", expect_rc=0)
    assert "upload:" in res.out
    assert _object_body(ws, "one.conf") == "v2"

    _rewrite(f, "v2 and more", None)
    res = ws.run("push", "-u", "one.conf", expect_rc=0)
    assert f"{CONFLICT}same mtime, size differs" in res.err
    assert _object_body(ws, "one.conf") == "v2"


# --- pull -u -----------------------------------------------------------------


def test_pull_update_keeps_a_newer_local_file_with_its_metadata(ws):
    a, b = _pushed_tree(ws)
    _rewrite(a, "newer edit", NEW)
    os.chmod(a, 0o600)
    _rewrite(b, "older edit", OLD)  # so the pull has real work to do

    res = ws.run("pull", "-u", "data", expect_rc=0)

    assert a.read_text() == "newer edit"
    assert _mtime_ns(a) == NEW * 1_000_000_000
    assert _mode(a) == 0o600
    assert "a.txt" not in res.out  # neither downloaded nor re-stamped
    assert b.read_text() == "beta"  # the older side was restored
    assert "download:" in res.out


def test_pull_update_restores_an_older_local_file(ws):
    a, _b = _pushed_tree(ws)
    recorded = _mtime_ns(a)
    _rewrite(a, "older edit", OLD)

    ws.run("pull", "-u", "data", expect_rc=0)

    assert a.read_text() == "alpha"
    assert _mtime_ns(a) == recorded


def test_pull_update_keeps_a_newer_local_file_when_nothing_else_differs(ws):
    a, _b = _pushed_tree(ws)
    _rewrite(a, "newer edit", NEW)

    res = ws.run("pull", "-u", "-v", "data", expect_rc=0)

    assert a.read_text() == "newer edit"
    assert res.out == ""
    assert "skip (local is newer): " in res.err  # the lane judged it, so -v says so


def test_pull_update_reports_a_skip_under_verbose(ws):
    a, b = _pushed_tree(ws)
    _rewrite(a, "newer edit", NEW)
    _rewrite(b, "older edit", OLD)

    res = ws.run("pull", "-u", "-v", "data", expect_rc=0)

    assert "skip (local is newer): " in res.err
    assert "a.txt" in res.err


def test_pull_update_tie_with_a_size_difference_is_a_conflict(ws):
    a, _b = _pushed_tree(ws)
    _rewrite(a, "alpha plus", None)
    warned = console.warning_count()

    res = ws.run("pull", "-u", "data", expect_rc=0)

    assert f"{CONFLICT}same mtime, size differs; skipped (touch the copy to keep): " in res.err
    assert console.warning_count() == warned + 1  # what cli.run turns into exit 2
    assert a.read_text() == "alpha plus"
    assert "download:" not in res.out


def test_pull_update_mode_only_tie_is_a_conflict(ws):
    a, _b = _pushed_tree(ws)
    mode = 0o600 if _mode(a) != 0o600 else 0o640
    os.chmod(a, mode)

    res = ws.run("pull", "-u", "data", expect_rc=0)

    assert f"{CONFLICT}same mtime, mode differs" in res.err
    assert _mode(a) == mode  # a plain pull would apply the recorded mode


def test_pull_update_dry_run_reports_conflicts_and_changes_nothing(ws):
    a, _b = _pushed_tree(ws)
    _rewrite(a, "alpha plus", None)

    res = ws.run("pull", "-u", "--dry-run", "data", expect_rc=0)

    assert f"{CONFLICT}same mtime, size differs" in res.err
    assert a.read_text() == "alpha plus"


def test_pull_update_unrecorded_object_over_a_local_file_is_a_conflict(ws):
    _a, b = _pushed_tree(ws)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/extra.txt", Body=b"around")
    extra = ws.write("data/extra.txt", "local copy")
    _rewrite(b, "older edit", OLD)  # past the no-op gate, which knows records only

    res = ws.run("pull", "-u", "data", expect_rc=0)

    assert f"{CONFLICT}not recorded in the manifest" in res.err
    assert extra.read_text() == "local copy"  # a plain pull would overwrite it
    assert b.read_text() == "beta"


def _directory_at_file_record(ws):
    """Replace the pushed a.txt with a directory holding data the backup does
    not: the type change no download can undo (docs/sync.md)."""
    a, _b = _pushed_tree(ws)
    a.unlink()
    a.mkdir()
    (a / "inner").write_text("unrecorded")
    return a


def test_pull_update_keeps_a_newer_directory_where_a_file_is_recorded(ws):
    a = _directory_at_file_record(ws)
    os.utime(a, (NEW, NEW))

    res = ws.run("pull", "-u", "-v", "data", expect_rc=0)

    assert f"skip (local is newer): {a}" in res.err
    assert "conflict" not in res.err and "download:" not in res.out
    assert (a / "inner").read_text() == "unrecorded"


def test_pull_update_older_directory_where_a_file_is_recorded_is_a_conflict(ws):
    # A newer record would replace a symlink at its key, but never a
    # directory: that pair is the conflict, and the directory stays.
    a = _directory_at_file_record(ws)
    os.utime(a, (OLD, OLD))
    warned = console.warning_count()

    res = ws.run("pull", "-u", "data", expect_rc=0)

    assert (
        f"{CONFLICT}newer in the backup, but a directory sits at its path;"
        f" skipped (touch the copy to keep): {a}"
    ) in res.err
    assert console.warning_count() == warned + 1
    assert "download:" not in res.out
    assert (a / "inner").read_text() == "unrecorded"


def test_pull_update_directory_tie_where_a_file_is_recorded_is_a_conflict(ws):
    a, _b = _pushed_tree(ws)
    recorded = _mtime_ns(a)
    a.unlink()
    a.mkdir()
    os.utime(a, ns=(recorded, recorded))

    res = ws.run("pull", "-u", "data", expect_rc=0)

    assert f"{CONFLICT}same mtime, type differs" in res.err
    assert a.is_dir()


def test_pull_update_delete_never_offers_the_contents_of_a_kept_directory(ws):
    # The directory kept at a file record's key holds what the manifest
    # cannot vouch for: neither it nor its contents are extras, whichever
    # side the rule picked.
    a = _directory_at_file_record(ws)
    for mtime, quiet in ((NEW, True), (OLD, False)):
        os.utime(a, (mtime, mtime))

        res = ws.run("pull", "-u", "--delete", "--yes", "data", expect_rc=0)

        assert "delete:" not in res.out
        # Kept by the rule, not by the name-folding alias guard, which the
        # stale record's own spelling used to trip.
        assert "not removed (a local name" not in res.err
        assert (res.err == "") is quiet  # the older directory is the conflict
        assert (a / "inner").read_text() == "unrecorded"


def test_pull_update_orders_a_directory_where_a_gone_object_is_recorded(ws):
    # The record's object gone, no lane ever sees the key: the metadata apply
    # applies the same rule to the directory it meets there.
    a = _directory_at_file_record(ws)
    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt")
    os.utime(a, (NEW, NEW))

    res = ws.run("pull", "-u", "data", expect_rc=0)

    assert res.err == ""
    assert (a / "inner").read_text() == "unrecorded"

    os.utime(a, (OLD, OLD))
    res = ws.run("pull", "-u", "data", expect_rc=0)

    assert f"{CONFLICT}newer in the backup, but a directory sits at its path" in res.err
    assert (a / "inner").read_text() == "unrecorded"


def test_pull_update_settles_a_directory_it_downloaded_into(ws):
    _a, b = _pushed_tree(ws)
    sub = ws.root / "data" / "sub"
    recorded = _mtime_ns(sub)
    _rewrite(b, "older edit", OLD)
    os.utime(sub, (NEW, NEW))  # newer locally - but the download dirties it

    ws.run("pull", "-u", "data", expect_rc=0)

    assert b.read_text() == "beta"
    assert _mtime_ns(sub) == recorded


def test_pull_update_leaves_a_newer_directory_it_did_not_touch(ws):
    a, _b = _pushed_tree(ws)
    sub = ws.root / "data" / "sub"
    _rewrite(a, "older edit", OLD)  # a sibling download, outside sub
    os.utime(sub, (NEW, NEW))

    res = ws.run("pull", "-u", "data", expect_rc=0)

    assert a.read_text() == "alpha"
    assert _mtime_ns(sub) == NEW * 1_000_000_000
    assert str(sub) not in res.out


def test_pull_update_applies_a_newer_directory_record(ws):
    _pushed_tree(ws)
    sub = ws.root / "data" / "sub"
    recorded = _mtime_ns(sub)
    os.utime(sub, (OLD, OLD))

    ws.run("pull", "-u", "data", expect_rc=0)

    assert _mtime_ns(sub) == recorded


def test_pull_update_settles_the_directories_it_created(ws):
    # A pull into an empty destination creates every level itself, so none of
    # them has a local side that could be newer: the fresh mtime and the
    # umask's mode are this run's own, and the record is what they settle to -
    # including a level no file record sits directly inside.
    ws.write("data/a/b/deep.txt", "beta")
    ws.config({"data": {"path": str(ws.root / "data")}})
    root = ws.root / "data"
    dirs = [root, root / "a", root / "a" / "b"]
    for d in reversed(dirs):
        os.chmod(d, 0o750)
        os.utime(d, (OLD, OLD))
    ws.run("push", "data", expect_rc=0)
    recorded = {d: _mtime_ns(d) for d in dirs}
    shutil.rmtree(root)

    ws.run("pull", "-u", "data", expect_rc=0)

    for d in dirs:
        assert _mtime_ns(d) == recorded[d], d
        assert _mode(d) == 0o750, d


def test_pull_update_settles_the_parent_a_new_directory_landed_in(ws):
    # The sync's mkdir gives the parent a new entry, and with it a fresh
    # mtime - the pull's own side effect, not a local change to keep.
    _pushed_tree(ws)
    root = ws.root / "data"
    recorded = _mtime_ns(root)
    shutil.rmtree(root / "sub")
    os.utime(root, ns=(recorded, recorded))

    ws.run("pull", "-u", "data", expect_rc=0)

    assert (root / "sub" / "b.txt").read_text() == "beta"
    assert _mtime_ns(root) == recorded


def test_pull_update_settles_an_empty_directory_it_recreated(ws):
    # An empty directory has no object behind it, so the metadata apply's own
    # makedirs is what creates it - and what must settle it afterwards.
    ws.write("data/a.txt", "alpha")
    empty = ws.root / "data" / "empty"
    empty.mkdir()
    os.chmod(empty, 0o700)
    os.utime(empty, (OLD, OLD))
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    recorded = _mtime_ns(empty)
    empty.rmdir()

    ws.run("pull", "-u", "data", expect_rc=0)

    assert _mtime_ns(empty) == recorded
    assert _mode(empty) == 0o700


def test_pull_update_single_file_entry(ws):
    f = ws.write("one.conf", "v1")
    ws.config({"one.conf": {"path": str(f)}})
    ws.run("push", "one.conf", expect_rc=0)
    recorded = _mtime_ns(f)

    _rewrite(f, "v2", NEW)
    res = ws.run("pull", "-u", "one.conf", expect_rc=0)
    assert "download:" not in res.out
    assert f.read_text() == "v2"
    assert _mtime_ns(f) == NEW * 1_000_000_000

    _rewrite(f, "v0", OLD)
    res = ws.run("pull", "-u", "one.conf", expect_rc=0)
    assert "download:" in res.out
    assert f.read_text() == "v1"
    assert _mtime_ns(f) == recorded

    _rewrite(f, "v1 and more", None)
    res = ws.run("pull", "-u", "one.conf", expect_rc=0)
    assert f"{CONFLICT}same mtime, size differs" in res.err
    assert f.read_text() == "v1 and more"


def test_pull_update_with_checksum(ws):
    a, b = _pushed_tree(ws)
    recorded_a = _mtime_ns(a)
    os.utime(a, (OLD, OLD))  # content-equal, record newer: no download, mtime applied
    _rewrite(b, "beta edited", NEW)  # content differs, local newer: kept

    res = ws.run("pull", "-u", "--checksum", "data", expect_rc=0)

    assert "download:" not in res.out
    assert _mtime_ns(a) == recorded_a
    assert b.read_text() == "beta edited"


def test_pull_update_with_checksum_content_tie_is_a_conflict(ws):
    a, _b = _pushed_tree(ws)
    _rewrite(a, "delta", None)

    res = ws.run("pull", "-u", "--checksum", "data", expect_rc=0)

    assert f"{CONFLICT}same mtime, content differs" in res.err
    assert a.read_text() == "delta"


def test_pull_update_delete_still_prunes_extras(ws, answers):
    a, _b = _pushed_tree(ws)
    _rewrite(a, "newer edit", NEW)
    extra = ws.write("data/extra.txt", "unpushed")
    answers.feed("y")

    ws.run("pull", "-u", "--delete", "data", expect_rc=0)

    assert not extra.exists()
    assert a.read_text() == "newer edit"  # -u kept the newer side through the re-settle


# --- symlinks ----------------------------------------------------------------


@pytest.mark.skipif(not SYMLINK_MTIME_SUPPORTED, reason="link mtimes are not set here")
def test_update_orders_symlinks_by_their_own_mtime(ws):
    ws.write("data/one.txt", "1")
    ws.write("data/two.txt", "2")
    link = ws.root / "data" / "link"
    os.symlink("one.txt", link)
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    def retarget(target: str, mtime: int) -> None:
        os.remove(link)
        os.symlink(target, link)
        os.utime(link, (mtime, mtime), follow_symlinks=False)

    retarget("two.txt", NEW)
    ws.run("push", "-u", "data", expect_rc=0)
    assert '"link":"two.txt"' in _manifest_body(ws, "data")

    retarget("one.txt", OLD)
    ws.run("push", "-u", "data", expect_rc=0)
    assert '"link":"two.txt"' in _manifest_body(ws, "data")  # the newer record is kept

    ws.run("pull", "-u", "data", expect_rc=0)
    assert os.readlink(link) == "two.txt"  # ...and pull -u restores it

    retarget("one.txt", NEW + 10)
    ws.run("pull", "-u", "data", expect_rc=0)
    assert os.readlink(link) == "one.txt"  # a newer local link is kept


# --- two machines ------------------------------------------------------------


def _two_machines(ws):
    """Machine A pushed prog1 and prog2; machine B pulled them. The entry
    name is shared, so switching its path switches the machine."""
    a, b = ws.root / "A", ws.root / "B"
    ws.write("A/prog1", "one")
    ws.write("A/prog2", "two")
    ws.config({"bin": {"path": str(a)}})
    ws.run("push", "bin", expect_rc=0)
    ws.run("pull", "bin", "-o", str(b), expect_rc=0)
    return a, b


def _on(ws, machine) -> None:
    ws.config({"bin": {"path": str(machine)}})


def _tree(root) -> dict[str, tuple[str, int]]:
    return {
        name: ((root / name).read_text(), _mtime_ns(root / name))
        for name in sorted(os.listdir(root))
        if (root / name).is_file()
    }


@pytest.mark.parametrize("b_pulls_first", [True, False])
def test_two_machines_converge_with_pull_u_and_push_u(ws, b_pulls_first):
    a, b = _two_machines(ws)
    _rewrite(a / "prog1", "one, edited on A", NEW)
    _rewrite(b / "prog2", "two, edited on B", NEW + 100)

    _on(ws, a)
    ws.run("pull", "-u", "bin", expect_rc=0)
    ws.run("push", "-u", "bin", expect_rc=0)

    _on(ws, b)
    order = ("pull", "push") if b_pulls_first else ("push", "pull")
    for command in order:
        ws.run(command, "-u", "bin", expect_rc=0)

    _on(ws, a)
    ws.run("pull", "-u", "bin", expect_rc=0)

    assert _tree(a) == _tree(b)
    assert (a / "prog1").read_text() == "one, edited on A"
    assert (a / "prog2").read_text() == "two, edited on B"
    for machine in (a, b):
        _on(ws, machine)
        assert ws.run("pull", "-u", "bin", expect_rc=0).out == ""
        assert ws.run("push", "-u", "bin", expect_rc=0).out == ""
        assert ws.run("status", "bin", expect_rc=0).out == ""


def test_two_machines_a_locally_deleted_file_comes_back(ws):
    # Deletions do not propagate: an S3-only record is restored like any other.
    a, _b = _two_machines(ws)
    _on(ws, a)
    os.remove(a / "prog2")

    ws.run("pull", "-u", "bin", expect_rc=0)

    assert (a / "prog2").read_text() == "two"


# --- the option itself -------------------------------------------------------


@pytest.mark.parametrize("command", ["push", "pull"])
def test_update_is_listed_in_help(capfd, command):
    from s3bak import cli

    with pytest.raises(SystemExit) as exc:
        cli.main([command, "--help"])

    assert exc.value.code == 0
    assert "-u, --update" in capfd.readouterr().out


@pytest.mark.parametrize("command", ["status", "verify"])
def test_update_is_refused_where_it_does_not_apply(ws, command):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run(command, "-u", "data")

    assert res.rc == 1
    assert "-u/--update only applies" in res.err


# --- the review's cases -------------------------------------------------------


@pytest.mark.skipif(not SYMLINK_MTIME_SUPPORTED, reason="link mtimes are not set here")
def test_push_update_orders_a_file_replacing_a_recorded_symlink(ws):
    ws.write("data/a.txt", "alpha")
    link = ws.root / "data" / "link"
    os.symlink("a.txt", link)
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    recorded = _mtime_ns(link)
    link_record = '"path":"./link","mode":"120'

    os.remove(link)
    link.write_text("a file now")
    os.utime(link, (OLD, OLD))
    res = ws.run("push", "-u", "data", expect_rc=0)
    assert "upload:" not in res.out  # the newer link record is kept
    assert link_record in _manifest_body(ws, "data")

    os.utime(link, ns=(recorded, recorded))
    res = ws.run("push", "-u", "data", expect_rc=0)
    assert f"{CONFLICT}same mtime, type differs" in res.err
    assert link_record in _manifest_body(ws, "data")

    os.utime(link, (NEW, NEW))
    res = ws.run("push", "-u", "data", expect_rc=0)
    assert "upload:" in res.out
    assert '"path":"./link","mode":"100' in _manifest_body(ws, "data")


def test_push_update_reports_a_symlink_replacing_a_file_as_a_type_change(ws):
    a, _b = _pushed_tree(ws)
    recorded = _mtime_ns(a)
    os.remove(a)
    os.symlink("sub/b.txt", a)
    if SYMLINK_MTIME_SUPPORTED:
        os.utime(a, ns=(recorded, recorded), follow_symlinks=False)

    res = ws.run("push", "-u", "data", expect_rc=0)

    assert f"{CONFLICT}same mtime, type differs" in res.err
    assert "link target differs" not in res.err


def test_push_update_single_file_entry_heals_a_missing_object(ws):
    f = ws.write("one.conf", "v1")
    ws.config({"one.conf": {"path": str(f)}})
    ws.run("push", "one.conf", expect_rc=0)
    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/one.conf")
    _rewrite(f, "v0", OLD)  # older than the record - which is now the only copy

    res = ws.run("push", "-u", "one.conf", expect_rc=0)

    assert "upload:" in res.out
    assert _object_body(ws, "one.conf") == "v0"


@pytest.mark.parametrize("checksum", [False, True])
def test_push_update_single_file_entry_drifted_object_is_a_conflict(ws, checksum):
    f = ws.write("one.conf", "v1")
    ws.config({"one.conf": {"path": str(f)}})
    ws.run("push", "one.conf", expect_rc=0)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/one.conf", Body=b"written around s3bak")
    _rewrite(f, "v0", OLD)
    args = ("push", "-u", "--checksum", "one.conf") if checksum else ("push", "-u", "one.conf")

    res = ws.run(*args, expect_rc=0)

    assert f"{CONFLICT}stored object does not match the record" in res.err
    assert _object_body(ws, "one.conf") == "written around s3bak"


def test_pull_update_delete_keeps_a_conflict_through_the_resettle(ws, answers):
    a, _b = _pushed_tree(ws)
    _rewrite(a, "alpha plus", None)  # a tie the sync reports and keeps
    extra = ws.write("data/extra.txt", "unpushed")
    answers.feed("y")

    res = ws.run("pull", "-u", "--delete", "data", expect_rc=0)

    assert res.err.count(f"{CONFLICT}same mtime, size differs") == 1
    assert "restored size does not match" not in res.err
    assert a.read_text() == "alpha plus"
    assert not extra.exists()


def test_pull_update_single_file_root_of_another_kind_is_replaced_whole(ws):
    f = ws.write("one.conf", "v1")
    ws.write("other.conf", "elsewhere")
    ws.config({"one.conf": {"path": str(f)}})
    ws.run("push", "one.conf", expect_rc=0)
    os.remove(f)
    os.symlink("other.conf", f)
    if SYMLINK_MTIME_SUPPORTED:
        os.utime(f, (NEW, NEW), follow_symlinks=False)

    res = ws.run("pull", "-u", "one.conf", expect_rc=0)

    assert "download:" in res.out
    assert not os.path.islink(f)
    assert f.read_text() == "v1"


def test_pull_update_single_file_entry_with_checksum(ws):
    f = ws.write("one.conf", "abc")
    ws.config({"one.conf": {"path": str(f)}})
    ws.run("push", "one.conf", expect_rc=0)
    recorded = _mtime_ns(f)

    _rewrite(f, "abd", None)  # same size, same mtime, other content
    res = ws.run("pull", "-u", "--checksum", "one.conf", expect_rc=0)
    assert f"{CONFLICT}same mtime, content differs" in res.err
    assert f.read_text() == "abd"

    _rewrite(f, "abc", OLD)  # content equal, record newer: metadata only
    res = ws.run("pull", "-u", "--checksum", "one.conf", expect_rc=0)
    assert "download:" not in res.out
    assert _mtime_ns(f) == recorded


def test_pull_update_windows_prep_does_not_disturb_the_rule(ws, monkeypatch):
    # Windows adds the write bit to every read-only local file before the sync
    # (restore.windows_collect_writable_prep). -u must judge, and hand back, the
    # mode from before that. Simulated: the prep and the mode predicate only
    # look at IS_WINDOWS, and chmod works the same here.
    from s3bak import commands, compare, restore

    a, b = _pushed_tree(ws)
    os.chmod(a, 0o444)
    ws.run("push", "data", expect_rc=0)  # the record says read-only
    _rewrite(b, "older edit", OLD)  # past the no-op gate
    for module in (commands, compare, restore):
        monkeypatch.setattr(module, "IS_WINDOWS", True)

    res = ws.run("pull", "-u", "data", expect_rc=0)

    assert CONFLICT not in res.err  # a read-only file under a read-only record is a match
    assert _mode(a) == 0o444  # ...and gets its own mode back, not the prep's
    assert b.read_text() == "beta"


def test_pull_update_delete_fast_path_keeps_a_newer_local_file(ws, answers):
    # Every record matches or is newer locally, so the no-op gate holds and
    # only the --delete pass runs; its re-settle must still keep the newer file.
    a, _b = _pushed_tree(ws)
    _rewrite(a, "alpha plus, newer", NEW)
    extra = ws.write("data/extra.txt", "unpushed")
    answers.feed("y")

    res = ws.run("pull", "-u", "--delete", "data", expect_rc=0)

    assert CONFLICT not in res.err
    assert "restored size does not match" not in res.err
    assert a.read_text() == "alpha plus, newer"
    assert _mtime_ns(a) == NEW * 1_000_000_000
    assert not extra.exists()


def test_pull_update_windows_prep_does_not_mask_a_mode_drift(ws, monkeypatch):
    from s3bak import commands, compare, restore

    a, b = _pushed_tree(ws)  # the record says writable
    os.chmod(a, 0o444)  # a local read-only drift, mtime tied
    _rewrite(b, "older edit", OLD)
    for module in (commands, compare, restore):
        monkeypatch.setattr(module, "IS_WINDOWS", True)

    res = ws.run("pull", "-u", "data", expect_rc=0)

    assert f"{CONFLICT}same mtime, mode differs" in res.err
    assert _mode(a) == 0o444  # kept, with the prep's write bit taken back


def test_pull_update_drifted_object_under_a_newer_local_file_is_a_conflict(ws):
    a, _b = _pushed_tree(ws)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt", Body=b"written around s3bak")
    _rewrite(a, "newer edit", NEW)  # the only difference the gate can see

    res = ws.run("pull", "-u", "data", expect_rc=0)

    assert f"{CONFLICT}stored object does not match the record" in res.err
    assert a.read_text() == "newer edit"


def test_push_update_single_file_entry_drifted_object_beats_a_newer_local_file(ws):
    f = ws.write("one.conf", "v1")
    ws.config({"one.conf": {"path": str(f)}})
    ws.run("push", "one.conf", expect_rc=0)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/one.conf", Body=b"written around s3bak")
    _rewrite(f, "v2", NEW)

    res = ws.run("push", "-u", "one.conf", expect_rc=0)

    assert f"{CONFLICT}stored object does not match the record" in res.err
    assert "upload:" not in res.out
    assert _object_body(ws, "one.conf") == "written around s3bak"


def test_pull_update_single_file_entry_drifted_object_is_a_conflict(ws):
    f = ws.write("one.conf", "v1")
    ws.config({"one.conf": {"path": str(f)}})
    ws.run("push", "one.conf", expect_rc=0)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/one.conf", Body=b"written around s3bak")
    _rewrite(f, "v0", OLD)  # the record is the newer side, but describes neither copy

    res = ws.run("pull", "-u", "one.conf", expect_rc=0)

    assert f"{CONFLICT}stored object does not match the record" in res.err
    assert "download:" not in res.out
    assert f.read_text() == "v0"


def test_pull_update_symlink_subpath_root_of_another_kind_is_replaced_whole(ws):
    ws.write("data/a.txt", "alpha")
    link = ws.root / "data" / "link"
    os.symlink("a.txt", link)
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    os.remove(link)
    link.write_text("a file now")
    os.utime(link, (NEW, NEW))

    res = ws.run("pull", "-u", "data/link", expect_rc=0)

    assert CONFLICT not in res.err
    assert os.path.islink(link)
    assert os.readlink(link) == "a.txt"


def test_pull_update_dry_run_previews_a_replaced_root_without_ordering_it(ws):
    f = ws.write("one.conf", "v1")
    ws.write("other.conf", "elsewhere")
    ws.config({"one.conf": {"path": str(f)}})
    ws.run("push", "one.conf", expect_rc=0)
    os.remove(f)
    os.symlink("other.conf", f)
    if SYMLINK_MTIME_SUPPORTED:
        os.utime(f, (NEW, NEW), follow_symlinks=False)

    res = ws.run("pull", "-u", "--dry-run", "-v", "one.conf", expect_rc=0)

    assert "(dry-run) would replace" in res.out
    assert CONFLICT not in res.err
    assert "skip (local is newer)" not in res.err
    assert os.path.islink(f)  # a rehearsal changes nothing
