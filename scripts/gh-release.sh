#!/bin/bash
# scripts/gh-release.sh - create a GitHub Release for each given tag, using
# that version's CHANGELOG.md section (read from the tag itself) as the notes.
# Run with --help for usage.

set -euo pipefail

prog=${0##*/}

usage() {
    cat <<EOF
Usage: $prog [options] vX.Y.Z...

Create a GitHub Release for each tag, with that version's CHANGELOG.md
section (read from the tag itself) as the notes.

Options:
  -n, --dry-run   Print each tag's notes and create nothing
  -h, --help      Show this help

Every tag is checked before any Release is created: it must be a vX.Y.Z
tag, pushed to origin, whose CHANGELOG.md has a [X.Y.Z] section. A tag
that already has a Release is skipped, so the script is safe to re-run.

Examples:
  $prog --dry-run v0.10.0
  $prog v0.10.0
EOF
}

die() {
    echo "$prog: $*" >&2
    exit 1
}

dry_run=0
tags=()
while (($#)); do
    case $1 in
        -n | --dry-run) dry_run=1 ;;
        -h | --help)
            usage
            exit 0
            ;;
        --)
            shift
            tags+=("$@")
            break
            ;;
        -*)
            echo "$prog: unknown option: $1" >&2
            echo "Run '$prog --help' for usage." >&2
            exit 2
            ;;
        *) tags+=("$1") ;;
    esac
    shift
done
if ((${#tags[@]} == 0)); then
    usage >&2
    exit 2
fi

# The section of CHANGELOG.md, as committed at the tag, for one version.
# Command substitution drops the trailing blank lines; sed drops the
# leading one.
notes_for() {
    git show "$1:CHANGELOG.md" |
        awk -v v="${1#v}" '
            /^## \[/ { in_section = (index($0, "## [" v "]") == 1); next }
            in_section { print }
        ' | sed '1{/^$/d}'
}

# Check every tag first, so a bad argument creates nothing at all.
notes=() # notes[i] belongs to tags[i]
remote_tags=$(git ls-remote --tags --refs origin)
for i in "${!tags[@]}"; do
    tag=${tags[i]}
    [[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "$tag: not a vX.Y.Z tag"
    git rev-parse -q --verify "refs/tags/$tag" >/dev/null || die "$tag: no such local tag"
    awk -v ref="refs/tags/$tag" '$2 == ref { found = 1 } END { exit !found }' <<<"$remote_tags" ||
        die "$tag: not pushed to origin"
    notes[i]=$(notes_for "$tag")
    [[ -n ${notes[i]} ]] || die "$tag: no [${tag#v}] section in CHANGELOG.md"
done

notes_file=$(mktemp)
trap 'rm -f "$notes_file"' EXIT

for i in "${!tags[@]}"; do
    tag=${tags[i]}
    if ((dry_run)); then
        echo "=== $tag ==="
        printf '%s\n' "${notes[i]}"
        continue
    fi
    if gh release view "$tag" >/dev/null 2>&1; then
        echo "$tag: release exists, skipped"
        continue
    fi
    printf '%s\n' "${notes[i]}" >"$notes_file"
    gh release create "$tag" --verify-tag --title "$tag" --notes-file "$notes_file"
done
