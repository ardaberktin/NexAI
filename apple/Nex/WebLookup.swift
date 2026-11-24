import SwiftUI

enum WebLookup {
    static func isRequest(_ question: String) -> Bool {
        let phrase = question.trimmingCharacters(in: .whitespacesAndNewlines)
        let polite = #"^(?:(?:hey\s+)?nex\s*[,.:!-]?\s*)?(?:(?:can|could|would) you\s+|please\s+)?"#
        let url = #"(?:https?://|www\.|(?:[a-z0-9-]+\.)+[a-z]{2,63}(?=[:/?#\s.,;!\)]|$))"#
        let search = #"(?:(?:search|look) (?:(?:the )?web|online|the internet)(?: for)?|web search(?: for)?|look up|search for|find online|web'de ara|internette ara)\b"#
        let open = #"(?:open|read|visit|check|go to|summari[sz]e)\s+(?:the (?:website|page)\s+)?"# + url
        return phrase.range(of: polite + "(?:" + search + "|" + open + "|" + url + ")",
                            options: [.regularExpression, .caseInsensitive]) != nil
    }

    static func request(for search: String) -> String {
        isRequest(search) ? search : "Search the web for " + search
    }
}

struct WebPageContent: View {
    let item: MemoryItem
    let loading: Bool
    let read: (Int?) -> Void

    var body: some View {
        if let title = item.webTitle {
            Text(title).font(.headline)
        }
        if let url = item.webUrl {
            Text(url)
                .font(.caption)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
        }
        Text(item.text).textSelection(.enabled)
        Button(loading ? "Reading…" : "Read current page", systemImage: "arrow.clockwise") { read(nil) }
            .disabled(loading)
        if let links = item.webLinks, !links.isEmpty {
            Divider()
            Text("Links on this page").font(.headline)
            ForEach(Array(links.enumerated()), id: \.offset) { index, link in
                Button(link.title, systemImage: "arrow.right") { read(index) }
                    .disabled(loading)
            }
        }
    }
}
