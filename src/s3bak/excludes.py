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
key, and filters apply beneath it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from boto3_s3 import globsieve

_WILDCARDS = ("*", "?", "[")

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


def _literal_head(pattern: str) -> str:
    """The text before the first wildcard: any key the pattern matches
    starts with it, and a wildcard-free pattern matches that key alone."""
    return pattern[
        : min((pattern.find(c) for c in _WILDCARDS if c in pattern), default=len(pattern))
    ]


@dataclass(frozen=True)
class _Rule:
    """One pattern's shape, for the prune analysis (``prunes_subtree``).

    ``pattern`` is the pattern folded to the ``/`` key space, its ``!``
    gone; ``head`` its literal head (``_literal_head``). ``cover`` is set for
    a relative pattern of the ``P*`` shape (a wildcard-free ``P`` and one
    trailing ``*``; ``dir/*`` and the catch-all ``*`` are the common cases):
    since ``*`` spans ``/``, such a pattern matches exactly the keys that
    start with ``P``, so it provably decides a whole subtree.
    """

    include: bool
    anchored: bool
    pattern: str
    head: str
    cover: str | None

    def could_match_under(self, dir_key: str, full_path: str | None) -> bool:
        """Whether some key at or under ``dir_key`` might match - the
        conservative test. A key the pattern matches starts with the
        pattern's literal head, and a key under the directory starts with the
        directory's key, so one must be a prefix of the other; a wildcard-
        free pattern matches its head alone, which then has to lie under the
        directory. An anchored pattern is matched against absolute paths, so
        it is judged against the directory's absolute path after being joined
        onto it exactly as the engine joins it onto every key beneath (on
        Windows the join lends the directory's drive to a driveless
        pattern). The join depends only on the directory's drive, which every
        key under it shares, so the joined pattern's head is the head of what
        each of them is matched against. Without the directory's absolute
        path there is nothing to compare, so it might match."""
        if self.anchored:
            if full_path is None:
                return True
            pattern = os.path.join(full_path, self.pattern).replace(os.sep, "/")
            head, key = _literal_head(pattern), full_path
        else:
            pattern, head, key = self.pattern, self.head, dir_key
        if head.startswith(key):
            return True
        return len(head) < len(pattern) and key.startswith(head)


def _parse(raw: str) -> tuple[globsieve.GlobPattern, _Rule]:
    include = raw.startswith(INCLUDE_PREFIX)
    pattern = raw[len(INCLUDE_PREFIX) :] if include else raw
    glob = (
        globsieve.GlobPattern.include(pattern)
        if include
        else globsieve.GlobPattern.exclude(pattern)
    )
    # Fold the pattern to the "/" key space exactly as globsieve.compile
    # does: on Windows "\" is a separator, and a drive-relative "C:foo"
    # anchors to the entry root as "foo". No-op on POSIX.
    if os.sep != "/":
        pattern = pattern.replace(os.sep, "/")
        drive, rest = os.path.splitdrive(pattern)
        if drive and not globsieve.is_anchored(pattern):
            pattern = rest
    anchored = globsieve.is_anchored(pattern)
    head = _literal_head(pattern)
    cover = None
    if not anchored and pattern.endswith("*") and len(head) == len(pattern) - 1:
        cover = head
    return glob, _Rule(include, anchored, pattern, head, cover)


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
        parsed = [_parse(p) for p in patterns]
        self._matcher = globsieve.compile(glob for glob, _rule in parsed)
        self._rules = tuple(rule for _glob, rule in parsed)

    def excluded(self, key: str, full_path: str | None = None) -> bool:
        """Whether the entry-rooted ``key`` (directories end with ``/``) is
        excluded. ``key`` is ``""`` for the entry root, which is never
        matched. ``full_path`` is the absolute local path (``/``-separated)
        for anchored patterns; pass ``None`` where none exists - a
        manifest-only or S3-side key - and anchored patterns stay inert."""
        if not key:
            return False
        return not self._matcher.included(key, full_path)

    def prunes_subtree(self, dir_key: str, full_path: str | None = None) -> bool:
        """Whether the walk may skip descending into ``dir_key`` (trailing
        ``/``) outright: every key at or under it is provably excluded, so
        pruning cannot change what ``excluded`` decides. Purely an
        optimization - a False here never means "included".

        The proof: the last rule that covers the whole subtree (a relative
        ``P*`` with ``dir_key`` under ``P``) overrides everything before it
        for every key beneath, so the subtree is excluded iff that rule is
        an exclude and no later include could match under the directory.
        ``full_path`` is the directory's absolute path (``/``-separated,
        trailing ``/``), which anchored includes are judged against; without
        it they count as possible matches."""
        last = -1
        for i, rule in enumerate(self._rules):
            if rule.cover is not None and dir_key.startswith(rule.cover):
                last = i
        if last < 0 or self._rules[last].include:
            return False
        return not any(
            r.include and r.could_match_under(dir_key, full_path) for r in self._rules[last + 1 :]
        )
