import SwiftUI

struct TranscriptEditor: View {
    @Environment(\.dismiss) private var dismiss
    let clipID: String
    let model: Controller
    let onSave: () -> Void

    @State private var snapshot: TranscriptSnapshot?
    @State private var draft = ""
    @State private var request: TranscriptCorrection?
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    Text("Edit transcript").font(.title2.weight(.semibold))
                    Spacer()
                    Button("Done") { dismiss() }
                }
                if let snapshot {
                    editor(snapshot)
                    ForEach(snapshot.history) { revision in
                        DisclosureGroup(NexDate.display(revision.changed) + " · " + revision.editor) {
                            Text(revision.previousText ?? "No previous transcript")
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .textSelection(.enabled)
                        }
                    }
                } else if busy {
                    ProgressView()
                }
                if let error { Text(error).foregroundStyle(.red) }
                Button("Reload transcript") { Task { await load() } }.disabled(busy)
            }
            .padding(24)
        }
        .task { await load() }
        #if os(macOS)
        .frame(minWidth: 480, minHeight: 360)
        #endif
    }

    private func editor(_ snapshot: TranscriptSnapshot) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if !snapshot.editable {
                Text("The clip is still processing. Reload it before editing.").foregroundStyle(.secondary)
            }
            TextEditor(text: $draft)
                .accessibilityLabel("Transcript")
                .frame(minHeight: 140)
                .padding(4)
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(.secondary.opacity(0.3)))
                .disabled(!snapshot.editable || busy)
            Button("Save correction") { Task { await save(snapshot) } }
                .buttonStyle(.borderedProminent)
                .disabled(busy || !snapshot.editable || trimmed.isEmpty || trimmed == snapshot.text
                          || trimmed.unicodeScalars.count > 12_000)
            if !snapshot.history.isEmpty {
                Text("Earlier transcript versions").font(.headline)
            }
        }
    }

    private var trimmed: String { draft.trimmingCharacters(in: .whitespacesAndNewlines) }

    private func load() async {
        busy = true
        error = nil
        defer { busy = false }
        do {
            apply(try await model.transcript(clipID))
        } catch { self.error = error.localizedDescription }
    }

    private func save(_ snapshot: TranscriptSnapshot) async {
        let correction: TranscriptCorrection
        if let request, request.text == trimmed && request.revision == snapshot.revision {
            correction = request
        } else {
            correction = TranscriptCorrection(id: nexID(), revision: snapshot.revision, text: trimmed)
        }
        request = correction
        busy = true
        error = nil
        defer { busy = false }
        do {
            apply(try await model.correctTranscript(clipID, correction: correction))
            onSave()
        } catch { self.error = error.localizedDescription }
    }

    private func apply(_ result: TranscriptSnapshot) {
        snapshot = result
        draft = result.text ?? ""
        request = nil
    }
}
