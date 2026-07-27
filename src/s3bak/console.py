# Requires Python 3.10+
"""Terminal I/O, warning accounting, and small path helpers.

The bottom layer: everything above depends on this, it depends on nothing in
s3bak. stdout/stderr writes are serialized (transfers report from worker
threads); transfer warnings are counted here so run() can turn a
warning-only run into exit code 2.
"""

from __future__ import annotations

import os
import shlex
import sys
import threading
from typing import NoReturn

PROG = "s3bak"
IS_WINDOWS = sys.platform == "win32"

_output_lock = threading.Lock()

# Transfer warnings (WARNED outcomes) are printed as they occur and counted;
# run() turns a warning-only run into exit code 2.
_warning_lock = threading.Lock()
_warning_count = 0


def err(msg: str) -> None:
    # Share the output lock with write_output/write_stderr so a worker thread's
    # error line cannot interleave with another thread's output under --all.
    with _output_lock:
        sys.stderr.write(f"{PROG}: {msg}\n")
        sys.stderr.flush()


def die(msg: str) -> NoReturn:
    err(msg)
    sys.exit(1)


def write_output(text: str) -> None:
    with _output_lock:
        sys.stdout.write(text)
        sys.stdout.flush()


def write_stderr(text: str) -> None:
    with _output_lock:
        sys.stderr.write(text)
        sys.stderr.flush()


def note_warning(msg: str) -> None:
    """Print a transfer warning and count it; run() maps any warning to exit 2."""
    global _warning_count
    write_stderr(f"{msg}\n")
    with _warning_lock:
        _warning_count += 1


def reset_warnings() -> None:
    """Zero the warning counter at the start of a run()."""
    global _warning_count
    _warning_count = 0


def warning_count() -> int:
    return _warning_count


def prompt_is_interactive() -> bool:
    """Whether a confirmation prompt can actually be asked and seen: both the
    answer channel (stdin) and the question channel (stderr) must be TTYs."""
    return sys.stdin.isatty() and sys.stderr.isatty()


def read_prompt_answer(prompt: str) -> str | None:
    """Write ``prompt`` to stderr and read one answer line from stdin.

    The write holds the output lock so a transfer worker's line cannot split
    the prompt, but the read happens outside it - blocking on the keyboard
    while holding the lock would deadlock worker-thread result reporting.
    Returns the stripped lowercased answer, or None on EOF - an empty answer
    (bare Enter) and a closed stdin must not be conflated."""
    with _output_lock:
        sys.stderr.write(prompt)
        sys.stderr.flush()
    line = sys.stdin.readline()
    if not line:
        return None
    return line.strip().lower()


def echo_command(verbose: bool, args: list[str]) -> None:
    if verbose:
        write_stderr(f"+ {shlex.join(args)}\n")


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
