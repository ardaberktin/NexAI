import SwiftUI

struct NewsStoryRow: View {
    let story: NewsStory
    let saved: Bool
    let save: () async -> Void
    let discuss: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                WorkspaceIcon(symbol: symbol)
                VStack(alignment: .leading, spacing: 3) {
                    Text(story.topic).font(.caption.weight(.bold)).foregroundStyle(WorkspaceStyle.accent)
                    Text(story.source + "  ·  " + NexDate.display(story.published))
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Link(destination: story.url) {
                Text(story.title).font(.title3.weight(.semibold)).foregroundStyle(.primary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .buttonStyle(.plain)
            if !story.summary.isEmpty {
                Text(story.summary).font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let url = story.image {
                AsyncImage(url: url) { phase in
                    if let image = phase.image {
                        Color.clear
                            .frame(height: 176)
                            .overlay { image.resizable().scaledToFill() }
                            .clipShape(RoundedRectangle(cornerRadius: 15))
                            .accessibilityHidden(true)
                    }
                }
            }
            HStack(spacing: 17) {
                Button(saved ? "Remove bookmark" : "Save story", systemImage: saved ? "bookmark.fill" : "bookmark") {
                    Task { await save() }
                }
                .labelStyle(.iconOnly)
                Button("Discuss", systemImage: "bubble.left", action: discuss)
                Spacer()
                Link(destination: story.url) {
                    Label("Source", systemImage: "arrow.up.right").font(.caption.weight(.medium))
                }
            }
            .font(.subheadline.weight(.medium))
            .buttonStyle(.plain)
            .foregroundStyle(WorkspaceStyle.accent)
            .padding(.top, 4)
        }
        .padding(.vertical, 18)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var symbol: String {
        switch story.topic {
        case "Canada": "map"
        case "Technology": "cpu"
        case "Business": "chart.line.uptrend.xyaxis"
        case "Science": "atom"
        case "Climate": "leaf"
        case "Culture": "theatermasks"
        case "Sports": "figure.run"
        default: "globe.europe.africa"
        }
    }
}
