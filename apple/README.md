# Nex for Apple devices

Build the SwiftUI clients from this directory. The Apple source archive includes the Xcode project, the app targets, and the local Swift package. An independently installed Nex Linux server is needed to pair and use the shared workspace.

## Build

Use Xcode 27 with the macOS, iOS, and watchOS SDKs installed. The target minimums are macOS 14, iOS 17, and watchOS 10. Some features require newer OS versions and supported hardware.

```bash
open Nex.xcodeproj
```

Select **Nex macOS** to run on your Mac or **Nex iOS** for an iPhone. Select **Nex Watch** to build or debug the companion on a paired Watch. The local Swift package downloads checksum-pinned Sherpa-ONNX and ONNX Runtime frameworks during resolution. An internet connection is needed for a clean build.

For a compile check without device signing, run:

```bash
xcodebuild -project Nex.xcodeproj -scheme 'Nex macOS' \
  -destination 'generic/platform=macOS' -derivedDataPath .build/mac CODE_SIGNING_ALLOWED=NO build
```

Run with **Nex iOS** and `generic/platform=iOS`, or **Nex Watch** and `generic/platform=watchOS`, to check those targets.

## Sign and pair

1. In Xcode, set the project-level user-defined build setting `NEX_BUNDLE_ID` to your own identifier, such as `org.example.nex`, for both Debug and Release. The Watch target derives its identifier by appending `.watchkitapp` and automatically references the same iPhone identifier.
2. Select your team under **Signing & Capabilities** for **Nex iOS** and **Nex Watch** before running on physical devices. Configure push with a matching App ID if you use the entitlement. For development without push, clear the iOS target’s `NEX_IOS_ENTITLEMENTS` setting and remove the Push Notifications capability if Xcode asserts it for the selected profile.
3. Connect your Mac and iPhone to the server’s tailnet. On each device, open Nex and import the corresponding profile in **Settings → Excalibur → Open pairing file**. Complete Touch ID or Face ID registration. The bundled transport policy covers Tailscale’s IPv4 range and the app verifies the specific server against its pairing fingerprint. [Apple’s ATS configuration guide](https://developer.apple.com/documentation/BundleResources/Information-Property-List/NSAppTransportSecurity/NSExceptionDomains)
4. Install or open the companion on the paired Apple Watch. It relays its workspace and recording requests through the iPhone. Allow microphone access and notifications when using those features.
5. Test a short Chat turn and recording from each paired device with a server-side language model configured.

## Optional local wake model

Run `./fetch-wake-model.sh` before building if you want local **Hey Nex** detection on Mac and iPhone. The script downloads the pinned upstream keyword-spotting model and verifies its checksum before copying the necessary files. The normal build works without the weights. Manual voice actions are available when the server is configured. See the [wake model provenance](NexVoiceCapture/Sources/NexVoiceCapture/Models/SOURCE.md) for upstream details.

Keep Apple signing profiles, server pairing files, and downloaded model weights in local storage. The source release contains the buildable project and its setup materials.
