import Foundation

struct MessageMarkdown {
    struct Block: Identifiable {
        let id: Int
        let kind: PresentationIntent.Kind
        var text = AttributedString()
        var children: [Block] = []

        fileprivate static func append(
            _ text: AttributedString,
            at path: ArraySlice<PresentationIntent.IntentType>,
            to blocks: inout [Block]
        ) {
            guard let component = path.first else { return }
            if blocks.last?.id != component.identity {
                blocks.append(Block(id: component.identity, kind: component.kind))
            }
            let index = blocks.count - 1
            if path.count == 1 {
                blocks[index].text.append(text)
            } else {
                append(text, at: path.dropFirst(), to: &blocks[index].children)
            }
        }

        var code: String {
            let source = String(text.characters)
            // Foundation adds one line ending to a code block.
            return source.hasSuffix("\n") ? String(source.dropLast()) : source
        }

        fileprivate var previewLines: [String] {
            switch kind {
            case .thematicBreak:
                return []
            case .tableHeaderRow, .tableRow:
                return [children.flatMap(\.previewLines).joined(separator: " · ")]
            default:
                return text.characters.isEmpty ? children.flatMap(\.previewLines) : [String(text.characters)]
            }
        }
    }

    let blocks: [Block]

    init(_ source: String) {
        guard let parsed = try? AttributedString(markdown: source) else {
            blocks = [Block(id: 0, kind: .paragraph, text: AttributedString(source))]
            return
        }

        var blocks: [Block] = []
        for (intent, range) in parsed.runs[\.presentationIntent] {
            let text = Self.displayText(AttributedString(parsed[range]))
            let path = Array((intent?.components ?? []).reversed())
            if path.isEmpty {
                blocks.append(Block(id: -blocks.count - 1, kind: .paragraph, text: text))
            } else {
                Block.append(text, at: path[...], to: &blocks)
            }
        }
        self.blocks = blocks
    }

    var preview: String {
        blocks.flatMap(\.previewLines).joined(separator: "\n")
    }

    private static func displayText(_ text: AttributedString) -> AttributedString {
        var display = AttributedString()
        for run in text.runs {
            if run.inlinePresentationIntent?.contains(.softBreak) == true {
                display.append(AttributedString("\n"))
                continue
            }

            var span = AttributedString(text[run.range])
            span.presentationIntent = nil
            if let url = run.link, !["http", "https"].contains(url.scheme?.lowercased()) {
                span.link = nil
            }
            display.append(span)
        }
        return display
    }
}
