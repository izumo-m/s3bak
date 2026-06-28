"""Manifest name-field parsing and round-tripping of tricky filenames."""

from __future__ import annotations

import signal

import pytest

from s3bak import cli
from s3bak.cli import parse_fname, shell_always_quote


@pytest.mark.parametrize(
    "name",
    [
        "myfile.txt",
        "notes -> archive",  # contains ' -> ' text but no embedded quotes
        "a' -> 'b",  # pathological: the quoted form embeds a literal ' -> '
        "it's a file.txt",  # apostrophe + spaces
        "->",  # the bare arrow as a name
    ],
)
def test_parse_fname_plain_names_roundtrip(name):
    assert parse_fname(shell_always_quote(name)) == (name, None)


def test_parse_fname_symlink():
    field = f"{shell_always_quote('link name')} -> {shell_always_quote('a -> b')}"
    assert parse_fname(field) == ("link name", "a -> b")


def test_newline_filename_is_skipped_with_warning(ws, monkeypatch):
    # A file whose name contains a newline cannot go in the line-oriented
    # manifest; it is skipped (data + manifest) with a warning, the rest backs
    # up, and the run exits 2. Exercised via run() (warnings -> exit 2).
    ws.write("data/good.txt", "good")
    (ws.root / "data" / "a\nb.txt").write_text("bad")
    ws.config({"data": {"path": str(ws.root / "data")}})

    monkeypatch.setattr("sys.argv", ["s3bak", "push", "data"])
    saved = signal.getsignal(signal.SIGINT)
    try:
        rc = cli.run()
    finally:
        signal.signal(signal.SIGINT, saved)

    assert rc == 2
    keys = ws.keys()
    assert "data/good.txt" in keys
    assert not any("\n" in k for k in keys)  # the newline file was not uploaded

    body = ws.s3.get_object(Bucket=ws.bucket, Key=f"{ws.prefix}/data-ls-l.txt")["Body"].read()
    text = body.decode()
    assert "good.txt" in text
    assert "a\nb.txt" not in text  # never reached the manifest


def test_filename_with_arrow_quote_roundtrips(ws):
    # A file literally named with ' -> ' must not be parsed as a symlink, so its
    # manifest entry matches the local file and status is clean after a push.
    ws.write("data/a' -> 'b", "content")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    res = ws.run("status", "data", expect_rc=0)
    assert res.out.strip() == ""
