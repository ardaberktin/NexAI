import SwiftUI

struct WatchFeedView: View {
    @State private var day: String?
    private var query: WatchRequest { WatchRequest(.feed, cursor: day, timeZone: NexDate.timeZone) }

    var body: some View {
        WatchPage(title: "Feed", key: day ?? "", request: query) { (feed: NewsFeed) in
            if let current = feed.day {
                Text(WorkspaceDate.edition(current)).font(.caption).foregroundStyle(.secondary)
            }
            if feed.pending { Text("Preparing the latest edition…").font(.caption) }
            if let issue = feed.error { WatchFailure(message: issue) }
            if !feed.saved.isEmpty {
                NavigationLink("Saved stories") {
                    WatchSavedStories(stories: feed.saved)
                }
            }
            if feed.stories.isEmpty { Text("No stories in this edition.").foregroundStyle(.secondary) }
            ForEach(feed.stories) { story in
                WatchStoryLink(story: story, saved: feed.saved.contains { $0.id == story.id })
            }
            if let earlier = feed.olderDay {
                Button("Older edition", systemImage: "chevron.left") { day = earlier }
            }
            if day != nil {
                Button("Latest edition", systemImage: "chevron.right") { day = nil }
            }
        }
    }
}

private struct WatchSavedStories: View {
    let stories: [NewsStory]
    var body: some View {
        List {
            ForEach(stories) { WatchStoryLink(story: $0, saved: true) }
        }
        .navigationTitle("Saved stories")
        .modifier(WatchPrivateScreen())
    }
}

private struct WatchStoryLink: View {
    let story: NewsStory
    let saved: Bool

    var body: some View {
        NavigationLink {
            WatchStoryView(story: story, saved: saved)
        } label: {
            WatchTimelineRow(title: story.title, subtitle: story.source)
        }
    }
}

private struct WatchStoryView: View {
    @EnvironmentObject private var workspace: WatchWorkspace
    let story: NewsStory
    @State var saved: Bool
    @State private var saving = false
    @State private var issue: String?

    var body: some View {
        List {
            Text(story.title).font(.headline)
            Text(story.source).font(.caption).foregroundStyle(.secondary)
            Text(story.summary)
            if story.url.scheme == "https" || story.url.scheme == "http" {
                Link(destination: story.url) { Label("Open article", systemImage: "safari") }
            }
            NavigationLink("Discuss with Nex", destination: WatchChatView(prompt: "Tell me more about " + story.title))
            Button(saved ? "Remove bookmark" : "Save story", systemImage: saved ? "bookmark.slash" : "bookmark") {
                Task { await updateBookmark() }
            }
            .disabled(saving)
            if let issue { WatchFailure(message: issue) }
        }
        .navigationTitle("Story")
        .modifier(WatchPrivateScreen())
    }

    private func updateBookmark() async {
        saving = true
        issue = nil
        defer { saving = false }
        do {
            let feed: NewsFeed = try await workspace.get(WatchRequest(.savedStory, id: story.id,
                                                       value: saved ? "false" : "true"))
            saved = feed.saved.contains { $0.id == story.id }
        } catch is CancellationError { }
        catch { issue = error.localizedDescription }
    }
}

struct WatchIdeasView: View {
    var body: some View {
        WatchPage(title: "Ideas", request: WatchRequest(.ideas), refreshInterval: .seconds(5)) { (result: IdeasSnapshot) in
            if result.pending == true {
                ProgressView("Finding ideas…")
            } else if result.failed == true {
                WatchFailure(message: String(localized: "Excalibur could not generate ideas."))
            } else if result.items.isEmpty {
                Text("No ideas available.").foregroundStyle(.secondary)
            }
            ForEach(result.items) { idea in
                NavigationLink {
                    if idea.opensGoal == true { WatchGoalEditor(draft: GoalDraft(prompt: idea.prompt)) }
                    else { WatchChatView(prompt: idea.prompt) }
                } label: {
                    WatchTimelineRow(title: idea.title, subtitle: idea.detail)
                }
            }
        }
    }
}
