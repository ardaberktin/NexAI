import Foundation

/// One exchange is in flight at a time. The next sequence acknowledges the preceding reply.
final class WatchExchangeReceipts {
    private var last = WatchLiveUpdate(sequence: 0)

    func respond(to sequence: Int?, makeUpdate: () -> WatchLiveUpdate) -> WatchLiveUpdate {
        guard let sequence else { return makeUpdate() }
        if sequence == last.sequence, sequence > 0 { return last }
        guard sequence == (last.sequence ?? 0) + 1 else {
            return WatchLiveUpdate(ended: true, error: "The conversation is out of sync. Tap Ask Nex to reconnect.")
        }
        var update = makeUpdate()
        update.sequence = sequence
        last = update
        return update
    }
}

struct WatchLiveMicrophoneQueue {
    private var pending = Data()

    mutating func append(_ pcm: Data, silence: Bool) throws {
        guard pcm.count <= WatchLiveLink.inputBacklog - pending.count else {
            throw NexError.capture("The live connection fell too far behind. Keep the paired iPhone nearby and try again.")
        }
        pending.append(silence ? Data(count: pcm.count) : pcm)
    }

    mutating func take(using codec: WatchRelayAudioCodec?) throws -> Data? {
        guard !pending.isEmpty else { return nil }
        let count = min(pending.count, codec == nil ? WatchLiveLink.inputBatch : WatchLiveLink.compactInputBatch)
        let pcm = pending.prefix(count)
        let audio = try codec?.encode(pcm) ?? pcm
        pending.removeFirst(count)
        return audio
    }
}

extension WatchLiveRequest {
    /// Excalibur accepts small PCM packets even when several seconds arrive from the Watch at once.
    func serverFrames(using codec: WatchRelayAudioCodec?) throws -> [Data] {
        guard let audio else { return [] }
        let pcm = try codec?.decode(audio, maximumPCMBytes: WatchLiveLink.compactInputBatch) ?? audio
        guard pcm.count.isMultiple(of: 2), codec != nil || pcm.count <= WatchLiveLink.inputBatch else {
            throw NexError.capture("The Watch sent an invalid microphone frame.")
        }
        return stride(from: pcm.startIndex, to: pcm.endIndex, by: WatchLiveLink.inputBatch).map { start in
            pcm.subdata(in: start..<min(start + WatchLiveLink.inputBatch, pcm.endIndex))
        }
    }
}

struct WatchLiveOutputQueue {
    private var items: [(event: WatchLiveEvent, size: Int)] = []
    private var bytes = 0
    var isEmpty: Bool { items.isEmpty }

    mutating func append(_ event: WatchLiveEvent, using codec: WatchRelayAudioCodec?) throws {
        var item = event
        if let audio = item.audio, let codec { item.audio = try codec.encode(audio) }
        let size = try WatchLiveLink.encode(item, binary: codec != nil).count + 1
        guard bytes + size <= WatchLiveLink.outputBacklog, items.count < 350 else {
            throw NexError.capture("The Watch could not keep up with reply audio.")
        }
        items.append((item, size))
        bytes += size
    }

    mutating func discardAudio() {
        items.removeAll { $0.event.kind == .audio }
        bytes = items.reduce(0) { $0 + $1.size }
    }

    mutating func take() -> [WatchLiveEvent] {
        var size = 0
        let batch = items.prefix(16).prefix { item in
            guard size + item.size <= 28_000 else { return false }
            size += item.size
            return true
        }
        let events = batch.map(\.event)
        items.removeFirst(events.count)
        bytes -= size
        return events
    }
}
