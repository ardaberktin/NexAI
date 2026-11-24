import SwiftUI

struct NewsFeedView: View {
    @ObservedObject var model: Controller
    let discuss: (String) -> Void
    @State private var feed: NewsFeed?
    @State private var selectedDay: String?
    @State private var showingSaved = false
    @State private var editing = false
    @State private var savingStory = false
    @State private var rebuilding = false
    @State private var failure: String?
    @State private var feedRequest: UUID?

    var body: some View {
        WorkspacePage(title: "Feed", subtitle: caption) {
            editionsMenu
            Button("Feed settings", systemImage: "slider.horizontal.3") { editing = true }
                .labelStyle(.iconOnly)
                #if os(macOS)
                .help("Feed settings")
                #else
                .buttonStyle(.bordered)
                .buttonBorderShape(.circle)
                .controlSize(.large)
                #endif
                .disabled(feed == nil)
        } content: {
            if let failure { WorkspaceIssue(message: failure, retry: { await load() }) }
            if let feed {
                if showingSaved {
                    Button("Back to feed", systemImage: "chevron.left") { showingSaved = false }
                        .font(.subheadline.weight(.semibold))
                } else {
                    promptButton(feed.settings.prompt)
                    if let error = feed.error {
                        WorkspaceIssue(message: error, retry: rebuild)
                    } else if feed.pending || rebuilding {
                        ProgressView("Preparing today’s edition…")
                            .font(.subheadline).controlSize(.small)
                    }
                }
                NewsFeedStories(model: model, feed: feed, showingSaved: showingSaved,
                                toggleSaved: toggleSaved, discuss: discuss)
                    .id(feed.day)
            } else if failure == nil {
                ProgressView("Opening Feed…").frame(maxWidth: .infinity).padding(.vertical, 24)
            }
        }
        .refreshable { await load() }
        .task(id: selectedDay) {
            repeat {
                await load()
                do { try await Task.sleep(for: .seconds(feed?.pending == true ? 2 : 15)) }
                catch { return }
            } while !Task.isCancelled && selectedDay == nil
        }
        .sheet(isPresented: $editing) {
            if let feed {
                FeedSettingsView(model: model, settings: feed.settings, didSave: settingsSaved)
            }
        }
    }

    private var editionsMenu: some View {
        Menu {
            Button("Latest edition", systemImage: "clock") { choose(nil) }
            Button("Saved stories", systemImage: "bookmark") { showingSaved = true }
            if feed != nil {
                Button("Rebuild today’s edition", systemImage: "arrow.clockwise") {
                    showingSaved = false
                    Task { await rebuild() }
                }
                .disabled(feed?.pending == true || rebuilding)
            }
            if let days = feed?.editions, !days.isEmpty {
                Divider()
                ForEach(days, id: \.self) { day in
                    Button(WorkspaceDate.edition(day)) { choose(day) }
                }
            }
        } label: {
            Image(systemName: "calendar")
                #if os(iOS)
                .frame(width: 32, height: 36)
                #endif
        }
        #if os(macOS)
        .help("Feed editions")
        #endif
        .menuIndicator(.hidden)
        .fixedSize()
        .accessibilityLabel("Feed editions")
        .disabled(savingStory || rebuilding)
    }

    private var caption: String {
        if showingSaved { return "Saved stories" }
        if let day = feed?.day { return WorkspaceDate.edition(day) }
        return "Your daily news edition"
    }

    private func promptButton(_ prompt: String) -> some View {
        Button { editing = true } label: {
            HStack(spacing: 10) {
                Image(systemName: "text.alignleft")
                    .foregroundStyle(WorkspaceStyle.accent)
                Text(prompt)
                    .lineLimit(2)
                    .foregroundStyle(.primary)
                Spacer(minLength: 0)
                Image(systemName: "chevron.right").font(.caption.weight(.semibold))
            }
            .font(.subheadline)
            .padding(13)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 15))
            .overlay { RoundedRectangle(cornerRadius: 15).strokeBorder(.primary.opacity(0.08)) }
        }
        .buttonStyle(.plain)
        .accessibilityHint("Opens feed settings")
    }

    private func choose(_ day: String?) {
        if day != selectedDay { feedRequest = nil }
        selectedDay = day
        showingSaved = false
    }

    private func settingsSaved(_ updated: NewsFeed, refreshNow: Bool) {
        feedRequest = nil
        if refreshNow { choose(nil) }
        if selectedDay == nil {
            feed = updated
            failure = nil
        } else {
            feed?.settings = updated.settings
        }
    }

    private func load() async {
        guard !savingStory, !rebuilding else { return }
        let day = selectedDay
        let request = UUID()
        feedRequest = request
        do {
            let result = try await model.newsFeed(day: day)
            guard !Task.isCancelled, feedRequest == request else { return }
            feed = result
            failure = nil
        } catch {
            if !Task.isCancelled && feedRequest == request { failure = error.localizedDescription }
        }
    }

    private func rebuild() async {
        guard !rebuilding, !savingStory else { return }
        feedRequest = nil
        rebuilding = true
        defer { rebuilding = false }
        do {
            feed = try await model.refreshFeed()
            selectedDay = nil
            failure = nil
        } catch { failure = error.localizedDescription }
    }

    private func toggleSaved(_ story: NewsStory) async {
        guard !savingStory, !rebuilding else { return }
        feedRequest = nil
        savingStory = true
        defer { savingStory = false }
        let wasSaved = feed?.saved.contains { $0.id == story.id } == true
        do {
            let result = try await model.saveStory(story.id, saved: !wasSaved)
            feed?.saved = result.saved
            failure = nil
        } catch { failure = error.localizedDescription }
    }
}
