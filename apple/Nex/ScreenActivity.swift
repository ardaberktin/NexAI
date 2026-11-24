#if os(macOS)
import Foundation

struct ScreenObservation: Sendable {
    let id = UUID()
    let time: Date
    let visibleText: String
    var caption: String?
}

struct ScreenReading: Sendable {
    let text: String
    let colors: [UInt8]

    func meaningfullyDiffers(from previous: Self) -> Bool {
        guard colors.count == previous.colors.count else { return true }
        let difference = zip(colors, previous.colors).reduce(0) { sum, pair in
            sum + abs(Int(pair.0) - Int(pair.1))
        }
        if difference >= colors.count * 9 { return true }
        // Comparing words ignores small OCR differences in punctuation as the cursor moves.
        let currentWords = ScreenActivity.words(text)
        let oldWords = ScreenActivity.words(previous.text)
        let changed = currentWords.symmetricDifference(oldWords).count
        return changed >= max(1, min(4, max(currentWords.count, oldWords.count) / 8))
    }
}

/// In-memory context for the currently shared source. No images are retained here.
struct ScreenActivity {
    private static let retention: TimeInterval = 24 * 60 * 60
    private static let maximumEntries = 8_640
    private static let common = Set("""
        a an and about did do for look me my on screen show tell the this to was were what when
        where which with work working you i is it at that in how have had today earlier last time
        """.split(whereSeparator: \.isWhitespace).map(String.init))

    private(set) var entries: [ScreenObservation] = []
    private var lastReading: ScreenReading?

    mutating func record(_ reading: ScreenReading, at time: Date) -> UUID? {
        prune(asOf: time)
        if let lastReading, !reading.meaningfullyDiffers(from: lastReading) { return nil }
        lastReading = reading
        if entries.count == Self.maximumEntries { entries.removeFirst() }
        let entry = ScreenObservation(time: time, visibleText: reading.text, caption: nil)
        entries.append(entry)
        return entry.id
    }

    mutating func annotate(_ id: UUID, with caption: String) {
        if let index = entries.lastIndex(where: { $0.id == id }) {
            entries[index].caption = caption
        }
    }

    mutating func prune(asOf time: Date) {
        let cutoff = time.addingTimeInterval(-Self.retention)
        let expired = entries.prefix(while: { $0.time < cutoff }).count
        entries.removeFirst(expired)
        if entries.isEmpty { lastReading = nil }
    }

    func evidence(for question: String) -> [ScreenObservation] {
        let eligible = entries.filter { !$0.visibleText.isEmpty || $0.caption != nil }
        guard eligible.count > 9 else { return eligible }
        let terms = Self.words(question).subtracting(Self.common)
        let ranked = eligible.enumerated().compactMap { index, item -> (Int, Int)? in
            let words = Self.words(item.visibleText + " " + (item.caption ?? ""))
            let score = terms.intersection(words).count
            return score > 0 ? (index, score) : nil
        }.sorted { first, second in
            first.1 == second.1 ? first.0 > second.0 : first.1 > second.1
        }
        let recent = Array(eligible.suffix(3))
        var selected = Set(recent.map(\.id))
        let relevant = Array(ranked.lazy.map { eligible[$0.0] }
            .filter { !selected.contains($0.id) }.prefix(4))
        selected.formUnion(relevant.map(\.id))
        let older = eligible.dropLast(recent.count).filter { !selected.contains($0.id) }
        let captions = older.filter { $0.caption != nil }
        let pool = captions.count >= 2 ? captions : older
        let count = min(2 + 4 - relevant.count, pool.count)
        let spread = (0..<count).map { offset in pool[offset * (pool.count - 1) / max(1, count - 1)] }
        return (recent + relevant + spread).sorted { $0.time < $1.time }
    }

    static func words(_ text: String) -> Set<String> {
        Set(text.folding(options: [.diacriticInsensitive, .caseInsensitive], locale: nil)
            .split { !$0.isLetter && !$0.isNumber }
            .filter { $0.count >= 2 }.map(String.init))
    }
}
#endif
