"""Manifest name-field parsing and round-tripping of tricky filenames."""

from __future__ import annotations

import pytest

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


def test_filename_with_arrow_quote_roundtrips(ws):
    # A file literally named with ' -> ' must not be parsed as a symlink, so its
    # manifest entry matches the local file and status is clean after a push.
    ws.write("data/a' -> 'b", "content")
    ws.config({"data": {"path": str(ws.root / "data")}})
    ws.run("push", "data", expect_rc=0)
    res = ws.run("status", "data", expect_rc=0)
    assert res.out.strip() == ""
