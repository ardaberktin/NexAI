// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "NexVoiceCapture",
    platforms: [.macOS(.v14), .iOS(.v17)],
    products: [.library(name: "NexVoiceCapture", targets: ["NexVoiceCapture"])],
    targets: [
        .target(
            name: "NexVoiceCapture",
            dependencies: [
                .target(name: "SherpaMac", condition: .when(platforms: [.macOS])),
                .target(name: "OnnxMac", condition: .when(platforms: [.macOS])),
                .target(name: "SherpaIOS", condition: .when(platforms: [.iOS])),
                .target(name: "OnnxIOS", condition: .when(platforms: [.iOS])),
            ],
            resources: [.copy("Models")],
            linkerSettings: [.linkedLibrary("c++")]
        ),
        .binaryTarget(
            name: "SherpaMac",
            url: "https://github.com/k2-fsa/sherpa-onnx/releases/download/xcframework/sherpa-onnx-v1.13.8-macos-static.xcframework.zip",
            checksum: "93f7a064abe99e0d6185a88c5b36ce18c4bff35cd0d5e4e81f81151de8e3e7e5"
        ),
        .binaryTarget(
            name: "SherpaIOS",
            url: "https://github.com/k2-fsa/sherpa-onnx/releases/download/xcframework/sherpa-onnx-v1.13.8-ios-static.xcframework.zip",
            checksum: "6b8e769cb153343270fdccbe92e3b3db0d1c421d67fa0989ab01fdf5b2fcf2de"
        ),
        .binaryTarget(
            name: "OnnxMac",
            url: "https://github.com/csukuangfj/onnxruntime-libs/releases/download/v1.28.1/onnxruntime-macos-static-xcframework-1.28.1.xcframework.zip",
            checksum: "38c9a1e8bcf2497ff5eb721195743e4b4367ade3cd1eb9869c0cfd1bd367c36b"
        ),
        .binaryTarget(
            name: "OnnxIOS",
            url: "https://github.com/csukuangfj/onnxruntime-libs/releases/download/v1.28.1/onnxruntime-ios-static-xcframework-1.28.1.xcframework.zip",
            checksum: "992d8a0cc6014cccc3a7815c36bbff5e5a06833ea2c4d47dd43ef071f639cf9d"
        ),
    ]
)
