# Optional services and integrations

## Live voice

Start the archive service first. The staged x86-64 Linux configuration installs pinned CPU speech dependencies, accepts `NEX_STAGE_VULKAN=1` for an optional Vulkan llama.cpp runtime, and downloads the upstream models into your private archive.

Run from the server directory using the archive and source paths in the [server setup](../README.md).

```bash
export NEX_DATA="$HOME/.local/share/nex-data"
bash deploy/stage-live-assets.sh
install -m 644 deploy/nex-live.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now nex-live.service
journalctl --user -u nex-live.service -n 30
```

The recipe uses Python 3.12, a pinned PyTorch CPU wheel, and several gigabytes of downloaded assets. On other Linux architectures supply compatible dependencies and model runtimes before starting the service. Set `NEX_LIVE_DEVICE=cpu` in the unit if you installed Vulkan and want to force CPU inference. `bash deploy/stage-gemma.sh` stages the second optional language model using the same `NEX_DATA`.

The voice service shares the archive’s pairing credentials and certificate on TCP 8767. It uses Nemotron with Sherpa-ONNX for streaming recognition, Silero and Smart Turn for turn detection, Kokoro for English speech, and EMA for Turkish speech. The model supervisor serves language requests on a private Unix socket. The archive still needs FFmpeg and Whisper for background transcription.

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

Use real Apple IDs and the iOS bundle identifier you built. Use `production` for a production APNs entitlement and build setting. Give both files mode `600` and the directory mode `700`. Restart the archive service and enable notifications on the paired device.
