"""CLI argument validation and error exit codes."""

from __future__ import annotations

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


def test_status_rejects_delete(cfg_ws):
    res = cfg_ws.run("status", "--delete", "data")
    assert res.rc == 1
    assert "delete" in res.err.lower()


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


def test_ls_remote_rejects_data_only(cfg_ws):
    res = cfg_ws.run("ls-remote", "--data-only")
    assert res.rc == 1


def test_show_rejects_meta_only(cfg_ws):
    res = cfg_ws.run("show", "--meta-only", "data")
    assert res.rc == 1
