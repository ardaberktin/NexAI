import SwiftUI

struct MarkdownMessageBody: View {
    let text: String

    private let selectable: Bool

    init(_ text: String, selectable: Bool = true) {
        self.selectable = selectable
        self.text = text
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(MessageMarkdown(text).blocks) { block in
                MarkdownBlockView(block: block)
            }
        }
        #if !os(watchOS)
        .modifier(MessageTextSelection(enabled: selectable))
        #endif
    }
}

private struct MarkdownBlockView: View {
    let block: MessageMarkdown.Block

    var body: some View {
        switch block.kind {
        case .header(let level):
            text.font(headingFont(level).weight(.semibold))
                .accessibilityAddTraits(.isHeader)
        case .orderedList:
            list(ordered: true)
        case .unorderedList:
            list(ordered: false)
        case .blockQuote:
            children
                .foregroundStyle(.secondary)
                .padding(.leading, 10)
                .overlay(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 1.5)
                        .fill(.secondary.opacity(0.5))
                        .frame(width: 3)
                        .accessibilityHidden(true)
                }
        case .codeBlock:
            MessageCodeBlock(source: block.code)
        case .thematicBreak:
            Divider()
        case .table(let columns):
            MarkdownTable(rows: block.children, columns: columns)
        default:
            if block.children.isEmpty { text } else { children }
        }
    }

    private var text: some View {
        Text(block.text).fixedSize(horizontal: false, vertical: true)
    }

    private var children: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(block.children) { child in
                MarkdownBlockView(block: child)
            }
        }
    }

    private func list(ordered: Bool) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(block.children) { item in
                if case .listItem(let ordinal) = item.kind {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(verbatim: ordered ? "\(ordinal)." : "•")
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                        MarkdownBlockView(block: item)
                    }
                }
            }
        }
    }

    private func headingFont(_ level: Int) -> Font {
        switch level {
        case 1: .title2
        case 2: .title3
        case 3: .headline
        default: .body
        }
    }
}
