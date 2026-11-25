import SwiftUI

struct PrivateInbox: View {
    @ObservedObject var model: Controller
    @ObservedObject var privacy: AppPrivacy
    @State private var expanded = false
    @State private var source: MemoryItem?

    private var inboxItems: [PrivateAnswer] {
        privacy.items.filter { $0.askedAt == nil }
    }

    private var visibleItems: [PrivateAnswer] {
        if expanded { return inboxItems }
        guard let first = inboxItems.first else { return [] }
        if first.answer == nil, let ready = inboxItems.first(where: { $0.answer != nil }) { return [first, ready] }
        return [first]
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if model.serverProfile != nil && !privacy.connected {
                Text(privacy.error ?? "Unlock your connection to Excalibur to view private answers.")
                    .font(.callout)
                Button("Connect private workspace") { Task { await privacy.unlock(model) } }
                    .disabled(privacy.authenticating)
            } else if !inboxItems.isEmpty {
                HStack {
                    Label("Private answers", systemImage: "lock.fill").font(.headline)
                    Spacer()
                    if inboxItems.count > 1 {
                        Button(expanded ? "Latest only" : "Show all (\(inboxItems.count))") { expanded.toggle() }
                    }
                }
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(visibleItems) { item in
                            entry(item)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: expanded ? 280 : 150)
            }
            if privacy.connected, let error = privacy.error { Text(error).font(.caption).foregroundStyle(.secondary) }
        }
        .padding(inboxItems.isEmpty && privacy.error == nil && (model.serverProfile == nil || privacy.connected) ? 0 : 12)
        .sheet(item: $source) { item in SourceSheet(item: item, model: model) }
    }

    @ViewBuilder private func entry(_ item: PrivateAnswer) -> some View {
        Text(item.question).font(.subheadline.bold())
        if item.speaker == "recognized" { Label("Recognized voice", systemImage: "waveform").font(.caption) }
        if let answer = item.answer {
            MarkdownMessageBody(answer)
            ForEach(Array(item.sources.enumerated()), id: \.offset) { index, id in
                Button("Source \(index + 1)") {
                    Task {
                        let opening = privacy.generation
                        do {
                            let found = try await model.source(id)
                            if privacy.unlocked, opening == privacy.generation { source = found }
                        } catch { if privacy.unlocked { privacy.error = error.localizedDescription } }
                    }
                }.font(.caption)
            }
        }
        PrivateAnswerActions(item: item, model: model, privacy: privacy, allowsDismissal: false)
    }
}

struct PrivateAnswerActions: View {
    let item: PrivateAnswer
    let model: Controller
    @ObservedObject var privacy: AppPrivacy
    let allowsDismissal: Bool

    var body: some View {
        if item.state == "pending" {
            Button(privacy.resolvingRequest == item.id ? "Preparing your private answer…" : "Show private answer") {
                Task { await privacy.resolve(item, model: model) }
            }
            .buttonStyle(.borderedProminent)
            .disabled(!privacy.connected || privacy.resolvingRequest != nil)
        } else if item.state == "failed", item.clockAlert != nil {
            Text("Reply unavailable").font(.caption).foregroundStyle(.secondary)
        } else if item.state != "ready" {
            Text(item.state == "failed" ? "This request could not be completed. Ask again." :
                 item.state == "screen" ? "Waiting for your unlocked Mac" : "Preparing your private answer…")
        }
        if allowsDismissal, ["ready", "pending", "failed"].contains(item.state) {
            Button("Dismiss") { Task { await privacy.dismiss(item, model: model) } }
                .font(.caption).disabled(privacy.resolvingRequest == item.id)
        }
    }
}
