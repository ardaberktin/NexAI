#!/usr/bin/env bash
# Install optional upstream keyword-spotting assets for a local Apple build.
set -euo pipefail

root=$(cd "$(dirname "$0")" && pwd)
models="$root/NexVoiceCapture/Sources/NexVoiceCapture/Models"
name=sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20
digest=68447f4fbc67e70eee3a93961f36e81e98f47aef73ce7e7ca00885c6cd3616a6
stage=$(mktemp -d "${TMPDIR:-/tmp}/nex-wake.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT

curl --fail --silent --show-error --location --retry 3 \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/$name.tar.bz2" \
    --output "$stage/model.tar.bz2"
printf '%s  %s\n' "$digest" "$stage/model.tar.bz2" | shasum -a 256 --check
variant=epoch-13-avg-2-chunk-8-left-64
tar -xjf "$stage/model.tar.bz2" -C "$stage" \
    "$name/encoder-$variant.int8.onnx" "$name/decoder-$variant.onnx" \
    "$name/joiner-$variant.int8.onnx" "$name/tokens.txt"
install -m 644 "$stage/$name/encoder-$variant.int8.onnx" "$models/encoder.onnx"
install -m 644 "$stage/$name/decoder-$variant.onnx" "$models/decoder.onnx"
install -m 644 "$stage/$name/joiner-$variant.int8.onnx" "$models/joiner.onnx"
install -m 644 "$stage/$name/tokens.txt" "$models/tokens.txt"
