"""CLI argument validation and error exit codes."""

from __future__ import annotations

from importlib.metadata import version

import pytest


@pytest.fixture
def cfg_ws(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    return ws


def test_meta_only_and_data_only_are_mutually_exclusive(cfg_ws):
    res = cfg_ws.run("push", "--meta-only", "--data-only", "data")
    assert res.rc == 1
    assert "mutually exclusive" in res.err.lower()


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


def test_pull_rejects_dry_run(cfg_ws):
    # --dry-run is push-only; silently ignoring it would perform a REAL
    # restore the user believed was a preview.
    res = cfg_ws.run("pull", "--dry-run", "data")
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
        ("push", "--delete", "data"),
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


def test_help_option_succeeds_without_loading_config(monkeypatch, capfd):
    monkeypatch.setenv("S3BAK_CONFIG", "/definitely/missing/config.py")
    from s3bak import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "Usage: s3bak" in captured.err
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
