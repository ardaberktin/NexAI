import SwiftUI

struct SpeechRecordingSettings: View {
    @ObservedObject var model: Controller

    var body: some View {
        Section("Speech") {
            Picker("Speech language", selection: $model.language) {
                ForEach(NexLanguage.allCases) { Text($0.title).tag($0) }
            }
            QuestionChimeVolumeSetting()
            Toggle("Live Conversation", isOn: $model.liveVoice)
            if model.liveVoice {
                Text("Talk and interrupt naturally while connected to Excalibur. Recording can continue during the conversation.")
                    .foregroundStyle(.secondary)
            }
            Toggle("On-device transcription fallback", isOn: $model.useDeviceTranscription)
            if model.useDeviceTranscription {
                Text("Choose English or Turkish for Apple’s offline recognizer. Excalibur uses Whisper when configured.")
                    .foregroundStyle(.secondary)
            }
            Toggle("Respond without a wake word", isOn: $model.automaticVoice)
            if model.automaticVoice {
                if model.liveVoice {
                    Toggle("Continuous listening while recording", isOn: $model.continuousListening)
                }
                Text(model.continuousListening && model.liveVoice
                     ? "While recording, microphone audio streams to Excalibur so Nex can hear longer requests and decide when to answer. This uses about 115 MB per hour before network overhead."
                     : "Nex checks short speech clips for questions while Live Conversation is enabled.")
                    .foregroundStyle(.secondary)
            } else {
                Toggle("Listen for “Hey Nex” questions", isOn: $model.handsFree)
                if model.handsFree {
                    Text("While recording, say “Hey Nex” followed by a question. Voice clips are processed locally when Excalibur is reachable.")
                        .foregroundStyle(.secondary)
                }
            }
        }
    }
}

private struct QuestionChimeVolumeSetting: View {
    @AppStorage(QuestionChime.volumeKey) private var volume = QuestionChime.defaultVolume

    var body: some View {
        let title: LocalizedStringKey = "Acknowledgement chime volume"
        VStack(alignment: .leading) {
            LabeledContent(title) {
                Text(volume, format: .percent.precision(.fractionLength(0)))
            }
            .accessibilityHidden(true)
            Slider(value: $volume, in: 0...1, step: 0.01) {
                Text(title)
            }
            .labelsHidden()
        }
    }
}
