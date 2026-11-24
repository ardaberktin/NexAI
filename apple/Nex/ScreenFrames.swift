#if os(macOS)
import CoreImage
import CoreMedia
import Foundation
import ScreenCaptureKit

struct ScreenSnapshot: Sendable {
    let revision: UInt64
    let time: Date
    let jpeg: Data
}

/// The capture queue retains the latest complete frame and encodes it on request.
final class ScreenFrames: NSObject, SCStreamOutput, @unchecked Sendable {
    let queue = DispatchQueue(label: "nex.screen.frames", qos: .userInitiated)
    private let render = CIContext(options: [.cacheIntermediates: false])
    private var frame: (image: CVPixelBuffer, time: Date)?
    private var revision: UInt64 = 0
    private var closed = false
    private var suspended = false

    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer, of type: SCStreamOutputType) {
        guard !closed, !suspended, type == .screen, sample.isValid,
              let attachments = CMSampleBufferGetSampleAttachmentsArray(sample, createIfNecessary: false)
                as? [[SCStreamFrameInfo: Any]],
              let raw = attachments.first?[.status] as? Int, let status = SCFrameStatus(rawValue: raw) else { return }
        switch status {
        case .complete, .started:
            if let incoming = sample.imageBuffer {
                revision &+= 1
                frame = (incoming, .now)
            }
        case .blank, .stopped, .suspended:
            frame = nil
        default:
            break
        }
    }

    func setSuspended(_ value: Bool) {
        queue.async {
            self.suspended = value
            if value { self.frame = nil }
        }
    }

    func close() {
        queue.async {
            self.closed = true
            self.frame = nil
        }
    }

    func snapshot(after previous: UInt64? = nil) async -> ScreenSnapshot? {
        await withCheckedContinuation { continuation in
            queue.async {
                guard let frame = self.frame, !self.closed, !self.suspended, previous != self.revision else {
                    continuation.resume(returning: nil)
                    return
                }
                let image = CIImage(cvPixelBuffer: frame.image)
                let data = self.render.jpegRepresentation(of: image, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
                    options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.82])
                continuation.resume(returning: data.map { ScreenSnapshot(revision: self.revision, time: frame.time, jpeg: $0) })
            }
        }
    }

    func hasVisibleFrame(revision expected: UInt64?) async -> Bool {
        await withCheckedContinuation { continuation in
            queue.async {
                continuation.resume(returning: !self.closed && !self.suspended && self.frame != nil && self.revision == expected)
            }
        }
    }
}
#endif
