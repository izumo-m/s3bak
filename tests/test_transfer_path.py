"""Small objects take the single-request lane; large ones keep S3.cp (multipart).

s3transfer is overkill for a small file - and on downloads it adds a
pre-transfer HeadObject probe. The store routes objects below the multipart
threshold through boto3-s3's ``S3Storage.get_file`` / ``put_file`` (one
GetObject / PutObject, no transfer engine), and only hands larger files to
S3.cp, where multipart parallelism matters. Either lane stores the same
bytes, so --checksum stays consistent across the threshold.
"""

from __future__ import annotations

import os
from collections import Counter

import pytest

from s3bak import cli


def _upload_files(pair) -> bool:
    """Stand-in for ``PushJournal.decide`` in a bare ``sync_up``: upload the
    local regular files and touch nothing else. The complete-view walk
    enumerates directories and special files too (docs/journal.md), which a
    real push vetoes through the journal, and a pair with no ``src`` is an S3
    orphan - never deleted here."""
    src = getattr(pair, "src", None)
    return src is not None and os.path.isfile(src.key.replace("/", os.sep))


def _store(ws) -> cli.Boto3S3Store:
    store = cli.load_config().store
    assert store is not None
    return store


def _api_counter(store) -> Counter:
    calls: Counter = Counter()
    store._client.meta.events.register(
        "before-call.s3.*", lambda model, **kw: calls.update([model.name])
    )
    return calls


def test_small_object_download_issues_no_head_object(ws):
    # The whole point: a small GetObject skips s3transfer's HeadObject probe,
    # so a manifest fetch (and every status/ls-remote) is one round trip.
    ws.write("data/a.txt", "hello")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)

    store = _store(ws)
    calls = _api_counter(store)
    dest = ws.root / "copy.txt"
    assert store.get_object("data/a.txt", str(dest), verbose=False) is True
    assert dest.read_text() == "hello"
    assert calls["GetObject"] == 1
    assert calls["HeadObject"] == 0  # no pre-transfer probe


def test_small_object_upload_is_a_single_put_object(ws, capfd):
    ws.write("solo.txt", "x" * 100)
    f = ws.root / "solo.txt"
    ws.config({"solo.txt": {"path": str(f)}})

    store = _store(ws)
    calls = _api_counter(store)
    capfd.readouterr()
    res = store.put_object("solo.txt", str(f))
    out = capfd.readouterr().out
    assert res.returncode == 0
    assert res.results == 1
    assert "upload:" in out and "solo.txt" in out
    assert calls["PutObject"] == 1
    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/solo.txt")["Body"].read()
    assert body == b"x" * 100
    etag = ws.s3.head_object(Bucket=ws.bucket, Key=f"{ws.prefix}/solo.txt")["ETag"].strip('"')
    assert "-" not in etag  # single-part plain MD5, not a composite


def test_missing_small_object_get_returns_false(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    store = _store(ws)
    assert store.get_object("data/nope.txt", str(ws.root / "out.txt")) is False


def test_direct_get_creates_missing_parent_dirs(ws):
    ws.write("data/a.txt", "deep")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    store = _store(ws)
    dest = ws.root / "a" / "b" / "c.txt"  # parents do not exist
    assert store.get_object("data/a.txt", str(dest)) is True
    assert dest.read_text() == "deep"


def test_large_object_takes_the_cp_multipart_path(ws, monkeypatch, capfd):
    # Force the large path with a tiny threshold instead of an 8 MiB fixture:
    # a size at or above it must route through S3.cp, whose upload/download
    # still round-trips correctly. (ETag stays multipart-or-not per real size;
    # here the object is small so S3 still stores a plain MD5 - the point is
    # that the cp code path runs.)
    ws.write("data/a.txt", "content-that-exceeds-the-tiny-limit")
    ws.config({"data": {"path": str(ws.root / "data")}})

    store = _store(ws)
    monkeypatch.setattr(store._transfer_config, "multipart_threshold", 4)  # all "large" now

    src = ws.root / "data" / "a.txt"
    capfd.readouterr()
    res = store.put_object("data/a.txt", str(src))
    out = capfd.readouterr().out
    assert res.returncode == 0
    assert res.results == 1
    assert "upload:" in out

    dest = ws.root / "back.txt"
    assert store.get_object("data/a.txt", str(dest), size=999) is True
    assert dest.read_text() == "content-that-exceeds-the-tiny-limit"


def test_multipart_threshold_defaults_to_8_mib(ws):
    # The lane split is s3transfer's own threshold, read off the store's one
    # TransferConfig - the same object every cp / sync runs under.
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    assert _store(ws)._transfer_config.multipart_threshold == 8 * 1024 * 1024


def test_failed_direct_download_preserves_existing_destination(ws, monkeypatch):
    from boto3_s3 import Boto3S3Error

    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    store = _store(ws)
    dest = ws.write("existing.txt", "original")

    class FailingBody:
        reads = 0

        def read(self, _size=-1):
            self.reads += 1
            if self.reads == 1:
                return b"partial"
            raise OSError("simulated interrupted response")

        def close(self):
            pass

    monkeypatch.setattr(store._client, "get_object", lambda **_kwargs: {"Body": FailingBody()})
    before = sorted(p.name for p in dest.parent.iterdir())

    # boto3-s3 files a broken body as a TransportError; run() reports it and
    # exits 1, as it did when this lane raised the OSError itself.
    with pytest.raises(Boto3S3Error, match="interrupted"):
        store.get_object("data/a.txt", str(dest))

    assert dest.read_text() == "original"
    # The sibling temp file the download finishes into is gone with it.
    assert sorted(p.name for p in dest.parent.iterdir()) == before


def test_direct_download_preserves_existing_file_mode(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    store = _store(ws)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/object", Body=b"new")
    dest = ws.write("existing.txt", "old")
    os.chmod(dest, 0o640)

    assert store.get_object("object", str(dest))

    assert dest.read_text() == "new"
    assert os.stat(dest).st_mode & 0o777 == 0o640


def test_delete_objects_reports_only_actually_deleted_keys(ws, monkeypatch, capfd):
    # A per-key DeleteObjects failure (Object Lock, a per-object policy) must not
    # be printed as a successful `delete:` line: only keys absent from the
    # response's Errors were actually deleted.
    ws.write("data/a.txt", "a")
    ws.config({"data": {"path": str(ws.root / "data")}})
    store = _store(ws)

    def fake_delete_objects(Bucket, Delete):  # noqa: N803 (boto3 kwarg names)
        errors = [
            {"Key": o["Key"], "Code": "AccessDenied"}
            for o in Delete["Objects"]
            if o["Key"].endswith("/b")
        ]
        return {"Errors": errors}

    monkeypatch.setattr(store._client, "delete_objects", fake_delete_objects)
    capfd.readouterr()
    result = store.delete_objects(["data/a", "data/b"])
    captured = capfd.readouterr()

    assert result.returncode == 1
    assert result.results == 1
    assert "data/a" in captured.out  # actually deleted
    assert "data/b" not in captured.out  # NOT claimed deleted
    assert "data/b" in captured.err  # reported as failed


def test_delete_objects_fails_batch_on_unattributable_error(ws, monkeypatch, capfd):
    # A DeleteObjects error whose Key is missing cannot be tied to a requested
    # key, so we cannot prove any key was deleted: fail the batch instead of
    # claiming a phantom success (which would orphan the object on S3 while the
    # manifest record was dropped).
    ws.config({"data": {"path": str(ws.root / "data")}})
    store = _store(ws)
    monkeypatch.setattr(
        store._client,
        "delete_objects",
        lambda **kw: {"Errors": [{"Code": "AccessDenied", "Message": "denied"}]},
    )
    capfd.readouterr()
    res = store.delete_objects(["a/b"])
    captured = capfd.readouterr()
    assert res.returncode == 1
    assert res.results == 0
    assert "delete:" not in captured.out  # never claim a success we cannot prove
    assert "a/b" in captured.err


def test_delete_objects_fails_batch_on_unknown_key(ws, monkeypatch, capfd):
    ws.config({"data": {"path": str(ws.root / "data")}})
    store = _store(ws)
    monkeypatch.setattr(
        store._client,
        "delete_objects",
        lambda **kw: {"Errors": [{"Key": "totally/other", "Code": "AccessDenied"}]},
    )
    capfd.readouterr()
    res = store.delete_objects(["a/b"])
    captured = capfd.readouterr()
    assert res.returncode == 1
    assert res.results == 0
    assert "delete:" not in captured.out


def test_delete_objects_reports_only_attributable_failures(ws, monkeypatch, capfd):
    # An ordinary per-key error keyed to a requested object still lets the other
    # keys report as deleted (unchanged behaviour).
    ws.config({"data": {"path": str(ws.root / "data")}})
    store = _store(ws)
    api_b = store._api_key("b")
    monkeypatch.setattr(
        store._client,
        "delete_objects",
        lambda **kw: {"Errors": [{"Key": api_b, "Code": "AccessDenied"}]},
    )
    capfd.readouterr()
    res = store.delete_objects(["a", "b", "c"])
    captured = capfd.readouterr()
    assert res.returncode == 1
    assert res.results == 2
    assert "/a" in captured.out and "/c" in captured.out
    assert "/b:" in captured.err


def test_transfer_lines_print_as_each_item_completes(ws, monkeypatch):
    # F1: a sync's result lines print from on_result as each item finishes,
    # not accumulated and flushed once as a single write after the whole sync
    # completes - the old shape, which held O(transfer count) lines in memory
    # and stayed silent until the very end. Wrapping the console write itself
    # (not just counting output lines) proves the store issues one print call
    # per transferred file, rather than one big join.
    from s3bak.console import console

    for i in range(5):
        ws.write(f"data/f{i}.txt", f"payload-{i}")
    ws.config({"data": {"path": str(ws.root / "data")}})
    store = _store(ws)

    calls: list[str] = []
    real_out = console.out
    monkeypatch.setattr(console, "out", lambda text: (calls.append(text), real_out(text))[0])

    result = store.sync_up(str(ws.root / "data"), "data", pair_filter=_upload_files)

    assert result.returncode == 0
    assert result.results == 5
    upload_calls = [c for c in calls if c.startswith("upload:")]
    # One console.out call per uploaded file, each carrying exactly its own
    # line - never one call joining every line after the sync finished.
    assert len(upload_calls) == 5
    assert all(c.count("\n") == 1 for c in upload_calls)


def test_iter_objects_rejects_unordered_listing(ws, monkeypatch):
    from boto3_s3 import Boto3S3Error

    ws.config({"data": {"path": str(ws.root / "data")}})
    store = _store(ws)
    prefix = store._api_key("data") + "/"

    class FakePaginator:
        def paginate(self, **kw):
            yield {
                "Contents": [
                    {"Key": prefix + "b", "Size": 1},
                    {"Key": prefix + "a", "Size": 1},  # regresses below "b"
                ]
            }

    monkeypatch.setattr(store._client, "get_paginator", lambda name: FakePaginator())
    with pytest.raises(Boto3S3Error, match="ascending key order"):
        list(store.iter_objects("data"))


def test_get_object_forces_glacier_transfer_on_large_downloads(ws, monkeypatch):
    # A large download goes through S3.cp, which WARN-skips a GLACIER source and
    # returns as if it succeeded. get_object must force the transfer so an
    # archived object fails loudly instead of leaving a phantom success.
    ws.config({"data": {"path": str(ws.root / "data")}})
    store = _store(ws)
    captured: dict = {}

    def fake_cp(src, dest, **opts):
        captured.update(opts)
        open(dest, "wb").close()

    monkeypatch.setattr(store._s3, "cp", fake_cp)
    dest = str(ws.root / "big.out")
    assert store.get_object("big", dest, size=store._transfer_config.multipart_threshold) is True
    assert captured.get("force_glacier_transfer") is True


def test_get_object_propagates_a_glacier_transfer_failure(ws, monkeypatch):
    from boto3_s3 import Boto3S3Error

    ws.config({"data": {"path": str(ws.root / "data")}})
    store = _store(ws)

    def fake_cp(src, dest, **opts):
        raise Boto3S3Error("InvalidObjectState: object is archived")

    monkeypatch.setattr(store._s3, "cp", fake_cp)
    threshold = store._transfer_config.multipart_threshold
    with pytest.raises(Boto3S3Error):
        store.get_object("big", str(ws.root / "x.out"), size=threshold)


def test_missing_object_reports_absent_on_both_lanes(ws):
    # Absence is "not present", not a failure - pull turns it into the stale
    # record warning. Both lanes raise NotFoundError; the cp lane is the one a
    # recorded size routes to, and it must answer the same way.
    ws.config({"data": {"path": str(ws.root / "data")}})
    store = _store(ws)
    out = str(ws.root / "out.bin")

    assert store.get_object("nope", out) is False
    assert store.get_object("nope", out, size=store._transfer_config.multipart_threshold) is False


def test_local_not_found_is_not_reported_as_a_missing_object(ws, monkeypatch):
    # get_file translates a LOCAL failure into the same taxonomy as an S3 one,
    # so a vanished destination directory also arrives as NotFoundError. Only
    # the S3-side one means "no such object"; the local one must propagate, or
    # pull would warn about a stale record while the object is fine.
    import boto3_s3

    ws.config({"data": {"path": str(ws.root / "data")}})
    store = _store(ws)
    ws.s3.put_object(Bucket=ws.bucket, Key=f"{ws.prefix}/obj", Body=b"x")

    def local_miss(self, path, *, key=""):
        raise boto3_s3.NotFoundError("no such directory", operation="get_file", key=str(path))

    monkeypatch.setattr(boto3_s3.S3Storage, "get_file", local_miss)
    with pytest.raises(boto3_s3.NotFoundError):
        store.get_object("obj", str(ws.root / "gone" / "out.bin"))


def _wrong_bucket_store(ws) -> cli.Boto3S3Store:
    """A store pointed at a bucket that does not exist - the shape of a typo in
    the config's `prefix`, or a profile resolving to the wrong account."""
    bucket = "s3bak-no-such-bucket"
    return cli.Boto3S3Store(_store(ws).profile, f"s3://{bucket}/p", bucket, "p")


def test_a_missing_bucket_is_not_reported_as_a_missing_object(ws):
    # boto3-s3 files NoSuchBucket under the same NotFoundError as NoSuchKey.
    # Reading that as "not present" would answer a bucket typo with "entry not
    # found on S3" - a backup tool must never say the backup is absent when it
    # was looking in a place that does not exist.
    from boto3_s3 import NotFoundError

    ws.config({"data": {"path": str(ws.root / "data")}})
    store = _wrong_bucket_store(ws)
    out = str(ws.root / "out.bin")

    with pytest.raises(NotFoundError):
        store.get_object("obj", out)
    with pytest.raises(NotFoundError):
        store.get_object("obj", out, size=store._transfer_config.multipart_threshold)
    with pytest.raises(NotFoundError):
        store.stream_object_to_stdout("obj")


def test_a_missing_bucket_stops_the_commands_that_read_a_manifest(ws):
    # The manifest download is the first S3 read of pull / status / show, so it
    # is where a wrong bucket has to surface: run() turns the SDK error into
    # exit 1 with the bucket named, instead of "entry not found on S3".
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws._config.write_text(
        ws._config.read_text().replace(
            f'prefix = "s3://{ws.bucket}/', 'prefix = "s3://s3bak-no-such-bucket/'
        )
    )

    from boto3_s3 import NotFoundError

    for args in (("pull", "data"), ("status", "data"), ("show", "data/a.txt")):
        # ws.run stops at cli.main; run() is what maps an SDK error to exit 1.
        with pytest.raises(NotFoundError, match="NoSuchBucket"):
            ws.run(*args)


# --- the single-file pull names the lane it took ---------------------------
#
# A directory pull's lines come from boto3-s3, which reports each transfer it
# makes. Nothing reports a single-object transfer, so download_from_s3 prints
# the line itself - and since the two lanes differ in more than part count
# (multipart parallelism, the glacier handling), the line names which ran.


def _single_file_ws(ws):
    f = ws.write("solo.txt", "content")
    ws.config({"solo.txt": {"path": str(f)}})
    ws.run("push", "solo.txt", expect_rc=0)
    f.write_text("locally changed, so the pull has work")
    return f


def test_single_file_pull_reports_the_direct_get_file(ws):
    _single_file_ws(ws)
    res = ws.run("pull", "solo.txt", expect_rc=0)
    assert "download: s3://" in res.out
    assert "(boto3-s3 get_file)" in res.out


def test_single_file_pull_reports_the_cp_lane_for_a_large_object(ws, monkeypatch):
    # Forced with a tiny limit rather than an 8 MiB fixture, like the upload
    # test above.
    monkeypatch.setattr(cli.Boto3S3Store, "_multipart", lambda self, size: size >= 4)
    _single_file_ws(ws)
    res = ws.run("pull", "solo.txt", expect_rc=0)
    assert "(boto3-s3 cp)" in res.out


def test_single_file_pull_dry_run_names_the_same_lane(ws):
    f = _single_file_ws(ws)
    before = f.read_text()
    res = ws.run("pull", "--dry-run", "solo.txt", expect_rc=0)
    assert "(dry-run) download: s3://" in res.out
    assert "(boto3-s3 get_file)" in res.out
    assert f.read_text() == before  # a rehearsal, as promised


def test_single_file_pull_prints_no_download_line_for_a_stale_record(ws):
    # The line is printed after the transfer, so the record whose object is
    # gone warns and prints nothing - a line up front would have announced a
    # download that never happened.
    _single_file_ws(ws)
    ws.s3.delete_object(Bucket=ws.bucket, Key=f"{ws.prefix}/solo.txt")

    res = ws.run("pull", "solo.txt", expect_rc=0)
    assert "download:" not in res.out
    assert "no data object behind this record" in res.err
