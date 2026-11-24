#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
revision=${1:-HEAD}
output=${2:-"$root/dist"}
mkdir -p -- "$output"
output=$(cd "$output" && pwd)
cd "$root"
commit=$(git rev-parse --verify --end-of-options "$revision^{commit}")
stage=$(mktemp -d "$output/.source.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT

shared=(.gitignore LICENSE THIRD_PARTY_NOTICES.md AGENTS.md docs/architecture.md docs/assistant-contract.md)
git archive --format=zip --prefix=Nex-Apple/ --output="$stage/nex-apple-source.zip" \
    "$commit" apple "${shared[@]}"
git archive --format=tar.gz --prefix=Nex-Server/ --output="$stage/nex-server-source.tar.gz" \
    "$commit" server "${shared[@]}"

mv -- "$stage/nex-apple-source.zip" "$stage/nex-server-source.tar.gz" "$output/"
printf '%s\n' "$output/nex-apple-source.zip" "$output/nex-server-source.tar.gz"
