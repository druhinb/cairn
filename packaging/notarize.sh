#!/bin/sh
# Sign, notarize, and staple dist/Cairn.app, then zip it as
# dist/Cairn-<version>.zip. Run packaging/build_app.sh first.
set -eu
cd "$(dirname "$0")/.."

if [ -z "${CAIRN_SIGNING_IDENTITY:-}" ] || [ -z "${CAIRN_NOTARY_PROFILE:-}" ]; then
	echo "set CAIRN_SIGNING_IDENTITY and CAIRN_NOTARY_PROFILE" >&2
	exit 2
fi

app="dist/Cairn.app"
version=$(/usr/libexec/PlistBuddy -c "Print CFBundleShortVersionString" "$app/Contents/Info.plist")
upload=dist/notarize-upload.zip
submission=dist/notarize-submission.json
release="dist/Cairn-$version.zip"
trap 'rm -f "$upload" "$submission"' EXIT

sign() {
	codesign --force --options runtime --timestamp \
		--entitlements packaging/entitlements.plist \
		--sign "$CAIRN_SIGNING_IDENTITY" "$1"
}

# inside out: every Mach-O file in the bundle, then the bundle, which seals them
find "$app/Contents" -type f ! -path "$app/Contents/MacOS/*" | while IFS= read -r file; do
	if file -b "$file" | grep -q '^Mach-O'; then
		sign "$file"
	fi
done
sign "$app"
codesign --verify --deep --strict "$app"

ditto -c -k --keepParent "$app" "$upload"
# the status in the JSON decides, whatever notarytool exits with
xcrun notarytool submit "$upload" --keychain-profile "$CAIRN_NOTARY_PROFILE" \
	--wait --output-format json >"$submission" || true
if ! id=$(plutil -extract id raw "$submission" 2>/dev/null); then
	echo "notarytool submit failed:" >&2
	cat "$submission" >&2
	exit 1
fi
status=$(plutil -extract status raw "$submission")
if [ "$status" != "Accepted" ]; then
	echo "notarization $id ended $status; Apple's log:" >&2
	xcrun notarytool log "$id" --keychain-profile "$CAIRN_NOTARY_PROFILE" >&2
	exit 1
fi
xcrun stapler staple "$app"

ditto -c -k --keepParent "$app" "$release"
echo "wrote $release"
