import Foundation

@main
enum WatchLiveRelayCheck {
    static let id = "6608feba-96bb-4385-a080-9e0c71870453"

    static func main() throws {
        guard let watch = WatchRelayAudioCodec(encodingRate: 16_000, decodingRate: 24_000),
              let phone = WatchRelayAudioCodec(encodingRate: 24_000, decodingRate: 16_000) else {
            throw NexError.capture("Apple IMA4 is unavailable.")
        }
        try delayedMicrophone(watch: watch, phone: phone)
        try replyPlayback(phone: phone, watch: watch)
        try simultaneousAudio(watch: watch, phone: phone)
        try lostResponse(phone: phone, watch: watch)
        try limitsAndCompatibility(watch: watch, phone: phone)
        print("Slow relay catch-up, speech quality, reply ordering, limits and legacy PCM passed.")
    }

    private static func lostResponse(phone: WatchRelayAudioCodec, watch: WatchRelayAudioCodec) throws {
        let receipts = WatchExchangeReceipts()
        let input = tone(frames: 1_600, rate: 16_000)
        let spoken = tone(frames: 3_000, rate: 24_000)
        var request = WatchLiveRequest(.exchange, id: id)
        request.audio = try watch.encode(input)
        request.sequence = 1
        let wire = try request.wire(binary: true)
        var output = WatchLiveOutputQueue()
        try output.append(.init(kind: .audio, audio: spoken), using: phone)
        try output.append(.init(kind: .reply, id: id, text: "Hello", language: "en"), using: phone)
        var submitted = Data()
        func exchange() throws -> WatchLiveUpdate {
            let incoming = try WatchLiveLink.decode(WatchLiveRequest.self, from: wire.dropFirst(WatchLiveLink.prefix.count))
            let frames = try incoming.serverFrames(using: phone)
            return receipts.respond(to: incoming.sequence) {
                for frame in frames { submitted.append(frame) }
                return WatchLiveUpdate(events: output.take(), ended: true)
            }
        }

        _ = try exchange() // The phone accepted the mic and dequeued the final answer. Lose this response.
        let recovered = try exchange()
        precondition(recovered.sequence == 1 && recovered.ended && submitted.count == input.count)
        precondition(recovered.events.map(\.kind) == [.audio, .reply] && recovered.events.last?.text == "Hello")
        let pcm = try watch.decode(recovered.events[0].audio!, maximumPCMBytes: WatchLiveLink.outputBatch)
        precondition(pcm.count == spoken.count && quality(spoken, pcm) > 20)
        let next = receipts.respond(to: 2) { WatchLiveUpdate(events: output.take()) }
        precondition(next.events.isEmpty && next.sequence == 2)
        let stale = receipts.respond(to: 1) { preconditionFailure("Stale microphone replay") }
        precondition(stale.error != nil)
        let legacy = receipts.respond(to: nil) { WatchLiveUpdate(ended: true) }
        precondition(legacy.sequence == nil && legacy.ended)
        print("Lost final response recovered with its audio and text. The phone accepted the microphone samples once.")
    }

    private static func delayedMicrophone(watch: WatchRelayAudioCodec, phone: WatchRelayAudioCodec) throws {
        let source = tone(frames: 16_000 * 60 + 13, rate: 16_000)
        var queue = WatchLiveMicrophoneQueue()
        var delivered = Data()
        var captured = 0
        var rounds = 0
        var exchangeSeconds = 0.17
        var peakBytes = 0
        while delivered.count < source.count {
            let arrival = min(captured + 2 * Int(exchangeSeconds * 16_000), source.count)
            try queue.append(source[captured..<arrival], silence: false)
            captured = arrival
            peakBytes = max(peakBytes, captured - delivered.count)
            var request = WatchLiveRequest(.exchange, id: id)
            request.audio = try queue.take(using: watch)
            let wire = try request.wire(binary: true)
            precondition(wire.count <= WatchLiveLink.maximumMessage)
            let incoming = try WatchLiveLink.decode(WatchLiveRequest.self, from: wire.dropFirst(WatchLiveLink.prefix.count))
            let frames = try incoming.serverFrames(using: phone)
            precondition(!frames.isEmpty && frames.allSatisfy { $0.count <= 16_384 && $0.count.isMultiple(of: 2) })
            for frame in frames { delivered.append(frame) }
            // Model a shared radio with one second of round-trip latency, 24 KB/sec capacity,
            // and one five-second stall while the Watch continues capturing in real time.
            exchangeSeconds = 1 + Double(wire.count) / 24_000 + (rounds == 5 ? 5 : 0)
            rounds += 1
        }
        precondition(peakBytes > 32_000 * 5 && delivered.count == source.count)
        precondition(quality(source, delivered) > 20)
        print("60 seconds of 16 kHz input delivered, peak queue \(Double(peakBytes) / 32_000) seconds.")
    }

    private static func replyPlayback(phone: WatchRelayAudioCodec, watch: WatchRelayAudioCodec) throws {
        let source = tone(frames: 24_000 * 8 + 7, rate: 24_000)
        var queue = WatchLiveOutputQueue()
        try queue.append(.init(kind: .turnEnd, id: id), using: phone)
        for offset in stride(from: 0, to: source.count, by: WatchLiveLink.outputBatch) {
            let pcm = source[offset..<min(offset + WatchLiveLink.outputBatch, source.count)]
            try queue.append(.init(kind: .audio, audio: pcm), using: phone)
        }
        try queue.append(.init(kind: .reply, id: id), using: phone)
        var events: [WatchLiveEvent] = []
        var exchanges = 0
        while !queue.isEmpty {
            let wire = try WatchLiveLink.encode(WatchLiveUpdate(events: queue.take()), binary: true)
            precondition(wire.count <= WatchLiveLink.maximumMessage)
            events += try WatchLiveLink.decode(WatchLiveUpdate.self, from: wire).events
            exchanges += 1
        }
        precondition(events.first?.kind == .turnEnd && events.last?.kind == .reply && exchanges <= 6)
        var restored = Data()
        for event in events.dropFirst().dropLast() {
            guard event.kind == .audio, let audio = event.audio else { preconditionFailure("Reply order") }
            restored.append(try watch.decode(audio, maximumPCMBytes: WatchLiveLink.outputBatch))
        }
        precondition(source.count == restored.count && quality(source, restored) > 20)
    }

    private static func simultaneousAudio(watch: WatchRelayAudioCodec, phone: WatchRelayAudioCodec) throws {
        let source = tone(frames: 16_000 * 30, rate: 16_000)
        let reply = tone(frames: 24_000 * 30, rate: 24_000)
        var microphone = WatchLiveMicrophoneQueue()
        var output = WatchLiveOutputQueue()
        var heard = Data()
        var played = Data()
        var elapsed = 0.14
        var captured = 0
        var supplied = 0
        while heard.count < source.count || played.count < reply.count {
            let end = min(captured + 2 * Int(elapsed * 16_000), source.count)
            try microphone.append(source[captured..<end], silence: false)
            captured = end
            let replyEnd = min(supplied + 2 * Int(elapsed * 24_000), reply.count)
            while supplied < replyEnd {
                let next = min(supplied + WatchLiveLink.outputBatch, replyEnd)
                try output.append(.init(kind: .audio, audio: reply[supplied..<next]), using: phone)
                supplied = next
            }
            var request = WatchLiveRequest(.exchange, id: id)
            request.audio = try microphone.take(using: watch)
            let up = try request.wire(binary: true)
            let incoming = try WatchLiveLink.decode(WatchLiveRequest.self, from: up.dropFirst(WatchLiveLink.prefix.count))
            for frame in try incoming.serverFrames(using: phone) { heard.append(frame) }
            let down = try WatchLiveLink.encode(WatchLiveUpdate(events: output.take()), binary: true)
            let update = try WatchLiveLink.decode(WatchLiveUpdate.self, from: down)
            for event in update.events {
                guard let audio = event.audio else { preconditionFailure("Missing reply audio") }
                played.append(try watch.decode(audio, maximumPCMBytes: WatchLiveLink.outputBatch))
            }
            precondition(!update.events.isEmpty || request.audio != nil)
            // Both directions share one radio and one outstanding request at a time.
            elapsed = 0.7 + Double(up.count + down.count) / 36_000
        }
        precondition(heard.count == source.count && quality(source, heard) > 20)
        precondition(played.count == reply.count && quality(reply, played) > 20)
    }

    private static func limitsAndCompatibility(watch: WatchRelayAudioCodec, phone: WatchRelayAudioCodec) throws {
        var queue = WatchLiveMicrophoneQueue()
        let signal = tone(frames: 7_003, rate: 16_000)
        try queue.append(signal, silence: true)
        var muted = WatchLiveRequest(.exchange, id: id)
        muted.audio = try queue.take(using: watch)
        let silence = try muted.serverFrames(using: phone).reduce(into: Data()) { $0.append($1) }
        precondition(muted.audio?.count == 4 && silence == Data(count: signal.count))

        try queue.append(signal, silence: false)
        var legacy = WatchLiveRequest(.exchange, id: id)
        legacy.audio = try queue.take(using: nil)
        let decoded = try WatchLiveLink.decode(WatchLiveRequest.self, from: legacy.wire().dropFirst(WatchLiveLink.prefix.count))
        let frames = try decoded.serverFrames(using: nil)
        precondition(decoded.compactAudio == nil && frames == [signal.prefix(WatchLiveLink.inputBatch)])
        let oldStart = try WatchLiveLink.decode(WatchLiveUpdate.self, from: Data(#"{"ended":false,"events":[]}"#.utf8))
        precondition(oldStart.compactAudio == nil)
        for invalid in [Data(), Data([1, 0]), Data(repeating: 255, count: 4), Data([3, 0, 0, 0, 1])] {
            precondition((try? phone.decode(invalid, maximumPCMBytes: WatchLiveLink.compactInputBatch)) == nil)
        }
        var outage = WatchLiveMicrophoneQueue()
        try outage.append(Data(count: WatchLiveLink.inputBacklog), silence: false)
        do {
            try outage.append(Data(count: 2), silence: false)
            preconditionFailure("The microphone queue must stay bounded.")
        } catch NexError.capture { }
    }

    private static func tone(frames: Int, rate: Double) -> Data {
        let samples = (0..<frames).map { index -> Int16 in
            let phase = Double(index) / rate
            let signal = sin(2 * .pi * 190 * phase + 2 * sin(phase * 5))
            return Int16(signal * 8_000 + sin(2 * .pi * 855 * phase) * 1_500).littleEndian
        }
        return samples.withUnsafeBytes { Data($0) }
    }

    private static func quality(_ reference: Data, _ received: Data) -> Double {
        reference.withUnsafeBytes { expected in
            received.withUnsafeBytes { actual in
                var energy = 0.0
                var error = 0.0
                for i in stride(from: 0, to: reference.count, by: 2) {
                    let original = Double(expected.loadUnaligned(fromByteOffset: i, as: Int16.self))
                    let value = Double(actual.loadUnaligned(fromByteOffset: i, as: Int16.self))
                    energy += original * original
                    error += (original - value) * (original - value)
                }
                return 10 * log10(energy / error)
            }
        }
    }
}
