"""status / diff / show behaviour against the live endpoint."""

from __future__ import annotations

import os
import re
import signal
import subprocess

import pytest

MTIME_DETAIL = re.compile(r"mtime: remote=(.+) [<>] local=(.+) \((.+)\)")


def test_status_clean_then_reports_changes(ws):
    ws.write("data/a.txt", "alpha")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    # Nothing changed -> no output.
    res = ws.run("status", "data", expect_rc=0)
    assert res.out.strip() == ""

    # Modify one file, add another -> M and A lines.
    (ws.root / "data" / "a.txt").write_text("changed!!")
    (ws.root / "data" / "c.txt").write_text("new")
    res = ws.run("status", "data", expect_rc=0)
    lines = res.out.splitlines()
    assert any(line.startswith("M") and "a.txt" in line for line in lines)
    assert any(line.startswith("A") and "c.txt" in line for line in lines)


def test_status_reports_m_d_a_interleaved_in_key_order(ws):
    # status is one merge-join over the manifest and a fresh walk, so every
    # line - M, D, and A alike - comes out in S3 key order (A is no longer
    # batched at the end). The root's own record sorts first (empty compare
    # key) and shows M too: the additions and the deletion all bumped the
    # directory's own mtime. D appears under --delete alone (a plain push
    # touches nothing at a manifest-only key); plain status shows the rest.
    ws.write("data/b.txt", "b")
    ws.write("data/d.txt", "d")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "a.txt").write_text("new")  # A, sorts first
    (ws.root / "data" / "b.txt").write_text("changed!")  # M
    (ws.root / "data" / "d.txt").unlink()  # D under --delete
    (ws.root / "data" / "e.txt").write_text("new2")  # A, sorts last

    res = ws.run("status", "data", expect_rc=0)
    marks = [(line.split()[0], os.path.basename(line.split()[1])) for line in res.out.splitlines()]
    assert marks == [
        ("M", "data"),
        ("A", "a.txt"),
        ("M", "b.txt"),
        ("A", "e.txt"),
    ]

    res = ws.run("status", "--delete", "data", expect_rc=0)
    marks = [(line.split()[0], os.path.basename(line.split()[1])) for line in res.out.splitlines()]
    assert marks == [
        ("M", "data"),
        ("A", "a.txt"),
        ("M", "b.txt"),
        ("D", "d.txt"),
        ("A", "e.txt"),
    ]


def test_status_excluded_paths_are_invisible_to_plain_status(ws):
    # An excluded path never reports at all in plain status - not the local
    # file (never an A), not the residue record (plain status prints no D).
    # status --delete, the preview of push --delete, shows the residue as D.
    ws.write("data/keep.txt", "k")
    ws.write("data/old.log", "o")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)  # old.log recorded: no excludes yet

    ws.write("data/new.log", "n")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*.log"]}})
    res = ws.run("status", "data", expect_rc=0)
    lines = res.out.splitlines()
    # Creating new.log bumped the root directory's own mtime - a genuine M a
    # push would settle - but neither .log path reports anything.
    assert not any(".log" in line for line in lines)
    assert not any(line.startswith("D") for line in lines)

    res = ws.run("status", "--delete", "data", expect_rc=0)
    lines = res.out.splitlines()
    assert not any("new.log" in line for line in lines)
    assert any(line.startswith("D") and "old.log" in line for line in lines)


def test_status_missing_subpath_errors(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("status", str(ws.root / "data" / "nope"))
    assert res.rc != 0
    assert "not found" in (res.err + res.out).lower()


def test_status_of_missing_directory_tree_reports_each_child(ws):
    # When the whole local tree is gone, status must classify the entry as a
    # directory from the manifest (not os.path.isdir of the missing path), so
    # each record maps to its own child path instead of folding onto one and
    # printing duplicate lines.
    import shutil

    ws.write("data/a.txt", "alpha")
    ws.write("data/sub/b.txt", "beta")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    shutil.rmtree(ws.root / "data")

    res = ws.run("status", "data", expect_rc=0)
    assert res.out.strip() == ""  # a plain push would touch nothing

    res = ws.run("status", "--delete", "data", expect_rc=0)
    d_targets = [ln.split(None, 1)[1] for ln in res.out.splitlines() if ln.startswith("D")]
    assert any(t.endswith("a.txt") for t in d_targets)
    assert any(t.endswith(os.path.join("sub", "b.txt")) for t in d_targets)
    assert len(d_targets) == len(set(d_targets))  # no folded duplicates


def test_status_reports_type_change_as_m_with_type_tag(ws):
    # A regular file replaced by a symlink keeps its key, so the pair
    # reports M with a `type` tag - a plain push acts on it (re-records the
    # kind) - never a D.
    ws.write("data/real.txt", "content")
    ws.write("data/u.txt", "u")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "u.txt").unlink()
    os.symlink("real.txt", ws.root / "data" / "u.txt")

    res = ws.run("status", "data", expect_rc=0)
    lines = res.out.splitlines()
    assert any(ln.startswith("M") and "u.txt" in ln and "type" in ln for ln in lines)
    assert not any(ln.startswith("D") for ln in lines)

    res = ws.run("status", "-v", "data", expect_rc=0)
    assert "type: remote=regular file local=symlink" in res.out


def test_status_detects_changed_symlink_target(ws):
    ws.write("data/real.txt", "r")
    ws.write("data/other.txt", "o")
    os.symlink("real.txt", ws.root / "data" / "link")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "link").unlink()
    os.symlink("other.txt", ws.root / "data" / "link")  # retarget
    res = ws.run("status", "data", expect_rc=0)
    assert "link" in res.out


def test_status_verbose_humanizes_large_size_diff(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "a.txt").write_text("y" * 5000)  # grow by ~5 KB
    res = ws.run("status", "--verbose", "data")
    assert "a.txt" in res.out
    assert "KB" in res.out  # humanized size detail shown in verbose mode


def test_status_verbose_reports_mtime_change(ws):
    f = ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    os.utime(f, (1_600_000_000, 1_600_000_000))  # change mtime only
    res = ws.run("status", "--verbose", "data")
    assert "a.txt" in res.out


def test_diff_shows_content_changes(ws):
    ws.write("data/a.txt", "one\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "a.txt").write_text("two\n")
    res = ws.run("diff", "data")
    assert "-one" in res.out
    assert "+two" in res.out


def test_diff_identical_returns_zero_no_output(ws):
    ws.write("data/a.txt", "same\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("diff", "data", expect_rc=0)
    assert res.out.strip() == ""


def test_diff_reports_new_local_file(ws):
    ws.write("data/a.txt", "a\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "new.txt").write_text("brand new\n")
    res = ws.run("diff", "data")
    assert res.rc == 1
    assert "brand new" in res.out


def test_diff_single_file_entry_shows_change(ws):
    f = ws.write("solo.txt", "v1\n")
    ws.config({"solo.txt": {"path": str(f)}})
    ws.run("push", "solo.txt", expect_rc=0)

    f.write_text("v2\n")
    res = ws.run("diff", "solo.txt")
    assert res.rc == 1
    assert "-v1" in res.out
    assert "+v2" in res.out


def test_diff_reports_removed_local_file(ws):
    ws.write("data/a.txt", "a\n")
    ws.write("data/gone.txt", "will be deleted\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "gone.txt").unlink()
    res = ws.run("diff", "data")
    assert res.rc == 1
    assert "will be deleted" in res.out


def test_diff_classifies_deleted_local_directory_from_manifest(ws):
    import shutil

    ws.write("data/a.txt", "from backup\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    shutil.rmtree(ws.root / "data")

    res = ws.run("diff", "data")

    assert res.rc == 1
    assert "from backup" in res.out


def test_diff_directory_subpath(ws):
    ws.write("data/sub/a.txt", "v1\n")
    ws.write("data/elsewhere.txt", "unchanged\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    (ws.root / "data" / "sub" / "a.txt").write_text("v2\n")

    res = ws.run("diff", "data/sub")

    assert res.rc == 1
    assert "-v1" in res.out
    assert "+v2" in res.out
    assert "elsewhere" not in res.out


def test_diff_whole_directory_understands_manifest_symlinks(ws):
    ws.write("data/a.txt", "a\n")
    ws.write("data/b.txt", "b\n")
    os.symlink("a.txt", ws.root / "data" / "link")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    clean = ws.run("diff", "data", expect_rc=0)
    assert clean.out == ""

    (ws.root / "data" / "link").unlink()
    os.symlink("b.txt", ws.root / "data" / "link")
    changed = ws.run("diff", "data")
    assert changed.rc == 1
    assert "symlink" in changed.out
    assert "a.txt" in changed.out
    assert "b.txt" in changed.out


def test_diff_does_not_follow_symlink_replacing_regular_file(ws):
    local = ws.write("data/a.txt", "backup\n")
    victim = ws.write("outside.txt", "must not be disclosed\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    local.unlink()
    os.symlink(victim, local)

    res = ws.run("diff", "data")

    assert res.rc == 1
    assert "symlink" in res.out
    assert "must not be disclosed" not in res.out


def test_diff_ignores_s3_object_not_in_manifest(ws):
    ws.write("data/a.txt", "a\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    # An orphan object (e.g. left by a --meta-only push after a local delete)
    # is not part of the backup: the source-of-truth manifest defines it.
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/sub/orphan.txt", Body=b"old\n")

    res = ws.run("diff", "data", expect_rc=0)
    assert res.out == ""


def test_diff_subpath_file(ws):
    ws.write("data/sub/b.txt", "v1\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "sub" / "b.txt").write_text("v2\n")
    res = ws.run("diff", str(ws.root / "data" / "sub" / "b.txt"))
    assert res.rc == 1
    assert "-v1" in res.out
    assert "+v2" in res.out


def test_diff_output_interleaves_record_types_in_manifest_key_order(ws):
    # diff_backup is one streaming merge-join over the manifest and a fresh
    # local walk (docs/manifest.md's Ordering invariant), so a regular file,
    # a symlink, and a local-only addition must come out in S3 key order, not
    # batched by record type (files, then symlinks, then local-only, as the
    # old three-dict implementation produced them).
    ws.write("data/z_file", "v1\n")
    os.symlink("nowhere1", ws.root / "data" / "a_link")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    os.remove(ws.root / "data" / "a_link")
    os.symlink("nowhere2", ws.root / "data" / "a_link")  # retarget: symlink diff
    (ws.root / "data" / "z_file").write_text("v2\n")  # content diff, sorts last
    (ws.root / "data" / "m_extra").write_text("new\n")  # local-only, sorts between

    res = ws.run("diff", "data")
    assert res.rc == 1

    pos_link = res.out.index("a_link")
    pos_extra = res.out.index("m_extra")
    pos_file = res.out.index("z_file")
    assert pos_link < pos_extra < pos_file


def test_diff_stages_at_most_one_downloaded_object_at_a_time(ws, monkeypatch):
    # diff_backup downloads recorded regular files one at a time into a fixed
    # per-run staging name, removing each right after its own compare - so
    # disk use never grows with the number of changed files. Wrap get_object
    # to observe the staging directory just before each download starts: any
    # object left over from a previous record would show up here.
    from s3bak.store import Boto3S3Store

    ws.write("data/a.txt", "aaa\n")
    ws.write("data/b.txt", "bbb\n")
    ws.write("data/c.txt", "ccc\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "a.txt").write_text("AAA\n")
    (ws.root / "data" / "b.txt").write_text("BBB\n")
    (ws.root / "data" / "c.txt").write_text("CCC\n")

    pre_counts: list[int] = []
    original_get_object = Boto3S3Store.get_object

    def wrapped_get_object(self, rel_key, dest_path, **kwargs):
        if os.path.basename(dest_path) == "object":  # diff_backup's staging name
            pre_counts.append(len(os.listdir(os.path.dirname(dest_path))))
        return original_get_object(self, rel_key, dest_path, **kwargs)

    monkeypatch.setattr(Boto3S3Store, "get_object", wrapped_get_object)

    res = ws.run("diff", "data")
    assert res.rc == 1
    assert len(pre_counts) == 3  # one download per changed regular file
    assert pre_counts == [0, 0, 0]  # nothing left staged from a prior record


def test_diff_regular_file_replaced_by_directory_reports_type_diff(ws):
    # A manifest file record's sort key ("a.txt") never pairs with a local
    # directory of the same name (sort key "a.txt/") in the merge-join, so
    # the walk lane comes back empty for this record - not because the path
    # is missing, but because its key shape changed. diff_backup must fall
    # back to a direct lstat and report the type change, not misread the
    # unpaired record as "locally gone" and dump the whole backup content
    # against /dev/null.
    ws.write("data/a.txt", "hello\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / "a.txt").unlink()
    (ws.root / "data" / "a.txt").mkdir()

    res = ws.run("diff", "data")
    assert res.rc == 1
    assert "-regular file" in res.out
    assert "+directory" in res.out
    assert "-hello" not in res.out  # not a devnull content dump


def test_diff_compares_excluded_recorded_file_against_real_local_content(ws):
    # An exclude pattern hides a path from local_keyed's walk, but a manifest
    # record for it can still exist (recorded before the exclude was added).
    # "not walked" must not be read as "locally missing" - apply_manifest
    # already relies on the same direct-lstat fallback for excluded paths it
    # still has to repair (restore.py's apply_manifest), and diff must follow
    # the same principle: compare the excluded file's real content, not
    # report it as removed.
    ws.write("data/old.log", "same\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)  # old.log recorded before any exclude

    ws.config({"data": {"path": str(ws.root / "data"), "excludes": ["*.log"]}})

    # Unchanged content, now excluded: still identical -> clean diff, proving
    # the real file was compared rather than treated as absent.
    clean = ws.run("diff", "data", expect_rc=0)
    assert clean.out == ""

    # Changed content, still excluded: a real content diff, not a "missing".
    (ws.root / "data" / "old.log").write_text("changed\n")
    res = ws.run("diff", "data")
    assert res.rc == 1
    assert "-same" in res.out
    assert "+changed" in res.out


def test_show_streams_file_to_stdout(ws):
    ws.write("data/a.txt", "hello\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("show", str(ws.root / "data" / "a.txt"), expect_rc=0)
    assert res.out == "hello\n"


def test_show_dotfile_subpath(ws):
    # Regression: file names were once cleaned with lstrip("./"), which strips
    # *characters* and mangled a dotfile (".bashrc" -> "bashrc") into a
    # nonexistent S3 key.
    ws.write("data/.bashrc", "export A=1\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    res = ws.run("show", str(ws.root / "data" / ".bashrc"), expect_rc=0)
    assert res.out == "export A=1\n"


def test_diff_dotfile_subpath(ws):
    # Same lstrip regression on the diff path.
    ws.write("data/.bashrc", "v1\n")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    (ws.root / "data" / ".bashrc").write_text("v2\n")
    res = ws.run("diff", str(ws.root / "data" / ".bashrc"))
    assert res.rc == 1
    assert "-v1" in res.out
    assert "+v2" in res.out


def test_status_of_unpushed_entry_reports_error(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    res = ws.run("status", "data")
    assert res.rc == 1
    assert "not found on s3" in res.err.lower()


def test_status_verbose_shows_subsecond_mtime_drift(ws):
    # A drift below one second (e.g. WSL2 drvfs truncating a restored mtime to
    # whole seconds) used to render as two identical second-precision
    # timestamps with a floor-divided "(-1s)". The detail line must show the
    # fractional digits that actually differ and a fractional diff.
    ws.write("data/a.txt", "alpha")
    ws.config({"data": {"path": str(ws.root / "data")}})
    p = ws.root / "data" / "a.txt"
    base = (os.lstat(p).st_mtime_ns // 1_000_000_000) * 1_000_000_000
    os.utime(p, ns=(base, base))  # whole-second mtime recorded in the manifest
    ws.run("push", "data", expect_rc=0)

    drifted = base - 961_276_900
    os.utime(p, ns=(drifted, drifted))
    res = ws.run("status", "-v", "data", expect_rc=0)
    m = MTIME_DETAIL.search(res.out)
    assert m is not None
    remote_disp, local_disp, diff_str = m.groups()
    assert remote_disp != local_disp
    assert remote_disp.endswith(".0")
    assert local_disp.endswith(".0387231")
    assert diff_str == "-0.9612769s"


def test_status_verbose_mtime_diff_matches_displayed_seconds(ws):
    # At one second or more the timestamps stay whole-second, and the diff is
    # the difference of the two displayed values (not a floor-divided delta
    # that can disagree with them by one).
    ws.write("data/a.txt", "alpha")
    ws.config({"data": {"path": str(ws.root / "data")}})
    p = ws.root / "data" / "a.txt"
    base = (os.lstat(p).st_mtime_ns // 1_000_000_000) * 1_000_000_000
    remote = base + 500_000_000  # manifest keeps a fractional part
    os.utime(p, ns=(remote, remote))
    ws.run("push", "data", expect_rc=0)

    local = base - 1_000_000_000  # displayed seconds differ by 1, exact by 1.5
    os.utime(p, ns=(local, local))
    res = ws.run("status", "-v", "data", expect_rc=0)
    m = MTIME_DETAIL.search(res.out)
    assert m is not None
    remote_disp, local_disp, diff_str = m.groups()
    assert "." not in remote_disp and "." not in local_disp
    assert diff_str == "-1s"


def test_diff_of_unpushed_single_file_reports_error(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    ws.write("data/new.txt", "n")
    res = ws.run("diff", str(ws.root / "data" / "new.txt"))
    assert res.rc == 1
    assert "not found on s3" in res.err.lower()


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs an unsearchable parent")
def test_status_warns_when_entry_path_is_unreadable(ws):
    # An unreadable entry path (unsearchable parent) is not "absent": status must
    # not silently report every record D and exit 0 - it warns that the
    # comparison could not be made.
    ws.write("locked/data/a.txt", "alpha")
    ws.config({"data": {"path": str(ws.root / "locked" / "data")}})
    ws.run("push", "data", expect_rc=0)
    locked = ws.root / "locked"
    os.chmod(locked, 0o000)
    try:
        res = ws.run("status", "data")
        assert "cannot read" in res.err
    finally:
        os.chmod(locked, 0o755)


def test_run_diff_maps_sigpipe_to_broken_pipe(monkeypatch):
    # A reader that closes the pipe (`s3bak diff | head`) makes the diff child die
    # with SIGPIPE; that maps to the documented 141, not a plain 1.
    from s3bak import commands
    from s3bak.config import Opts

    monkeypatch.setattr(
        commands.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, -signal.SIGPIPE),
    )
    with pytest.raises(BrokenPipeError):
        commands._run_diff("a", "b", "label", Opts())


def test_run_diff_on_windows_does_not_touch_sigpipe(monkeypatch):
    # signal.SIGPIPE does not exist on Windows. Simulate that by both flipping
    # IS_WINDOWS and removing the attribute, so a fix that merely reorders the
    # check without actually branching on IS_WINDOWS still raises AttributeError
    # here and fails the test.
    from s3bak import commands
    from s3bak.config import Opts

    monkeypatch.setattr(commands, "IS_WINDOWS", True)
    monkeypatch.delattr(signal, "SIGPIPE", raising=False)
    monkeypatch.setattr(
        commands.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0),
    )
    assert commands._run_diff("a", "b", "label", Opts()) == 0
