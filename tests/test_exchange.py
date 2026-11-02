"""exchange: every target pulled, then every target pushed, with the same
options (docs/cli.md, "exchange"). A warnings-only pull still pushes; a pull
that failed - an error, a q at a prompt - does not."""

from __future__ import annotations

import os
import sys

import pytest

from s3bak.console import console

NEW = 2_000_000_000  # 2033: newer than any record written by the suite


def _object_body(ws, key: str) -> str:
    return ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/{key}")["Body"].read().decode()


def _pin(p, content: str, mtime: int) -> None:
    p.write_text(content)
    os.utime(p, (mtime, mtime))


def _two_machines(ws):
    """Machine A pushed prog1 and prog2; machine B pulled them. The entry
    name is shared, so switching its path switches the machine."""
    a, b = ws.root / "A", ws.root / "B"
    ws.write("A/prog1", "one")
    ws.write("A/prog2", "two")
    _on(ws, a)
    ws.run("push", "bin", expect_rc=0)
    ws.run("pull", "bin", "-o", str(b), expect_rc=0)
    return a, b


def _on(ws, machine) -> None:
    ws.config({"bin": {"path": str(machine)}})


def _tree(root) -> dict[str, tuple[str, int]]:
    return {
        name: ((root / name).read_text(), os.lstat(root / name).st_mtime_ns)
        for name in sorted(os.listdir(root))
        if (root / name).is_file()
    }


def _pushed_tree(ws, **entry):
    ws.write("data/a.txt", "alpha")
    ws.write("data/b.txt", "beta")
    ws.config({"data": {"path": str(ws.root / "data"), **entry}})
    ws.run("push", "data", expect_rc=0)
    return ws.root / "data"


def test_two_machines_converge_with_exchange_u(ws):
    a, b = _two_machines(ws)
    _pin(a / "prog1", "one, edited on A", NEW)
    _pin(b / "prog2", "two, edited on B", NEW + 100)

    _on(ws, b)
    ws.run("exchange", "-u", "bin", expect_rc=0)
    _on(ws, a)
    res = ws.run("exchange", "-u", "bin", expect_rc=0)
    # A pulled B's edit before pushing its own: the pull comes first.
    assert res.out.index("download:") < res.out.index("upload:")
    assert "prog2" in res.out.splitlines()[0]
    _on(ws, b)
    ws.run("exchange", "-u", "bin", expect_rc=0)

    assert _tree(a) == _tree(b)
    assert (b / "prog1").read_text() == "one, edited on A"
    assert (a / "prog2").read_text() == "two, edited on B"
    for machine in (a, b):
        _on(ws, machine)
        assert ws.run("exchange", "-u", "bin", expect_rc=0).out == ""


def test_exchange_passes_the_options_to_both_commands(ws):
    data = _pushed_tree(ws)
    # Without -u the pull would restore the backup's "alpha" over this newer
    # edit, and the push would then have nothing to send.
    _pin(data / "a.txt", "alpha, edited", NEW)

    res = ws.run("exchange", "-u", "data", expect_rc=0)

    # The pull got -u. (That the push got it too is what the conflict test
    # below shows: a plain push would upload the conflicting copy.)
    assert "download:" not in res.out
    assert _object_body(ws, "data/a.txt") == "alpha, edited"


def test_exchange_without_u_lets_the_backup_win(ws):
    data = _pushed_tree(ws)
    _pin(data / "a.txt", "alpha, edited", NEW)

    res = ws.run("exchange", "data", expect_rc=0)

    assert (data / "a.txt").read_text() == "alpha"
    assert "upload:" not in res.out
    assert _object_body(ws, "data/a.txt") == "alpha"


def test_a_warnings_only_pull_still_pushes(ws):
    data = _pushed_tree(ws)
    # A conflict: the same mtime, a different size - the pull warns.
    before = os.lstat(data / "a.txt").st_mtime_ns
    (data / "a.txt").write_text("alpha, same mtime")
    os.utime(data / "a.txt", ns=(before, before))
    ws.write("data/new.txt", "new")
    warnings = console.warning_count()

    res = ws.run("exchange", "-u", "data", expect_rc=0)

    assert "warning: conflict - " in res.err
    # cli.main leaves the warnings -> exit 2 mapping to cli.run.
    assert console.warning_count() > warnings
    assert _object_body(ws, "data/new.txt") == "new"
    # The conflict stays unresolved on both sides: the pull kept the local
    # copy, and the push - given -u as well - did not upload it.
    assert (data / "a.txt").read_text() == "alpha, same mtime"
    assert _object_body(ws, "data/a.txt") == "alpha"


def test_a_stale_record_the_pull_skips_is_settled_by_the_push(ws):
    data = _pushed_tree(ws)
    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/a.txt")
    os.remove(data / "a.txt")

    res = ws.run("exchange", "data", expect_rc=0)

    assert "warning: no data object behind this record" in res.err
    manifest_key = f"{ws.prefix}/data-manifest.jsonl"
    body = ws.s3.get_object(Bucket=ws.bucket, Key=manifest_key)["Body"].read().decode()
    assert "a.txt" not in body  # the push retired the stale record
    assert "b.txt" in body


def test_a_failed_pull_stops_before_the_push(ws):
    data = _pushed_tree(ws)
    # A directory where the backup records a file: the plain pull cannot
    # restore it and fails (exit 1).
    os.remove(data / "a.txt")
    ws.write("data/a.txt/inner.txt", "inner")
    ws.write("data/new.txt", "new")

    res = ws.run("exchange", "data")

    assert res.rc == 1
    assert "upload:" not in res.out
    assert "data/new.txt" not in ws.keys()
    assert "the pull did not finish, so nothing was pushed" in res.err


def test_a_q_at_the_pull_prompt_stops_before_the_push(ws, answers):
    _pushed_tree(ws)
    ws.write("data/extra.txt", "only local")
    answers.feed("q")

    res = ws.run("exchange", "--delete", "data")

    assert res.rc == 1
    assert len(answers.prompts) == 1
    assert (ws.root / "data" / "extra.txt").exists()  # q kept it, and the push never ran
    assert "data/extra.txt" not in ws.keys()
    assert "nothing was pushed" in res.err


def test_the_push_status_is_the_exchange_status(ws):
    data = _pushed_tree(ws)
    hook = [sys.executable, "-c", "raise SystemExit(5)"]
    ws.config({"data": {"path": str(data), "pre_hook": hook}})
    _pin(data / "a.txt", "alpha, edited", NEW)
    os.remove(data / "b.txt")

    res = ws.run("exchange", "-u", "data")

    assert res.rc == 5  # the pre_hook's own status, from the push
    assert (data / "b.txt").read_text() == "beta"  # the pull ran first
    assert _object_body(ws, "data/a.txt") == "alpha"  # the push stopped at its hook


def test_exchange_dry_run_changes_nothing(ws):
    data = _pushed_tree(ws)
    _pin(data / "a.txt", "alpha, edited", NEW)
    os.remove(data / "b.txt")

    res = ws.run("exchange", "-u", "--dry-run", "data", expect_rc=0)

    lines = res.out.splitlines()
    assert any(line.startswith("(dry-run) download: ") and "b.txt" in line for line in lines)
    assert any(line.startswith("(dry-run) upload: ") and "a.txt" in line for line in lines)
    assert not (data / "b.txt").exists()
    assert (data / "a.txt").read_text() == "alpha, edited"
    assert _object_body(ws, "data/a.txt") == "alpha"


def _two_entries(ws):
    """Entries one and two, both pushed; each then has a file only in the
    backup (for its pull) and one only local (for its push)."""
    for name in ("one", "two"):
        ws.write(f"{name}/old.txt", f"old {name}")
    entries = {name: {"path": str(ws.root / name)} for name in ("one", "two")}
    ws.config(entries)
    ws.run("push", "--all", expect_rc=0)
    for name in ("one", "two"):
        os.remove(ws.root / name / "old.txt")
        ws.write(f"{name}/new.txt", f"new {name}")
    return entries


def test_exchange_pulls_every_target_before_pushing_any(ws):
    _two_entries(ws)

    res = ws.run("exchange", "-u", "--all", expect_rc=0)

    lines = res.out.splitlines()
    downloads = [i for i, line in enumerate(lines) if line.startswith("download: ")]
    uploads = [i for i, line in enumerate(lines) if line.startswith("upload: ")]
    assert len(downloads) == 2 and len(uploads) == 2
    assert max(downloads) < min(uploads)
    assert {"one/new.txt", "two/new.txt"} <= ws.keys()
    assert (ws.root / "one" / "old.txt").exists() and (ws.root / "two" / "old.txt").exists()


def test_one_failing_pull_stops_the_push_of_every_target(ws):
    entries = _two_entries(ws)
    ws.write("three/a.txt", "never pushed")
    entries["three"] = {"path": str(ws.root / "three")}
    ws.config(entries)

    res = ws.run("exchange", "-u", "one", "three")

    assert res.rc == 1
    assert "entry not found on S3: three" in res.err
    assert "the pull did not finish, so nothing was pushed" in res.err
    assert (ws.root / "one" / "old.txt").exists()  # one's own pull ran
    assert "one/new.txt" not in ws.keys()
    assert "three/a.txt" not in ws.keys()


def test_exchange_checks_its_targets_before_pulling(ws):
    ws.write("outer/inner/a.txt", "a")
    ws.config(
        {
            "outer": {"path": str(ws.root / "outer")},
            "inner": {"path": str(ws.root / "outer" / "inner")},
        }
    )

    res = ws.run("exchange", "outer", "inner")

    assert res.rc == 1
    assert "pull restore destinations overlap" in res.err


def test_exchange_rejects_one_entry_named_with_two_targets(ws):
    _pushed_tree(ws)

    res = ws.run("exchange", "data", "data/a.txt")

    assert res.rc == 1
    assert "duplicate entry in exchange: data" in res.err


@pytest.mark.parametrize(
    ("option", "message"),
    [
        (("-o", "/tmp/elsewhere"), "-o/--output only applies to pull"),
        (("--color",), "--color only applies to status and diff"),
    ],
)
def test_exchange_refuses_options_one_side_does_not_take(ws, option, message):
    _pushed_tree(ws)

    res = ws.run("exchange", *option, "data")

    assert res.rc == 1
    assert message in res.err


def test_exchange_help(capfd):
    from s3bak import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["exchange", "--help"])

    assert exc.value.code == 0
    out = capfd.readouterr().out
    assert "s3bak exchange [options] <entry|path>..." in out
    assert "-u, --update" in out
    assert "-o, --output" not in out
    assert "y/n/a/d/q" in out
