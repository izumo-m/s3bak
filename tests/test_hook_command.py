"""The `hook pre|post` command: on-demand hook runs outside any push."""

from __future__ import annotations

import sys


def _touch_hook(ws, marker) -> list[str]:
    script = ws.write(
        "touch-marker.py",
        "from pathlib import Path\nimport sys\nPath(sys.argv[1]).touch()\n",
    )
    return [sys.executable, str(script), str(marker)]


def _record_journal_env_hook(ws, out) -> list[str]:
    script = ws.write(
        "record-env.py",
        "import os, sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(os.environ.get('S3BAK_JOURNAL', 'UNSET'))\n",
    )
    return [sys.executable, str(script), str(out)]


def test_hook_post_runs_the_post_hook(ws):
    marker = ws.root / "ran"
    ws.config({"data": {"path": str(ws.root / "data"), "post_hook": _touch_hook(ws, marker)}})

    ws.run("hook", "post", "data", expect_rc=0)

    assert marker.exists()


def test_hook_pre_runs_the_pre_hook(ws):
    marker = ws.root / "ran"
    ws.config({"data": {"path": str(ws.root / "data"), "pre_hook": _touch_hook(ws, marker)}})

    ws.run("hook", "pre", "data", expect_rc=0)

    assert marker.exists()


def test_hook_runs_without_a_journal(ws):
    # No push, hence no journal: S3BAK_JOURNAL must be unset, which the hook
    # contract reads as "no per-file detail; assume anything may have changed".
    out = ws.root / "env"
    ws.config(
        {"data": {"path": str(ws.root / "data"), "post_hook": _record_journal_env_hook(ws, out)}}
    )

    ws.run("hook", "post", "data", expect_rc=0)

    assert out.read_text() == "UNSET"


def test_hook_without_configured_hook_fails(ws):
    # Naming the hook is an instruction; silence would read as success.
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("hook", "post", "data")

    assert res.rc == 1
    assert "no post_hook configured" in res.err


def test_hook_requires_pre_or_post(ws):
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("hook", "data")
    assert res.rc == 1
    assert "'pre' or 'post'" in res.err

    res = ws.run("hook")
    assert res.rc == 1
    assert "'pre' or 'post'" in res.err


def test_hook_rejects_sub_paths(ws):
    # Rejected during argument resolution, before any hook could run, so no
    # hook is configured here on purpose.
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})

    res = ws.run("hook", "post", "data/a.txt")

    assert res.rc == 1
    assert "sub path is not allowed" in res.err


def test_hook_dry_run_prints_without_running(ws):
    marker = ws.root / "ran"
    ws.config({"data": {"path": str(ws.root / "data"), "post_hook": _touch_hook(ws, marker)}})

    res = ws.run("hook", "post", "--dry-run", "data", expect_rc=0)

    assert "(dry-run) would run post_hook" in res.out
    assert not marker.exists()


def test_hook_propagates_the_hook_exit_status(ws):
    ws.config(
        {
            "data": {
                "path": str(ws.root / "data"),
                "post_hook": [sys.executable, "-c", "raise SystemExit(3)"],
            }
        }
    )

    res = ws.run("hook", "post", "data")

    assert res.rc == 3
    assert "post_hook failed" in res.err


def test_hook_all_runs_configured_hooks_and_skips_the_rest(ws):
    # --all means "run every configured hook of that kind": a hook-less
    # entry is outside the operation's domain, not an error - so a real
    # hook failure's exit status is never shadowed by one.
    marker = ws.root / "ran-b"
    ws.config(
        {
            "a": {"path": str(ws.root / "a")},
            "b": {"path": str(ws.root / "b"), "post_hook": _touch_hook(ws, marker)},
        }
    )

    res = ws.run("hook", "post", "--all", expect_rc=0)

    assert marker.exists()
    assert "no post_hook configured" not in res.err

    res = ws.run("hook", "post", "--all", "-v", expect_rc=0)
    assert "skipped (no post_hook): a" in res.err + res.out


def test_hook_all_with_no_configured_hooks_fails(ws):
    # Every entry outside the domain: the instruction is a no-op in full,
    # which - like a named entry without the hook - must not read as success.
    ws.config({"a": {"path": str(ws.root / "a")}, "b": {"path": str(ws.root / "b")}})

    res = ws.run("hook", "post", "--all")

    assert res.rc == 1
    assert "no entry configures a post_hook" in res.err


def test_hook_all_does_not_shadow_a_real_failure(ws):
    # A hook-less entry sorting first must not mask a genuine hook failure's
    # exit status.
    ws.config(
        {
            "a": {"path": str(ws.root / "a")},
            "b": {
                "path": str(ws.root / "b"),
                "post_hook": [sys.executable, "-c", "raise SystemExit(3)"],
            },
        }
    )

    res = ws.run("hook", "post", "--all")

    assert res.rc == 3


def test_hook_needs_no_s3_client(ws, monkeypatch):
    # `hook` loads configuration without constructing an S3 client, so an
    # unusable AWS profile must not stop a hook run (like `list`).
    marker = ws.root / "ran"
    hook = _touch_hook(ws, marker)
    cfg = ws.root / "config-bad-profile.py"
    cfg.write_text(
        'profile = "no-such-profile-anywhere"\n'
        f'prefix = "s3://{ws.bucket}/{ws.prefix}"\n'
        f"entries = {{'data': {{'path': {str(ws.root / 'data')!r}, 'post_hook': {hook!r}}}}}\n"
    )
    monkeypatch.setenv("S3BAK_CONFIG", str(cfg))

    res = ws.run("hook", "post", "data")

    assert res.rc == 0, res.err
    assert marker.exists()
