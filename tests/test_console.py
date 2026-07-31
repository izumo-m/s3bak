"""The console's terminal ownership: what a pending question does to output."""

from __future__ import annotations

import threading
import time

import pytest

from s3bak.console import console


def test_prompt_session_holds_output_until_the_answer(capfd):
    # The bug this exists for: a transfer worker's result line landing between
    # the question and its answer scrolled the question off the screen (and,
    # with no trailing newline on the prompt, appended itself to it). While a
    # session is open every other thread's write waits - nothing is buffered,
    # the writer itself is held - and lands once the answer is in.
    reached_write = threading.Event()

    def worker() -> None:
        reached_write.set()
        console.out("upload: late.txt to s3://bucket/late.txt\n")

    thread = threading.Thread(target=worker)
    with console.prompt() as session:
        session.say("s3bak: data: delete s3://bucket/data/gone.txt? [y/n/a/d/q/?] ")
        thread.start()
        reached_write.wait(5)
        time.sleep(0.1)  # long enough for an unheld write to have landed
        assert thread.is_alive()  # blocked on the console, not finished
        assert "upload:" not in capfd.readouterr().out

    thread.join(5)
    assert not thread.is_alive()
    assert "upload: late.txt" in capfd.readouterr().out


def test_prompt_owner_writes_through_the_ordinary_api(capfd):
    # The session holds the lock across the answer, so the thread that owns it
    # must be able to write without retaking it. (A regression here deadlocks
    # rather than fails: the owning thread waits for a lock it already holds.)
    with console.prompt() as session:
        assert console._owner == threading.get_ident()
        session.say("legend\n")
        console.out("owner stdout\n")
        console.err("owner stderr")
        console.warn("warning: owner warning")
    assert console._owner is None
    warnings = console.warning_count()
    console.reset_warnings()  # module-level singleton: leave the counter clean

    captured = capfd.readouterr()
    assert captured.out == "owner stdout\n"
    assert "legend\n" in captured.err
    assert "s3bak: owner stderr\n" in captured.err
    assert "warning: owner warning\n" in captured.err
    assert warnings == 1


def test_prompt_sessions_do_not_nest():
    # A second session on the same thread would wait on a lock it already
    # holds. The assert turns that into a failure rather than a hang at a
    # prompt, where a hang looks exactly like waiting for the operator.
    with console.prompt():
        with pytest.raises(AssertionError, match="do not nest"):
            with console.prompt():
                pass
    assert console._owner is None


def test_prompt_session_releases_the_terminal_on_an_exception():
    with pytest.raises(RuntimeError):
        with console.prompt():
            raise RuntimeError("q")
    assert console._owner is None
    # Not deadlocked: the next writer goes straight through.
    console.err("after the abort")


def test_read_answer_normalizes_and_reports_eof(monkeypatch, capfd):
    monkeypatch.setattr("sys.stdin", _Stdin(["  Y \n", "", "\n"]))
    assert console.read_answer("ask? ") == "y"
    assert console.read_answer("ask? ") is None  # EOF, not an empty answer
    assert console.read_answer("ask? ") == ""  # bare Enter
    assert capfd.readouterr().err == "ask? " * 3


class _Stdin:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def readline(self) -> str:
        return self._lines.pop(0)
