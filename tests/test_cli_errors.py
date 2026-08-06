"""CLI argument validation and error exit codes."""

from __future__ import annotations

import os
from importlib.metadata import version

import pytest


@pytest.fixture
def cfg_ws(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    return ws


def test_meta_only_and_data_only_flags_are_gone(cfg_ws):
    # Removed in 0.6: a one-sided push/pull broke the manifest's
    # correspondence with S3. The options must be rejected, not ignored.
    res = cfg_ws.run("push", "--meta-only", "data")
    assert res.rc == 1
    assert "unknown option" in res.err
    res = cfg_ws.run("pull", "--data-only", "data")
    assert res.rc == 1
    assert "unknown option" in res.err


def test_all_with_explicit_entry_errors(cfg_ws):
    res = cfg_ws.run("push", "--all", "data")
    assert res.rc == 1
    assert "--all" in res.err


def test_unknown_command_errors(cfg_ws):
    res = cfg_ws.run("bogus")
    assert res.rc != 0
    assert "unknown command" in res.err.lower()


def test_unknown_option_errors(cfg_ws):
    res = cfg_ws.run("push", "--frobnicate", "data")
    assert res.rc == 1
    assert "unknown option" in res.err.lower()


def test_no_args_shows_usage(cfg_ws):
    res = cfg_ws.run()
    assert res.rc != 0


def test_version_is_reported_without_loading_config(monkeypatch, capfd):
    monkeypatch.setenv("S3BAK_CONFIG", "/definitely/missing/config.py")
    from s3bak import cli

    assert cli.main(["--version"]) == 0
    captured = capfd.readouterr()
    assert captured.out == f"s3bak {version('s3bak')}\n"
    assert captured.err == ""


def test_status_rejects_delete(cfg_ws):
    res = cfg_ws.run("status", "--delete", "data")
    assert res.rc == 1
    assert "delete" in res.err.lower()


def test_status_rejects_dry_run(cfg_ws):
    # --dry-run applies to push and pull only; silently ignoring it elsewhere
    # would blur the "reject, don't ignore" contract for preview-like flags.
    res = cfg_ws.run("status", "--dry-run", "data")
    assert res.rc == 1
    assert "--dry-run" in res.err


def test_push_rejects_output_flag(cfg_ws):
    res = cfg_ws.run("push", "-o", "/tmp/x", "data")
    assert res.rc == 1
    assert "--output" in res.err


def test_diff_rejects_mtime_window(cfg_ws):
    res = cfg_ws.run("diff", "--mtime-window", "1", "data")
    assert res.rc == 1
    assert "mtime-window" in res.err.lower()


def test_entry_without_path_dies_cleanly(ws):
    # A malformed entry must die with a message, not a KeyError traceback.
    ws.config({"data": {}})
    res = ws.run("list")
    assert res.rc == 1
    assert "path" in res.err


@pytest.mark.parametrize("bad_path", ["~/data", "data", "./data"])
def test_relative_entry_path_is_rejected(ws, bad_path):
    # "~" is not expanded, so "~/data" is a relative path too; a relative path
    # would silently depend on the working directory.
    ws.config({"data": {"path": bad_path}})
    res = ws.run("list")
    assert res.rc == 1
    assert "absolute" in res.err


def test_root_entry_path_is_rejected(ws):
    # The typo'd-f-string guard: an empty HOME turns f"{HOME}/" into "/".
    root = "C:\\" if os.name == "nt" else "/"
    ws.config({"data": {"path": root}})
    res = ws.run("list")
    assert res.rc == 1
    assert "path root" in res.err


def test_entry_with_non_list_excludes_dies_cleanly(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data"), "excludes": "*.log"}})
    res = ws.run("list")
    assert res.rc == 1
    assert "excludes" in res.err


def test_diff_rejects_all(cfg_ws):
    res = cfg_ws.run("diff", "--all")
    assert res.rc == 1


def test_list_rejects_arguments(cfg_ws):
    res = cfg_ws.run("list", "data")
    assert res.rc == 1


def test_pull_unknown_entry_without_output_errors(cfg_ws):
    res = cfg_ws.run("pull", "nonexistent")
    assert res.rc == 1
    assert "no such entry" in res.err.lower()


def test_invalid_color_value_errors(cfg_ws):
    res = cfg_ws.run("status", "--color=purple", "data")
    assert res.rc == 1
    assert "color" in res.err.lower()


def test_output_flag_requires_value(cfg_ws):
    res = cfg_ws.run("pull", "data", "-o")
    assert res.rc == 1


def test_output_flag_does_not_consume_the_next_option(cfg_ws):
    res = cfg_ws.run("pull", "data", "--output", "--delete")
    assert res.rc == 1
    assert "requires a path" in res.err.lower()


def test_removed_outpath_alias_is_rejected(cfg_ws):
    res = cfg_ws.run("pull", "data", "--outpath", "/tmp/x")

    assert res.rc == 1
    assert "unknown option: --outpath" in res.err.lower()


def test_pull_rejects_output_with_multiple_entries_before_loading_config(monkeypatch, capfd):
    monkeypatch.setenv("S3BAK_CONFIG", "/definitely/missing/config.py")
    from s3bak import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["pull", "d1", "d2", "-o", "/tmp/out"])

    assert exc.value.code == 1
    captured = capfd.readouterr()
    assert "multiple" in captured.err.lower()
    assert "--output" in captured.err
    assert "config file not found" not in captured.err.lower()


def test_pull_rejects_multiple_paths_from_same_entry(cfg_ws):
    res = cfg_ws.run("pull", "data/a.txt", "data/b.txt")

    assert res.rc == 1
    assert "duplicate entry in pull: data" in res.err.lower()


@pytest.mark.parametrize("args", [("outer", "inner"), ("--all",)])
def test_pull_rejects_overlapping_restore_destinations(ws, args):
    ws.config(
        {
            "outer": {"path": str(ws.root / "data")},
            "inner": {"path": str(ws.root / "data" / "sub")},
        }
    )

    res = ws.run("pull", *args)

    assert res.rc == 1
    assert "restore destinations overlap" in res.err.lower()
    assert "outer" in res.err
    assert "inner" in res.err


def test_pull_rejects_destinations_overlapping_through_parent_symlink(ws):
    real = ws.root / "real"
    real.mkdir()
    alias = ws.root / "alias"
    alias.symlink_to(real, target_is_directory=True)
    ws.config(
        {
            "outer": {"path": str(real)},
            "inner": {"path": str(alias / "sub")},
        }
    )

    res = ws.run("pull", "outer", "inner")

    assert res.rc == 1
    assert "restore destinations overlap" in res.err.lower()
    assert "outer" in res.err
    assert "inner" in res.err


def test_pull_rejects_destinations_that_differ_only_by_case(ws):
    ws.config(
        {
            "outer": {"path": str(ws.root / "Data")},
            "inner": {"path": str(ws.root / "data" / "sub")},
        }
    )

    res = ws.run("pull", "outer", "inner")

    assert res.rc == 1
    assert "restore destinations overlap" in res.err.lower()
    assert "outer" in res.err
    assert "inner" in res.err


def test_pull_rejects_destinations_differing_only_by_trailing_dot(ws):
    # Win32 drops a trailing dot (or space) from a path's final component, so
    # ".../data" and ".../data." can land on the SAME directory there even
    # though they are two different paths on POSIX (W-F4). The
    # destination-overlap check folds it out on every platform (see
    # restore.fs_alias_key), so this is caught here - not discovered as a
    # live overlap the first time both entries run --delete.
    ws.config(
        {
            "outer": {"path": str(ws.root / "data")},
            "inner": {"path": str(ws.root / "data." / "sub")},
        }
    )

    res = ws.run("pull", "outer", "inner")

    assert res.rc == 1
    assert "restore destinations overlap" in res.err.lower()
    assert "outer" in res.err
    assert "inner" in res.err


def test_ls_remote_rejects_data_only(cfg_ws):
    res = cfg_ws.run("ls-remote", "--data-only")
    assert res.rc == 1


def test_show_rejects_meta_only(cfg_ws):
    res = cfg_ws.run("show", "--meta-only", "data")
    assert res.rc == 1


def test_mtime_window_flag_rejects_non_number(cfg_ws):
    res = cfg_ws.run("push", "--mtime-window", "abc", "data")
    assert res.rc == 1
    assert "mtime-window" in res.err.lower()


def test_mtime_window_flag_rejects_negative(cfg_ws):
    res = cfg_ws.run("push", "--mtime-window", "-1", "data")
    assert res.rc == 1
    assert "mtime-window" in res.err.lower()


def test_mtime_window_flag_requires_value(cfg_ws):
    res = cfg_ws.run("push", "data", "--mtime-window")
    assert res.rc == 1


@pytest.mark.parametrize(
    "args",
    [
        ("push", "--checksum", "--meta-only", "data"),
        ("push", "--checksum", "--mtime-window", "0", "data"),
        ("pull", "--delete", "--meta-only", "data"),
        ("push", "--meta-only", "--delete", "data"),
        ("push", "--data-only", "--delete", "data"),
        ("push", "--yes", "data"),
        ("pull", "--yes", "data"),
    ],
)
def test_ignored_option_combinations_are_rejected(cfg_ws, args):
    res = cfg_ws.run(*args)
    assert res.rc == 1


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_mtime_window_flag_rejects_non_finite_values(cfg_ws, value):
    res = cfg_ws.run("push", "--mtime-window", value, "data")
    assert res.rc == 1
    assert "mtime-window" in res.err.lower()


def test_mtime_window_flag_rejects_huge_finite_value(cfg_ws):
    # Finite but so large that seconds * 1e9 overflows to inf; window_ns_for's
    # round() would raise an uncaught OverflowError. Reject cleanly instead.
    res = cfg_ws.run("push", "--mtime-window", "1e308", "data")
    assert res.rc == 1
    assert "mtime-window" in res.err.lower()


def test_config_mtime_window_rejects_huge_finite_value(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}}, mtime_window=1e308)
    res = ws.run("list")
    assert res.rc == 1
    assert "mtime_window" in res.err


def test_config_mtime_window_rejects_huge_integer(ws):
    # A huge Python int overflows the int->float conversion math.isfinite does;
    # it must die with a message, not an uncaught OverflowError traceback.
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}}, mtime_window=10**1000)
    res = ws.run("list")
    assert res.rc == 1
    assert "mtime_window" in res.err


@pytest.mark.parametrize("field", ["path", "pre_hook"])
def test_config_rejects_nul_byte(ws, field):
    # A NUL survives every isinstance/normpath check but raises ValueError deep
    # in the first filesystem/subprocess call, which run() does not catch. It
    # must die at config load with a message, not a traceback.
    entry = {"path": str(ws.root / "data")}
    if field == "path":
        entry["path"] = str(ws.root / "data") + "\x00bad"
    else:
        entry["pre_hook"] = ["echo", "\x00"]
    ws.write("data/a.txt", "x")
    ws.config({"data": entry})
    res = ws.run("list")
    assert res.rc == 1
    assert field.split("_")[0] in res.err.lower() or "nul" in res.err.lower()


def test_push_subpath_allows_colon_in_posix_filename(cfg_ws):
    # The Windows drive-letter guard (os.path.splitdrive) must be a no-op on
    # POSIX: a filename containing ':' is legal there and must still push.
    if os.name == "nt":
        import pytest as _pytest

        _pytest.skip("':' is not a legal filename component on Windows")
    cfg_ws.write("data/a:b.txt", "colon")
    res = cfg_ws.run("push", "data/a:b.txt")
    assert res.rc == 0
    assert any(k.endswith("a:b.txt") and not k.endswith("manifest.jsonl") for k in cfg_ws.keys())


def test_unknown_command_is_reported_before_loading_config(monkeypatch, capfd):
    monkeypatch.setenv("S3BAK_CONFIG", "/definitely/missing/config.py")
    from s3bak import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["bogus"])
    assert exc.value.code == 1
    captured = capfd.readouterr()
    assert "unknown command" in captured.err.lower()
    assert "config file not found" not in captured.err.lower()


@pytest.mark.parametrize("argument", ["help", "-h"])
def test_unsupported_help_forms_are_rejected(monkeypatch, capfd, argument):
    monkeypatch.setenv("S3BAK_CONFIG", "/definitely/missing/config.py")
    from s3bak import cli

    with pytest.raises(SystemExit) as exc:
        cli.main([argument])
    assert exc.value.code == 1
    captured = capfd.readouterr()
    assert f"unknown command: {argument}" in captured.err.lower()
    assert "config file not found" not in captured.err.lower()


def test_global_help_succeeds_without_loading_config(monkeypatch, capfd):
    monkeypatch.setenv("S3BAK_CONFIG", "/definitely/missing/config.py")
    from s3bak import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    captured = capfd.readouterr()
    assert "Usage: s3bak <command> [options] [args]" in captured.out
    assert "Global options:" in captured.out
    assert "s3bak <command> --help" in captured.out
    assert "--dry-run" not in captured.out
    assert "Examples:" not in captured.out
    assert captured.err == ""


def test_push_help_shows_only_push_reference_without_loading_config(monkeypatch, capfd):
    monkeypatch.setenv("S3BAK_CONFIG", "/definitely/missing/config.py")
    from s3bak import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["push", "--help"])

    assert exc.value.code == 0
    captured = capfd.readouterr()
    assert "s3bak push [options] <entry|path>..." in captured.out
    assert "Back up configured entries or selected sub-paths to S3." in captured.out
    assert "--dry-run" in captured.out
    assert "--delete" in captured.out
    assert "Examples:" in captured.out
    assert "--output" not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("command", ["push", "pull"])
def test_delete_command_help_explains_confirmation_behavior(capfd, command):
    from s3bak import cli

    with pytest.raises(SystemExit) as exc:
        cli.main([command, "--help"])

    assert exc.value.code == 0
    captured = capfd.readouterr()
    assert "y/n/a/d/q" in captured.out
    assert "Without a TTY, every answer is no unless --yes is set." in captured.out


@pytest.mark.parametrize(
    ("command", "usage", "command_detail"),
    [
        ("pull", "s3bak pull [options] <entry|path>...", "--output <path>"),
        ("show", "s3bak show [options] <entry|path>", "Print a single backed-up file"),
        ("status", "s3bak status [options] <entry|path>...", "Status letters:"),
        ("diff", "s3bak diff [options] <entry|path>", "--color[=WHEN]"),
        ("list", "s3bak list", "List locally configured entries."),
        ("ls-remote", "s3bak ls-remote [options] [entry|path]", "stored on S3"),
    ],
)
def test_each_command_has_its_own_help(monkeypatch, capfd, command, usage, command_detail):
    monkeypatch.setenv("S3BAK_CONFIG", "/definitely/missing/config.py")
    from s3bak import cli

    with pytest.raises(SystemExit) as exc:
        cli.main([command, "--help"])

    assert exc.value.code == 0
    captured = capfd.readouterr()
    assert usage in captured.out
    assert command_detail in captured.out
    assert "Global options:" not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    "args",
    [
        ("push", "--color=always", "data"),
        ("pull", "--no-color", "data"),
        ("show", "--color", "data/a.txt"),
        ("list", "--verbose"),
        ("ls-remote", "--color=never"),
    ],
)
def test_options_omitted_from_command_help_are_rejected(cfg_ws, args):
    res = cfg_ws.run(*args)

    assert res.rc == 1
    assert "only applies" in res.err.lower()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("push", "--help", "--frobnicate"), "unknown option: --frobnicate"),
        (("list", "--verbose", "--help"), "--verbose only applies"),
    ],
)
def test_help_does_not_hide_invalid_options(monkeypatch, capfd, args, message):
    monkeypatch.setenv("S3BAK_CONFIG", "/definitely/missing/config.py")
    from s3bak import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(list(args))

    assert exc.value.code == 1
    captured = capfd.readouterr()
    assert message in captured.err.lower()
    assert "config file not found" not in captured.err.lower()


@pytest.mark.parametrize(
    "bad_name",
    ["", ".", "..", "nested/name", "windows\\name", "x-manifest.jsonl", "line\nbreak"],
)
def test_invalid_entry_names_are_rejected(ws, bad_name):
    ws.write("data/a.txt", "x")
    ws.config({bad_name: {"path": str(ws.root / "data")}})

    res = ws.run("list")

    assert res.rc == 1
    assert "entry name" in res.err.lower() or "no entries" in res.err.lower()


@pytest.mark.parametrize("field", ["profile", "prefix"])
def test_non_string_required_config_values_are_rejected(ws, field):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    body = ws._config.read_text()
    body = body.replace(f'{field} = "', f"{field} = 123  # ", 1)
    ws._config.write_text(body)

    res = ws.run("list")

    assert res.rc == 1
    assert "profile and prefix" in res.err.lower()
