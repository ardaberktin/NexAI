import Combine
import Foundation

@MainActor
final class Controller: ObservableObject {
    @Published var serverProfile: ServerProfile?
    @Published private(set) var signingOut = false
    @Published private(set) var signOutError: String?
    @Published var status: ServerStatus?
    @Published private(set) var workspaceFeatures: WorkspaceFeatures?
    @Published private(set) var savingWorkspaceFeature = false
    @Published private(set) var statusUnavailable = false
    @Published var message: String?
    @Published private(set) var voiceUnrecognized = false
    @Published var pending = 0
    @Published var localSpeechUnavailable = false
    @Published var language = NexLanguage.auto {
        didSet {
            capture.language = language
            recordingAttention.cancel()
            saved.set(language.rawValue, forKey: "language")
        }
    }
    @Published var handsFree = false {
        didSet {
            saved.set(handsFree, forKey: "handsFree")
            configureLiveWake()
        }
    }
    @Published var automaticVoice = true {
        didSet {
            saved.set(automaticVoice, forKey: "automaticVoice")
            configureAutomaticVoice()
        }
    }
    @Published var useDeviceTranscription = false {
        didSet {
            localSpeechUnavailable = false
            saved.set(useDeviceTranscription, forKey: "deviceTranscription")
        }
    }
    @Published var liveVoice = true {
        didSet {
            saved.set(liveVoice, forKey: "liveVoice")
            configureAutomaticVoice()
            if !liveVoice { stopLive() }
        }
    }
    @Published var continuousListening = true {
        didSet {
            saved.set(continuousListening, forKey: "continuousListening")
            configureAutomaticVoice()
        }
    }
    @Published private(set) var liveContext: DaybookContext?
    @Published private(set) var liveError: String?
    @Published var liveState: LiveConversation.State?
    @Published private(set) var liveTranscript: String?
    @Published var playing = false
    @Published private(set) var chatSpeechID: String?
    @Published private(set) var chatSpeechError: String?
    @Published var reply: Reply?
    let voiceChatMessages = PassthroughSubject<WorkspaceMessage, Never>()

    let capture: Capture
    let privacy = AppPrivacy()
    let weatherSharing = WeatherSharing()
    let accountSignIn = AccountSignIn()
    @Published var recognizedSpeaker = false
    private let recordingReporter = RecordingReporter()
    #if os(macOS)
    let gemma: MacGemmaControl
    let screenSharing: ScreenSharing
    #endif
    private let spoolRoot: Spool
    private var spool: Spool
    private var liveConversation: LiveConversation?
    private let recordingAttention = RecordingAttention()
    private let speech = DeviceSpeech()
    private let saved = UserDefaults.standard
    private var api: NexAPI?
    private var syncInProgress = false
    private var syncRequested = false
    private var waitingForVoice: Set<String> = []
    private var speakingStream: String?
    private var chatSpeechTask: Task<Void, Never>?
    private var privateChatSpeechGeneration: UUID?
    private var connectSince = Date.now
    private var seenReplies: Set<String> = []
    private var lastStatus = Date.distantPast
    private var workspaceFeatureVersion = 0
    private var workspaceFeatureCacheKey: String?
    private var lastSpeechIssue = Date.distantPast
    private var lastSpeechLanguage: NexLanguage?

    init(spool: Spool, profile: ServerProfile?) throws {
        #if os(macOS)
        let gemma = MacGemmaControl()
        self.gemma = gemma
        screenSharing = ScreenSharing(gemma: gemma)
        #endif
        spoolRoot = spool
        self.spool = if let profile { try spool.scoped(to: profile.workspaceScope) } else { spool }
        capture = Capture(spool: self.spool)
        language = NexLanguage(rawValue: saved.string(forKey: "language") ?? "auto") ?? .auto
        handsFree = saved.bool(forKey: "handsFree")
        automaticVoice = saved.object(forKey: "automaticVoice") as? Bool ?? true
        useDeviceTranscription = saved.bool(forKey: "deviceTranscription")
        liveVoice = saved.object(forKey: "liveVoice") as? Bool ?? true
        continuousListening = saved.object(forKey: "continuousListening") as? Bool ?? true
        capture.language = language
        if let profile {
            do {
                setConnection(try NexAPI(profile), featureCacheScope: profile.workspaceScope)
                serverProfile = profile
            } catch { message = error.localizedDescription }
        }
        weatherSharing.connect(serverProfile)
        configureAutomaticVoice()
        capture.liveInterrupted = { [weak self] in self?.stopLive() }
        capture.notify = { [weak self] in
            Task { await self?.sync() }
        }
    }

    func signIn(link: String, username: String, password: String) async throws {
        guard !signingOut, serverProfile == nil else { throw CancellationError() }
        let server = try NexServerLink(link)
        try await privacy.signIn(server, username: username, password: password) { profile, connection in
            try install(profile, connection: connection)
        }
    }

    private func install(_ profile: ServerProfile, connection: NexAPI) throws {
        let accountSpool = try spoolRoot.scoped(to: profile.workspaceScope)
        try ServerProfileKeychain.save(profile)
        if serverProfile?.workspaceScope != profile.workspaceScope {
            stopAccountActivity()
            capture.useSpool(accountSpool)
            spool = accountSpool
            #if os(iOS)
            PhoneWatchRelay.shared.setEnabled(false)
            #endif
        }
        setConnection(connection, featureCacheScope: profile.workspaceScope)
        serverProfile = profile
        #if os(iOS)
        PhoneWatchRelay.shared.accountChanged()
        #endif
        weatherSharing.connect(profile)
        resetConnectionStatus()
        Task { await sync() }
    }

    func signOut(revoke: Bool = true) async {
        guard !signingOut, let profile = serverProfile else { return }
        signingOut = true
        signOutError = nil
        defer { signingOut = false }
        do { try ServerProfileKeychain.clear() }
        catch {
            signOutError = error.localizedDescription
            privacy.lock()
            return
        }
        weatherSharing.disconnect()
        #if os(iOS)
        IPhonePush.shared.signedOut()
        PhoneWatchRelay.shared.setEnabled(false)
        #endif
        stopAccountActivity()
        privacy.lock()
        setConnection(nil)
        serverProfile = nil
        #if os(iOS)
        PhoneWatchRelay.shared.accountChanged()
        #endif
        capture.useSpool(spoolRoot)
        spool = spoolRoot
        resetConnectionStatus()
        await GoalNotifications.removeAll()
        await ClockNotifications.clear()
        OwnerKey.discard(profile.biometricSlot)
        if revoke, let api = try? NexAPI(profile) {
            do { try await api.signOut() }
            catch NexError.api(401, _) {}
            catch { message = "Signed out on this device. The server could not confirm revocation." }
        }
    }

    private func stopAccountActivity() {
        saved.set(false, forKey: "resumeRecording")
        recordingAttention.cancel()
        capture.stopForAccountChange()
        stopLive()
        if playing { stopPlayback() }
        #if os(macOS)
        screenSharing.stop()
        #endif
    }

    private func resetConnectionStatus() {
        lastStatus = .distantPast
        status = nil
        statusUnavailable = false
        connectSince = .now
        seenReplies.removeAll()
        waitingForVoice.removeAll()
        pending = 0
        message = nil
        reply = nil
        recognizedSpeaker = false
    }

    func lockWorkspace() {
        #if os(iOS)
        accountSignIn.workspaceWillLock()
        #endif
        privacy.lock()
    }

    private func setConnection(_ connection: NexAPI?, featureCacheScope: String? = nil) {
        if chatSpeechID != nil { stopPlayback() }
        accountSignIn.reset()
        api?.cancelPending()
        api = connection
        workspaceFeatureVersion += 1
        workspaceFeatureCacheKey = featureCacheScope.map { "nex.workspaceFeatures." + $0 }
        let cached = workspaceFeatureCacheKey.flatMap(saved.data(forKey:))
        applyWorkspaceFeatures(cached.flatMap { try? JSONDecoder().decode(WorkspaceFeatures.self, from: $0) })
        savingWorkspaceFeature = false
        recordingReporter.start(capture, connection: connection)
        #if os(macOS)
        screenSharing.contextChanged = { [weak self, weak connection] summary, date in
            Task {
                guard let self, let connection, self.api === connection else { return }
                await self.reportScreenContext(summary, observed: date)
            }
        }
        #endif
    }

    func toggleRecording() async {
        if capture.ambient && !capture.interrupted {
            pauseRecording()
        } else {
            if liveConversation != nil { stopLive() }
            await capture.resumeRecording()
            if capture.ambient { saved.set(true, forKey: "resumeRecording") }
            if capture.recording { await RecordingNotifications.requestAccessIfNeeded() }
        }
    }

    func pauseRecording() {
        recordingAttention.cancel()
        saved.set(false, forKey: "resumeRecording")
        capture.pauseAmbient()
        stopLive()
    }

    func toggleVoice(context: DaybookContext? = nil) async {
        guard context == nil || workspaceFeatureEnabled(.daybook) else { return }
        recordingAttention.cancel()
        voiceUnrecognized = false
        liveError = nil
        if let context {
            if liveConversation != nil && liveContext == context {
                stopLive()
            } else {
                stopLive()
                await startLive(context: context)
            }
        } else if liveConversation != nil {
            stopLive()
        } else if (liveVoice || sharesScreen), !capture.voice, !playing, api != nil {
            await startLive()
        } else {
            await capture.toggleVoice()
        }
    }

    private var sharesScreen: Bool {
        #if os(macOS)
        screenSharing.isSharing
        #else
        false
        #endif
    }

    private func configureLiveWake() {
        capture.wake = usesRecordedWake
        if liveVoice && (usesRecordedWake || (automaticVoice && !continuousListening)) {
            capture.liveWake = { [weak self] wake in
                Task { @MainActor in
                    guard let self, self.liveVoice, self.automaticVoice || self.handsFree,
                          self.capture.ambient, self.capture.recording, !self.capture.interrupted else {
                        _ = wake.finish()
                        return
                    }
                    await self.startLive(wake: wake)
                }
            }
        } else {
            capture.liveWake = nil
        }
    }

    private var readyForAutomaticVoice: Bool {
        !watchLiveActive && liveVoice && automaticVoice && capture.ambient && capture.recording && !capture.interrupted
            && !capture.voice && !capture.listeningForQuestion && !playing && liveConversation == nil
    }

    private var watchLiveActive: Bool {
        #if os(iOS)
        PhoneWatchRelay.shared.watchLiveActive
        #else
        false
        #endif
    }

    private var usesRecordedWake: Bool { handsFree && !automaticVoice }

    private func configureAutomaticVoice() {
        configureLiveWake()
        recordingAttention.configure(capture: capture, enabled: automaticVoice && liveVoice, continuous: continuousListening,
            context: { [weak self] in
                guard let self, self.readyForAutomaticVoice, let api = self.api else { return nil }
                return (api, self.language.rawValue)
            }, open: { [weak self] opening in
                await self?.startLive(opening: opening)
            })
    }

    private func startLive(wake: LiveWake? = nil, context: DaybookContext? = nil, opening: AutomaticOpening? = nil) async {
        guard liveConversation == nil, !playing, !capture.voice else {
            _ = wake?.finish()
            if let opening { await api?.declineAttention(opening.ticket) }
            return
        }
        if watchLiveActive {
            _ = wake?.finish()
            if let opening { await api?.declineAttention(opening.ticket) }
            if wake == nil, opening == nil { liveError = "End the live conversation on Apple Watch first." }
            return
        }
        recordingAttention.cancel()
        guard let api else {
            if context == nil, opening == nil { await capture.toggleVoice(seed: wake?.finish()) }
            return
        }
        message = nil
        liveError = nil
        liveContext = context
        clearLiveTurn()
        let addressed = wake != nil || opening != nil
        let openingChimed = opening?.alreadyChimed == true || (addressed && capture.questionChime.play())
        let conversation = LiveConversation(spool: spool, chime: capture.questionChime, openingChimed: openingChimed)
        liveConversation = conversation
        liveState = .connecting
        observeLive(conversation, context: context)
        var fallback = false
        do {
            try await api.checkPrivacyVersion()
            try await api.checkPrivacyVersion(live: true)
            try await conversation.run(socket: api.liveSocket(), language: language.rawValue,
                                       wake: wake != nil, context: context, opening: opening) {
                let seed = try await self.capture.beginLive(wake: wake) { self.liveConversation === conversation }
                return (self.capture.ambient, seed)
            }
        } catch {
            if liveConversation === conversation, opening == nil || conversation.hasTurn {
                fallback = opening == nil && context == nil && !sharesScreen && !conversation.hasTurn
                let cause = (error as? NexError)?.errorDescription
                    ?? "The connection to Excalibur ended."
                liveError = cause + (fallback ? " Using recorded voice." : " Tap Ask by voice to reconnect.")
            }
        }
        if let opening, !conversation.hasTurn { await api.declineAttention(opening.ticket) }
        var seed = wake?.finish()
        seed?.openingChimed = openingChimed
        guard liveConversation === conversation else { return }
        stopLive()
        if fallback { await capture.toggleVoice(seed: seed) }
    }

    private func observeLive(_ conversation: LiveConversation, context: DaybookContext?) {
        conversation.onSpeaker = { [weak self] matched in self?.recognizedSpeaker = matched }
        conversation.onState = { [weak self] state in self?.liveState = state }
        conversation.onMicrophoneChanged = { [weak capture] active in capture?.liveMicrophoneActive = active }
        conversation.onReply = { [weak self] id, reply in
            self?.publishVoiceReply(reply, chatID: context == nil ? id : nil)
        }
        conversation.onTurnStarted = { [weak self] in self?.clearLiveTurn() }
        conversation.onTranscript = { [weak self] id, text in
            self?.liveTranscript = text
            if context == nil {
                self?.voiceChatMessages.send(WorkspaceMessage(id: "q-" + id, role: .user, text: text,
                    date: NexDate.wire(), sources: []))
            }
        }
        let connection = api
        conversation.onArchive = { [weak self, weak connection] error in
            Task { @MainActor in
                guard let self, let connection, self.api === connection else { return }
                if let error { self.message = error }
                else { await self.sync() }
            }
        }
    }

    private func stopLive() {
        let old = liveConversation
        liveConversation = nil
        liveState = nil
        liveContext = nil
        liveTranscript = nil
        old?.onSpeaker = nil
        old?.onState = nil
        old?.onMicrophoneChanged = nil
        old?.onReply = nil
        old?.onTurnStarted = nil
        old?.onTranscript = nil
        old?.stop()
        capture.liveMicrophoneActive = false
        capture.endLive()
    }

    private func clearLiveTurn() {
        recognizedSpeaker = false
        liveTranscript = nil
        reply = nil
    }

    func endDaybookVoice(_ context: DaybookContext) {
        if liveContext == context { stopLive() }
    }

    func loop() async {
        if serverProfile != nil && !capture.ambient && saved.bool(forKey: "resumeRecording") {
            await capture.resumeRecording()
        }
        while !Task.isCancelled {
            weatherSharing.retryClear()
            #if os(iOS)
            PhoneWatchRelay.shared.sync()
            #endif
            await sync()
            do { try await Task.sleep(for: .seconds(3)) }
            catch { return }
        }
    }

    func sync() async {
        if syncInProgress {
            syncRequested = true
            return
        }
        syncInProgress = true
        defer { syncInProgress = false }
        repeat {
            syncRequested = false
            await syncOnce()
        } while syncRequested && !Task.isCancelled
    }

    func refreshStatus() async throws {
        guard let connection = api else { return }
        let featureVersion = workspaceFeatureVersion
        do {
            let remote: ServerStatus = try await connection.get("/v1/status")
            guard api === connection else { return }
            status = remote
            if featureVersion == workspaceFeatureVersion && !savingWorkspaceFeature {
                applyWorkspaceFeatures(remote.workspaceFeatures)
            }
            statusUnavailable = false
            lastStatus = .now
        } catch {
            if api === connection && !Task.isCancelled { statusUnavailable = true }
            throw error
        }
    }

    func workspaceFeatureEnabled(_ feature: WorkspaceFeature) -> Bool {
        workspaceFeatures?[feature] ?? true
    }

    func setWorkspaceFeature(_ feature: WorkspaceFeature, enabled: Bool) async throws {
        guard !savingWorkspaceFeature else { return }
        let connection = try connection()
        savingWorkspaceFeature = true
        workspaceFeatureVersion += 1
        defer {
            if api === connection {
                savingWorkspaceFeature = false
                workspaceFeatureVersion += 1
            }
        }
        struct Request: Encodable { let feature: String; let enabled: Bool }
        let features: WorkspaceFeatures = try await connection.send(
            "/v1/workspace/features", method: "PUT", body: Request(feature: feature.rawValue, enabled: enabled))
        guard api === connection else { return }
        applyWorkspaceFeatures(features)
    }

    private func applyWorkspaceFeatures(_ features: WorkspaceFeatures?) {
        guard workspaceFeatures != features else { return }
        workspaceFeatures = features
        if let key = workspaceFeatureCacheKey {
            let encoded = features.flatMap { try? JSONEncoder().encode($0) }
            saved.set(encoded, forKey: key)
        }
        if !workspaceFeatureEnabled(.daybook) && liveContext != nil { stopLive() }
        if !workspaceFeatureEnabled(.goals) {
            Task { await GoalNotifications.removeAll() }
        }
    }

    private func syncOnce() async {
        let spool = self.spool
        let attempt = api
        do {
            let clips = try spool.clips().filter { $0.duration != nil }.sorted {
                if $0.mode != $1.mode { return $0.mode == .voice }
                return $0.started < $1.started
            }
            pending = clips.count
            guard let connection = attempt else { return }
            if Date.now.timeIntervalSince(lastStatus) > 14 {
                try await refreshStatus()
                guard api === connection else { return }
            }
            for gap in try spool.gaps().prefix(6) {
                guard api === connection else { return }
                let _: [String: Bool] = try await connection.send("/v1/gaps", body: gap)
                try spool.remove(gap)
            }
            for clip in clips.prefix(5) {
                guard api === connection else { return }
                let url = spool.audio(clip.id)
                guard FileManager.default.fileExists(atPath: url.path) else {
                    try spool.remove(clip)
                    continue
                }
                let recording = try await connection.upload(clip, data: Data(contentsOf: url))
                guard api === connection else { return }
                if useDeviceTranscription && status?.models.transcription != "configured"
                    && recording.transcript == nil && recording.state != "ready" {
                    try await supplyTranscript(clip, url: url, connection: connection)
                }
                guard api === connection else { return }
                try spool.remove(clip)
                if clip.mode == .voice && clip.liveHandled != true && clip.started >= Date.now.addingTimeInterval(-90)
                    || clip.wake && usesRecordedWake && clip.started >= Date.now.addingTimeInterval(-30) {
                    watchVoiceReply(recording, connection: connection, openingChimed: clip.openingChimed == true)
                }
            }
            try await checkReplies(connection)
            guard api === connection else { return }
            pending = try spool.clips().filter { $0.duration != nil }.count
            message = nil
        } catch NexError.api(401, _) {
            if api === attempt, serverProfile != nil { await signOut(revoke: false) }
        } catch {
            guard api === attempt else { return }
            message = error.localizedDescription
            lastStatus = .distantPast
        }
    }

    private func supplyTranscript(_ clip: Clip, url: URL, connection: NexAPI) async throws {
        // Apple requires an explicit recognition locale. Automatic server detection uses Whisper.
        let locale = NexLanguage(rawValue: clip.language) ?? .auto
        guard locale != .auto else {
            localSpeechUnavailable = true
            return
        }
        if lastSpeechLanguage == locale && Date.now.timeIntervalSince(lastSpeechIssue) < 60 { return }
        let transcriber = OfflineRecognition()
        let text: String
        do {
            text = try await transcriber.transcribe(url, language: locale).trimmingCharacters(in: .whitespacesAndNewlines)
            localSpeechUnavailable = false
        } catch {
            lastSpeechLanguage = locale
            lastSpeechIssue = .now
            localSpeechUnavailable = true
            return
        }
        if !text.isEmpty {
            let _: [String: Bool] = try await connection.send(
                "/v1/recordings/" + clip.id + "/transcript", method: "PUT", body: ["text": text]
            )
        }
    }

    private func checkReplies(_ connection: NexAPI) async throws {
        guard liveConversation == nil, !playing, !capture.voice, !capture.listeningForQuestion else { return }
        let fresh = max(connectSince, Date.now.addingTimeInterval(-110))
        let response: ListResponse<Recording> = try await connection.get(
            "/v1/replies", query: [URLQueryItem(name: "since", value: NexDate.wire(fresh))]
        )
        guard api === connection else { return }
        for clip in response.items where !waitingForVoice.contains(clip.id) {
            guard api === connection else { return }
            if await present(clip, since: fresh) { break }
        }
        guard api === connection else { return }
        if seenReplies.count > 1200 {
            seenReplies = seenReplies.intersection(response.items.map(\.id))
        }
    }

    private func watchVoiceReply(_ queued: Recording, connection: NexAPI, openingChimed: Bool) {
        guard waitingForVoice.insert(queued.id).inserted else { return }
        Task { [weak self] in
            guard let self else { return }
            defer {
                if self.api === connection { self.waitingForVoice.remove(queued.id) }
            }
            do {
                var playStream = true
                var chimed = openingChimed
                let result = try await connection.watchVoice(queued.id, receiveChime: {
                    guard !chimed, self.api === connection, self.canPlayStream(queued) else { return }
                    chimed = self.capture.questionChime.play()
                }) { audio in
                    guard self.api === connection, playStream else { return }
                    playStream = try self.playStreamAudio(audio, clip: queued)
                }
                guard self.api === connection else { return }
                if let answer = result.reply {
                    await self.completeVoice(queued, answer: answer)
                } else if result.state == "unrecognized" {
                    self.showUnrecognized(queued)
                }
            } catch {
                if self.api === connection, self.speakingStream == queued.id { self.speech.endAudioStream() }
                await self.waitForCompletedVoice(queued, connection: connection)
            }
            if self.api === connection, self.speakingStream == queued.id { self.speech.endAudioStream() }
        }
    }

    private func waitForCompletedVoice(_ queued: Recording, connection: NexAPI) async {
        var clip = queued
        for _ in 0..<120 {
            guard api === connection else { return }
            if clip.state == "unrecognized" {
                showUnrecognized(clip)
                return
            }
            if let answer = clip.reply, !playing, !capture.voice, !capture.listeningForQuestion {
                await completeVoice(clip, answer: answer)
                return
            }
            guard clip.reply != nil || ["queued", "processing"].contains(clip.state) else { return }
            do {
                try await Task.sleep(for: .milliseconds(250))
                if clip.reply == nil { clip = try await connection.get("/v1/recordings/" + queued.id + "/receipt") }
            } catch { return }
        }
    }

    private var oldestVoiceReply: Date { max(connectSince, Date.now.addingTimeInterval(-110)) }

    private func showUnrecognized(_ clip: Recording) {
        guard clip.mode == .voice, liveConversation == nil, !capture.voice, !capture.listeningForQuestion,
              isRecent(clip, since: oldestVoiceReply) else { return }
        voiceUnrecognized = true
    }

    private func completeVoice(_ clip: Recording, answer: Reply) async {
        guard liveConversation == nil else { return }
        voiceUnrecognized = false
        if seenReplies.contains(clip.id), isRecent(clip, since: oldestVoiceReply) {
            publishVoiceReply(answer, chatID: clip.id)
        } else if !playing && !capture.voice && !capture.listeningForQuestion {
            _ = await present(clip, since: oldestVoiceReply, answer: answer)
        }
    }

    private func playStreamAudio(_ audio: Data, clip: Recording) throws -> Bool {
        if speakingStream == clip.id {
            try speech.enqueueAudio(audio)
            return true
        }
        guard canPlayStream(clip) else { return false }
        beginPlayback()
        speakingStream = clip.id
        speech.beginAudioStream { [weak self] in self?.endPlayback() }
        do {
            try speech.enqueueAudio(audio)
            seenReplies.insert(clip.id)
            return true
        } catch {
            speech.endAudioStream()
            throw error
        }
    }

    private func isRecent(_ clip: Recording, since date: Date) -> Bool {
        let style = Date.ISO8601FormatStyle(includingFractionalSeconds: true)
        return (try? style.parse(clip.started)).map { $0 >= date } ?? false
    }

    private func canPlayStream(_ clip: Recording) -> Bool {
        liveConversation == nil && !playing && !capture.voice && !capture.listeningForQuestion
            && !seenReplies.contains(clip.id) && isRecent(clip, since: oldestVoiceReply) && allowsSpeech(for: clip)
    }

    private func allowsSpeech(for clip: Recording) -> Bool {
        clip.mode == .voice || (usesRecordedWake && capture.ambient && !capture.interrupted)
    }

    private func present(_ clip: Recording, since: Date, answer: Reply? = nil) async -> Bool {
        guard seenReplies.insert(clip.id).inserted, isRecent(clip, since: since),
              allowsSpeech(for: clip), let answer = answer ?? clip.reply else { return false }
        publishVoiceReply(answer, chatID: clip.id)
        await speak(answer)
        return true
    }

    func connection() throws -> NexAPI {
        guard let api else { throw NexError.connection("Sign in to Nex first.") }
        return api
    }

    func play(_ id: String) async throws {
        guard let api else { throw NexError.connection("Sign in to Nex first.") }
        let data = try await api.bytes("/v1/recordings/" + id + "/audio")
        if self.api !== api || playing || capture.voice { return }
        stopLive()
        beginPlayback()
        do { try speech.play(data) { [weak self] in self?.endPlayback() } }
        catch { endPlayback(); throw error }
    }

    func speak(_ answer: Reply) async {
        let text = String(answer.voiceText.prefix(1800))
        guard liveConversation == nil, !text.isEmpty else { return }
        let source = api
        let serverVoice = status?.models.speech.contains(answer.language) == true
        var audio: Data?
        if serverVoice, let source {
            audio = try? await serverSpeech(text, language: answer.language, using: source)
        }
        guard api === source, liveConversation == nil, !playing, !capture.voice, !capture.listeningForQuestion else { return }
        beginPlayback()
        if let audio {
            do {
                try speech.play(audio) { [weak self] in self?.endPlayback() }
                return
            } catch { message = error.localizedDescription }
        }
        if !speech.speak(text, language: answer.language, onEnd: { [weak self] in self?.endPlayback() }) {
            message = "No offline voice for this language is available."
            endPlayback()
        }
    }

    var canStartChatSpeech: Bool {
        serverProfile != nil && liveConversation == nil && !playing && !capture.voice && !capture.listeningForQuestion
    }

    func readChatMessage(_ message: WorkspaceMessage, language: String) {
        if chatSpeechID == message.id {
            stopPlayback()
            return
        }
        guard canStartChatSpeech, let api, message.offersReadAloud, let text = message.copyableText,
              !message.isPrivate || (privacy.unlocked && privacy.connected) else { return }

        let spokenText = MessageMarkdown(text).preview
        let chunks = ChatSpeech.chunks(spokenText.isEmpty ? text : spokenText)
        guard !chunks.isEmpty else { return }

        chatSpeechError = nil
        chatSpeechID = message.id
        privateChatSpeechGeneration = message.isPrivate ? privacy.generation : nil
        beginPlayback()
        speech.beginAudioStream { [weak self] in self?.endPlayback() }
        chatSpeechTask = Task { [weak self] in
            guard let self else { return }
            do {
                for chunk in chunks {
                    try checkChatSpeechAccess()
                    let audio = try await serverSpeech(chunk, language: language, using: api)
                    try checkChatSpeechAccess()
                    try speech.enqueueAudio(audio)
                }
                speech.endAudioStream()
            } catch {
                guard !Task.isCancelled else { return }
                if !(error is CancellationError) { chatSpeechError = error.localizedDescription }
                stopPlayback()
            }
        }
    }

    private func checkChatSpeechAccess() throws {
        try Task.checkCancellation()
        if let generation = privateChatSpeechGeneration,
           generation != privacy.generation || !privacy.unlocked || !privacy.connected {
            throw CancellationError()
        }
    }

    func stopPrivateChatSpeech() {
        if privateChatSpeechGeneration != nil { stopPlayback() }
    }

    private func serverSpeech(_ text: String, language: String, using api: NexAPI) async throws -> Data {
        try await api.bytes("/v1/speech", method: "POST", body: JSONEncoder().encode([
            "text": text, "language": language
        ]), timeout: 50)
    }

    func stopPlayback() {
        chatSpeechTask?.cancel()
        speech.stop()
    }

    private func beginPlayback() {
        playing = true
        capture.beginPlayback()
    }

    private func endPlayback() {
        chatSpeechTask = nil
        chatSpeechID = nil
        privateChatSpeechGeneration = nil
        speakingStream = nil
        playing = false
        capture.endPlayback()
    }
}
