import SwiftUI

struct ChatComposer: View {
    @ObservedObject var model: Controller
    @ObservedObject var chat: WorkspaceChat
    let send: (Bool) -> Void
    @FocusState private var focused: Bool

    private var canSend: Bool {
        !chat.sending && !chat.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        #if os(macOS)
        HStack(alignment: .bottom, spacing: 10) {
            options
                .menuStyle(.borderlessButton)
                .frame(width: 28, height: 28)
                .help("Message options")
            editor.padding(.vertical, 6)
            trailingButton
                .frame(minWidth: 28, minHeight: 28)
        }
        .padding(8)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
        .overlay {
            RoundedRectangle(cornerRadius: 12)
                .strokeBorder(focused ? WorkspaceStyle.accent.opacity(0.65) : Color(nsColor: .separatorColor))
                .allowsHitTesting(false)
        }
        .frame(maxWidth: WorkspaceStyle.conversationWidth)
        .padding(.horizontal, WorkspaceStyle.inset)
        .padding(.top, 6)
        .padding(.bottom, 18)
        #else
        HStack(alignment: .bottom, spacing: 6) {
            options
            editor.padding(.vertical, 10)
            trailingButton
        }
        .padding(6)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 22))
        .overlay { RoundedRectangle(cornerRadius: 22).strokeBorder(.primary.opacity(0.11)) }
        .padding(.horizontal, WorkspaceStyle.inset)
        .padding(.top, 5)
        #endif
    }

    private var options: some View {
        Menu {
            Button("Ask privately", systemImage: "lock") {
                Task { await chat.send(model, privately: true) }
            }.disabled(!canSend)
            Button("Search the web", systemImage: "globe") { submit(web: true) }
                .disabled(!canSend || chat.replyingPrivately)
            if model.playing { Button("Stop playback", systemImage: "stop.fill", action: model.stopPlayback) }
        } label: {
            #if os(macOS)
            Label("Message options", systemImage: "plus.circle").labelStyle(.iconOnly)
            #else
            Image(systemName: "plus").frame(width: 38, height: 40)
            #endif
        }
        .menuIndicator(.hidden)
        .fixedSize()
        .accessibilityLabel("Message options")
    }

    private var editor: some View {
        TextField("Message Nex", text: $chat.draft, axis: .vertical)
            .textFieldStyle(.plain)
            .lineLimit(1...4)
            .focused($focused)
            .onSubmit { submit() }
            .onChange(of: chat.replyInsertion) { focused = true }
    }

    @ViewBuilder private var trailingButton: some View {
        if canSend {
            Button(chat.replyingPrivately ? "Send privately" : "Send message",
                   systemImage: chat.replyingPrivately ? "lock.fill" : "arrow.up") { submit() }
                .labelStyle(.iconOnly)
                .buttonStyle(.borderedProminent)
                .buttonBorderShape(.circle)
                #if os(iOS)
            .controlSize(.large)
                #else
            .help(chat.replyingPrivately ? "Send privately (⌘Return)" : "Send message (⌘Return)")
                #endif
                .keyboardShortcut(.return, modifiers: .command)
        } else if !chat.sending {
            WorkspaceVoiceButton(model: model, capture: model.capture) { focused = false }
        }
    }

    private func submit(web: Bool = false) {
        guard canSend else { return }
        #if os(iOS)
        focused = false
        #endif
        send(web)
    }
}

private struct WorkspaceVoiceButton: View {
    @ObservedObject var model: Controller
    @ObservedObject var capture: Capture
    let willStart: () -> Void

    private var title: LocalizedStringKey {
        if model.liveState != nil { return "End voice conversation" }
        return capture.voice ? "Send voice question" : "Ask by voice"
    }

    var body: some View {
        Button(title, systemImage: model.liveState != nil || capture.voice ? "stop.circle" : "mic") {
            willStart()
            Task { await model.toggleVoice() }
        }
        .labelStyle(.iconOnly)
        #if os(macOS)
        .buttonStyle(.borderless)
        .help(Text(title))
        #else
        .buttonStyle(.plain)
        .frame(width: 44, height: 44)
        .contentShape(Rectangle())
        #endif
        .disabled(model.playing)
    }
}
