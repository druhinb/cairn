#!/bin/sh
# Build dist/Cairn.app, unsigned, from this checkout in a fresh virtualenv.
# CAIRN_PYTHON picks the Python uv builds with (default 3.14).
set -eu
cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
	echo "build_app.sh needs uv: https://docs.astral.sh/uv/" >&2
	exit 2
fi

venv=build/app-venv
rm -rf "$venv"
UV_PROJECT_ENVIRONMENT="$venv" uv sync --frozen --extra app --python "${CAIRN_PYTHON:-3.14}"

iconset=build/AppIcon.iconset
rm -rf "$iconset"
mkdir -p "$iconset"
"$venv/bin/python" packaging/make_icon.py build/AppIcon-1024.png
for size in 16 32 128 256 512; do
	sips -z "$size" "$size" build/AppIcon-1024.png \
		--out "$iconset/icon_${size}x${size}.png" >/dev/null
	double=$((size * 2))
	sips -z "$double" "$double" build/AppIcon-1024.png \
		--out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns -o packaging/AppIcon.icns "$iconset"

"$venv/bin/pyinstaller" --noconfirm --clean --distpath dist --workpath build/pyinstaller \
	packaging/cairn.spec
echo "built dist/Cairn.app"
