#!/usr/bin/env bash
set -euo pipefail
umask 077
assets="${NEX_DATA:?Set NEX_DATA to the initialized Nex archive directory}/assets/live"
revision=bbcac0d065076c47042838c0675c602411b0dd4c
name=gemma-3-4b-it-qat-Q4_0.gguf
digest=ee91c3e7a4ab95d8c95672f9fcb58bf236b257e9f217966bcf53a5a6df4ab49a
cd "$assets"
if [[ -f "$name" ]]; then
    printf '%s  %s\n' "$digest" "$name" | sha256sum --check
    exit
fi
stage=$(mktemp .gemma-XXXXXXXX)
trap 'rm -f -- "$stage"' EXIT
curl --fail --silent --show-error --location --retry 3 \
    "https://huggingface.co/ggml-org/gemma-3-4b-it-qat-GGUF/resolve/$revision/$name" -o "$stage"
printf '%s  %s\n' "$digest" "$stage" | sha256sum --check
mv -- "$stage" "$name"
