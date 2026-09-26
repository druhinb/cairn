#!/bin/sh
# Create each ruleset in .github/rulesets on the origin repository, or replace
# the one that already has its name.
set -eu
cd "$(dirname "$0")/.."

fail() {
	echo "apply_rulesets.sh: $*" >&2
	exit 1
}

command -v gh >/dev/null 2>&1 || fail "needs the GitHub CLI: brew install gh"
gh auth status >/dev/null 2>&1 || fail "log in to GitHub first: gh auth login"
repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null) ||
	fail "add the GitHub repository as origin first"

for file in .github/rulesets/*.json; do
	name=$(plutil -extract name raw "$file" 2>/dev/null) || fail "$file has no name"
	id=$(gh api --paginate "repos/$repo/rulesets?includes_parents=false" \
		--jq ".[] | select(.name == \"$name\") | .id")
	if [ -n "$id" ]; then
		gh api -X PUT "repos/$repo/rulesets/$id" --input "$file" >/dev/null
		echo "updated \"$name\" on $repo"
	else
		gh api -X POST "repos/$repo/rulesets" --input "$file" >/dev/null
		echo "created \"$name\" on $repo"
	fi
done
