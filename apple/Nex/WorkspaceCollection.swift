import SwiftUI

struct WorkspaceCollection<Item: Identifiable, Row: View>: View {
    let items: [Item]
    @ViewBuilder var row: (Item) -> Row

    var body: some View {
        #if os(macOS)
        LazyVGrid(
            columns: [GridItem(.adaptive(minimum: 300), spacing: 18, alignment: .topLeading)],
            alignment: .leading, spacing: 18
        ) {
            ForEach(items) { item in
                row(item)
                    .padding(.horizontal, 16)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                    .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
                    .overlay {
                        RoundedRectangle(cornerRadius: 12).strokeBorder(.quaternary)
                            .allowsHitTesting(false)
                    }
            }
        }
        #else
        LazyVStack(spacing: 0) {
            ForEach(items) { item in
                row(item)
                Divider().opacity(0.65)
            }
        }
        #endif
    }
}
