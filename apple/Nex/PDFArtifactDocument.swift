import CoreTransferable
import PDFKit
import SwiftUI
import UniformTypeIdentifiers

struct PDFArtifactDocument: View {
    @ObservedObject var model: Controller
    let artifact: NexArtifact
    @State private var file: PDFFile?
    @State private var document: PDFDocument?
    @State private var failure: String?
    @State private var exporting = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 5) {
                    Text(artifact.title).font(.title2.weight(.semibold)).accessibilityAddTraits(.isHeader)
                    Text(artifact.subtitle).font(.subheadline).foregroundStyle(.secondary)
                }
                Spacer(minLength: 4)
                if let file { options(file) }
            }
            .padding(WorkspaceStyle.inset)
            Divider()
            if let failure {
                WorkspaceIssue(message: failure, retry: load).padding(WorkspaceStyle.inset)
            }
            if let document {
                PDFPreview(document: document)
            } else if failure == nil {
                ProgressView("Opening PDF…").frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .task { await load() }
        .fileExporter(isPresented: $exporting, document: file,
                      contentType: .pdf, defaultFilename: artifact.filename) { result in
            if case .failure(let error) = result { failure = error.localizedDescription }
        }
    }

    private func options(_ file: PDFFile) -> some View {
        Menu {
            Button("Save PDF", systemImage: "square.and.arrow.down") { exporting = true }
            ShareLink(item: file, preview: SharePreview(artifact.title, image: Image(systemName: artifact.symbol)))
        } label: {
            Label("File options", systemImage: "ellipsis.circle").labelStyle(.iconOnly)
        }
        .menuIndicator(.hidden)
        .fixedSize()
    }

    private func load() async {
        failure = nil
        do {
            let data = try await model.artifactPDF(artifact.id)
            guard !Task.isCancelled else { return }
            guard let preview = PDFDocument(data: data), preview.pageCount > 0 else {
                throw CocoaError(.fileReadCorruptFile)
            }
            file = PDFFile(data: data, name: artifact.filename)
            document = preview
        } catch {
            if !Task.isCancelled { failure = error.localizedDescription }
        }
    }
}

private struct PDFFile: FileDocument, Transferable {
    static let readableContentTypes: [UTType] = [.pdf]
    static var transferRepresentation: some TransferRepresentation {
        DataRepresentation(exportedContentType: .pdf) { $0.data }
            .suggestedFileName { $0.name + ".pdf" }
    }

    let data: Data
    let name: String

    init(data: Data, name: String) {
        self.data = data
        self.name = name
    }

    init(configuration: ReadConfiguration) throws {
        guard let data = configuration.file.regularFileContents else { throw CocoaError(.fileReadCorruptFile) }
        self.init(data: data, name: "nex-file")
    }

    func fileWrapper(configuration: WriteConfiguration) throws -> FileWrapper {
        FileWrapper(regularFileWithContents: data)
    }
}

private struct PDFPreview {
    let document: PDFDocument

    func makeView() -> PDFView {
        let view = PDFView()
        view.displayMode = .singlePageContinuous
        view.autoScales = true
        view.document = document
        return view
    }

    func updateView(_ view: PDFView) {
        if view.document !== document { view.document = document }
    }
}

#if os(macOS)
extension PDFPreview: NSViewRepresentable {
    func makeNSView(context: Context) -> PDFView { makeView() }
    func updateNSView(_ view: PDFView, context: Context) { updateView(view) }
}
#else
extension PDFPreview: UIViewRepresentable {
    func makeUIView(context: Context) -> PDFView { makeView() }
    func updateUIView(_ view: PDFView, context: Context) { updateView(view) }
}
#endif
