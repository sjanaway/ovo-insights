#!/usr/bin/env bash
# Print one version's section from CHANGELOG.md (without its heading), for a release's notes.
#   scripts/release-notes.sh 1.0.0       the "## [1.0.0]" section
#   scripts/release-notes.sh Unreleased  the "## [Unreleased]" section (dry runs)
# Exits 1 if there is no such section or it is empty, so a release can't go out without notes.
set -euo pipefail

version="${1:?usage: release-notes.sh VERSION}"
changelog="${2:-$(dirname "$0")/../CHANGELOG.md}"

notes="$(awk -v want="$version" '
    /^## \[/ {
        if (in_section) exit
        heading = $0
        sub(/^## \[/, "", heading)
        sub(/\].*$/, "", heading)
        in_section = (heading == want)
        next
    }
    in_section { print }
' "$changelog")"

# Strip blank lines at both ends.
notes="$(printf '%s\n' "$notes" | sed -e :a -e '/^[[:space:]]*$/{$d;N;ba' -e '}' | sed '/./,$!d')"

if [ -z "$notes" ]; then
    echo "CHANGELOG.md has no (or an empty) section for [$version]" >&2
    exit 1
fi
printf '%s\n' "$notes"
