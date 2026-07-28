"""S3's per-request/per-page limits, at and across the 1,000-item boundary.

DeleteObjects accepts at most 1,000 keys per request; ListObjectsV2 returns at
most 1,000 entries per page. Boto3S3Store.delete_objects batches around the
first limit (store.py, ~line 570) and iter_objects paginates around the
second (store.py, ~lines 369-392). An off-by-one on either boundary is the
kind of bug a backup tool cannot afford silently: a leftover, never-deleted
key (an orphan) or a listed key silently dropped mid-page (a gap the
merge-join - see docs/manifest.md - would then misread as absent). moto is
in-memory, so exercising these boundaries directly is cheap.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import pytest

from s3bak import cli


def _store(ws: Any) -> cli.Boto3S3Store:
    ws.config({"data": {"path": str(ws.root / "data")}})
    store = cli.load_config().store
    assert store is not None
    return store


def _spy_delete_objects(monkeypatch: Any) -> list[list[str]]:
    """Record each DeleteObjects request's key list, delegating every call to
    the real implementation (moto) so the delete actually happens - this
    observes batching without faking the deletion itself."""
    import botocore.client

    calls: list[list[str]] = []
    original = botocore.client.BaseClient._make_api_call

    def spy(self: Any, operation_name: str, api_params: Any) -> Any:
        if operation_name == "DeleteObjects":
            calls.append([obj["Key"] for obj in api_params["Delete"]["Objects"]])
        return original(self, operation_name, api_params)

    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", spy)
    return calls


def _put_empty_objects(ws: Any, rel_prefix: str, count: int) -> list[str]:
    """Upload `count` empty objects under rel_prefix, returning their
    entry-relative keys in ascending order (zero-padding keeps byte order
    equal to numeric order up to 99999)."""
    keys = [f"{rel_prefix}/obj-{i:05d}" for i in range(count)]
    for key in keys:
        ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/{key}", Body=b"")
    return keys


# --- DeleteObjects: batches never exceed the 1,000-key request limit -------


@pytest.mark.parametrize(
    ("count", "expected_calls"),
    [(999, 1), (1000, 1), (1001, 2), (2000, 2)],
)
def test_delete_objects_batches_stay_within_the_1000_key_limit(
    ws: Any, monkeypatch: Any, capfd: Any, count: int, expected_calls: int
) -> None:
    # The keys need not exist: DeleteObjects does not error on a missing key,
    # and this case is only about how delete_objects slices the request -
    # call count and per-batch size - so fabricated keys keep it fast.
    store = _store(ws)
    calls = _spy_delete_objects(monkeypatch)
    keys = [f"data/missing-{i:05d}" for i in range(count)]

    capfd.readouterr()
    result = store.delete_objects(keys)
    out = capfd.readouterr().out

    assert len(calls) == expected_calls
    assert all(len(batch) <= 1000 for batch in calls)
    assert sum(len(batch) for batch in calls) == count
    assert result.returncode == 0
    assert result.results == count
    assert out.count("delete:") == count


def test_delete_objects_leaves_no_orphan_across_the_batch_boundary(
    ws: Any, monkeypatch: Any, capfd: Any
) -> None:
    # The real-deletion counterpart of the case above: 1,001 keys that
    # actually exist on S3, crossing the request boundary (1000 + 1), with
    # every one confirmed gone afterwards - the property a backup tool
    # cannot get wrong (a leftover key here is a silent orphan).
    store = _store(ws)
    count = 1001
    keys = _put_empty_objects(ws, "data", count)
    assert ws.keys() == set(keys)  # sanity: everything landed before deleting

    calls = _spy_delete_objects(monkeypatch)
    capfd.readouterr()
    result = store.delete_objects(keys)
    out = capfd.readouterr().out

    assert len(calls) == 2  # 1000 + 1: the request boundary was crossed
    assert all(len(batch) <= 1000 for batch in calls)
    assert result.returncode == 0
    assert result.results == count
    assert out.count("delete:") == count
    assert ws.keys() == set()  # nothing left behind


# --- ListObjectsV2: iter_objects covers every key across a listing page ----


@pytest.mark.parametrize("count", [1001, 2001])
def test_iter_objects_covers_every_key_across_the_listing_boundary(ws: Any, count: int) -> None:
    store = _store(ws)
    uploaded = _put_empty_objects(ws, "data", count)
    expected = sorted(key[len("data/") :] for key in uploaded)

    seen = [meta.key for meta in store.iter_objects("data")]

    assert len(seen) == count  # nothing dropped mid-page
    assert seen == expected  # complete and in ascending S3 key order
    assert all(a < b for a, b in pairwise(seen))  # strictly increasing
    assert len(set(seen)) == count  # no duplicate across the page boundary


# --- delete_subtree: a subtree straddling the batch boundary ---------------


def test_delete_subtree_removes_every_object_across_the_batch_boundary(
    ws: Any, monkeypatch: Any, capfd: Any
) -> None:
    store = _store(ws)
    count = 1001
    _put_empty_objects(ws, "data/sub", count)
    # The exact rel_key itself may also be a stored object; delete_subtree
    # must remove it too (see doomed_keys in store.py).
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/sub", Body=b"")
    # A sibling that merely shares the prefix string must survive: the
    # listing is slash-bounded (Boto3S3Store.delete_subtree's docstring).
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data/sub-sibling.txt", Body=b"")

    calls = _spy_delete_objects(monkeypatch)
    capfd.readouterr()
    result = store.delete_subtree("data/sub")
    out = capfd.readouterr().out

    total = count + 1  # the subtree objects plus the exact key
    assert sum(len(batch) for batch in calls) == total
    assert all(len(batch) <= 1000 for batch in calls)
    assert result.returncode == 0
    assert result.results == total
    assert out.count("delete:") == total
    assert ws.keys() == {"data/sub-sibling.txt"}  # only the sibling survives
