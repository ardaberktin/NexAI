import SwiftUI

enum MessageBlock: Equatable {
    case prose(String)
    case code(String)

    static func parse(_ text: String) -> [Self] {
        guard text.contains("```") || text.contains("~~~") else { return [.prose(text)] }
        var blocks: [Self] = []
        var lines: [Substring] = []
        var open: Fence?

        func finishBlock() {
            let value = lines.joined(separator: "\n")
            if open != nil {
                blocks.append(.code(value))
            } else {
                let prose = value.trimmingCharacters(in: .newlines)
                if !prose.isEmpty { blocks.append(.prose(prose)) }
            }
            lines.removeAll(keepingCapacity: true)
        }

        for line in text.split(omittingEmptySubsequences: false, whereSeparator: \.isNewline) {
            let marker = Fence(line)
            if let fence = open {
                if let marker, marker.bare, marker.run.hasPrefix(fence.run) {
                    finishBlock()
                    open = nil
                } else {
                    let indent = line.prefix(fence.indent).prefix(while: { $0 == " " })
                    lines.append(line.dropFirst(indent.count))
                }
            } else if let marker {
                finishBlock()
                open = marker
            } else {
                lines.append(line)
            }
        }
        guard open != nil || !blocks.isEmpty else { return [.prose(text)] }
        finishBlock()
        return blocks
    }

    private struct Fence {
        let run: Substring
        let indent: Int
        let bare: Bool

        init?(_ line: Substring) {
            indent = line.prefix(while: { $0 == " " }).count
            guard indent <= 3 else { return nil }
            let rest = line.dropFirst(indent)
            guard let mark = rest.first, mark == "`" || mark == "~" else { return nil }
            run = rest.prefix(while: { $0 == mark })
            let info = rest.dropFirst(run.count)
            guard run.count >= 3, mark != "`" || !info.contains("`") else { return nil }
            bare = info.allSatisfy { $0 == " " || $0 == "\t" }
        }
    }
}

struct MessageBody: View {
    private let blocks: [MessageBlock]

    init(_ text: String) {
        blocks = MessageBlock.parse(text)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(blocks.indices, id: \.self) { index in
                switch blocks[index] {
                case .prose(let text):
                    Text(verbatim: text).fixedSize(horizontal: false, vertical: true)
                case .code(let source):
                    MessageCodeBlock(source: source)
                }
            }
        }
        #if !os(watchOS)
        .textSelection(.enabled)
        #endif
    }
}

struct MessageCodeBlock: View {
    let source: String

    var body: some View {
        ScrollView(.horizontal) {
            Text(verbatim: source)
                .font(.system(.callout, design: .monospaced))
                .fixedSize()
                .padding(12)
        }
        .fixedSize(horizontal: false, vertical: true)
        .background(.primary.opacity(0.065), in: RoundedRectangle(cornerRadius: 8))
    }
}
