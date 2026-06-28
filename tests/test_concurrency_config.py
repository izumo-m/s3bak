"""config.py exposes two independent concurrency knobs.

``max_concurrency`` tunes the transfer thread pool (cp / sync), ``compare_workers``
tunes the parallel ETag comparison; either may be set without the other. s3bak
does not read aws-cli's ``[s3]`` config, so these are the only way to change them.
"""

from __future__ import annotations

import threading
import time

import pytest

from s3bak import cli


def _store(ws):
    return cli.load_config().store


def test_defaults_leave_both_unset(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})

    store = _store(ws)
    assert store.max_concurrency is None
    assert store.compare_workers is None
    assert store._s3()._transfer_config is None  # library default (10) applies
    assert store._content_compare().workers is None  # library defaults it at sync time


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
    assert store._s3()._transfer_config.max_concurrency == 7

    cmp = store._content_compare()
    assert cmp.workers == 3
    assert type(cmp.compare).__name__ == "EtagComparison"


def test_compare_workers_alone_leaves_transfer_default(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}}, compare_workers=5)

    store = _store(ws)
    assert store._s3()._transfer_config is None  # transfers keep the default
    assert store._content_compare().workers == 5


def test_max_concurrency_alone_leaves_compare_unset(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}}, max_concurrency=6)

    store = _store(ws)
    assert store._s3()._transfer_config.max_concurrency == 6
    # compare_workers unset -> the library defaults it to max_concurrency at run time.
    assert store._content_compare().workers is None


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
    # ParallelCompare(workers=N) actually wired into push and pull).
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
