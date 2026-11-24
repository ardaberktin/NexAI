import SwiftUI

struct PersonalContextView: View {
    @ObservedObject var model: Controller
    @Environment(\.scenePhase) private var scenePhase
    @State private var selection: ContextFileName = .now
    @State private var context: PersonalContext?
    @State private var edited: ContextFile?
    @State private var error: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                #if os(iOS)
                Text("Memory").font(.largeTitle.weight(.semibold))
                #endif
                if model.pairing == nil {
                    ContentUnavailableView("Pair with Excalibur", systemImage: "externaldrive",
                                           description: Text("Pair in Settings to open your personal files."))
                } else {
                    #if os(iOS)
                    filePicker
                    #endif
                    if let file = context?[selection] {
                        document(file)
                    } else if error == nil {
                        ProgressView("Opening personal files…").frame(maxWidth: .infinity)
                    }
                    if let error {
                        Text(error).font(.footnote).foregroundStyle(.secondary)
                        Button("Try again") { Task { await load() } }
                    }
                }
            }
            .padding(24)
            #if os(macOS)
            .frame(maxWidth: 940)
            #else
            .frame(maxWidth: 690)
            #endif
            .frame(maxWidth: .infinity)
        }
        #if os(macOS)
        .toolbar {
            ToolbarItem(placement: .primaryAction) { filePicker.frame(width: 220) }
        }
        #endif
        .refreshable { await load() }
        .task(id: model.pairing?.url) {
            context = nil
            error = nil
            guard model.pairing != nil else { return }
            while !Task.isCancelled {
                if scenePhase == .active { await load() }
                do { try await Task.sleep(for: .seconds(15)) }
                catch { break }
            }
        }
        .sheet(item: $edited, onDismiss: { Task { await load() } }) { file in
            ContextEditor(model: model, file: file)
        }
    }

    private var filePicker: some View {
        Picker("Personal file", selection: $selection) {
            ForEach(ContextFileName.allCases) { Text($0.title).tag($0) }
        }
        .pickerStyle(.segmented)
        .labelsHidden()
    }

    private func document(_ file: ContextFile) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Label(file.name.filename, systemImage: file.name.symbol)
                    .font(.system(.headline, design: .monospaced))
                    .foregroundStyle(Color.accentColor)
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                Button("Edit", systemImage: "pencil") { edited = file }
                    .buttonStyle(.bordered)
            }
            Text(file.name.detail).font(.subheadline).foregroundStyle(.secondary)
            Divider()
            ContextMarkdown(file: file)
            if let date = try? Date.ISO8601FormatStyle(includingFractionalSeconds: true).parse(file.modified) {
                Text("Updated \(date, format: .relative(presentation: .named))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 18))
    }

    private func load() async {
        do {
            let latest = try await model.personalContext()
            guard !Task.isCancelled else { return }
            context = latest
            error = nil
        } catch {
            if !Task.isCancelled { self.error = error.localizedDescription }
        }
    }
}

private struct ContextMarkdown: View {
    let file: ContextFile

    private var lines: [String] {
        var contents = file.markdown.split(whereSeparator: \.isNewline).map(String.init)
        if file.name != .soul { contents.removeAll { $0.hasPrefix("# ") } }
        return contents
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            if !lines.contains(where: { !$0.hasPrefix("#") }) {
                Text(file.name.empty).foregroundStyle(.secondary)
            } else {
                ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                    row(line)
                }
            }
        }
        .textSelection(.enabled)
    }

    @ViewBuilder
    private func row(_ line: String) -> some View {
        if line.hasPrefix("#") {
            Text(line.drop(while: { $0 == "#" || $0 == " " }))
                .font(.headline)
                .padding(.top, 4)
        } else if line.hasPrefix("*"),
                  let date = try? Date.ISO8601FormatStyle(includingFractionalSeconds: true)
                    .parse(line.trimmingCharacters(in: CharacterSet(charactersIn: "*"))) {
            Text(date.formatted(date: .abbreviated, time: .shortened))
                .font(.caption)
                .foregroundStyle(.secondary)
        } else {
            let visible = line.hasPrefix("- ") ? "• " + line.dropFirst(2) : line
            Text((try? AttributedString(markdown: visible,
                                        options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
                 ?? AttributedString(visible))
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
