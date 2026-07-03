"""Small objects take a direct client call; large ones keep S3.cp (multipart).

s3transfer is overkill for a small file - and on downloads it adds a
pre-transfer HeadObject probe. The store routes objects below
``_small_limit`` through a plain PutObject / GetObject, and only hands large
files (>= the multipart threshold) to S3.cp, where multipart parallelism and
the composite ETag matter. The size gate is byte-identical either way below
the limit (same plain-MD5 ETag), so --checksum stays consistent.
"""

from __future__ import annotations

from collections import Counter

from s3bak import cli


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


def test_small_object_upload_is_a_single_put_object(ws):
    ws.write("solo.txt", "x" * 100)
    f = ws.root / "solo.txt"
    ws.config({"solo.txt": {"path": str(f)}})

    store = _store(ws)
    calls = _api_counter(store)
    res = store.put_object("solo.txt", str(f))
    assert res.returncode == 0
    assert "upload:" in res.stdout and "solo.txt" in res.stdout
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


def test_large_object_takes_the_cp_multipart_path(ws, monkeypatch):
    # Force the large path with a tiny limit instead of an 8 MiB fixture: a
    # size >= _small_limit must route through S3.cp, whose upload/download still
    # round-trips correctly. (ETag stays multipart-or-not per real size; here
    # the object is small so S3 still stores a plain MD5 - the point is that the
    # cp code path runs.)
    ws.write("data/a.txt", "content-that-exceeds-the-tiny-limit")
    ws.config({"data": {"path": str(ws.root / "data")}})

    store = _store(ws)
    monkeypatch.setattr(store, "_small_limit", 4)  # everything is "large" now

    src = ws.root / "data" / "a.txt"
    res = store.put_object("data/a.txt", str(src))
    assert res.returncode == 0
    assert "upload:" in res.stdout

    dest = ws.root / "back.txt"
    assert store.get_object("data/a.txt", str(dest), size=999) is True
    assert dest.read_text() == "content-that-exceeds-the-tiny-limit"


def test_small_limit_defaults_to_8_mib(ws):
    ws.write("data/a.txt", "x")
    ws.config({"data": {"path": str(ws.root / "data")}})
    assert _store(ws)._small_limit == 8 * 1024 * 1024
