"""config.py knobs: concurrency settings and the size+mtime-check window.

``max_concurrency`` tunes the transfer thread pool (cp / sync), ``compare_workers``
tunes the parallel ETag comparison under --checksum; either may be set without
the other. s3bak does not read aws-cli's ``[s3]`` config, so these are the only
way to change them. ``mtime_window`` (seconds, 0 allowed = strict) bounds the
size+mtime-check tolerance.
"""

from __future__ import annotations

import threading
import time

import pytest

from s3bak import cli


def _store(ws) -> cli.Boto3S3Store:
    store = cli.load_config().store
    assert store is not None
    return store


def test_defaults_leave_both_unset(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})

    store = _store(ws)
    assert store.max_concurrency is None
    assert store.compare_workers is None
    assert store._s3._transfer_config is None  # library default (10) applies
    # content_compare is the bare EtagComparison; the pool is sized at sync time.
    assert type(store.content_compare()).__name__ == "EtagComparison"
    assert store._compare_pool_size() == 10  # both unset -> boto3's default


def test_client_built_once_and_reused(ws):
    # boto3 client construction is not thread-safe, so the store builds one
    # client up front and every S3-side location reuses it. Guard against a
    # regression to per-call construction: patch S3.client to count calls and
    # confirm push/pull issue no further builds.
    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})

    store = _store(ws)
    assert store._client is not None  # eagerly built in __init__

    calls = {"n": 0}
    real_client = store._client
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        store._s3, "client", lambda: calls.__setitem__("n", calls["n"] + 1) or real_client
    )
    try:
        # Every S3-side location resolves to an S3Storage carrying the shared
        # client, so the library never calls S3.client() again.
        store.put_file("probe.txt", str(ws.root / "data" / "a.txt"))
        store.sync_up(str(ws.root / "data"), "data")
        assert store.head_object("data/a.txt") is not None
    finally:
        monkey.undo()
    assert calls["n"] == 0  # no client was constructed after __init__


def test_both_set_independently(ws):
    ws.write("data/a.txt", "x")
    ws.config(
        {"data": {"path": str(ws.root / "data")}},
        max_concurrency=7,
        compare_workers=3,
    )

    store = _store(ws)
    assert store.max_concurrency == 7
    assert store.compare_workers == 3
    tc = store._s3._transfer_config
    assert tc is not None and tc.max_concurrency == 7

    assert type(store.content_compare()).__name__ == "EtagComparison"
    assert store._compare_pool_size() == 3  # compare_workers wins


def test_compare_workers_alone_leaves_transfer_default(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}}, compare_workers=5)

    store = _store(ws)
    assert store._s3._transfer_config is None  # transfers keep the default
    assert store._compare_pool_size() == 5


def test_max_concurrency_alone_leaves_compare_unset(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}}, max_concurrency=6)

    store = _store(ws)
    tc = store._s3._transfer_config
    assert tc is not None and tc.max_concurrency == 6
    # compare_workers unset -> the compare pool falls back to max_concurrency.
    assert store._compare_pool_size() == 6


@pytest.mark.parametrize("name", ["max_concurrency", "compare_workers", "entry_concurrency"])
@pytest.mark.parametrize("bad", [0, -1, True, "lots", 1.5])
def test_invalid_value_is_rejected(ws, name, bad):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}}, **{name: bad})
    with pytest.raises(SystemExit):
        cli.load_config()


def test_entry_concurrency_is_read(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}}, entry_concurrency=3)
    assert cli.load_config().entry_concurrency == 3


def test_entry_concurrency_defaults_to_none(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    assert cli.load_config().entry_concurrency is None


def test_mtime_window_defaults_to_10ms(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    cfg = cli.load_config()
    assert cfg.mtime_window == 0.01
    assert cfg.window_for("data") == 0.01
    assert cfg.window_ns_for("data") == 10_000_000


def test_mtime_window_accepts_fractional_seconds(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}}, mtime_window=0.5)
    cfg = cli.load_config()
    assert cfg.window_for("data") == 0.5
    assert cfg.window_ns_for("data") == 500_000_000


def test_per_entry_mtime_window_overrides_top_level(ws):
    ws.write("data/a.txt", "x")
    ws.config(
        {
            "strict": {"path": str(ws.root / "data"), "mtime_window": 0},
            "loose": {"path": str(ws.root / "data")},
        },
        mtime_window=5,
    )
    cfg = cli.load_config()
    assert cfg.window_for("strict") == 0  # per-entry wins
    assert cfg.window_for("loose") == 5  # falls back to top-level
    assert cfg.window_for("unknown") == 5  # unknown entry -> top-level


def test_cli_override_beats_per_entry(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data"), "mtime_window": 9}})
    cfg = cli.load_config()
    assert cfg.window_for("data") == 9
    cfg.mtime_window_override = 0  # what --mtime-window sets
    assert cfg.window_for("data") == 0  # CLI override wins over per-entry


def test_mtime_window_zero_is_allowed(ws):
    # 0 = strict st_mtime_ns equality; unlike the concurrency knobs it is valid.
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}}, mtime_window=0)
    assert cli.load_config().mtime_window == 0


@pytest.mark.parametrize("bad", [-1, -0.5, True, "lots"])
def test_mtime_window_invalid_value_is_rejected(ws, bad):
    # A fractional value is fine now; negative / bool / non-number are not.
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}}, mtime_window=bad)
    with pytest.raises(SystemExit):
        cli.load_config()


@pytest.mark.parametrize("bad", [-1, -0.5, True, "lots"])
def test_per_entry_mtime_window_invalid_value_is_rejected(ws, bad):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data"), "mtime_window": bad}})
    with pytest.raises(SystemExit):
        cli.load_config()


def _peak_concurrency(entry_concurrency: int | None, n_entries: int) -> tuple[int, int]:
    """Run n_entries through run_entries and report (rc, peak simultaneous fn calls)."""
    lock = threading.Lock()
    state = {"cur": 0, "peak": 0}

    def fn(cfg, entry, opts):
        with lock:
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
        time.sleep(0.1)  # hold the slot so concurrent calls overlap observably
        with lock:
            state["cur"] -= 1
        return 0

    cfg = cli.Config(
        profile="p",
        prefix="s3://b/x",
        bucket="b",
        path_prefix="x",
        entries={},
        entry_concurrency=entry_concurrency,
    )
    rc = cli.run_entries(fn, cfg, [f"e{i}" for i in range(n_entries)], cli.Opts())
    return rc, state["peak"]


def test_run_entries_caps_at_entry_concurrency():
    rc, peak = _peak_concurrency(entry_concurrency=2, n_entries=6)
    assert rc == 0
    assert peak == 2  # never more than the configured cap, and it reaches it


def test_run_entries_unbounded_when_unset():
    rc, peak = _peak_concurrency(entry_concurrency=None, n_entries=4)
    assert rc == 0
    assert peak == 4  # one thread per entry by default


def test_run_entries_cap_above_count_runs_all():
    rc, peak = _peak_concurrency(entry_concurrency=10, n_entries=3)
    assert rc == 0
    assert peak == 3  # cap is a ceiling, not padding


def test_push_pull_roundtrip_with_concurrency_settings(ws):
    # The full sync path must work with non-default workers (TransferConfig and
    # the per-sync ParallelFilter compare pool actually wired into push and pull).
    ws.write("data/a.txt", "hello")
    ws.config(
        {"data": {"path": str(ws.root / "data")}},
        max_concurrency=4,
        compare_workers=2,
    )
    ws.run("push", "data", expect_rc=0)

    dest = ws.root / "out"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert (dest / "a.txt").read_text() == "hello"
