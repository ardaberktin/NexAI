import SwiftUI

enum WorkspaceStyle {
    static let readable: CGFloat = 680
    #if os(macOS)
    static let pageWidth: CGFloat = 1440
    static let conversationWidth: CGFloat = 1120
    static let inset: CGFloat = 28
    #else
    static let pageWidth = readable
    static let conversationWidth = readable
    static let inset: CGFloat = 20
    #endif
    static let accent = Color.accentColor

    static func background(_ scheme: ColorScheme) -> Color {
        scheme == .dark ? Color(red: 0.055, green: 0.078, blue: 0.08) :
            Color(red: 0.964, green: 0.971, blue: 0.958)
    }
}

struct NexMark: View {
    let size: CGFloat

    var body: some View {
        Image(systemName: "waveform")
            .font(.system(size: size * 0.5, weight: .bold))
            .foregroundStyle(Color(red: 0.60, green: 0.92, blue: 0.81))
            .frame(width: size, height: size)
            .background(Color(red: 0.06, green: 0.15, blue: 0.15), in: RoundedRectangle(cornerRadius: size * 0.28))
            .accessibilityHidden(true)
    }
}

struct WorkspacePage<Actions: View, Content: View>: View {
    let title: LocalizedStringKey
    let subtitle: String
    var fullWidthTitle = false
    @ViewBuilder var actions: () -> Actions
    @ViewBuilder var content: () -> Content

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                #if os(macOS)
                if fullWidthTitle { heading }
                #else
                if fullWidthTitle {
                    VStack(alignment: .leading, spacing: 14) {
                        HStack(spacing: 10) { actions() }
                        heading
                    }
                } else {
                    HStack(alignment: .center, spacing: 10) {
                        heading
                        Spacer(minLength: 0)
                        actions()
                    }
                }
                #endif
                content()
            }
            .frame(maxWidth: WorkspaceStyle.pageWidth, alignment: .leading)
            .padding(.horizontal, WorkspaceStyle.inset)
            .padding(.top, 20)
            .padding(.bottom, 22)
            .frame(maxWidth: .infinity)
        }
        #if os(macOS)
        .navigationSubtitle(fullWidthTitle ? "" : subtitle)
        .toolbar { ToolbarItemGroup(placement: .primaryAction) { actions() } }
        #else
        .scrollIndicators(.hidden)
        #endif
    }

    private var heading: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.system(.largeTitle, design: .rounded, weight: .bold))
                .accessibilityAddTraits(.isHeader)
            if !subtitle.isEmpty {
                Text(subtitle).font(.subheadline).foregroundStyle(.secondary)
            }
        }
    }
}

struct WorkspaceIcon: View {
    let symbol: String
    var tint: Color = WorkspaceStyle.accent
    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: 19, weight: .semibold))
            .foregroundStyle(tint)
            .frame(width: 42, height: 42)
            .background(tint.opacity(0.12), in: RoundedRectangle(cornerRadius: 14))
            .accessibilityHidden(true)
    }
}

struct WorkspaceEmpty: View {
    let title: LocalizedStringKey
    let symbol: String
    let detail: LocalizedStringKey

    var body: some View {
        VStack(spacing: 12) {
            WorkspaceIcon(symbol: symbol)
            Text(title).font(.headline)
            Text(detail).font(.subheadline).foregroundStyle(.secondary)
        }
        .multilineTextAlignment(.center)
        .frame(maxWidth: .infinity)
        .padding(.vertical, 32)
        .padding(.horizontal, 24)
    }
}

struct WorkspaceIssue: View {
    let message: String
    var retry: (() async -> Void)?

    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: "exclamationmark.circle").foregroundStyle(.orange)
            Text(message).frame(maxWidth: .infinity, alignment: .leading)
            if let retry {
                Button("Retry") { Task { await retry() } }
            }
        }
        .font(.subheadline)
        .padding(12)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 13))
    }
}

struct WorkspaceSheet<Content: View>: View {
    @Environment(\.dismiss) private var dismiss
    let title: LocalizedStringKey
    @ViewBuilder var content: () -> Content

    var body: some View {
        NavigationStack {
            content()
                .navigationTitle(title)
                #if os(iOS)
                .navigationBarTitleDisplayMode(.inline)
                #endif
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Done") { dismiss() }
                    }
                }
        }
        #if os(macOS)
        .frame(minWidth: 460, minHeight: 420)
        #endif
    }
}
