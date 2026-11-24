import SwiftUI

struct NewsFeedStories: View {
    let model: Controller
    let feed: NewsFeed
    let showingSaved: Bool
    let toggleSaved: (NewsStory) async -> Void
    let discuss: (String) -> Void
    @State private var olderEditions: [Edition] = []
    @State private var requestedDay: String?
    @State private var olderFailure: String?

    var body: some View {
        LazyVStack(alignment: .leading, spacing: 22) {
            if showingSaved {
                if feed.saved.isEmpty {
                    WorkspaceEmpty(title: "No saved stories", symbol: "bookmark",
                                   detail: "Bookmark a story to keep it here.")
                } else {
                    stories(feed.saved)
                }
            } else {
                if feed.stories.isEmpty && !feed.pending {
                    WorkspaceEmpty(title: "No edition yet", symbol: "newspaper",
                                   detail: "Your next edition will appear here.")
                }
                stories(feed.stories)
                ForEach(olderEditions) { edition in
                    VStack(alignment: .leading, spacing: 12) {
                        Text(WorkspaceDate.edition(edition.day))
                            .font(.title3.weight(.semibold))
                            .accessibilityAddTraits(.isHeader)
                        stories(edition.stories)
                    }
                }
                if let olderDay { olderNewsButton(olderDay) }
            }
        }
        .task(id: requestedDay) { [requestedDay] in
            if let requestedDay { await loadOlder(requestedDay) }
        }
    }

    private var olderDay: String? {
        if let last = olderEditions.last { return last.olderDay }
        return feed.olderDay
    }

    private func stories(_ items: [NewsStory]) -> some View {
        let bookmarks = Set(feed.saved.map(\.id))
        return WorkspaceCollection(items: items) { story in
            NewsStoryRow(story: story, saved: bookmarks.contains(story.id)) {
                await toggleSaved(story)
            } discuss: {
                discuss("Read \(story.url.absoluteString) and help me understand this story.")
            }
        }
    }

    @ViewBuilder private func olderNewsButton(_ day: String) -> some View {
        if let olderFailure {
            WorkspaceIssue(message: olderFailure, retry: { requestOlder(day) })
        } else if requestedDay != nil {
            ProgressView("Loading older news…")
                .frame(maxWidth: .infinity)
        } else {
            Button { requestOlder(day) } label: {
                Label("Load older news", systemImage: "chevron.down")
                    .font(.subheadline.weight(.semibold))
                    .padding(.horizontal, 16)
                    .padding(.vertical, 10)
                    .background(.regularMaterial, in: Capsule())
            }
            .buttonStyle(.plain)
            .frame(maxWidth: .infinity)
        }
    }

    private func requestOlder(_ day: String) {
        guard requestedDay == nil else { return }
        olderFailure = nil
        requestedDay = day
    }

    private func loadOlder(_ day: String) async {
        do {
            let page = try await model.newsFeed(day: day)
            guard !Task.isCancelled else { return }
            olderEditions.append(Edition(day: day, stories: page.stories, olderDay: page.olderDay))
        } catch {
            guard !Task.isCancelled else { return }
            olderFailure = error.localizedDescription
        }
        requestedDay = nil
    }

    private struct Edition: Identifiable {
        let day: String
        let stories: [NewsStory]
        let olderDay: String?
        var id: String { day }
    }
}
