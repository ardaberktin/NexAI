import SwiftUI

struct MarkdownTable: View {
    let rows: [MessageMarkdown.Block]
    let columns: [PresentationIntent.TableColumn]

    var body: some View {
        ScrollView(.horizontal) {
            Grid(alignment: .leading, horizontalSpacing: 0, verticalSpacing: 0) {
                ForEach(rows) { row in
                    if row.id != rows.first?.id {
                        Divider().gridCellUnsizedAxes(.horizontal)
                    }
                    GridRow {
                        cells(in: row)
                    }
                    .background {
                        if row.kind == .tableHeaderRow { Color.primary.opacity(0.065) }
                    }
                }
            }
            .clipShape(.rect(cornerRadius: 8))
            .overlay {
                RoundedRectangle(cornerRadius: 8).strokeBorder(.primary.opacity(0.15))
            }
        }
        .fixedSize(horizontal: false, vertical: true)
    }

    private func cells(in row: MessageMarkdown.Block) -> some View {
        let cells = row.children.reduce(into: [Int: AttributedString]()) { cells, cell in
            if case .tableCell(let column) = cell.kind { cells[column] = cell.text }
        }
        return ForEach(columns.indices, id: \.self) { column in
            let alignment = alignment(for: columns[column])
            Text(cells[column] ?? AttributedString())
                .fontWeight(row.kind == .tableHeaderRow ? .semibold : .regular)
                .fixedSize()
                .padding(.horizontal, 10)
                .padding(.vertical, 7)
                .frame(maxWidth: .infinity, alignment: alignment)
        }
    }

    private func alignment(for column: PresentationIntent.TableColumn) -> Alignment {
        switch column.alignment {
        case .left: .leading
        case .center: .center
        case .right: .trailing
        @unknown default: .leading
        }
    }
}
