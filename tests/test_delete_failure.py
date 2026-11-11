"""The command-boundary half of the "fail old" rule (docs/recovery.md): a
manifest that drops a record publishes only after the deletion it describes
has actually succeeded. These tests pin that guarantee across two delete-
then-publish paths: the ordinary directory ``push --delete`` sync lane, and
the explicit sub-path deletion (``push --delete entry/sub``, which goes
through ``store.delete_objects`` directly via ``delete_subtree``).

A plain local deletion is the positive control. Against it, a DeleteObjects
error for the requested key must fail the push and leave the manifest
untouched. Both deletions run through boto3-s3's ``S3Deleter``.

Failures are injected by wrapping ``botocore.client.BaseClient._make_api_call``
so only the ``DeleteObjects`` call is intercepted; every other S3 call (the
manifest GET/PUT, HeadObject, ListObjectsV2) runs unmodified against moto.
"""

from __future__ import annotations

from typing import Any

import botocore.client


def _manifest_body(ws, entry: str) -> str:
    key = f"{ws.prefix}/{entry}-manifest.jsonl"
    return ws.s3.get_object(Bucket=ws.bucket, Key=key)["Body"].read().decode()


def _manifest_etag(ws, entry: str) -> str:
    key = f"{ws.prefix}/{entry}-manifest.jsonl"
    return ws.s3.head_object(Bucket=ws.bucket, Key=key)["ETag"]


def test_push_delete_removes_key_and_drops_manifest_record(ws):
    # The positive control every failure-injection test below is contrasted
    # against: an ordinary local deletion, converged by push --delete, drops
    # both the S3 object and its manifest record.
    ws.write("data/keep.txt", "keep")
    ws.write("data/gone.txt", "gone")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    assert "gone.txt" in _manifest_body(ws, "data")

    (ws.root / "data" / "gone.txt").unlink()
    res = ws.run("push", "--delete", "--yes", "data", expect_rc=0)

    assert "delete:" in res.out
    assert "data/gone.txt" not in ws.keys()
    body = _manifest_body(ws, "data")
    assert "gone.txt" not in body
    assert "keep.txt" in body


def test_push_delete_failure_keeps_stale_manifest(ws, monkeypatch):
    # The main directory delete lane (an orphan taken by sync_up's pair filter,
    # dispatched through boto3-s3's S3Deleter) reports a DeleteObjects error tied
    # to the requested key: the push must fail, the object must survive, and -
    # the point of this test - the manifest must not be rewritten to drop the
    # record the (refused) deletion would have removed.
    ws.write("data/keep.txt", "keep")
    ws.write("data/gone.txt", "gone")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    before_body = _manifest_body(ws, "data")
    before_etag = _manifest_etag(ws, "data")
    assert "gone.txt" in before_body

    (ws.root / "data" / "gone.txt").unlink()
    target_key = f"{ws.prefix}/data/gone.txt"

    def fake_call(self: Any, operation_name: str, api_params: Any):
        if operation_name == "DeleteObjects":
            return {"Errors": [{"Key": target_key, "Code": "AccessDenied", "Message": "denied"}]}
        return real_call(self, operation_name, api_params)

    real_call = botocore.client.BaseClient._make_api_call
    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", fake_call)

    res = ws.run("push", "--delete", "--yes", "data")

    assert res.rc != 0
    assert "gone.txt" in res.err
    assert "AccessDenied" in res.err
    assert "data/gone.txt" in ws.keys()  # the refused delete never reached S3
    assert _manifest_body(ws, "data") == before_body  # never rewritten
    assert _manifest_etag(ws, "data") == before_etag


def test_push_delete_subpath_failure_keeps_stale_manifest(ws, monkeypatch):
    # The explicit sub-path deletion (push --delete entry/gone-sub) goes
    # through delete_subtree -> store.delete_objects directly, gated the same
    # way: drop_subtree_records runs only after delete_subtree returns 0.
    ws.write("data/keep.txt", "keep")
    ws.write("data/removed.txt", "target")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    before_body = _manifest_body(ws, "data")
    before_etag = _manifest_etag(ws, "data")
    assert "removed.txt" in before_body

    (ws.root / "data" / "removed.txt").unlink()
    target_key = f"{ws.prefix}/data/removed.txt"

    def fake_call(self: Any, operation_name: str, api_params: Any):
        if operation_name == "DeleteObjects":
            return {"Errors": [{"Key": target_key, "Code": "AccessDenied", "Message": "denied"}]}
        return real_call(self, operation_name, api_params)

    real_call = botocore.client.BaseClient._make_api_call
    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", fake_call)

    res = ws.run("push", "--delete", "--yes", "data/removed.txt")

    assert res.rc != 0
    assert "removed.txt" in res.err
    assert "data/removed.txt" in ws.keys()  # the refused delete never reached S3
    assert _manifest_body(ws, "data") == before_body  # never rewritten
    assert _manifest_etag(ws, "data") == before_etag
