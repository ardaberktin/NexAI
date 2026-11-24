import AVFAudio
import Foundation

/// Kept separate from answer playback so a pickup never pauses the microphone.
@MainActor
final class QuestionChime {
    nonisolated static let volumeKey = "questionChimeVolume"
    // The louder app asset matches the original chime at 30%.
    nonisolated static let defaultVolume = 0.30

    private let player: AVAudioPlayer?
    var available: Bool { player != nil }

    init(url: URL? = Bundle.main.url(forResource: "question-chime", withExtension: "wav")) {
        player = url.flatMap { try? AVAudioPlayer(contentsOf: $0) }
    }

    func prepare() { player?.prepareToPlay() }

    @discardableResult
    func play() -> Bool {
        let savedVolume = UserDefaults.standard.object(forKey: Self.volumeKey) as? Double
        let volume = savedVolume ?? Self.defaultVolume
        // A muted chime still handles the acknowledgement, including on older servers.
        guard volume > 0 else { return true }
        guard let player else { return false }
        player.volume = Float(min(volume, 1))
        player.currentTime = 0
        return player.play()
    }
}
