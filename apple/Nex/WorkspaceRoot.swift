import SwiftUI

struct NexRoot: View {
    @ObservedObject var model: Controller

    var body: some View {
        WorkspaceRoot(model: model, privacy: model.privacy)
            .id(model.pairing?.url)
            .tint(WorkspaceStyle.accent)
    }
}

private struct WorkspaceRoot: View {
    @Environment(\.scenePhase) private var scene
    @ObservedObject var model: Controller
    @ObservedObject var privacy: AppPrivacy
    @StateObject private var chat = WorkspaceChat()
    @State private var tab = WorkspaceTab.chat
    @State private var activityDate = Calendar.current.startOfDay(for: .now)
    #if os(macOS)
    @Environment(\.openSettings) private var openMacSettings
    #else
    @State private var tool: WorkspaceTool?
    #endif
    @State private var goalDraft: GoalDraft?
    @State private var selectedGoal: String?

    private var availableTabs: [WorkspaceTab] {
        WorkspaceTab.allCases.filter { $0.isAvailable(in: model.workspaceFeatures) }
    }

    private var selection: WorkspaceTab { availableTabs.contains(tab) ? tab : .chat }

    var body: some View {
        windowContent
            .modifier(WorkspaceRecordingMonitor(model: model))
            #if os(iOS)
            .safeAreaInset(edge: .top, spacing: 0) { privateInbox }
            #endif
            .sheet(item: $goalDraft) { draft in
                GoalEditor(model: model, draft: draft) { goal in openGoal(goal.id) }
            }
            .onChange(of: availableTabs) { _, tabs in
                if !tabs.contains(tab) { tab = .chat }
                if !tabs.contains(.goals) {
                    goalDraft = nil
                    selectedGoal = nil
                }
            }
            .onReceive(model.voiceChatMessages) { chat.receiveVoiceMessage($0) }
            .onReceive(privacy.$items) { chat.reconcilePrivate($0) }
            .onChange(of: privacy.generation) {
                chat.clearPrivateReply()
                model.stopPrivateChatSpeech()
            }
            .onReceive(NotificationCenter.default.publisher(for: .nexInboxChanged)) { _ in
                if privacy.connected { Task { await chat.load(model) } }
            }
            .onReceive(NotificationCenter.default.publisher(for: .nexOpenInbox)) { _ in
                tab = .chat
                if privacy.connected { Task { await chat.load(model) } }
            }
            .task(id: privacy.connected) { await privacy.observeInbox(model) }
            .task(id: scene != .background && privacy.connected) {
                guard scene != .background, privacy.connected else { return }
                await chat.observe(model)
            }
            .task(id: scene != .background && model.workspaceFeatureEnabled(.goals)) {
                guard scene != .background else { return }
                await syncGoalAlerts()
            }
            #if os(iOS)
            .onAppear {
                if model.accountSignIn.takeSettingsReturn() { tool = .settings(.accounts) }
            }
            #endif
    }

    @ViewBuilder private var windowContent: some View {
        #if os(macOS)
        MacWorkspaceView(model: model, selection: Binding(get: { selection }, set: selectTab), availableTabs: availableTabs) {
            VStack(spacing: 0) {
                privateInbox
                content.frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        #else
        PhoneWorkspaceView(model: model, selection: selection, availableTabs: availableTabs, select: selectTab, tool: $tool) { content }
        #endif
    }

    @ViewBuilder private var privateInbox: some View {
        if selection == .chat {
            PrivateInbox(model: model, privacy: privacy)
        }
    }

    private func syncGoalAlerts() async {
        #if os(macOS)
        await GoalNotifications.removeAll()
        #else
        if model.pairing == nil || !model.workspaceFeatureEnabled(.goals) {
            await GoalNotifications.removeAll()
            return
        }
        while !Task.isCancelled {
            await IPhonePush.shared.refresh(using: model)
            if let goals = try? await model.goals(), !Task.isCancelled {
                await GoalNotifications.sync(goals, using: model)
            }
            do { try await Task.sleep(for: .seconds(5)) } catch { return }
        }
        #endif
    }

    @ViewBuilder private var content: some View {
        if model.pairing == nil && selection != .chat && selection != .daybook {
            VStack {
                #if os(macOS)
                Spacer()
                #endif
                WorkspaceEmpty(
                    title: "Connect to Excalibur", symbol: "externaldrive.connected.to.line.below",
                    detail: "Pair Nex to sync and open your workspace.")
                Button("Open Settings", action: openSettings).buttonStyle(.borderedProminent)
                Spacer()
            }
        } else {
            destination
        }
    }

    @ViewBuilder private var destination: some View {
        switch selection {
        case .chat:
            WorkspaceChatView(model: model, chat: chat, privacy: privacy,
                              openGoal: openGoal, openActivities: openActivities, pair: openSettings)
        case .daybook:
            DaybookView(model: model, openSettings: openSettings)
                .id(model.workspaceFeatureEnabled(.activities))
        case .activities:
            ActivitiesView(model: model, date: $activityDate)
        case .feed:
            NewsFeedView(model: model, discuss: compose)
        case .ideas:
            IdeasView(model: model, compose: compose, newGoal: createGoal)
                .id([model.workspaceFeatureEnabled(.feed), model.workspaceFeatureEnabled(.goals)])
        case .goals:
            if let selectedGoal {
                GoalDetailView(model: model, id: selectedGoal) { self.selectedGoal = nil }
                    .id(selectedGoal)
            } else {
                GoalsView(model: model, open: openGoal) { createGoal("") }
            }
        case .files:
            WorkspaceLibrary(model: model)
        #if os(macOS)
        case .memory: PersonalContextView(model: model)
        case .archive: ArchiveView(model: model)
        #endif
        }
    }

    private func selectTab(_ item: WorkspaceTab) {
        guard availableTabs.contains(item) else { return }
        if item == .goals && tab == .goals { selectedGoal = nil }
        if item == .activities && tab != .activities { activityDate = Calendar.current.startOfDay(for: .now) }
        tab = item
    }

    private func openActivities(_ date: Date) {
        guard availableTabs.contains(.activities) else { return }
        activityDate = Calendar.current.startOfDay(for: min(date, .now))
        tab = .activities
    }

    private func compose(_ text: String) {
        chat.draft = text
        tab = .chat
    }

    private func openGoal(_ identifier: String) {
        guard availableTabs.contains(.goals) else { return }
        tab = .goals
        selectedGoal = identifier
    }

    private func createGoal(_ prompt: String) {
        guard availableTabs.contains(.goals) else { return }
        goalDraft = GoalDraft(prompt: prompt)
    }

    private func openSettings() {
        #if os(macOS)
        openMacSettings()
        #else
        tool = .settings(.excalibur)
        #endif
    }
}
