import Foundation

@main
enum ChatActivityCheck {
    static let anchor = Date(timeIntervalSince1970: 1_800_000_000)

    static func sample(_ category: String, endedAgo: TimeInterval, device: String = "iphone") -> ChatActivity {
        let end = anchor.addingTimeInterval(-endedAgo)
        return ChatActivity(
            category: category, title: category, confidence: "Possible", device: device,
            started: NexDate.wire(end.addingTimeInterval(-12)), ended: NexDate.wire(end))
    }

    static func main() throws {
        let game = sample("gaming", endedAgo: 36)
        let music = sample("music", endedAgo: 12, device: "mac")
        let quiet = sample("unknown", endedAgo: 5)
        let speech = sample("speech", endedAgo: 2, device: "mac")
        func legacy(_ items: [ChatActivity]) -> [ChatDetection] {
            ChatActivitySnapshot(items: items, asOf: nil, freshFor: nil).detections(receivedAt: anchor)
        }
        precondition(legacy([]).isEmpty)
        precondition(legacy([game, music]).map(\.id) == ["music", "gaming"])
        precondition(legacy([quiet, music, game]).map(\.id) == ["music"])
        precondition(legacy([quiet, game, music, speech]).isEmpty)
        precondition(legacy([game, sample("gaming", endedAgo: 10, device: "mac")]).count == 1)
        precondition(legacy([sample("ps5", endedAgo: 90), sample("phone", endedAgo: -1)]).isEmpty)

        let date = NexDate.wire(anchor)
        let packet = """
            {"as_of":"\(date)","fresh_for":90,"items":[{"category":"ps5","title":"Playing PS5",
            "confidence":"Likely","device":"iphone","started":"\(game.started)","ended":"\(game.ended)"}]}
            """
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let snapshot = try decoder.decode(ChatActivitySnapshot.self, from: Data(packet.utf8))
        let received = anchor.addingTimeInterval(300)
        let detection = snapshot.detections(receivedAt: received)
        precondition(detection.first?.activity.title == "Playing PS5")
        precondition(detection.first?.observedAt == anchor.addingTimeInterval(-36))
        precondition(detection.first?.expiresAt == received.addingTimeInterval(54))
        print("Chat activity checks passed.")
    }
}
