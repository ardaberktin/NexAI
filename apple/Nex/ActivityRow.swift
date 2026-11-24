import SwiftUI

struct ActivityRow: View {
    let activity: RecordedActivity
    let open: () -> Void

    var body: some View {
        Button(action: open) {
            HStack(alignment: .top, spacing: 12) {
                WorkspaceIcon(symbol: activity.symbol, tint: activity.tint)
                VStack(alignment: .leading, spacing: 4) {
                    Text(activity.title).font(.headline)
                    Text("\(activity.timeRange) · \(activity.deviceName)").font(.subheadline)
                    Text("\(activity.confidence) · \(activity.basis)").font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 4)
                Image(systemName: "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(.tertiary)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 14))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
    }
}
