#!/usr/bin/env bash
set -euo pipefail
umask 077

assets="${NEX_DATA:?Set NEX_DATA to the initialized Nex archive directory}/assets/live"
revision=e87f176479d0855a907a41277aca2f8ee7a09523
name=Qwen3.5-4B-Q4_K_M.gguf
digest=00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4
cd "$assets"
if [[ -f "$name" ]]; then
    printf '%s  %s\n' "$digest" "$name" | sha256sum --check
    exit
fi

stage=$(mktemp .qwen-3.5-XXXXXXXX)
trap 'rm -f -- "$stage"' EXIT
curl --fail --silent --show-error --location --retry 3 \
    "https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/$revision/$name" -o "$stage"
printf '%s  %s\n' "$digest" "$stage" | sha256sum --check
mv -- "$stage" "$name"
