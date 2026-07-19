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
d = keep this and everything after, q = abort the whole command. Full words
(yes/no/all/quit) are accepted; "delete" deliberately is not, because d means
keep. ``?`` - or any answer not understood, a bare Enter included - prints the
legend and re-asks, and a one-line summary of the answers precedes the first
question of a run. What each answer means for the manifest is the journal
emitter's business (a confirmed deletion journals its record's drop at the
decision point - see syncops.PushJournal); the confirmer only answers.
"""

from __future__ import annotations

import threading
from enum import Enum, auto

from s3bak.console import prompt_is_interactive, read_prompt_answer, write_stderr


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

# Printed once before the first per-item question of a run, so the answer keys
# are explained before the first one is typed.
_ANSWER_SUMMARY = (
    "s3bak: --delete answers: y=delete, n=keep, a=delete all, d=keep all, q=abort, ?=help\n"
)
_summary_shown = False

# Printed by ? - or any answer not understood - before re-asking.
_ANSWER_LEGEND = (
    "y, yes  - delete this one\n"
    "n, no   - keep this one\n"
    "a, all  - delete this one and everything after, without asking again\n"
    "d       - keep this one and everything after, without asking again\n"
    "q, quit - abort the whole command; nothing further is deleted\n"
)


def reset_confirmations() -> None:
    global _summary_shown
    _abort.clear()
    _summary_shown = False


class DeleteConfirmer:
    """Per-entry y/n/a/d/q decider carrying the a/d sticky state.

    ``confirm`` returns True to delete. EOF on stdin is treated as q: a
    deletion flow that lost its answer channel must stop, not guess."""

    def __init__(self, mode: AnswerMode, entry: str) -> None:
        self._mode = mode
        self._entry = entry

    def confirm(self, display: str) -> bool:
        if _abort.is_set():
            raise DeletionAbortedError()
        if self._mode is AnswerMode.ALL_YES:
            return True
        if self._mode is AnswerMode.ALL_NO:
            return False
        with _prompt_lock:
            if _abort.is_set():  # another entry aborted while we waited
                raise DeletionAbortedError()
            global _summary_shown
            if not _summary_shown:
                _summary_shown = True
                write_stderr(_ANSWER_SUMMARY)
            while True:
                answer = read_prompt_answer(
                    f"s3bak: {self._entry}: delete {display}? [y/n/a/d/q/?] "
                )
                if answer is None:  # EOF
                    _abort.set()
                    raise DeletionAbortedError()
                if answer in ("y", "yes"):
                    return True
                if answer in ("n", "no"):
                    return False
                if answer in ("a", "all"):
                    self._mode = AnswerMode.ALL_YES
                    return True
                if answer == "d":
                    self._mode = AnswerMode.ALL_NO
                    return False
                if answer in ("q", "quit"):
                    _abort.set()
                    raise DeletionAbortedError()
                # ? or anything unrecognized - "delete" (which d does NOT
                # mean) and a bare Enter included - explains and re-asks.
                write_stderr(_ANSWER_LEGEND)


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
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no") or answer is None:  # None = EOF
                return False
            write_stderr("y, yes - delete the subtree\nn, no  - keep it\n")
