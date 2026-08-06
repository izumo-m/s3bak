# Requires Python 3.10+
"""The exclusion predicate (docs/excludes.md), delegated to boto3-s3.

``Excludes`` wraps ``globsieve`` - the aws-cli ``--exclude`` engine - so
s3bak cannot drift from what ``aws s3 sync --exclude`` would decide. Every
path is judged alone, against its whole entry-rooted key: directories carry
a trailing ``/`` (which is why ``dir/*`` covers the directory itself - the
``*`` may match the empty tail - and every descendant, each on its own key,
while a bare ``dir`` matches only a file or symlink of that name), ``*``
spans ``/``, and an absolute pattern is matched against the absolute local
path instead (inert against manifest-only keys, which carry no anchor). The
entry root itself is never matched: in aws terms the operation root has no
key, and filters apply beneath it.
"""

from __future__ import annotations

import os

from boto3_s3 import globsieve

_WILDCARDS = ("*", "?", "[")


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
        self._matcher = globsieve.compile(globsieve.GlobPattern.exclude(p) for p in patterns)
        # The provable-prune shapes: a relative ``dir/*`` whose dir part is
        # wildcard-free covers, as a plain prefix, the directory key and
        # every key beneath it - the one case where skipping the descent
        # cannot change what ``excluded`` decides. Patterns are folded to
        # ``/`` space like globsieve folds them; anchored (absolute)
        # patterns never prune (their full-path matching is not a key
        # prefix).
        prefixes = []
        for p in patterns:
            if os.sep != "/":
                p = p.replace(os.sep, "/")
            if (
                not globsieve.is_anchored(p)
                and p.endswith("/*")
                and not any(c in p[:-1] for c in _WILDCARDS)
            ):
                prefixes.append(p[:-1])  # keep the trailing "/"
        self._prune_prefixes = tuple(prefixes)

    def excluded(self, key: str, full_path: str | None = None) -> bool:
        """Whether the entry-rooted ``key`` (directories end with ``/``) is
        excluded. ``key`` is ``""`` for the entry root, which is never
        matched. ``full_path`` is the absolute local path (``/``-separated)
        for anchored patterns; pass ``None`` where none exists - a
        manifest-only or S3-side key - and anchored patterns stay inert."""
        if not key:
            return False
        return not self._matcher.included(key, full_path)

    def prunes_subtree(self, dir_key: str) -> bool:
        """Whether the walk may skip descending into ``dir_key`` (trailing
        ``/``) outright: every key at or under it provably matches a
        ``dir/*`` pattern, so pruning cannot change what ``excluded``
        decides. Purely an optimization - a False here never means
        "included"."""
        return bool(self._prune_prefixes) and dir_key.startswith(self._prune_prefixes)
