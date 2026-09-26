#!/bin/sh
# Tag and publish a GitHub release for the version already committed on main.
set -eu
cd "$(dirname "$0")/.."

fail() {
	echo "release.sh: $*" >&2
	exit 1
}

[ $# -eq 1 ] || {
	echo "usage: packaging/release.sh <version>" >&2
	exit 2
}
version=$1
tag="v$version"

command -v gh >/dev/null 2>&1 || fail "needs the GitHub CLI: brew install gh"
command -v uv >/dev/null 2>&1 || fail "needs uv: https://docs.astral.sh/uv/"
gh auth status >/dev/null 2>&1 || fail "log in to GitHub first: gh auth login"
[ "$(git branch --show-current)" = main ] || fail "check out main first"
[ -z "$(git status --porcelain)" ] || fail "commit or stash your changes first"
git remote get-url origin >/dev/null 2>&1 || fail "add the GitHub repository as origin first"

in_pyproject=$(awk -F'"' '/^\[/ { project = $0 == "[project]" }
	project && /^version = "/ { print $2; exit }' pyproject.toml)
in_package=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/cairn/__init__.py)
[ "$in_pyproject" = "$version" ] || fail "pyproject.toml says '$in_pyproject'"
[ "$in_package" = "$version" ] || fail "src/cairn/__init__.py says '$in_package'"

git fetch --quiet --tags origin || fail "couldn't reach origin"
if git rev-parse -q --verify refs/remotes/origin/main >/dev/null &&
	! git merge-base --is-ancestor origin/main HEAD; then
	fail "origin/main has commits this main lacks; pull first"
fi
if gh release view "$tag" >/dev/null 2>&1; then
	fail "$tag is already released"
fi
if tagged=$(git rev-parse -q --verify "refs/tags/$tag^{commit}"); then
	[ "$tagged" = "$(git rev-parse HEAD)" ] || fail "$tag already points at another commit"
fi

notes=$(mktemp)
trap 'rm -f "$notes"' EXIT
# the lines under "## [<version>]" up to the next heading, less the link
# definitions and blank lines that end the file
awk -v head="## [$version]" '
	index($0, "## ") == 1 { inside = index($0, head) == 1; next }
	inside { lines[++n] = $0 }
	END {
		while (n > 0 && (lines[n] ~ /^[[:space:]]*$/ || lines[n] ~ /^\[[^]]*\]: /)) n--
		for (i = 1; i <= n; i++) print lines[i]
	}
' CHANGELOG.md | sed '/./,$!d' >"$notes"
[ -s "$notes" ] || fail "CHANGELOG.md has no notes under ## [$version]"

{
	printf '\n## How to update\n\n'
	cat <<'EOF'
If you installed with uv, run `uv tool upgrade cairn-jobs`. With pipx, run `pipx upgrade cairn-jobs`.
EOF
	echo "If you use the app on a Mac with Apple silicon, download Cairn-$version.zip below once it shows up, a few minutes after this release. Unzip it and move Cairn into Applications in place of the old one."
	echo 'If macOS says it can'"'"'t check the app, open System Settings › Privacy & Security, scroll down, and click Open Anyway.'
	echo 'Your jobs, notes and settings stay as they are.'
} >>"$notes"

uv lock --check >/dev/null 2>&1 || fail "uv.lock is out of date: run uv lock and commit it"
uv run --locked --extra test python -m unittest discover -s tests -q ||
	fail "the tests failed"

[ -n "${tagged:-}" ] || git tag -a "$tag" -m "Cairn $version"
git push --atomic origin main "$tag"
gh release create "$tag" --verify-tag --title "Cairn $version" --notes-file "$notes"
