import SwiftUI
#if os(macOS)
import AppKit
#else
import UIKit
import UniformTypeIdentifiers
#endif

struct ChatMessageFooter: View {
    let message: WorkspaceMessage
    let playing: Bool
    let canRead: Bool
    let readAloud: () -> Void
    let copy: () -> Void
    let reply: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Text(NexDate.display(message.date))
                .font(.caption)
            #if os(macOS)
            if message.copyableText != nil {
                icon("Copy message", symbol: "document.on.document", action: copy)
            }
            if message.offersReply {
                icon("Reply", symbol: "arrowshape.turn.up.left", action: reply)
            }
            #endif
            if message.offersReadAloud {
                icon(playing ? "Stop reading aloud" : "Read aloud",
                     symbol: playing ? "stop.circle" : "speaker.wave.2", action: readAloud)
                    .disabled(!playing && !canRead)
            }
        }
        .foregroundStyle(.secondary)
    }

    private func icon(_ title: LocalizedStringKey, symbol: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: symbol)
                .labelStyle(.iconOnly)
                #if os(iOS)
                .frame(minWidth: 44, minHeight: 44)
                .contentShape(Rectangle())
                #endif
        }
        .buttonStyle(.plain)
        #if os(macOS)
        .help(Text(title))
        #endif
    }
}

@MainActor
func copyChatText(_ text: String, privately: Bool) {
    #if os(macOS)
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(text, forType: .string)
    #else
    UIPasteboard.general.setItems([[UTType.utf8PlainText.identifier: text]], options: [.localOnly: privately])
    #endif
}

#if os(iOS)
struct ChatTextSelection: Identifiable {
    let id: String
    let text: String
    let isPrivate: Bool
}

struct ChatSelectionSheet: View {
    let selection: ChatTextSelection

    var body: some View {
        WorkspaceSheet(title: "Select text") {
            SelectableChatText(selection: selection)
                .privacySensitive()
        }
    }
}

private struct SelectableChatText: UIViewRepresentable {
    let selection: ChatTextSelection

    func makeUIView(context: Context) -> SelectionTextView {
        let view = SelectionTextView()
        view.backgroundColor = .clear
        view.font = .preferredFont(forTextStyle: .body)
        view.adjustsFontForContentSizeCategory = true
        view.isEditable = false
        view.isSelectable = true
        view.textContainerInset = UIEdgeInsets(top: 16, left: 12, bottom: 16, right: 12)
        return view
    }

    func updateUIView(_ view: SelectionTextView, context: Context) {
        view.isPrivate = selection.isPrivate
        if view.text != selection.text { view.text = selection.text }
    }
}

private final class SelectionTextView: UITextView {
    var isPrivate = false

    override func copy(_ sender: Any?) {
        guard let selectedTextRange, let selected = text(in: selectedTextRange), !selected.isEmpty else { return }
        copyChatText(selected, privately: isPrivate)
    }
}
#endif
