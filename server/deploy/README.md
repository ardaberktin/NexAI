# Optional services and integrations

## Live voice

Start the archive service first. The staged x86-64 Linux configuration installs pinned CPU speech dependencies, accepts `NEX_STAGE_VULKAN=1` for an optional Vulkan llama.cpp runtime, and downloads the upstream models into your private archive.

Run from the server directory using the archive and source paths in the [server setup](../README.md). The startup override requires systemd 254 or later, including Ubuntu 24.04.

```bash
export NEX_DATA="$HOME/.local/share/nex-data"
bash deploy/stage-live-assets.sh
install -m 644 deploy/nex-live.service "$HOME/.config/systemd/user/"
install -D -m 644 deploy/nex.service.d/20-live-model-startup.conf \
  "$HOME/.config/systemd/user/nex.service.d/20-live-model-startup.conf"
systemctl --user daemon-reload
systemctl --user enable --now nex-live.service
journalctl --user -u nex-live.service -n 30
```

The recipe uses Python 3.12, a pinned PyTorch CPU wheel, and several gigabytes of downloaded assets. On other Linux architectures supply compatible dependencies and model runtimes before starting the service. Set `NEX_LIVE_DEVICE=cpu` in the unit if you installed Vulkan and want to force CPU inference. Gemma 3 4B serves Chat and voice on Excalibur. For an existing installation, run `bash deploy/stage-gemma.sh` with the same `NEX_DATA` before upgrading.

The override starts the live service with the archive and keeps its startup queued while the archive retries, such as when Tailscale is still starting at boot. Existing installations can install the override with the same `install -D` command and reload systemd.

If Gemma is unavailable, Nex reports a model failure while the archive stays accessible. Managed mode is recorded in `$NEX_DATA/live-model`. To retire the managed service, remove the startup override, reload systemd, disable and stop `nex-live.service`, and remove the marker to return to the archive's configured model route.

The voice service shares the archive’s account registry and certificate on TCP 8767. It uses Nemotron with Sherpa-ONNX for streaming recognition, Silero and Smart Turn for turn detection, Kokoro for English speech, and EMA for Turkish speech. The model supervisor serves language requests on a private Unix socket. The archive still needs FFmpeg and Whisper for background transcription.

## Accounts

From the server source directory, run either command and follow the printed browser instructions. If running over SSH, forward loopback port 8627 from the browser machine to the server during authorization.

```bash
python3 -m nex --data "$NEX_DATA" connect google --client /path/to/desktop-oauth-client.json
python3 -m nex --data "$NEX_DATA" connect ticktick
python3 -m nex --data "$NEX_DATA" connections
```

Google requires a Desktop OAuth client in a project with Gmail and Google Calendar enabled. TickTick setup prompts for your own app credentials. Keep provider credentials and OAuth files outside Git. Inference is local. Enabled web, weather, account, or notification tools contact external providers as needed.

## Apple push notifications

For remote iPhone goal and clock alerts, use a push-enabled iOS build signed with your team and a matching APNs provider key. Install `cryptography` in the Python environment running the archive service and use curl with HTTP/2 support. In the private archive’s `apns/` directory, store the key as `key.p8` and create `config.json`:

```json
{
  "environment": "development",
  "team_id": "YOURTEAMID",
  "key_id": "YOURKEYID",
  "topic": "org.example.nex"
}
```

Use real Apple IDs and the iOS bundle identifier you built. Use `production` for a production APNs entitlement and build setting. Give both files mode `600` and the directory mode `700`. Restart the archive service and enable notifications on your signed-in iPhone.
