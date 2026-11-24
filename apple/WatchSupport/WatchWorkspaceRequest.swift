import Foundation

struct WatchRequest: Codable, Sendable {
    enum Action: String, Codable, Sendable {
        case open, chunk, chat, ask, daybook, conversation, activities, feed, savedStory, ideas, recordingStatus
        case goals, goal, goalEdit, goalAction, goalFollowUp, files, recordings, recording, audio
        case transcript, correctTranscript, memory, source, context, editContext, inbox, dismiss
    }

    let action: Action
    var id: String?
    var cursor: String?
    var day: DaybookContext?
    var text: String?
    var language: String?
    var timeZone: String?
    var deviceClock: DeviceClockReading?
    var value: String?
    var offset: Int?
    var body: Data?

    init(_ action: Action, id: String? = nil, cursor: String? = nil, day: DaybookContext? = nil,
         text: String? = nil, language: String? = nil, timeZone: String? = nil, value: String? = nil,
         offset: Int? = nil, body: Data? = nil, deviceClock: DeviceClockReading? = nil) {
        self.action = action
        self.id = id
        self.cursor = cursor
        self.day = day
        self.text = text
        self.language = language
        self.timeZone = timeZone
        self.deviceClock = deviceClock
        self.value = value
        self.offset = offset
        self.body = body
    }
}

struct WatchResponse: Codable, Sendable {
    var data: Data? = nil
    var remainder: String? = nil
    var error: String? = nil
    var locked = false
}

struct WatchAnswerProgress: Codable, Sendable {
    let answer: Reply?
}
