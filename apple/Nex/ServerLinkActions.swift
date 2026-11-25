import SwiftUI
#if os(macOS)
import AppKit
#else
import UIKit
#endif

struct ServerLinkActions: View {
    let link: String
    @State private var presenter = ServerLinkSharePresenter()
    @State private var copied = false
    @State private var copyFailed = false

    var body: some View {
        Button {
            presenter.share(link)
        } label: {
            Label("Share Server Link", systemImage: "square.and.arrow.up")
        }
        .background(ServerLinkShareAnchor(presenter: presenter).allowsHitTesting(false))
        Button {
            copied = copyLink()
            copyFailed = !copied
        } label: {
            Label(copied ? "Server Link Copied" : "Copy Server Link",
                  systemImage: copied ? "checkmark" : "document.on.document")
        }
        .alert("Couldn’t Copy Server Link", isPresented: $copyFailed) {
            Button("OK", role: .cancel) {}
        }
        .onChange(of: link) { _, _ in copied = false }
    }

    private func copyLink() -> Bool {
        #if os(macOS)
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        return pasteboard.setString(link, forType: .string)
        #else
        UIPasteboard.general.string = link
        return true
        #endif
    }
}

#if os(macOS)
private struct ServerLinkShareAnchor: NSViewRepresentable {
    let presenter: ServerLinkSharePresenter

    func makeNSView(context: Context) -> NSView { NSView() }

    func updateNSView(_ view: NSView, context: Context) { presenter.anchor = view }
}

@MainActor
private final class ServerLinkSharePresenter {
    weak var anchor: NSView?
    private var picker: NSSharingServicePicker?

    func share(_ link: String) {
        guard let anchor, anchor.window != nil else { return }
        picker?.close()
        let picker = NSSharingServicePicker(items: [link])
        self.picker = picker
        picker.show(relativeTo: anchor.bounds, of: anchor, preferredEdge: .minY)
    }
}
#else
private struct ServerLinkShareAnchor: UIViewRepresentable {
    let presenter: ServerLinkSharePresenter

    func makeUIView(context: Context) -> UIView { UIView() }

    func updateUIView(_ view: UIView, context: Context) { presenter.anchor = view }
}

@MainActor
private final class ServerLinkSharePresenter {
    weak var anchor: UIView?

    func share(_ link: String) {
        guard let anchor, anchor.window != nil else { return }
        var responder: UIResponder? = anchor
        while let current = responder, !(current is UIViewController) {
            responder = current.next
        }
        guard let presenter = responder as? UIViewController, presenter.presentedViewController == nil else { return }

        let sheet = UIActivityViewController(activityItems: [link], applicationActivities: nil)
        if anchor.traitCollection.userInterfaceIdiom == .pad { sheet.modalPresentationStyle = .popover }
        sheet.popoverPresentationController?.sourceView = anchor
        sheet.popoverPresentationController?.sourceRect = anchor.bounds
        presenter.present(sheet, animated: true)
    }
}
#endif
