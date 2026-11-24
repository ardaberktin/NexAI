import Foundation
import SherpaOnnxC

/// One audio queue owns the detector and its streaming state.
public final class WakeWord {
    private let spotter: OpaquePointer
    private let stream: OpaquePointer

    public convenience init?() {
        guard let folder = Bundle.module.resourceURL?.appending(path: "Models") else { return nil }
        self.init(models: folder)
    }

    init?(models: URL) {
        let names = ["encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt", "keywords.txt"]
        let paths = names.map { models.appending(path: $0).path }
        guard paths.allSatisfy({ FileManager.default.isReadableFile(atPath: $0) }) else { return nil }
        let strings = paths.map { strdup($0) }
        defer { strings.forEach { free($0) } }
        guard strings.allSatisfy({ $0 != nil }) else { return nil }
        var config = SherpaOnnxKeywordSpotterConfig()
        config.feat_config.sample_rate = 16_000
        config.feat_config.feature_dim = 80
        config.model_config.transducer.encoder = UnsafePointer(strings[0])
        config.model_config.transducer.decoder = UnsafePointer(strings[1])
        config.model_config.transducer.joiner = UnsafePointer(strings[2])
        config.model_config.tokens = UnsafePointer(strings[3])
        config.model_config.num_threads = 1
        config.keywords_file = UnsafePointer(strings[4])
        config.max_active_paths = 4
        config.num_trailing_blanks = 1
        config.keywords_score = 1
        config.keywords_threshold = 0.25
        guard let spotter = SherpaOnnxCreateKeywordSpotter(&config) else { return nil }
        guard let stream = SherpaOnnxCreateKeywordStream(spotter) else {
            SherpaOnnxDestroyKeywordSpotter(spotter)
            return nil
        }
        self.spotter = spotter
        self.stream = stream
    }

    deinit {
        SherpaOnnxDestroyOnlineStream(stream)
        SherpaOnnxDestroyKeywordSpotter(spotter)
    }

    /// Returns immediately after the keyword is decoded from the rolling audio.
    public func accept(_ samples: UnsafeBufferPointer<Float>, rate: Int32) -> Bool {
        guard let base = samples.baseAddress, !samples.isEmpty else { return false }
        SherpaOnnxOnlineStreamAcceptWaveform(stream, rate, base, Int32(samples.count))
        while SherpaOnnxIsKeywordStreamReady(spotter, stream) == 1 {
            SherpaOnnxDecodeKeywordStream(spotter, stream)
            if detected() {
                reset()
                return true
            }
        }
        return false
    }

    public func reset() {
        SherpaOnnxResetKeywordStream(spotter, stream)
    }

    private func detected() -> Bool {
        guard let result = SherpaOnnxGetKeywordResult(spotter, stream) else { return false }
        defer { SherpaOnnxDestroyKeywordResult(result) }
        return result.pointee.keyword?.pointee != 0 && result.pointee.count > 0
    }
}
