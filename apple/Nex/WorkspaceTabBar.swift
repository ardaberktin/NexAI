import SwiftUI

enum WorkspaceTab: Int, CaseIterable {
    case chat = 1, daybook, activities = 9, feed = 3, ideas, goals, files
    #if os(macOS)
    case memory, archive
    #endif

    var feature: WorkspaceFeature? {
        switch self {
        case .daybook: .daybook
        case .activities: .activities
        case .feed: .feed
        case .ideas: .ideas
        case .goals: .goals
        default: nil
        }
    }

    func isAvailable(in features: WorkspaceFeatures?) -> Bool {
        guard let feature else { return true }
        return features?[feature] ?? true
    }

    var title: LocalizedStringKey {
        switch self {
        case .chat: "Chat"
        case .daybook: "Daybook"
        case .activities: "Activities"
        case .feed: "Feed"
        case .ideas: "Ideas"
        case .goals: "Goals"
        case .files: "Files"
        #if os(macOS)
        case .memory: "Memory"
        case .archive: "Archive"
        #endif
        }
    }

    var symbol: String {
        switch self {
        case .chat: "bubble.left.and.bubble.right"
        case .daybook: "book.closed"
        case .activities: "waveform.path"
        case .feed: "newspaper"
        case .ideas: "lightbulb"
        case .goals: "checkmark.square"
        case .files: "folder"
        #if os(macOS)
        case .memory: "person.text.rectangle"
        case .archive: "clock.arrow.circlepath"
        #endif
        }
    }
}

#if os(iOS)
struct WorkspaceTabBar: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Namespace private var selectionAnimation

    let selection: WorkspaceTab
    let availableTabs: [WorkspaceTab]
    let select: (WorkspaceTab) -> Void

    var body: some View {
        surface
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
            .frame(maxWidth: .infinity)
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Workspace tabs")
    }

    @ViewBuilder private var surface: some View {
        if #available(iOS 26.0, *) {
            tabs.glassEffect(.regular, in: Capsule())
        } else {
            tabs
                .background(.regularMaterial, in: Capsule())
                .overlay { Capsule().strokeBorder(.primary.opacity(0.10)).allowsHitTesting(false) }
        }
    }

    private var tabs: some View {
        HStack(spacing: 0) {
            ForEach(availableTabs, id: \.self) { tab in
                tabButton(tab)
            }
        }
        .padding(5)
        .frame(maxWidth: 500)
        .animation(reduceMotion ? nil : .snappy(duration: 0.25), value: selection)
    }

    private func tabButton(_ tab: WorkspaceTab) -> some View {
        let isSelected = selection == tab

        return Button { select(tab) } label: {
            VStack(spacing: 4) {
                Image(systemName: tab.symbol)
                    .font(.system(size: 18, weight: isSelected ? .semibold : .medium))
                if !dynamicTypeSize.isAccessibilitySize {
                    Text(tab.title)
                        .font(.caption2.weight(.semibold))
                        .lineLimit(1)
                        .minimumScaleFactor(0.8)
                }
            }
            .foregroundStyle(isSelected ? WorkspaceStyle.accent : Color.secondary)
            .padding(.horizontal, 2)
            .frame(minWidth: 44, maxWidth: .infinity, minHeight: 48)
            .background {
                if isSelected {
                    Capsule().fill(WorkspaceStyle.accent.opacity(0.14))
                        .matchedGeometryEffect(id: "selection", in: selectionAnimation)
                        .transition(.identity)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(Text(tab.title))
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }
}
#endif
