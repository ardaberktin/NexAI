#if os(macOS)
import CoreAudio
import Foundation
import IOKit

enum MacMicrophoneRoute {
    static var blockedByClosedLid: Bool {
        let root = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPMrootDomain"))
        guard root != IO_OBJECT_NULL else { return false }
        defer { IOObjectRelease(root) }
        let lid = IORegistryEntryCreateCFProperty(root, "AppleClamshellState" as CFString, kCFAllocatorDefault, 0)
        guard lid?.takeRetainedValue() as? Bool == true,
              let input = property(kAudioHardwarePropertyDefaultInputDevice, of: AudioObjectID(kAudioObjectSystemObject)) else {
            return false
        }
        return property(kAudioDevicePropertyTransportType, of: input) == kAudioDeviceTransportTypeBuiltIn
    }

    private static func property(_ selector: AudioObjectPropertySelector, of device: AudioObjectID) -> UInt32? {
        var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                                 mElement: kAudioObjectPropertyElementMain)
        var value: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        return AudioObjectGetPropertyData(device, &address, 0, nil, &size, &value) == noErr ? value : nil
    }
}
#endif
