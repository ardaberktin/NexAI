#!/usr/bin/env bash
# Install the pinned offline room-audio models used by automatic voice.
set -euo pipefail
umask 077

assets="${NEX_DATA:?Set NEX_DATA to the initialized Nex archive directory}/assets/live"
destination="$assets/room-context"
base=https://github.com/k2-fsa/sherpa-onnx/releases/download
tagger=sherpa-onnx-ced-mini-audio-tagging-2024-04-19
segmentation=sherpa-onnx-pyannote-segmentation-3-0
speaker=3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx

if [[ -f "$destination/model.int8.onnx" && -f "$destination/class_labels_indices.csv" &&
      -f "$destination/segmentation.onnx" && -f "$destination/speaker.onnx" ]]; then
    exit 0
fi
install -d -m 700 "$assets"
stage=$(mktemp -d "$assets/.room-context.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT
cd "$stage"

fetch() { curl --fail --silent --show-error --location --retry 3 "$1" -o "$2"; }
fetch "$base/audio-tagging-models/$tagger.tar.bz2" tagger.tar.bz2
fetch "$base/speaker-segmentation-models/$segmentation.tar.bz2" segmentation.tar.bz2
fetch "$base/speaker-recongition-models/$speaker" speaker.onnx
sha256sum --check --status <<'SUMS'
bec8bfa0af2c20ec3a9e7c6dd6c92d0fb6e96d6b25370ebe7a48e7283a60a02c  tagger.tar.bz2
24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488  segmentation.tar.bz2
f1ee61be04000b50392a63442e5dd9feaea7e81dddd494d3647cf1c3d88a1498  speaker.onnx
SUMS

tar xjf tagger.tar.bz2 --strip-components=1 "$tagger/model.int8.onnx" "$tagger/class_labels_indices.csv"
tar xjf segmentation.tar.bz2 --strip-components=1 "$segmentation/model.onnx"
mv model.onnx segmentation.onnx
install -d -m 700 "$destination"
chmod 600 model.int8.onnx class_labels_indices.csv segmentation.onnx speaker.onnx
mv -f model.int8.onnx class_labels_indices.csv segmentation.onnx speaker.onnx "$destination/"
