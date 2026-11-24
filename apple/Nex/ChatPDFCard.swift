import PDFKit
import SwiftUI

struct ChatPDFCard: View {
    let artifact: NexArtifact
    let loadPDF: () async throws -> Data
    let open: () -> Void
    @State private var visible = false
    @State private var thumbnail: Image?

    var body: some View {
        Button(action: open) {
            HStack(spacing: 12) {
                preview
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 5) {
                    Text(artifact.title)
                        .font(.subheadline.weight(.semibold))
                        .lineLimit(2)
                    fileType
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 4)
                Image(systemName: "arrow.up.right")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(WorkspaceStyle.accent)
                    .accessibilityHidden(true)
            }
            .padding(10)
            .frame(maxWidth: 340, alignment: .leading)
            .background(.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 12))
            .overlay { RoundedRectangle(cornerRadius: 12).strokeBorder(.primary.opacity(0.12)) }
            .contentShape(RoundedRectangle(cornerRadius: 12))
        }
        .buttonStyle(.plain)
        .accessibilityHint("Opens the PDF viewer")
        // Chat lays out offscreen rows eagerly to keep its scroll position stable.
        .onGeometryChange(for: Bool.self) { geometry in
            guard let viewport = geometry.bounds(of: .scrollView) else { return true }
            return geometry.frame(in: .local).intersects(viewport)
        } action: { visible = $0 }
        .task(id: visible) { await loadThumbnail() }
    }

    private var preview: some View {
        ZStack {
            if let thumbnail {
                thumbnail.resizable().scaledToFit()
            } else {
                Image(systemName: "doc.richtext")
                    .font(.title2)
                    .foregroundStyle(WorkspaceStyle.accent)
            }
        }
        .frame(width: 60, height: 78)
        .background(.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 6))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }

    private var fileType: Text {
        switch artifact.pageCount {
        case 1: Text("PDF · 1 page")
        case let pages?: Text("PDF · \(pages) pages")
        case nil: Text("PDF")
        }
    }

    private func loadThumbnail() async {
        guard visible, thumbnail == nil, let data = try? await loadPDF(),
              !Task.isCancelled, let document = PDFDocument(data: data), let page = document.page(at: 0) else { return }
        let image = page.thumbnail(of: CGSize(width: 160, height: 208), for: .cropBox)
        #if os(macOS)
        thumbnail = Image(nsImage: image)
        #else
        thumbnail = Image(uiImage: image)
        #endif
    }
}
