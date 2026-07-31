# Requires Python 3.10+
"""Terminal I/O, warning accounting, and small path helpers.

The bottom layer: everything above depends on this, it depends on nothing in
s3bak. One `Console` (the module-level `console`) owns both terminal streams,
so an interactive question can hold the terminal from the prompt to the answer
instead of being scrolled away by a transfer worker's result line - see
`Console.prompt`. Transfer warnings are counted here too, so run() can turn a
warning-only run into exit code 2.

Two writers stay outside the console on purpose: `Boto3S3Store.cat` streams
bytes straight to `sys.stdout.buffer` (a single-item, non-line-oriented
passthrough), and cli's usage/version text prints before any concurrency
starts. `sys.stdout` itself is never wrapped or replaced - `compare.isatty`
reads it, and `diff` inherits it as a file descriptor.
"""

from __future__ import annotations

import os
import shlex
import stat as stat_mod
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO, NoReturn

PROG = "s3bak"
IS_WINDOWS = sys.platform == "win32"

# The stat module only defines this name on Windows builds; the numeric value
# is fixed by the NTFS on-disk format, so it is hardcoded as a fallback here.
# That lets is_junction be exercised by a Linux unit test that monkeypatches
# an lstat result's st_reparse_tag, and reads correctly on a real Windows host
# regardless of which name the platform's stat module happens to expose.
_IO_REPARSE_TAG_MOUNT_POINT = getattr(stat_mod, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


class PromptSession:
    """The terminal, held for one interactive question (`Console.prompt`).

    ``ask`` puts the question and reads its answer; ``say`` writes the rest of
    the same interaction - the answer summary, the legend a re-ask prints.
    Everything a session writes goes out unlocked (the session already holds
    the lock, and it runs on one thread), so the whole exchange reaches the
    terminal as one uninterrupted block."""

    def __init__(self, console: Console) -> None:
        self._console = console

    def ask(self, prompt: str) -> str | None:
        return self._console.read_answer(prompt)

    def say(self, text: str) -> None:
        self._console.diag(text)


class Console:
    """The single terminal channel: every line of s3bak's output, and every
    interactive question, passes through one instance.

    One lock serializes the writes (transfers report from worker threads, and
    a half-written line must never take another thread's line inside it). A
    prompt session holds that same lock across the answer's keystrokes, which
    is what keeps a question on screen: while it is open, every other thread's
    output waits. ``_owner`` is the session's escape hatch - the thread
    holding the lock writes without retaking it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: int | None = None
        self._warnings = 0

    # --- writing ------------------------------------------------------------
    def _write(self, stream: IO[str], text: str, *, warning: bool = False) -> None:
        if self._owner == threading.get_ident():
            # Inside our own prompt session: the lock is already ours, and a
            # session is single-threaded, so writing straight through is both
            # safe and the only way to avoid deadlocking on ourselves. The
            # warning counter is safe to touch here for the same reason -
            # nobody else can be inside the lock.
            self._emit(stream, text, warning)
            return
        with self._lock:
            self._emit(stream, text, warning)

    def _emit(self, stream: IO[str], text: str, warning: bool) -> None:
        stream.write(text)
        stream.flush()
        # Counted after the write: a broken pipe means the warning never
        # reached anyone, so it must not raise the run's exit code either.
        if warning:
            self._warnings += 1

    def out(self, text: str) -> None:
        """Write ``text`` (already newline-terminated) to stdout."""
        self._write(sys.stdout, text)

    def diag(self, text: str) -> None:
        """Write ``text`` (already newline-terminated) to stderr."""
        self._write(sys.stderr, text)

    def err(self, msg: str) -> None:
        self._write(sys.stderr, f"{PROG}: {msg}\n")

    def die(self, msg: str) -> NoReturn:
        self.err(msg)
        sys.exit(1)

    def warn(self, msg: str) -> None:
        """Print a transfer warning and count it; run() maps any warning to exit 2."""
        self._write(sys.stderr, f"{msg}\n", warning=True)

    def echo_command(self, verbose: bool, args: list[str]) -> None:
        if verbose:
            self.diag(f"+ {shlex.join(args)}\n")

    # --- warning accounting -------------------------------------------------
    def reset_warnings(self) -> None:
        """Zero the warning counter at the start of a run()."""
        self._warnings = 0

    def warning_count(self) -> int:
        return self._warnings

    # --- asking -------------------------------------------------------------
    @contextmanager
    def prompt(self) -> Iterator[PromptSession]:
        """Hold the terminal for one interactive exchange: the question, any
        re-asks it needs, and the answer.

        Other threads' output blocks for the duration, so the question stays
        the last line on screen while the operator reads it, and the work
        that was reporting resumes only once the answer is in. Nothing is
        buffered - waiting writers are simply held - so memory stays flat no
        matter how long the operator takes.

        Two conditions make that safe, and both must hold:

        - **Only the main thread opens a session.** An interactive --delete
          run is serialized onto it (cli.run_entries), so the thread deciding
          the deletions is the one that can be interrupted by Ctrl-C while
          it waits for an answer.
        - **A session only writes here and reads the answer.** It must never
          call into the store or boto3-s3: a delete dispatch waits for the
          previous batch to land (boto3_s3.deleter's backpressure point)
          while that batch's worker waits on this lock to print its
          ``delete:`` line - holding the lock across such a call closes the
          cycle. The same shape exists in s3transfer, whose done-callbacks
          report from its worker threads.
        """
        assert threading.current_thread() is threading.main_thread(), (
            "a prompt session may only be opened on the main thread"
        )
        with self._lock:
            self._owner = threading.get_ident()
            try:
                yield PromptSession(self)
            finally:
                self._owner = None

    def read_answer(self, prompt: str) -> str | None:
        """Write ``prompt`` to stderr and read one answer line from stdin.

        Returns the stripped lowercased answer, or None on EOF - an empty
        answer (bare Enter) and a closed stdin must not be conflated. This is
        also the seam the tests replace to script answers."""
        self.diag(prompt)
        line = sys.stdin.readline()
        if not line:
            return None
        return line.strip().lower()


console = Console()


def prompt_is_interactive() -> bool:
    """Whether a confirmation prompt can actually be asked and seen: both the
    answer channel (stdin) and the question channel (stderr) must be TTYs."""
    return sys.stdin.isatty() and sys.stderr.isatty()


def expand_home(path: str) -> str:
    # os.path.expanduser on Windows-native Python (ucrt64/mingw) resolves "~"
    # via USERPROFILE, which ignores the msys HOME. Prefer HOME when set.
    if not path.startswith("~"):
        return path
    home = os.environ.get("HOME")
    if home and (path == "~" or path.startswith("~/")):
        return home + path[1:]
    return os.path.expanduser(path)


def normalize_local_path(arg: str) -> str:
    expanded = expand_home(arg) if arg.startswith("~") else arg
    return os.path.abspath(expanded)


def is_junction(st: os.stat_result) -> bool:
    """True if ``st`` (an ``os.lstat`` result) is a Windows directory
    junction (``mklink /J``): a mount-point reparse point. Windows does not
    model a junction as a symlink - ``os.path.islink()``/``stat.S_ISLNK`` are
    both False for it - yet it lstats as an ordinary directory, so a plain
    type check treats it as one and walks through it like any other
    directory. ``st_reparse_tag`` (Python 3.8+, Windows only) is how it is
    told apart from a real directory; the attribute is simply absent on
    every other platform, so ``getattr`` makes the read safe there (always
    False)."""
    return getattr(st, "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT
