#if os(macOS)
import SwiftUI

struct MacArtifactBrowser: View {
    let model: Controller
    let items: [NexArtifact]
    let open: (NexArtifact) -> Void
    @State private var selection: NexArtifact.ID?

    var body: some View {
        GeometryReader { window in
            if window.size.width >= 780 {
                HSplitView {
                    List(items, selection: $selection) { item in
                        row(item)
                            .tag(item.id)
                    }
                    .listStyle(.inset)
                    .frame(minWidth: 230, idealWidth: 280, maxWidth: 360)
                    ZStack {
                        if let selected = items.first(where: { $0.id == selection }) {
                            ArtifactDocument(model: model, artifact: selected).id(selected.id)
                        } else {
                            ContentUnavailableView("Select a file", systemImage: "doc.text")
                        }
                    }
                    .frame(minWidth: 300, maxWidth: .infinity, maxHeight: .infinity)
                }
                .onChange(of: items.map(\.id), initial: true) { _, ids in
                    if let selection, ids.contains(selection) { return }
                    selection = ids.first
                }
            } else {
                List(items) { item in
                    Button {
                        open(item)
                    } label: {
                        row(item)
                    }
                    .buttonStyle(.plain)
                }
                .listStyle(.inset)
            }
        }
    }

    private func row(_ item: NexArtifact) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: item.symbol)
                .foregroundStyle(WorkspaceStyle.accent)
                .frame(width: 20)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(item.title).font(.headline).lineLimit(2)
                Text(item.subtitle).font(.subheadline).foregroundStyle(.secondary)
                Text(NexDate.display(item.date)).font(.caption).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 6)
        .contentShape(Rectangle())
    }
}
#endif
