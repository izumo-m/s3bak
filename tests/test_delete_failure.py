"""The command-boundary half of the "fail old" rule (docs/recovery.md): a
manifest that drops a record publishes only after the deletion it describes
has actually succeeded. These tests pin that guarantee across two delete-
then-publish paths: the ordinary directory ``push --delete`` sync lane, and
the explicit sub-path deletion (``push --delete entry/sub``, which goes
through ``store.delete_objects`` directly via ``delete_subtree``).

A plain local deletion is the positive control. Against it, a per-key
(attributable) DeleteObjects error must fail the push and leave the manifest
untouched; on the sub-path lane, an error that cannot be tied to any
requested key (unattributable - ``store.delete_objects``'s own failsafe, see
its "unattributable" branch around line 535) must fail the whole batch and
leave the manifest untouched even when the object was in fact removed from
S3 underneath.

Failures are injected by wrapping ``botocore.client.BaseClient._make_api_call``
so only the ``DeleteObjects`` call is intercepted; every other S3 call (the
manifest GET/PUT, HeadObject, ListObjectsV2) runs unmodified against moto. The
sub-path test's wrapper calls through to the real implementation first and
then substitutes the response, so the underlying delete genuinely happens on
S3 even while the client-observed response reports an error.
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


def test_push_delete_attributable_failure_keeps_stale_manifest(ws, monkeypatch):
    # The main directory delete lane (sync_up's delete_filter, dispatched
    # through boto3-s3's S3Deleter) reports a per-key DeleteObjects error tied
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


def test_push_delete_subpath_unattributable_failure_keeps_stale_manifest_despite_real_delete(
    ws, monkeypatch
):
    # A second delete-then-publish path, distinct from the ordinary directory
    # delete lane above: an explicit sub-path deletion (push --delete
    # entry/gone-sub) goes through delete_subtree -> store.delete_objects
    # directly (commands.py's _push_sub, the "local_sub_present is False"
    # branch), gated the same way - drop_subtree_records runs only after
    # cfg.store.delete_subtree returns 0.
    #
    # Here the injected response lets the real DeleteObjects call go through
    # first (so the object is actually removed from S3) and only then splices
    # in an unattributable error, to pin the strongest form of the guarantee:
    # even when S3 truly deleted the object, an unattributable error in the
    # response still blocks the manifest rewrite that would drop its record -
    # the safe side of the trade, matching store.delete_objects's own comment
    # (the alternative, a false success, would orphan the object with no
    # record and no way to notice).
    ws.write("data/keep.txt", "keep")
    ws.write("data/removed.txt", "target")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    before_body = _manifest_body(ws, "data")
    before_etag = _manifest_etag(ws, "data")
    assert "removed.txt" in before_body

    (ws.root / "data" / "removed.txt").unlink()

    def fake_call(self: Any, operation_name: str, api_params: Any):
        result = real_call(self, operation_name, api_params)
        if operation_name == "DeleteObjects":
            result = dict(result)
            result["Errors"] = [{"Code": "InternalError", "Message": "boom"}]
        return result

    real_call = botocore.client.BaseClient._make_api_call
    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", fake_call)

    res = ws.run("push", "--delete", "--yes", "data/removed.txt")

    assert res.rc != 0
    assert "delete failed" in res.err
    assert "data/removed.txt" not in ws.keys()  # the real delete_objects call did happen
    assert _manifest_body(ws, "data") == before_body  # the stale record survives
    assert _manifest_etag(ws, "data") == before_etag
