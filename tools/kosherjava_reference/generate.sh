#!/usr/bin/env bash
# Regenerates tests/data/kosherjava_reference.jsonl from a KosherJava checkout.
# Usage: tools/kosherjava_reference/generate.sh /path/to/KosherJava/zmanim
set -euo pipefail
KJ="${1:?path to a KosherJava/zmanim checkout}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HERE/../.."
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
mapfile -d '' SOURCES < <(find "$KJ/src/main/java" -name '*.java' -print0)
javac -nowarn -d "$BUILD" "${SOURCES[@]}" "$HERE/KJRef.java"
mkdir -p "$ROOT/tests/data"
java -cp "$BUILD" KJRef > "$ROOT/tests/data/kosherjava_reference.jsonl"
echo "kosherjava commit: $(git -C "$KJ" rev-parse --short HEAD)" > "$ROOT/tests/data/kosherjava_reference.version"
wc -l "$ROOT/tests/data/kosherjava_reference.jsonl"
