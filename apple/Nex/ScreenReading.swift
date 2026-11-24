#if os(macOS)
import CoreGraphics
import Foundation
import ImageIO
import Vision

enum ScreenReader {
    static func read(_ data: Data) async -> ScreenReading? {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .utility).async {
                continuation.resume(returning: readJPEG(data))
            }
        }
    }

    private static func readJPEG(_ data: Data) -> ScreenReading? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { return nil }
        return ScreenReading(text: recognize(image), colors: fingerprint(image))
    }

    private static func recognize(_ image: CGImage) -> String {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .fast
        request.usesLanguageCorrection = false
        request.minimumTextHeight = 0.009
        do {
            try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
        } catch {
            return ""
        }
        let lines = (request.results ?? []).sorted {
            let firstRow = Int($0.boundingBox.midY * 80)
            let secondRow = Int($1.boundingBox.midY * 80)
            if firstRow != secondRow { return firstRow > secondRow }
            return $0.boundingBox.minX < $1.boundingBox.minX
        }.compactMap { $0.topCandidates(1).first?.string }
        return String(lines.joined(separator: "\n").prefix(1_100))
    }

    private static func fingerprint(_ image: CGImage) -> [UInt8] {
        let width = 16
        let height = 9
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        pixels.withUnsafeMutableBytes { storage in
            guard let context = CGContext(data: storage.baseAddress, width: width, height: height,
                                          bitsPerComponent: 8, bytesPerRow: width * 4,
                                          space: CGColorSpaceCreateDeviceRGB(),
                                          bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return }
            context.interpolationQuality = .medium
            context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        }
        return pixels
    }
}
#endif
