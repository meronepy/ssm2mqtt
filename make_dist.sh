#!/usr/bin/env bash
set -eu

ROOT_DIR=$(cd "$(dirname "$0")" && pwd)
VERSION=$(awk -F'"' '/^version[[:space:]]*=/{print $2; exit}' "$ROOT_DIR/pyproject.toml")
DIST_NAME="ssm2mqtt_${VERSION}.zip"
DIST_DIR="$ROOT_DIR/dist"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

STAGE_DIR="$TMP_DIR/ssm2mqtt"
mkdir -p "$STAGE_DIR"

cp "$ROOT_DIR/discover.py" "$STAGE_DIR/"
cp "$ROOT_DIR/LICENSE" "$STAGE_DIR/"
cp "$ROOT_DIR/main.py" "$STAGE_DIR/"
cp "$ROOT_DIR/README.md" "$STAGE_DIR/"
cp "$ROOT_DIR/resources/config.json" "$STAGE_DIR/config.json"
cp "$ROOT_DIR/resources/ssm2mqtt.service" "$STAGE_DIR/ssm2mqtt.service"

awk '
/^dependencies[[:space:]]*=\s*\[/ {in_deps=1; next}
in_deps && /\]/ {in_deps=0; exit}
in_deps {
  gsub(/^[[:space:]]+|[",]/, "", $0)
  if (length($0) >  0) print $0
}
' "$ROOT_DIR/pyproject.toml" > "$STAGE_DIR/requirements.txt"

mkdir -p "$DIST_DIR"
echo "*" > "$DIST_DIR/.gitignore"
(cd "$TMP_DIR" && zip -r "$DIST_DIR/$DIST_NAME" ssm2mqtt > /dev/null)

echo "Created $DIST_DIR/$DIST_NAME"
