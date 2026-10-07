#!/bin/sh
# Regenerates tests/data/kosherjava_reference.jsonl from a KosherJava checkout.
# Usage: tools/kosherjava_reference/generate.sh /path/to/KosherJava/zmanim
set -e
KJ="${1:?path to a KosherJava/zmanim checkout}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HERE/../.."
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
javac -nowarn -d "$BUILD" $(find "$KJ/src/main/java" -name '*.java') "$HERE/KJRef.java"
mkdir -p "$ROOT/tests/data"
java -cp "$BUILD" KJRef > "$ROOT/tests/data/kosherjava_reference.jsonl"
echo "kosherjava commit: $(git -C "$KJ" rev-parse --short HEAD)" > "$ROOT/tests/data/kosherjava_reference.version"
wc -l "$ROOT/tests/data/kosherjava_reference.jsonl"
