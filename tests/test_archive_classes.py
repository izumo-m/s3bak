"""Archive storage class (GLACIER / DEEP_ARCHIVE) behaviour.

Before this file, exactly one test each in test_verify.py and
test_transfer_path.py pinned this - the check that catches "the backup does
not actually restore" before a real pull hits it. This file pins the fuller
matrix: (GLACIER, DEEP_ARCHIVE) x (directory entry, single-file entry), plus
the contrast against storage classes that sound archived but are not, plus
what `pull` itself does when it meets an unrestored archived object.

Those two pre-existing tests stay where they are; this file does not
duplicate them.
"""

from __future__ import annotations

import signal

import pytest

_ARCHIVED_CLASSES = ("GLACIER", "DEEP_ARCHIVE")


def _push_dir_entry(ws, entry: str = "data") -> None:
    ws.write(f"{entry}/a.txt", "alpha")
    ws.config({entry: {"path": str(ws.root / entry)}})
    ws.run("push", entry, expect_rc=0)


def _push_single_file_entry(ws, name: str = "solo.txt", content: str = "content") -> None:
    local = ws.write(name, content)
    ws.config({name: {"path": str(local)}})
    ws.run("push", name, expect_rc=0)


# --- verify: an archived object is always an error, in either entry shape -


@pytest.mark.parametrize("storage_class", _ARCHIVED_CLASSES)
def test_verify_dir_entry_archived_object_is_an_error(ws, storage_class):
    _push_dir_entry(ws)
    ws.s3.put_object(
        Bucket=ws.bucket,
        Key=f"{ws.prefix}/data/a.txt",
        Body=b"alpha",
        StorageClass=storage_class,
    )
    res = ws.run("verify", "data", expect_rc=1)
    assert f"storage class {storage_class} blocks restore" in res.err
    assert f"{ws.prefix}/data/a.txt" in res.err
    assert "1 error(s)" in res.out


@pytest.mark.parametrize("storage_class", _ARCHIVED_CLASSES)
def test_verify_single_file_entry_archived_object_is_an_error(ws, storage_class):
    _push_single_file_entry(ws)
    ws.s3.put_object(
        Bucket=ws.bucket,
        Key=f"{ws.prefix}/solo.txt",
        Body=b"content",
        StorageClass=storage_class,
    )
    res = ws.run("verify", "solo.txt", expect_rc=1)
    assert f"storage class {storage_class} blocks restore" in res.err
    assert f"{ws.prefix}/solo.txt" in res.err
    assert "1 error(s)" in res.out


# --- verify: the check runs on every listed object, recorded or not -------


@pytest.mark.parametrize("storage_class", _ARCHIVED_CLASSES)
def test_verify_unrecorded_archived_object_in_dir_entry_is_still_an_error(ws, storage_class):
    """store.iter_objects feeds _verify_dir's merge-join every listed object,
    and _check_archived runs unconditionally whenever an object is present -
    before the record/no-record branch that decides "unrecorded object". A
    stray out-of-band upload that happens to be archived is not exempted:
    pull's own listing-driven download would try to fetch it too (see
    docs/verify.md, "Archived storage class")."""
    _push_dir_entry(ws)
    ws.s3.put_object(
        Bucket=ws.bucket,
        Key=f"{ws.prefix}/data/stray.bin",
        Body=b"stray",
        StorageClass=storage_class,
    )
    res = ws.run("verify", "data", expect_rc=1)
    assert f"storage class {storage_class} blocks restore" in res.err
    assert "data/stray.bin" in res.err
    assert "unrecorded object" in res.err and "data/stray.bin" in res.err
    assert "1 error(s), 1 warning(s)" in res.out


# --- verify: storage classes that are not archived must not false-positive


def test_verify_dir_entry_standard_storage_class_is_not_an_error(ws):
    _push_dir_entry(ws)
    res = ws.run("verify", "data", expect_rc=0)
    assert "storage class" not in res.err
    assert "data: OK" in res.out


@pytest.mark.parametrize("storage_class", ["STANDARD", "STANDARD_IA", "GLACIER_IR"])
def test_verify_dir_entry_non_archived_storage_classes_do_not_error(ws, storage_class):
    """_ARCHIVED_CLASSES is exactly ("GLACIER", "DEEP_ARCHIVE"); a class that
    also restricts retrieval speed/cost but stays synchronously readable
    (STANDARD_IA, GLACIER_IR) must not be mistaken for one that blocks
    get_object outright."""
    _push_dir_entry(ws)
    ws.s3.put_object(
        Bucket=ws.bucket,
        Key=f"{ws.prefix}/data/a.txt",
        Body=b"alpha",
        StorageClass=storage_class,
    )
    res = ws.run("verify", "data", expect_rc=0)
    assert "storage class" not in res.err
    assert "data: OK" in res.out


@pytest.mark.parametrize("storage_class", ["STANDARD_IA", "GLACIER_IR"])
def test_verify_single_file_entry_non_archived_storage_classes_do_not_error(ws, storage_class):
    _push_single_file_entry(ws)
    ws.s3.put_object(
        Bucket=ws.bucket,
        Key=f"{ws.prefix}/solo.txt",
        Body=b"content",
        StorageClass=storage_class,
    )
    res = ws.run("verify", "solo.txt", expect_rc=0)
    assert "storage class" not in res.err
    assert "solo.txt: OK" in res.out


# --- verify vs. a completed restore: current behaviour, not a design sign-off


def test_verify_ignores_a_completed_restore_and_still_reports_the_archived_error(ws):
    """Not an endorsement of this as correct - a note of what actually
    happens today.

    s3bak never reads the `x-amz-restore` / Restore header restore_object
    sets (grepping src/ for "Restore"/"ongoing-request"/"restore_object"
    finds nothing); _check_archived looks only at StorageClass, which AWS
    (and moto) leaves at GLACIER/DEEP_ARCHIVE even after a restore completes -
    the restored copy is a separate, temporary, non-archived copy layered on
    top. So verify keeps reporting the archived-storage-class error for an
    object a pull could, in fact, now download - true here because
    store.sync_down forces the glacier transfer (see
    test_pull_dir_entry_over_restored_archived_object_succeeds), so this
    same restored directory entry is exactly what that test pulls
    successfully. Confirmed against moto 5.2.2: restore_object here
    immediately reports
    ongoing-request="false" (moto's ManagedState default "immediate"
    progression skips any transient in-progress state), so this test only
    exercises the completed-restore case, not an in-progress one - trying to
    hold moto in the transient state relies on an internal per-call tick
    counter (moto.s3.models.FakeKey / ManagedState) that every head_object or
    get_object call also advances, making it unreliable to pin here; it is
    also moot for this check, since _check_archived reads only StorageClass,
    which is identical in both the in-progress and completed states.
    """
    _push_dir_entry(ws)
    key = f"{ws.prefix}/data/a.txt"
    ws.s3.put_object(Bucket=ws.bucket, Key=key, Body=b"alpha", StorageClass="GLACIER")
    ws.s3.restore_object(
        Bucket=ws.bucket,
        Key=key,
        RestoreRequest={"Days": 5, "GlacierJobParameters": {"Tier": "Standard"}},
    )
    head = ws.s3.head_object(Bucket=ws.bucket, Key=key)
    assert 'ongoing-request="false"' in head.get("Restore", "")  # moto: restore already complete
    assert head.get("StorageClass") == "GLACIER"  # ...yet the storage class never moved

    res = ws.run("verify", "data", expect_rc=1)
    assert "storage class GLACIER blocks restore" in res.err


# --- pull: an unrestored archived object must not succeed silently --------


@pytest.mark.parametrize("storage_class", _ARCHIVED_CLASSES)
def test_pull_dir_entry_over_unrestored_archived_object_fails_closed(ws, storage_class):
    """A directory pull's data transfer is store.sync_down -> S3.sync, which
    passes force_glacier_transfer (matching store.get_object's large-file cp
    call), so boto3-s3's glacier gate never gets a chance to WARN-skip the
    archived source - the sync forwards the GetObject straight to S3 and lets
    it decide. An unrestored GLACIER/DEEP_ARCHIVE source is rejected there
    with InvalidObjectState, which S3.sync reports as a per-key failure and
    then a summary BatchError ("N of M operations failed"), both landing on
    stderr; sync_down's TransferResult carries that nonzero returncode back
    through download_from_s3, which returns it immediately - before
    apply_manifest (and its enforce_size gate) ever runs."""
    _push_dir_entry(ws)
    ws.s3.put_object(
        Bucket=ws.bucket,
        Key=f"{ws.prefix}/data/a.txt",
        Body=b"alpha",
        StorageClass=storage_class,
    )
    dest = ws.root / "fresh"
    res = ws.run("pull", "data", "-o", str(dest), expect_rc=1)
    assert not (dest / "a.txt").exists()
    assert "InvalidObjectState" in res.err


def test_pull_data_only_over_unrestored_archived_object_fails_closed(ws):
    """The same failure as above, pinned again with --data-only: that flag
    only skips apply_manifest (and, with it, _verify_restored_sizes), it does
    not change how the data is fetched. The forced sync still hits
    InvalidObjectState at the GetObject and download_from_s3 still returns
    the sync's nonzero rc immediately, so --data-only is not a bypass for
    this failure. Pinned separately because a single test on the default
    lane would leave that open."""
    _push_dir_entry(ws)
    ws.s3.put_object(
        Bucket=ws.bucket,
        Key=f"{ws.prefix}/data/a.txt",
        Body=b"alpha",
        StorageClass="GLACIER",
    )
    dest = ws.root / "fresh"
    res = ws.run("pull", "--data-only", "data", "-o", str(dest), expect_rc=1)
    assert not (dest / "a.txt").exists()
    assert "InvalidObjectState" in res.err


@pytest.mark.parametrize("storage_class", _ARCHIVED_CLASSES)
def test_pull_single_file_entry_over_unrestored_archived_object_fails_closed(
    ws, monkeypatch, capfd, storage_class
):
    """A single-file entry's (small) download is a direct GetObject, not
    S3.cp - store.get_object deliberately lets any error other than
    not-found propagate ("other errors ... propagate to run()"), so an
    InvalidObjectState from an unrestored archive source is not caught until
    cli.run()'s outer boundary (_sdk_errors). cli.main alone (what ws.run
    drives) would let it escape as a raw exception, so this test drives
    cli.run() directly - the actual `s3bak` entry point - like
    test_verify_warnings_map_to_exit_2_via_run in test_verify.py."""
    from s3bak import cli

    _push_single_file_entry(ws)
    ws.s3.put_object(
        Bucket=ws.bucket,
        Key=f"{ws.prefix}/solo.txt",
        Body=b"content",
        StorageClass=storage_class,
    )
    dest = ws.root / "fresh_solo.txt"
    capfd.readouterr()  # drain the push output above before capturing this run
    monkeypatch.setattr("sys.argv", ["s3bak", "pull", "solo.txt", "-o", str(dest)])
    saved = signal.getsignal(signal.SIGINT)
    try:
        rc = cli.run()
    finally:
        signal.signal(signal.SIGINT, saved)
    captured = capfd.readouterr()
    assert rc == 1
    assert not dest.exists()
    assert "InvalidObjectState" in captured.err


# --- pull: a restored archived object now downloads (the fix) -------------


@pytest.mark.parametrize("storage_class", _ARCHIVED_CLASSES)
def test_pull_dir_entry_over_restored_archived_object_succeeds(ws, storage_class):
    """Pins the actual fix: store.sync_down now passes force_glacier_transfer
    to S3.sync, alongside store.get_object's large-file cp call. Before that
    flag was wired onto sync_down, this exact restore-then-pull sequence
    failed permanently - the gate has no HeadObject to read `Restore` from
    during a recursive listing, so it could not see the completed restore and
    WARN-skipped the object regardless (see
    test_pull_dir_entry_over_unrestored_archived_object_fails_closed); a
    directory pull could never download a restored GLACIER/DEEP_ARCHIVE
    object, no matter how carefully it had been restored. Forcing the
    transfer removes that client-side guess and lets S3 decide for real: a
    completed restore now downloads."""
    _push_dir_entry(ws)
    key = f"{ws.prefix}/data/a.txt"
    ws.s3.put_object(Bucket=ws.bucket, Key=key, Body=b"alpha", StorageClass=storage_class)
    ws.s3.restore_object(
        Bucket=ws.bucket,
        Key=key,
        RestoreRequest={"Days": 5, "GlacierJobParameters": {"Tier": "Standard"}},
    )
    dest = ws.root / "fresh"
    ws.run("pull", "data", "-o", str(dest), expect_rc=0)
    assert (dest / "a.txt").exists()
    assert (dest / "a.txt").read_text() == "alpha"


def test_pull_dir_entry_partial_archive_failure_still_downloads_ordinary_files(ws):
    """A per-object failure must not abort the whole sync. S3.sync's engine
    keeps every other transfer running and only tallies the archived key as
    failed (the BatchError counts failed vs. succeeded transfers, not an
    all-or-nothing outcome), so a directory holding one unrestored archived
    object alongside ordinary ones still delivers the ordinary files - and
    still exits 1, since the archived one never came down."""
    ws.write("data/a.txt", "alpha")
    ws.write("data/b.txt", "bravo")
    ws.write("data/c.txt", "charlie")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    ws.s3.put_object(
        Bucket=ws.bucket,
        Key=f"{ws.prefix}/data/a.txt",
        Body=b"alpha",
        StorageClass="GLACIER",
    )
    dest = ws.root / "fresh"
    res = ws.run("pull", "data", "-o", str(dest), expect_rc=1)
    assert not (dest / "a.txt").exists()
    assert (dest / "b.txt").read_text() == "bravo"
    assert (dest / "c.txt").read_text() == "charlie"
    assert "InvalidObjectState" in res.err
