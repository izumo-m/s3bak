"""Entry groups: named sets of entry names, expanded during argument
resolution. Groups are a config/CLI concept only - none ever reaches S3."""

from __future__ import annotations

import sys

import pytest


def _recording_hook(ws, log) -> list[str]:
    """A hook that records every run of itself as one line in `log`, so a test
    can tell "ran" from "ran twice"."""
    script = ws.write(
        "record-run.py",
        "from pathlib import Path\nimport sys\n"
        "with Path(sys.argv[1]).open('a') as f:\n"
        "    f.write('ran\\n')\n",
    )
    return [sys.executable, str(script), str(log)]


@pytest.fixture
def group_ws(ws):
    """Two entries with one file each, plus the group naming both."""
    ws.write("a/a.txt", "a")
    ws.write("b/b.txt", "b")
    ws.config(
        {"a": {"path": str(ws.root / "a")}, "b": {"path": str(ws.root / "b")}},
        groups={"both": ["a", "b"]},
    )
    return ws


# =============================================================================
# Expansion
# =============================================================================


def test_push_of_a_group_pushes_every_member(group_ws):
    group_ws.run("push", "both", expect_rc=0)

    keys = group_ws.keys()
    assert {"a/a.txt", "b/b.txt", "a-manifest.jsonl", "b-manifest.jsonl"} <= keys
    # The group is a way of naming entries, not a place on S3.
    assert not any(key.startswith("both") for key in keys)


@pytest.mark.parametrize("arg", ["both/", "both/."])
def test_a_trailing_slash_names_the_whole_group(group_ws, arg):
    # `a/` and `a/.` are the whole entry a, and a group spells the same forms
    # the same way; only a real sub path under a group is refused.
    group_ws.run("push", arg, expect_rc=0)

    assert {"a/a.txt", "b/b.txt"} <= group_ws.keys()


def test_status_of_a_group_covers_every_member(group_ws):
    group_ws.run("push", "both", expect_rc=0)
    group_ws.write("a/new-a.txt", "x")
    group_ws.write("b/new-b.txt", "x")

    res = group_ws.run("status", "both", expect_rc=0)

    assert str(group_ws.root / "a" / "new-a.txt") in res.out
    assert str(group_ws.root / "b" / "new-b.txt") in res.out


def test_verify_of_a_group_covers_every_member(group_ws):
    group_ws.run("push", "both", expect_rc=0)

    res = group_ws.run("verify", "both", expect_rc=0)

    assert "a: OK" in res.out
    assert "b: OK" in res.out
    assert res.err == ""


def test_pull_of_a_group_restores_every_member(group_ws):
    group_ws.run("push", "both", expect_rc=0)
    (group_ws.root / "a" / "a.txt").unlink()
    (group_ws.root / "b" / "b.txt").unlink()

    group_ws.run("pull", "both", expect_rc=0)

    assert (group_ws.root / "a" / "a.txt").read_text() == "a"
    assert (group_ws.root / "b" / "b.txt").read_text() == "b"


def test_nested_groups_expand_depth_first_keeping_the_first_occurrence(ws):
    from s3bak import cli

    ws.config(
        {name: {"path": str(ws.root / name)} for name in ("a", "b", "c")},
        groups={"inner": ["c", "b"], "outer": ["inner", "a", "b"]},
    )

    cfg = cli.load_config(create_store=False)

    assert cfg.groups["inner"] == ["c", "b"]
    assert cfg.groups["outer"] == ["c", "b", "a"]


def test_deeply_nested_groups_with_repeated_members_expand_once(ws):
    # Every level names the next one twice, so an expansion that forgot a
    # finished group would double its work at each level and never return.
    from s3bak import cli

    depth = 40
    ws.config(
        {f"e{i}": {"path": str(ws.root / f"e{i}")} for i in range(depth)},
        groups={
            f"g{i}": [f"e{i}", *([f"g{i + 1}", f"g{i + 1}"] if i + 1 < depth else [])]
            for i in range(depth)
        },
    )

    cfg = cli.load_config(create_store=False)

    assert cfg.groups["g0"] == [f"e{i}" for i in range(depth)]


def test_an_entry_may_belong_to_several_groups(ws):
    from s3bak import cli

    ws.config(
        {name: {"path": str(ws.root / name)} for name in ("a", "b")},
        # "left" also repeats a member, which expansion settles.
        groups={"left": ["a", "b", "a"], "right": ["b"]},
    )

    cfg = cli.load_config(create_store=False)

    assert cfg.groups == {"left": ["a", "b"], "right": ["b"]}


def test_a_group_and_one_of_its_members_named_together_run_once(group_ws):
    # Two ways of asking for the same entry are one request, not a conflict.
    res = group_ws.run("push", "both", "a", expect_rc=0)

    assert "duplicate entry" not in res.err
    assert {"a/a.txt", "b/b.txt"} <= group_ws.keys()


def test_the_same_entry_named_twice_is_deduplicated(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("push", "data", "data", expect_rc=0)

    assert "duplicate entry" not in res.err
    assert "data/a.txt" in ws.keys()


@pytest.mark.parametrize("args", [("data/one", "data/two"), ("data", "data/one")])
def test_one_entry_named_twice_with_different_targets_is_rejected(ws, args):
    # Deduplication settles exact repeats only: two subtrees of one entry, or
    # the entry beside a subtree of itself, would still be a parallel push of
    # the same tree.
    ws.write("data/one/a.txt", "x")
    ws.write("data/two/b.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("push", *args)

    assert res.rc == 1
    assert "duplicate entry in push: data" in res.err


@pytest.mark.parametrize("args", [("data/one", "data/two"), ("data", "data/one")])
def test_status_still_rejects_one_entry_named_twice(ws, args):
    # Deduplication runs first, so the conflict check must still see the two
    # surviving targets.
    ws.write("data/one/a.txt", "x")
    ws.write("data/two/b.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("status", *args)

    assert res.rc == 1
    assert "conflicting sub paths for entry data" in res.err


# =============================================================================
# Where a group may not be used
# =============================================================================


def test_a_group_with_a_sub_path_is_rejected(group_ws):
    res = group_ws.run("push", "both/a.txt")

    assert res.rc == 1
    assert "a group has no single root" in res.err


@pytest.mark.parametrize("command", ["diff", "show", "ls-remote"])
def test_single_target_commands_reject_a_group(group_ws, command):
    res = group_ws.run(command, "both")

    assert res.rc == 1
    assert f"{command} takes a single entry or path, not a group: both" in res.err


def test_an_unknown_bare_name_names_both_lookups(group_ws):
    res = group_ws.run("push", "nope")

    assert res.rc == 1
    assert "no such entry or group: nope" in res.err


def test_pull_output_rejects_a_group_of_several_entries(group_ws):
    group_ws.run("push", "both", expect_rc=0)

    res = group_ws.run("pull", "both", "-o", str(group_ws.root / "out"))

    assert res.rc == 1
    assert "multiple pull targets" in res.err


def test_pull_output_accepts_a_group_of_one_entry(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}}, groups={"one": ["data"]})
    ws.run("push", "one", expect_rc=0)
    dest = ws.root / "out"

    ws.run("pull", "one", "-o", str(dest), expect_rc=0)

    assert (dest / "a.txt").read_text() == "x"


def test_pull_output_rejects_a_group_of_one_beside_its_member(ws):
    # -o takes one argument, counted before the configuration is read: two
    # arguments naming one entry between them are still two arguments.
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}}, groups={"one": ["data"]})

    res = ws.run("pull", "one", "data", "-o", str(ws.root / "out"))

    assert res.rc == 1
    assert "-o/--output cannot be combined with multiple pull targets" in res.err


def test_pull_of_a_group_checks_destination_overlap(ws):
    ws.config(
        {
            "outer": {"path": str(ws.root / "data")},
            "inner": {"path": str(ws.root / "data" / "sub")},
        },
        groups={"nested": ["outer", "inner"]},
    )

    res = ws.run("pull", "nested")

    assert res.rc == 1
    assert "restore destinations overlap" in res.err


# =============================================================================
# hook
# =============================================================================


def test_hook_of_a_group_runs_the_members_that_configure_it(ws):
    marker = ws.root / "ran-b"
    ws.config(
        {
            "a": {"path": str(ws.root / "a")},
            "b": {"path": str(ws.root / "b"), "post_hook": _recording_hook(ws, marker)},
        },
        groups={"both": ["a", "b"]},
    )

    res = ws.run("hook", "post", "both", "-v", expect_rc=0)

    assert marker.exists()
    assert "skipped (no post_hook): a" in res.err + res.out
    assert "no post_hook configured" not in res.err


def test_hook_of_a_group_without_any_configured_hook_fails(ws):
    ws.config(
        {"a": {"path": str(ws.root / "a")}, "b": {"path": str(ws.root / "b")}},
        groups={"both": ["a", "b"]},
    )

    res = ws.run("hook", "post", "both")

    assert res.rc == 1
    assert "no entry in group both configures a post_hook" in res.err


def test_hook_keeps_the_strict_reading_of_a_named_member(ws):
    # A group skips a hook-less member; naming that member is still an
    # instruction, and that is where a `post_hok:` typo surfaces.
    marker = ws.root / "ran-b"
    ws.config(
        {
            "a": {"path": str(ws.root / "a")},
            "b": {"path": str(ws.root / "b"), "post_hook": _recording_hook(ws, marker)},
        },
        groups={"both": ["a", "b"]},
    )

    res = ws.run("hook", "post", "both", "a", "-v")

    assert res.rc == 1
    assert "a: no post_hook configured" in res.err
    assert "skipped (no post_hook): a" not in res.err + res.out


def test_a_named_member_answers_for_the_group_it_came_from(ws):
    # The group stands for a alone, and a has no hook. Naming a is the
    # instruction that answers, so the report is a's rather than the group's.
    ws.config({"a": {"path": str(ws.root / "a")}}, groups={"g": ["a"]})

    res = ws.run("hook", "post", "g", "a")

    assert res.rc == 1
    assert "a: no post_hook configured" in res.err
    assert "no entry in group g configures a post_hook" not in res.err


def test_a_group_configuring_no_hook_stops_the_command_before_any_hook_runs(ws):
    # The group contributes nothing and nothing of it was named, so resolution
    # fails - and a resolution failure stops the whole command, b included.
    log = ws.root / "runs"
    ws.config(
        {
            "b": {"path": str(ws.root / "b"), "post_hook": _recording_hook(ws, log)},
            "c": {"path": str(ws.root / "c")},
            "d": {"path": str(ws.root / "d")},
        },
        groups={"g": ["c", "d"]},
    )

    res = ws.run("hook", "post", "b", "g")

    assert res.rc == 1
    assert "no entry in group g configures a post_hook" in res.err
    assert not log.exists()


def test_hook_runs_an_entry_named_twice_once(ws):
    log = ws.root / "runs"
    ws.config({"a": {"path": str(ws.root / "a"), "post_hook": _recording_hook(ws, log)}})

    ws.run("hook", "post", "a", "a", expect_rc=0)

    assert log.read_text() == "ran\n"


def test_hook_runs_a_named_member_of_a_named_group_once(ws):
    log = ws.root / "runs"
    ws.config(
        {"a": {"path": str(ws.root / "a"), "post_hook": _recording_hook(ws, log)}},
        groups={"g": ["a"]},
    )

    ws.run("hook", "post", "g", "a", expect_rc=0)

    assert log.read_text() == "ran\n"


def test_a_hook_less_member_of_two_groups_is_reported_skipped_once(ws):
    log = ws.root / "runs"
    ws.config(
        {
            "a": {"path": str(ws.root / "a"), "post_hook": _recording_hook(ws, log)},
            "b": {"path": str(ws.root / "b")},
        },
        groups={"left": ["a", "b"], "right": ["a", "b"]},
    )

    res = ws.run("hook", "post", "left", "right", "-v", expect_rc=0)

    assert (res.err + res.out).count("skipped (no post_hook): b") == 1


def test_hook_of_a_group_with_a_sub_path_is_rejected(group_ws):
    res = group_ws.run("hook", "post", "both/a.txt")

    assert res.rc == 1
    assert "a group has no single root" in res.err


# =============================================================================
# list
# =============================================================================


def test_list_prints_groups_after_the_entries(ws):
    ws.config(
        {name: {"path": str(ws.root / name)} for name in ("a", "b")},
        groups={"outer": ["inner", "a"], "inner": ["b"]},
    )

    res = ws.run("list", expect_rc=0)

    lines = res.out.splitlines()
    assert lines[0].startswith("a ")
    assert lines[1].startswith("b ")
    # Groups sort by name and print their members as configured, unexpanded.
    assert lines[2] == f"{'inner':<20s} = b"
    assert lines[3] == f"{'outer':<20s} = inner, a"


# =============================================================================
# Configuration errors
# =============================================================================


@pytest.mark.parametrize(
    ("groups", "message"),
    [
        ("nope", "groups must be a dict"),
        ({"g": []}, "must be a non-empty list of strings"),
        ({"g": "data"}, "must be a non-empty list of strings"),
        ({"g": [1]}, "must be a non-empty list of strings"),
        ({"g": ["missing"]}, "which is neither an entry nor a group"),
        ({"data": ["data"]}, "collides with the entry of that name"),
        ({"g": ["g"]}, "contains itself"),
        ({"g": ["h"], "h": ["g"]}, "contains itself"),
    ],
)
def test_invalid_groups_are_rejected(ws, groups, message):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}}, groups=groups)

    res = ws.run("list")

    assert res.rc == 1
    assert message in res.err


@pytest.mark.parametrize("bad_name", ["", ".", "..", "nested/name", "windows\\name", "line\nbreak"])
def test_invalid_group_names_are_rejected(ws, bad_name):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}}, groups={bad_name: ["data"]})

    res = ws.run("list")

    assert res.rc == 1
    assert "group name" in res.err.lower()


def test_a_group_name_may_end_with_the_manifest_suffix(ws):
    # The reservation belongs to entry names, which name S3 objects; a group
    # never does.
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}}, groups={"x-manifest.jsonl": ["data"]})

    res = ws.run("list", expect_rc=0)

    assert "x-manifest.jsonl" in res.out
