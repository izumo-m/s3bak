#!/bin/bash
# scripts/gh-release.sh - create a GitHub Release for each given tag, using
# that version's CHANGELOG.md section (read from the tag itself) as the notes.
#
#   scripts/gh-release.sh v0.9.0
#   scripts/gh-release.sh --dry-run v0.9.0      # print the notes, create nothing
#
# The tag must already be pushed to origin. A tag that already has a Release
# is skipped, so the script is safe to re-run.

set -euo pipefail

dry_run=0
if [[ ${1:-} == --dry-run ]]; then
    dry_run=1
    shift
fi
if [[ $# -eq 0 ]]; then
    echo "usage: $0 [--dry-run] vX.Y.Z..." >&2
    exit 2
fi

notes=$(mktemp)
trap 'rm -f "$notes"' EXIT

for tag in "$@"; do
    [[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "$tag: not a vX.Y.Z tag" >&2; exit 1; }
    version=${tag#v}

    # Command substitution drops the trailing blank lines; sed drops the
    # leading one.
    body=$(git show "$tag:CHANGELOG.md" |
        awk -v v="$version" '
            /^## \[/ { in_section = (index($0, "## [" v "]") == 1); next }
            in_section { print }
        ' | sed '1{/^$/d}')
    printf '%s\n' "$body" >"$notes"
    if [[ -z $body ]]; then
        echo "$tag: no [$version] section in CHANGELOG.md" >&2
        exit 1
    fi

    if ((dry_run)); then
        echo "=== $tag ==="
        cat "$notes"
        continue
    fi
    if gh release view "$tag" >/dev/null 2>&1; then
        echo "$tag: release exists, skipped"
        continue
    fi
    gh release create "$tag" --verify-tag --title "$tag" --notes-file "$notes"
done
