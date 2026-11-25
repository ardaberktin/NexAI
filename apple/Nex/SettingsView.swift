import SwiftUI

struct SettingsView: View {
    #if os(iOS)
    @Environment(\.dismiss) private var dismiss
    @State private var path: [SettingsCategory]

    init(model: Controller, initialCategory: SettingsCategory? = nil) {
        self.model = model
        _path = State(initialValue: initialCategory.map { [$0] } ?? [])
    }
    #else
    @State private var selection: SettingsCategory?

    init(model: Controller) {
        self.model = model
        _selection = State(initialValue: model.pairing == nil ? .excalibur : .workspace)
    }
    #endif

    @ObservedObject var model: Controller

    var body: some View {
        #if os(iOS)
        NavigationStack(path: $path) {
            List(SettingsCategory.allCases, id: \.self) { category in
                NavigationLink(value: category) { category.label }
            }
            .listStyle(.insetGrouped)
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { done }
            .navigationDestination(for: SettingsCategory.self) { category in
                SettingsPage(model: model, category: category)
                    .toolbar { done }
            }
        }
        #else
        NavigationSplitView {
            List(SettingsCategory.allCases, id: \.self, selection: $selection) { category in
                category.label
            }
            .listStyle(.sidebar)
            .navigationTitle("Settings")
            .navigationSplitViewColumnWidth(min: 190, ideal: 210)
        } detail: {
            if let selection {
                SettingsPage(model: model, category: selection)
                    .id(selection)
            } else {
                ContentUnavailableView("Select a Settings Category", systemImage: "gearshape")
            }
        }
        #endif
    }

    #if os(iOS)
    private var done: some ToolbarContent {
        ToolbarItem(placement: .confirmationAction) {
            Button("Done") { dismiss() }
        }
    }
    #endif
}

enum SettingsCategory: CaseIterable, Hashable {
    case workspace
    case speech
    case notifications
    case privacy
    case nexAccount
    case accounts
    case excalibur
    case models
    #if os(iOS)
    case watch
    #endif

    var title: LocalizedStringKey {
        switch self {
        case .workspace: "Workspace"
        case .speech: "Voice & Recording"
        case .notifications: "Notifications"
        case .privacy: "Privacy & Security"
        case .nexAccount: "Nex Account"
        case .accounts: "Connected Accounts"
        case .excalibur: "Excalibur"
        case .models: "Language Models"
        #if os(iOS)
        case .watch: "Apple Watch"
        #endif
        }
    }

    private var symbol: String {
        switch self {
        case .workspace: "rectangle.grid.2x2"
        case .speech: "waveform"
        case .notifications: "bell"
        case .privacy: "lock.shield"
        case .nexAccount: "person.crop.circle"
        case .accounts: "link"
        case .excalibur: "externaldrive.connected.to.line.below"
        case .models: "cpu"
        #if os(iOS)
        case .watch: "applewatch"
        #endif
        }
    }

    var label: some View {
        Label(title, systemImage: symbol)
    }
}
