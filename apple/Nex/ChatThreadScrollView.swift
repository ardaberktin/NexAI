import SwiftUI

struct ChatThreadScrollView<Content: View, PagingStatus: View>: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Namespace private var bottom
    @State private var viewport = Viewport()
    @State private var reachedBottom = false

    let firstMessageID: String?
    let latestMessageID: String?
    let isSending: Bool
    let nextBefore: String?
    let loadOlder: @MainActor () async -> Void
    let refresh: @MainActor @Sendable () async -> Void
    @ViewBuilder let content: () -> Content
    @ViewBuilder let pagingStatus: () -> PagingStatus

    var body: some View {
        ScrollViewReader { proxy in thread(using: proxy) }
    }

    private func restoreMessage(_ id: String, using proxy: ScrollViewProxy) {
        Task { @MainActor in
            // Wait for the newly prepended rows to enter the scroll view's layout.
            await Task.yield()
            proxy.scrollTo(id, anchor: .top)
        }
    }

    private func thread(using proxy: ScrollViewProxy) -> some View {
        ScrollView {
            // Measure the loaded rows before anchoring. Estimated heights can leave the
            // initial viewport blank when the conversation contains very tall messages.
            VStack(spacing: 16) {
                content()
                Color.clear.frame(height: 4).id(bottom).accessibilityHidden(true)
            }
            .frame(maxWidth: WorkspaceStyle.conversationWidth)
            .padding(.horizontal, WorkspaceStyle.inset)
            .padding(.top, 20)
            .frame(maxWidth: .infinity)
            .onGeometryChange(for: Viewport.self) { [firstMessageID, latestMessageID, isSending, nextBefore] geometry in
                let bounds = geometry.bounds(of: .scrollView)
                return Viewport(firstMessageID: firstMessageID, latestMessageID: latestMessageID, isSending: isSending,
                                nextBefore: nextBefore, nearBottom: bounds.map { geometry.size.height - $0.maxY <= 44 },
                                nearTop: (bounds?.minY ?? .infinity) <= 120)
            } action: { updateViewport($0, using: proxy) }
        }
        .defaultScrollAnchor(.bottom)
        .scrollDismissesKeyboard(.interactively)
        .refreshable { await refresh() }
        .onChange(of: viewport.nearTop ? viewport.nextBefore : nil, initial: true) { _, before in
            if before != nil { Task { await loadOlder() } }
        }
        .overlay(alignment: .top) { pagingStatus() }
        .overlay(alignment: .bottom) {
            if latestMessageID != nil && reachedBottom && viewport.nearBottom == false {
                Button {
                    jumpToBottom(using: proxy)
                } label: {
                    Label("Jump to latest message", systemImage: "arrow.down")
                        .labelStyle(.iconOnly)
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundStyle(.primary)
                        .frame(width: 44, height: 44)
                        .background(.regularMaterial, in: Circle())
                        .overlay { Circle().strokeBorder(.primary.opacity(0.16)) }
                        .contentShape(Circle())
                }
                .buttonStyle(.plain)
                #if os(macOS)
                .help("Jump to latest message")
                #endif
                .padding(.bottom, 10)
            }
        }
    }

    private func updateViewport(_ current: Viewport, using proxy: ScrollViewProxy) {
        // Compare with the viewport measured before the new message changed the content height.
        let previous = viewport
        let wasFollowingLatest = !reachedBottom || previous.nearBottom != false
        viewport = current
        // The initial bounds can arrive before the scroll view settles at its bottom anchor.
        reachedBottom = current.latestMessageID != nil && (reachedBottom || current.nearBottom == true)
        let startedSending = current.isSending && !previous.isSending
        let latestChanged = current.latestMessageID != nil && current.latestMessageID != previous.latestMessageID
        if startedSending || (latestChanged && wasFollowingLatest) {
            jumpToBottom(using: proxy, animated: previous.latestMessageID != nil)
        } else if let first = previous.firstMessageID, first != current.firstMessageID,
                  previous.nextBefore != current.nextBefore, previous.nearTop {
            viewport.nearTop = false
            restoreMessage(first, using: proxy)
        }
    }

    private func jumpToBottom(using proxy: ScrollViewProxy, animated: Bool = true) {
        withAnimation(animated && !reduceMotion ? .easeOut(duration: 0.2) : nil) {
            proxy.scrollTo(bottom, anchor: .bottom)
        }
    }

    private struct Viewport: Equatable, Sendable {
        var firstMessageID: String?
        var latestMessageID: String?
        var isSending = false
        var nextBefore: String?
        var nearBottom: Bool?
        var nearTop = false
    }
}
