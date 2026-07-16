# Requires Python 3.10+
"""Interactive confirmation of deletions (``--delete``).

Deleting is never the default: push keeps S3 objects (and their manifest
records) for locally-vanished files, and pull keeps local extras. ``--delete``
turns deletions on behind a per-item confirmation; ``--yes`` answers yes to
every question, and a non-interactive run without ``--yes`` answers no to
every question (a successful all-no run exits 0 - "no" is a valid answer, not
a failure).

The interactive answers follow ``rm -i`` / ``git add -p`` conventions:
y = delete this one, n = keep this one, a = delete this and everything after,
d = keep this and everything after, q = abort the whole command. Kept keys are
appended (in arrival order, which the sync guarantees is ascending key order)
to a temp file the manifest merge later streams (manifest.KeptKeys).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from enum import Enum, auto
from typing import IO

from s3bak.console import prompt_is_interactive, read_prompt_answer


class DeletionAbortedError(Exception):
    """The user answered q: abort the whole command, delete and record nothing
    further (push skips the manifest update and post_hook)."""


class AnswerMode(Enum):
    ASK = auto()  # interactive: prompt per deletion candidate
    ALL_YES = auto()  # --yes: delete everything unattended
    ALL_NO = auto()  # non-interactive without --yes: keep everything


def resolve_answer_mode(*, yes: bool) -> AnswerMode:
    """How --delete answers its confirmations this run (only meaningful when
    --delete was given)."""
    if yes:
        return AnswerMode.ALL_YES
    if prompt_is_interactive():
        return AnswerMode.ASK
    return AnswerMode.ALL_NO


# One question at a time across the parallel --all entry threads; the prompt
# text carries the entry name so interleaved entries stay attributable.
_prompt_lock = threading.Lock()

# q aborts the whole command, not just one entry: once set, every later
# confirm() raises immediately without prompting, so sibling --all entries
# wind down too. Reset at each cli.main() entry (in-process test runs).
_abort = threading.Event()


def reset_confirmations() -> None:
    _abort.clear()


class DeleteConfirmer:
    """Per-entry y/n/a/d/q decider carrying the a/d sticky state.

    ``confirm`` returns True to delete. Every kept item's ``kept_key`` (when
    given) is appended to a lazily-created temp file, one ``json.dumps(rel)``
    per line - JSON because filenames may contain newlines. EOF on stdin is
    treated as q: a deletion flow that lost its answer channel must stop, not
    guess."""

    def __init__(self, mode: AnswerMode, entry: str) -> None:
        self._mode = mode
        self._entry = entry
        self._kept_path: str | None = None
        self._kept_file: IO[str] | None = None

    def _note_kept(self, kept_key: str | None) -> bool:
        if kept_key is not None:
            if self._kept_file is None:
                fd, self._kept_path = tempfile.mkstemp(suffix=".kept")
                self._kept_file = os.fdopen(fd, "w", encoding="utf-8")
            self._kept_file.write(json.dumps(kept_key, ensure_ascii=True) + "\n")
        return False

    def confirm(self, display: str, kept_key: str | None = None) -> bool:
        if _abort.is_set():
            raise DeletionAbortedError()
        if self._mode is AnswerMode.ALL_YES:
            return True
        if self._mode is AnswerMode.ALL_NO:
            return self._note_kept(kept_key)
        with _prompt_lock:
            if _abort.is_set():  # another entry aborted while we waited
                raise DeletionAbortedError()
            while True:
                answer = read_prompt_answer(f"s3bak: {self._entry}: delete {display}? [y/n/a/d/q] ")
                if answer == "y":
                    return True
                if answer == "n":
                    return self._note_kept(kept_key)
                if answer == "a":
                    self._mode = AnswerMode.ALL_YES
                    return True
                if answer == "d":
                    self._mode = AnswerMode.ALL_NO
                    return self._note_kept(kept_key)
                if answer == "q" or answer is None:  # None = EOF; bare Enter re-asks
                    _abort.set()
                    raise DeletionAbortedError()

    def kept_keys_path(self) -> str | None:
        """Path of the kept-keys file, flushed for reading; None if every
        answer was a deletion."""
        if self._kept_file is not None:
            self._kept_file.flush()
        return self._kept_path

    def close(self) -> None:
        if self._kept_file is not None:
            self._kept_file.close()
            self._kept_file = None
        if self._kept_path is not None:
            os.unlink(self._kept_path)
            self._kept_path = None


def confirm_subtree_delete(mode: AnswerMode, entry: str, display: str) -> bool:
    """One y/n question for deleting a whole backup subtree (an explicit
    ``push --delete entry/gone-sub``). EOF answers n: unlike a per-item flow
    there is nothing to abort beyond this one decision."""
    if _abort.is_set():
        raise DeletionAbortedError()
    if mode is AnswerMode.ALL_YES:
        return True
    if mode is AnswerMode.ALL_NO:
        return False
    with _prompt_lock:
        while True:
            answer = read_prompt_answer(
                f"s3bak: {entry}: delete the backup subtree {display}? [y/n] "
            )
            if answer == "y":
                return True
            if answer == "n" or answer is None:  # None = EOF; bare Enter re-asks
                return False
