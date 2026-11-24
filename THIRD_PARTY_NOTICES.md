# Third-party software and models

The repository’s MIT license covers original Nex code and documentation. External packages, downloaded runtimes, and model weights retain their respective terms.

- The Apple build resolves checksum-pinned [Sherpa-ONNX](https://github.com/k2-fsa/sherpa-onnx) (Apache-2.0) and [ONNX Runtime](https://github.com/microsoft/onnxruntime) (MIT) static frameworks through Swift Package Manager. Review the notices in upstream artifacts before distributing compiled apps.
- The optional wake detector uses the upstream [Chinese/English Zipformer keyword-spotting weights](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html). Nex source releases exclude those weights and the upstream vocabulary. Review their terms before redistributing a build that includes them. [Upstream redistribution discussion](https://github.com/k2-fsa/sherpa-onnx/issues/3852)
- Server runtimes and weights are installed separately. The server setup guide and `server/deploy/stage-live-assets.sh` identify the upstream sources. Review the terms of each selected runtime and model before redistribution.
