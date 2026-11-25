import Foundation

enum ChatSpeech {
    // The archive speech endpoint measures its limit in Unicode scalars.
    static let requestLimit = 1800

    static func chunks(_ text: String) -> [String] {
        var remaining = text.unicodeScalars[...]
        var chunks: [String] = []
        while !remaining.isEmpty {
            let prefix = remaining.prefix(requestLimit)
            var end = prefix.endIndex
            if end != remaining.endIndex,
               let space = prefix.dropFirst(requestLimit / 2).lastIndex(where: { $0.properties.isWhitespace }) {
                end = remaining.index(after: space)
            }
            let chunk = String(remaining[..<end]).trimmingCharacters(in: .whitespacesAndNewlines)
            if !chunk.isEmpty { chunks.append(chunk) }
            remaining = remaining[end...]
        }
        return chunks
    }
}
