# Wake model

The encoder, decoder, joiner and vocabulary come from the sherpa-onnx 3M Chinese and English Zipformer keyword spotting release dated December 20, 2025. Nex uses the chunk-8 encoder and joiner in INT8 and the corresponding floating-point decoder. The local keyword list accepts “Hey Nex” with or without a final T.

Upstream archive SHA-256: `68447f4fbc67e70eee3a93961f36e81e98f47aef73ce7e7ca00885c6cd3616a6`

[Upstream model documentation](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html)
