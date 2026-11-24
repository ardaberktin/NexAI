#!/usr/bin/env bash
# Run on an x86-64 Linux server to stage the live model artifacts and runtime.
set -euo pipefail
umask 077
src=$(cd "$(dirname "$0")" && pwd)
app="${NEX_LIVE_HOME:-$HOME/.local/share/nex-live}"
data="${NEX_DATA:?Set NEX_DATA to the initialized Nex archive directory}"
assets="$data/assets/live"
sherpa=https://github.com/k2-fsa/sherpa-onnx/releases/download
llama=https://github.com/ggml-org/llama.cpp/releases/download/b12359
asr=sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-560ms-int8-2026-06-11
test -f "$data/access.json"
install -d -m 700 "$app" "$assets" "$assets/vulkan"
stage=$(mktemp -d "$assets/.stage.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT

fetch() { curl --fail --silent --show-error --location --retry 3 "$1" -o "$2"; }
if [[ ! -x "$app/uv" ]]; then
    fetch https://github.com/astral-sh/uv/releases/download/0.13.8/uv-x86_64-unknown-linux-gnu.tar.gz "$stage/uv.tgz"
    tar xzf "$stage/uv.tgz" -C "$app" --strip-components=1 uv-x86_64-unknown-linux-gnu/uv
fi
if [[ ! -x "$app/venv/bin/python" ]]; then "$app/uv" venv --python 3.12 "$app/venv"; fi
python="$app/venv/bin/python"
"$app/uv" pip install --python "$python" --index https://download.pytorch.org/whl/cpu 'torch==2.7.1+cpu'
"$app/uv" pip install --python "$python" -r "$src/live-requirements.txt"

if [[ ! -f "$assets/nemotron/encoder.int8.onnx" ]]; then
    fetch "$sherpa/asr-models/$asr.tar.bz2" "$stage/asr.tar.bz2"
    tar xjf "$stage/asr.tar.bz2" -C "$stage"
    mv "$stage/$asr" "$assets/nemotron"
fi
if [[ ! -f "$assets/kokoro-en-v0_19/model.onnx" ]]; then
    fetch "$sherpa/tts-models/kokoro-en-v0_19.tar.bz2" "$stage/kokoro.tar.bz2"
    tar xjf "$stage/kokoro.tar.bz2" -C "$assets"
fi
if [[ ! -x "$assets/llama-b12359/llama-server" ]]; then
    fetch "$llama/llama-b12359-bin-ubuntu-x64.tar.gz" "$stage/llama.tgz"
    tar xzf "$stage/llama.tgz" -C "$assets"
fi
if [[ "${NEX_STAGE_VULKAN:-0}" == 1 && ! -x "$assets/vulkan/llama-b12359/llama-server" ]]; then
    fetch "$llama/llama-b12359-bin-ubuntu-vulkan-x64.tar.gz" "$stage/llama-vulkan.tgz"
    tar xzf "$stage/llama-vulkan.tgz" -C "$assets/vulkan"
fi
if [[ ! -f "$assets/qwen.gguf" ]]; then
    qwen=https://huggingface.co/bartowski/Qwen_Qwen3-1.7B-GGUF/resolve/dcb19155b962dbb6389f4691a982043a8e651022
    fetch "$qwen/Qwen_Qwen3-1.7B-Q4_K_M.gguf" "$stage/qwen.gguf"
    mv "$stage/qwen.gguf" "$assets/qwen.gguf"
fi
HF_HOME="$assets/hf" "$python" -c 'from ema_lightning import EMA; EMA(device="cpu").say("Hazırım.")'
bash "$src/stage-room-context.sh"
