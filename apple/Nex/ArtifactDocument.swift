import SwiftUI
import UniformTypeIdentifiers

struct ArtifactDocument: View {
    let model: Controller
    let artifact: NexArtifact

    var body: some View {
        if artifact.kind == "pdf" {
            PDFArtifactDocument(model: model, artifact: artifact)
        } else {
            TextArtifactDocument(artifact: artifact)
        }
    }
}

private struct TextArtifactDocument: View {
    let artifact: NexArtifact
    @State private var exporting = false
    @State private var failure: String?

    var body: some View {
        Group {
            #if os(macOS)
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    HStack(alignment: .top, spacing: 12) {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(artifact.title).font(.title.weight(.semibold)).accessibilityAddTraits(.isHeader)
                            Text(artifact.subtitle).font(.subheadline).foregroundStyle(.secondary)
                        }
                        Spacer(minLength: 4)
                        options
                    }
                    Divider()
                    documentText
                }
                .frame(maxWidth: WorkspaceStyle.readable, alignment: .leading)
                .padding(24)
                .frame(maxWidth: .infinity)
            }
            #else
            WorkspacePage(title: LocalizedStringKey(artifact.title), subtitle: artifact.subtitle) {
                options
            } content: {
                documentText
            }
            #endif
        }
        .fileExporter(
            isPresented: $exporting, document: ArtifactText(text: artifact.text),
            contentType: .plainText, defaultFilename: artifact.filename
        ) { result in
            if case .failure(let error) = result { failure = error.localizedDescription }
        }
    }

    private var options: some View {
        Menu {
            Button("Save text file", systemImage: "square.and.arrow.down") { exporting = true }
            ShareLink(item: artifact.text)
        } label: {
            Label("File options", systemImage: "ellipsis.circle")
                .labelStyle(.iconOnly)
                #if os(iOS)
            .font(.title3).frame(width: 36, height: 40)
                #endif
        }
        .menuIndicator(.hidden)
        .fixedSize()
    }

    @ViewBuilder private var documentText: some View {
        if let failure { WorkspaceIssue(message: failure) }
        Text(artifact.text).textSelection(.enabled)
    }
}

extension NexArtifact {
    var symbol: String {
        switch kind {
        case "feed": "newspaper"
        case "pdf": "doc.richtext"
        default: "doc.text"
        }
    }

    var filename: String {
        let allowed = title.map { $0.isLetter || $0.isNumber ? $0 : "-" }
        let words = allowed.split(separator: "-").map { String($0) }
        let name = words.joined(separator: "-").prefix(85).lowercased()
        return name.isEmpty ? "nex-file" : name
    }
}

private struct ArtifactText: FileDocument {
    static let readableContentTypes: [UTType] = [.plainText]
    var text: String

    init(text: String) { self.text = text }

    init(configuration: ReadConfiguration) throws {
        guard let data = configuration.file.regularFileContents,
            let text = String(data: data, encoding: .utf8)
        else { throw CocoaError(.fileReadCorruptFile) }
        self.text = text
    }

    func fileWrapper(configuration: WriteConfiguration) throws -> FileWrapper {
        FileWrapper(regularFileWithContents: Data(text.utf8))
    }
}
