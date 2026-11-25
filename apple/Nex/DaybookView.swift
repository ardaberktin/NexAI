import SwiftUI

struct DaybookView: View {
    @ObservedObject var model: Controller
    let openSettings: () -> Void
    @Environment(\.scenePhase) private var scenePhase
    @State private var date = Calendar.current.startOfDay(for: .now)
    @State private var overview: DaybookDay?
    @State private var error: String?
    @State private var openConversation: DaybookContext?
    @State private var question: DaybookContext?
    @State private var openSource: MemoryItem?
    @State private var openActivity: RecordedActivity?

    private var context: DaybookContext {
        DaybookContext(date: date, timeZone: TimeZone(identifier: NexDate.timeZone) ?? .gmt)
    }

    var body: some View {
        Group {
            #if os(macOS)
            GeometryReader { window in page(sideBySide: window.size.width >= 760) }
                .toolbar {
                    ToolbarItemGroup(placement: .primaryAction) {
                        previousDay
                        datePicker
                        nextDay
                    }
                }
            #else
            page(sideBySide: false)
            #endif
        }
        .refreshable { await load(context) }
        .task(id: context) {
            let selected = context
            overview = nil
            error = nil
            await load(selected)
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(20)) }
                catch { break }
                if scenePhase == .active { await load(selected) }
            }
        }
        .onChange(of: model.serverProfile?.url) {
            overview = nil
            Task { await load(context) }
        }
        .sheet(item: $openConversation, onDismiss: { Task { await load(context) } }) { selection in
            DaybookConversationSheet(model: model, context: selection)
        }
        .sheet(item: $question) { selection in
            DaybookAskSheet(model: model, context: selection)
        }
        .sheet(item: $openSource) { source in
            SourceSheet(item: source, model: model) { Task { await load(context) } }
        }
        .sheet(item: $openActivity) { activity in
            ActivityDetailView(model: model, activity: activity, day: context)
        }
    }

    private func page(sideBySide: Bool) -> some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 22) {
                #if os(iOS)
                header
                #endif
                if model.serverProfile == nil {
                    ContentUnavailableView {
                        Label("Sign In to Nex", systemImage: "book.closed")
                    } description: {
                        Text("Sign in to open your Daybook.")
                    } actions: {
                        Button("Open Settings", action: openSettings)
                            .buttonStyle(.borderedProminent)
                    }
                } else if let overview {
                    DaybookOverview(day: overview, context: context, sideBySide: sideBySide,
                                    ask: { question = $0 }, openConversation: { openConversation = $0 },
                                    openActivity: { openActivity = $0 }, showSource: showSource)
                } else if error == nil {
                    ProgressView("Opening Daybook…").frame(maxWidth: .infinity)
                }
                if let error {
                    Label(error, systemImage: "exclamationmark.circle").foregroundStyle(.secondary)
                    Button("Try again") { Task { await load(context) } }
                }
            }
            .padding(24)
            #if os(macOS)
            .frame(maxWidth: WorkspaceStyle.pageWidth)
            #else
            .frame(maxWidth: 690)
            #endif
            .frame(maxWidth: .infinity)
        }
    }

    private var datePicker: some View {
        DatePicker("Journal date", selection: $date, in: ...Date.now, displayedComponents: .date)
            .labelsHidden()
            .fixedSize()
    }

    private var previousDay: some View {
        Button("Previous day", systemImage: "chevron.left") { moveDay(-1) }
            .labelStyle(.iconOnly)
    }

    private var nextDay: some View {
        Button("Next day", systemImage: "chevron.right") { moveDay(1) }
            .labelStyle(.iconOnly)
            .disabled(Calendar.current.isDateInToday(date))
    }

    #if os(iOS)
    private var header: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Daybook").font(.largeTitle.weight(.semibold))
            HStack {
                datePicker
                Spacer()
                previousDay
                nextDay
            }
            .buttonStyle(.bordered)
        }
        .accessibilityElement(children: .contain)
    }
    #endif

    private func moveDay(_ offset: Int) {
        if let next = Calendar.current.date(byAdding: .day, value: offset, to: date), next <= .now {
            date = next
        }
    }

    private func load(_ selected: DaybookContext) async {
        guard model.serverProfile != nil else { return }
        do {
            let day = try await model.daybook(selected)
            guard selected == context, !Task.isCancelled else { return }
            overview = day
            error = nil
        } catch {
            if selected == context && !Task.isCancelled { self.error = error.localizedDescription }
        }
    }

    private func showSource(_ source: String) async {
        do { openSource = try await model.source(source) }
        catch { self.error = error.localizedDescription }
    }
}
