# Nex Server

Nex Server is the Linux component for model-backed Chat, the shared audio archive, scheduled goals, and optional streaming voice. The server runs independently of the Apple source tree. See the [architecture](../docs/architecture.md) for the request flow.

## Requirements

- A Linux host, a non-root service account, Python 3.12 with SQLite FTS5, OpenSSL, and Bubblewrap. The production listeners use Linux interface binding and the model and Python sandboxes use Linux namespaces. A Linux VM can run the server on a Mac.
- Tailscale installed at `/usr/bin/tailscale`, joined to the same tailnet as your Apple devices, using the Linux `tailscale0` interface. If you use tailnet grants or ACLs, allow only users’ trusted devices to access TCP 8765 and, for live voice, 8767.
- Disk space for the archive and the models you choose. The base API runs with Python’s standard library. Staging local speech and language models requires additional runtimes.

For example, on Ubuntu 24.04 install `python3 python3-venv bubblewrap openssl ca-certificates curl` with apt, then install and connect [Tailscale](https://tailscale.com/kb/1031/install-linux). Verify that `tailscale ip -4` returns the server’s tailnet address.

## Start the archive API

Run these commands from the directory containing this README, whether you cloned the monorepo or unpacked the server asset.

```bash
export NEX_DATA="$HOME/.local/share/nex-data"
export NEX_RUNTIME="${XDG_RUNTIME_DIR:?Run this from a Linux user session}/nex"

python3 -m nex --data "$NEX_DATA" init --audio-quota-gib 48 --min-free-gib 2
python3 -m nex --data "$NEX_DATA" provision-tailnet
python3 -m nex --data "$NEX_DATA" serve --runtime "$NEX_RUNTIME"
```

Initialization generates stable keys for `admin`, `mac`, and `iphone`. Running it again preserves the existing access file and storage limits. The archive service creates its SQLite schemas and working directories when it starts.

From the client Mac, open an SSH tunnel to the server’s loopback console. Replace the SSH target with your user and server address.

```bash
ssh -N -L 127.0.0.1:8766:127.0.0.1:8766 user@your-linux-host
```

Open <http://127.0.0.1:8766> while that tunnel runs. On the server, run `python3 -m nex --data "$NEX_DATA" pair admin` from the server source directory to get the console key. Keep it private.

## Configure Chat and transcription

Use the console’s **Models** panel. Place the runtimes and weights under `$NEX_DATA/assets/` and select their relative paths. Install executables and any sibling shared libraries from the same compatible build. [Example asset layout](deploy/models.example.json)

| Feature | Assets |
| --- | --- |
| Chat, model-written notes, and scheduled model work | [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` and a compatible instruction-tuned GGUF, such as [Gemma 3 4B GGUF](https://huggingface.co/ggml-org/gemma-3-4b-it-qat-GGUF) |
| Archive transcription | Executable [FFmpeg](https://ffmpeg.org/) with AAC support, [whisper.cpp](https://github.com/ggml-org/whisper.cpp) `whisper-cli`, and a multilingual Whisper model such as `ggml-small.bin` |
| Recorded voice synthesis | Optional [Piper](https://github.com/OHF-Voice/piper1-gpl) executable with voice ONNX and matching JSON metadata |

Start with zero GPU layers for CPU inference. Keep the archive and search available while staging models. Chat needs a working model to reply, and transcription needs its own assets even if live speech is running.

## Enable PDF generation

Install the PDF dependencies in the Python environment that runs the archive service.

```bash
python3.12 -m pip install -r requirements-pdf.txt
```

If you use a virtual environment, point `ExecStart` in `nex.service` at that environment's Python and restart the
service after updating the server source. On a distribution that protects its system Python, use a virtual
environment with `--system-site-packages` if the service already uses system-installed packages. The archive
adds the PDF storage table on startup.

Ask Nex to create a PDF in Chat, or include the PDF format and layout in a goal's instructions. The paired Mac
and iPhone apps can open, save, and share the result from **Library → Files**. A PDF produced by or attached to a
goal also appears in that goal's **Artifacts**. The renderer provides single-column reports and two-column
newspapers with embedded fonts for English and Turkish. It checks page geometry, text, and rasterized output
before publishing a file.

## Create accounts and connect the Apple clients

Run account management on the host as the Nex service user. Passwords are entered at an interactive prompt.

```bash
python3 -m nex --data "$NEX_DATA" account create alice --owner
python3 -m nex --data "$NEX_DATA" account create bob
python3 -m nex --data "$NEX_DATA" account server-link
```

Give each user the server link and their username and password over a trusted channel. The same link works for every account and contains the Tailscale address and TLS certificate fingerprint. On Mac or iPhone, connect Tailscale, open Nex, enter the link and credentials, and register with Touch ID or Face ID.

`--owner` attaches the existing archive to exactly one account. Other accounts get separate archives under `$NEX_DATA/users/` and their own audio allowance, recording history, Chat, private requests, scheduled goals, and integrations. The host console continues to manage the original archive and shared model configuration. Additional accounts use server-side models and have no access to the owner’s Mac model relay.

**Upgrading a paired server.** Finish syncing the Mac, iPhone, and Watch queues before creating the owner account. Update both server services and all clients. Creating the owner account disables legacy Mac and iPhone credentials. Sign in again on both clients, then enable Apple Watch access again in iPhone settings. Any unsynced legacy recordings stay in their original local queues. The new account won't upload them automatically.

An account can have one active Mac session and one active iPhone session. Signing in on a replacement of the same type revokes the previous session. Device sessions expire after 90 days. The Watch uses its iPhone’s account. Sign-out revokes that device when the server can be reached. Administrators can revoke all devices with a password reset or disable an account while keeping its data.

```bash
python3 -m nex --data "$NEX_DATA" account list
python3 -m nex --data "$NEX_DATA" account reset-password bob
python3 -m nex --data "$NEX_DATA" account disable bob
python3 -m nex --data "$NEX_DATA" account enable bob
```

### Legacy pairing

Legacy profiles remain available to installations that have yet to assign the original archive to an account.

```bash
umask 077
python3 -m nex --data "$NEX_DATA" pair mac > "$HOME/Nex-Pairing-mac.json"
python3 -m nex --data "$NEX_DATA" pair iphone > "$HOME/Nex-Pairing-iphone.json"
```

Transfer each file privately to its intended device. Import it from **Legacy pairing → Open pairing file** and finish biometric registration. Export a fresh file when pairing another installation or replacing a device. Apple Watch connects through the paired iPhone.

## Run as user services

The included unit templates expect this source directory at `~/.local/share/nex-server/server` and your archive at `~/.local/share/nex-data`. Place the source there, or edit both templates for your paths and Python installation. Stop the foreground process before starting the unit. Use `sudo loginctl enable-linger "$(id -un)"` if the service should run after you log out.

```bash
install -d -m 700 "$HOME/.config/systemd/user"
install -m 644 deploy/nex.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now nex.service
journalctl --user -u nex.service -n 30
```

See [Live voice and optional integrations](deploy/README.md) for the second service and account configuration.

## Maintain and verify

- Back up the entire private archive with Nex stopped or take a consistent filesystem snapshot. Include any externally configured SSD audio directory. Include `accounts.sqlite` and the account archives under `users/`. Keep backups and pairing files outside the source checkout.
- The paired endpoint uses a pinned self-signed certificate for the current tailnet IP. If the certificate expires or the IP changes, provision the current identity, restart both services, and give users a new server link. Drain device queues before a planned certificate change. For same-IP renewal, stop both services and move the old identity directory under `tailnet-tls/` to a private backup before running `provision-tailnet`.
- Check that the console opens through SSH, sign in on a client, test Chat, and upload a short recording. Enable Live Conversation separately and try it on a physical device.
- Run `python3.12 -m pip install -r requirements-test.txt` in a development virtual environment and `python3.12 -m unittest discover -s tests -v` from this directory. Tests that require Bubblewrap or other Linux-specific facilities can skip on macOS.
