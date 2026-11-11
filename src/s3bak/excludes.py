# Requires Python 3.10+
"""The exclusion predicate (docs/excludes.md), delegated to boto3-s3.

``Excludes`` wraps ``globsieve`` - the aws-cli ``--exclude`` / ``--include``
engine - so s3bak cannot drift from what ``aws s3 sync`` would decide. An
entry's ``excludes`` list is an ordered rule list: a plain pattern is an
exclude, a ``!``-prefixed one an include, and the last pattern that matches
a key decides, an unmatched key being included (aws-cli's evaluation order,
which is what lets a later include take back part of an earlier exclude).
Every path is judged alone, against its whole entry-rooted key: directories
carry a trailing ``/`` (which is why ``dir/*`` covers the directory itself -
the ``*`` may match the empty tail - and every descendant, each on its own
key, while a bare ``dir`` matches only a file or symlink of that name), ``*``
spans ``/``, and an absolute pattern is matched against the absolute local
path instead (inert against manifest-only keys, which carry no anchor). The
entry root itself is never matched: in aws terms the operation root has no
key, and filters apply beneath it. Which directories a walk may skip whole
is the engine's decision too (``excludes_below``), so the skip folds names
exactly as the matching does.
"""

from __future__ import annotations

from boto3_s3 import Exclude, Include, globsieve

#: The prefix that turns a pattern into an include (aws-cli ``--include``).
INCLUDE_PREFIX = "!"


def check_patterns(patterns: list[str]) -> str | None:
    """The shape rules a config ``excludes`` list must satisfy beyond "a list
    of strings" - the complaint, phrased as a predicate of the list, or
    ``None``. An empty pattern matches nothing (and a bare ``!`` would take
    nothing back), and the list must not open with an include: everything
    is included until an exclude matches, so a leading include takes nothing
    back either, and the operator almost certainly meant the opposite order
    (aws-cli's lone ``--include`` is the same trap)."""
    if INCLUDE_PREFIX in patterns:
        return f"has a bare {INCLUDE_PREFIX!r} (an include needs a pattern after it)"
    if "" in patterns:
        return "has an empty pattern"
    if patterns and patterns[0].startswith(INCLUDE_PREFIX):
        return (
            f"must not start with an include ({patterns[0]!r} takes nothing back:"
            " everything is included until an exclude matches)"
        )
    return None


def _rule(raw: str) -> Exclude | Include:
    if raw.startswith(INCLUDE_PREFIX):
        return Include(raw[len(INCLUDE_PREFIX) :])
    return Exclude(raw)


class Excludes:
    """One entry's compiled ``excludes`` list.

    ``excluded`` is the one predicate every command shares (the walker, the
    manifest-side skips, verify's residue report), so the scan side and the
    compare side can never disagree on what an exclude means.
    """

    def __init__(self, patterns: list[str]) -> None:
        #: True for the no-patterns case, so per-record hot paths can skip
        #: key/anchor construction outright.
        self.empty = not patterns
        self._matcher = globsieve.compile(_rule(p) for p in patterns)

    def excluded(self, key: str, full_path: str | None = None) -> bool:
        """Whether the entry-rooted ``key`` (directories end with ``/``) is
        excluded. ``key`` is ``""`` for the entry root, which is never
        matched. ``full_path`` is the absolute local path (``/``-separated)
        for anchored patterns; pass ``None`` where none exists - a
        manifest-only or S3-side key - and anchored patterns stay inert."""
        if not key:
            return False
        return not self._matcher.included(key, full_path)

    def excludes_below(self, dir_key: str, full_path: str | None = None) -> bool:
        """Whether the walk may skip the directory ``dir_key`` (trailing
        ``/``) outright: ``excluded`` provably holds for it and every key
        under it, so skipping cannot change what ``excluded`` decides.
        Purely an optimization - a False here never means "included".
        ``full_path`` is the directory's absolute path (``/``-separated,
        trailing ``/``), as ``excluded`` takes it.

        The proof is the engine's own (``GlobFilter.excludes_below``): the
        latest exclude ending in ``*`` that matches the directory matches
        everything below it, so the subtree is excluded unless a later
        include could match there - judged, conservatively, by whether the
        include's literal head and the directory's path are prefix-related.
        The entry root is never matched, so it is never skipped either."""
        if not dir_key:
            return False
        return self._matcher.excludes_below(dir_key, full_path)
