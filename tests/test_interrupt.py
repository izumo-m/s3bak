"""Ctrl-C: one orderly stop, then an immediate exit.

The first SIGINT raises `KeyboardInterrupt` - the shape every layer below
recognizes, so boto3-s3 abandons a scan's page worker and `run()` maps it to
130. The second gives up on the orderly stop, because that stop waits for
s3transfer to join its transfer threads and a request stuck in a socket read
holds them until it times out.
"""

from __future__ import annotations

import os
import signal
from typing import Any

import pytest

from s3bak import cli


class _Exited(BaseException):
    """Stands in for the process death `os._exit` would have caused."""


@pytest.fixture
def said(monkeypatch: Any) -> list[str]:
    """Capture what the handler writes, and forget any earlier interrupt.

    The handler writes straight to fd 2 rather than through the console: it
    runs on the main thread, which may already hold the (non-reentrant)
    console lock inside `Console._write`, so `console.err` there would
    deadlock the run it is explaining.
    """
    cli.reset_interrupt_state()
    lines: list[str] = []
    monkeypatch.setattr(cli, "_write_stderr", lines.append)
    return lines


def test_first_interrupt_raises_keyboard_interrupt(said: list[str]) -> None:
    with pytest.raises(KeyboardInterrupt):
        cli._on_sigint(signal.SIGINT, None)

    assert len(said) == 1
    assert "Ctrl-C again" in said[0]  # the escape hatch is offered, not implied


def test_second_interrupt_exits_without_unwinding(said: list[str], monkeypatch: Any) -> None:
    def fake_exit(code: int) -> None:
        raise _Exited(code)

    # os._exit, not sys.exit: unwinding would run the very joins being escaped
    # (and then the interpreter's own wait for the non-daemon threads).
    monkeypatch.setattr(os, "_exit", fake_exit)

    with pytest.raises(KeyboardInterrupt):
        cli._on_sigint(signal.SIGINT, None)
    with pytest.raises(_Exited) as excinfo:
        cli._on_sigint(signal.SIGINT, None)

    assert excinfo.value.args[0] == 130
    assert "push this entry again" in said[1]  # what the hard exit leaves behind


def test_run_installs_the_handler_and_maps_the_interrupt_to_130(monkeypatch: Any) -> None:
    def interrupted() -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "main", interrupted)
    previous = signal.getsignal(signal.SIGINT)
    try:
        assert cli.run() == 130
        assert signal.getsignal(signal.SIGINT) is cli._on_sigint
    finally:
        signal.signal(signal.SIGINT, previous)


def test_run_forgets_an_earlier_interrupt(monkeypatch: Any) -> None:
    # run() is one-shot in a real process, but the suite drives it in-process:
    # a leftover flag would make the next run's FIRST Ctrl-C the hard exit.
    monkeypatch.setattr(cli, "_interrupted", True)
    monkeypatch.setattr(cli, "main", lambda: 0)
    previous = signal.getsignal(signal.SIGINT)
    try:
        assert cli.run() == 0
    finally:
        signal.signal(signal.SIGINT, previous)
    assert cli._interrupted is False


def test_write_stderr_survives_a_closed_stderr(monkeypatch: Any) -> None:
    # An interrupt still has to do its job when the output is already gone.
    def closed(fd: int, data: bytes) -> int:
        raise OSError("stderr is gone")

    monkeypatch.setattr(os, "write", closed)
    cli._write_stderr("anything")  # must not raise
