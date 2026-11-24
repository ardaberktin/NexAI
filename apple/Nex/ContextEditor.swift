import SwiftUI

struct ContextEditor: View {
    let model: Controller
    @Environment(\.dismiss) private var dismiss
    @State private var base: ContextFile
    @State private var draft: String
    @State private var saving = false
    @State private var error: String?
    @State private var latest: ContextFile?

    init(model: Controller, file: ContextFile) {
        self.model = model
        _base = State(initialValue: file)
        _draft = State(initialValue: file.markdown)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text(base.name.filename).font(.system(.title2, design: .monospaced).weight(.semibold))
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Save") { Task { await save() } }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(saving || draft == base.markdown || draft.utf8.count > 16_000)
            }
            TextEditor(text: $draft)
                .font(.system(.body, design: .monospaced))
                .padding(6)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
                .accessibilityLabel("Markdown")
                .disabled(saving)
            if draft.utf8.count > 16_000 {
                Text("Keep the file under 16 KB.").foregroundStyle(.secondary)
            }
            if let error {
                Text(error).foregroundStyle(.secondary)
            }
            if let latest {
                HStack {
                    Button("Use latest version") {
                        base = latest
                        draft = latest.markdown
                        self.latest = nil
                        error = nil
                    }
                    Button("Keep my draft") {
                        base = latest
                        self.latest = nil
                        error = nil
                    }
                }
                .buttonStyle(.bordered)
            }
        }
        .padding(20)
        #if os(macOS)
        .frame(minWidth: 540, minHeight: 410)
        #endif
    }

    private func save() async {
        saving = true
        defer { saving = false }
        do {
            _ = try await model.saveContext(base, markdown: draft)
            dismiss()
        } catch {
            self.error = error.localizedDescription
            if case NexError.api(409, _) = error {
                latest = try? await model.personalContext()[base.name]
            }
        }
    }
}
