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

Initialization generates the loopback console key. Running it again preserves the existing access file and storage limits. The archive service creates its SQLite schemas and working directories when it starts.

## Create the first owner account

The Nex service logs the path to its private `setup.sock` when it starts. On your Mac, use SSH to open the setup page. Replace the socket path with the one from the service output and connect as the user running Nex. The example uses a systemd user service with UID 1000.

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:8768:/run/user/1000/nex/setup.sock user@your-linux-host
```

1. Open <http://127.0.0.1:8768> and click **Getting Started**.
2. Choose a username and password, then click **Create Owner Account**.
3. Click **Copy Server Link**. On a Mac or iPhone connected to the server’s tailnet, open Nex, paste the link, and sign in with the account you just created. Confirm with Touch ID or Face ID. You can reuse this link on other devices and close the SSH tunnel when setup is complete.

The same setup page shows the reusable server link after the owner has been created. The first owner uses the archive already on the server. If you already have an account, use your existing credentials.

## Open the admin console

From the client Mac, open an SSH tunnel to the server’s loopback console. Replace the SSH target with your user and server address.

```bash
ssh -N -L 127.0.0.1:8766:127.0.0.1:8766 user@your-linux-host
```

Open <http://127.0.0.1:8766> while that tunnel runs. On the server, run `python3 -m nex --data "$NEX_DATA" admin-key` from the server source directory to get the console key. Keep it private.

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

Ask Nex to create a PDF in Chat, or include the PDF format and layout in a goal's instructions. The Mac
and iPhone apps can open, save, and share the result from **Library → Files**. A PDF produced by or attached to a
goal also appears in that goal's **Artifacts**. The renderer provides single-column reports and two-column
newspapers with embedded fonts for English and Turkish. It checks page geometry, text, and rasterized output
before publishing a file.

## Manage accounts and devices

Open **Settings → Nex Account** to share the reusable server link. Use **Manage Users** to add other accounts. Give each user their own credentials through a trusted channel.

**Nex Account** lets users rename themselves, change their password and sign out other devices. The owner can appoint administrators. Administrators can create and rename member accounts, reset member passwords, disable or restore access, and set audio limits. Disabling an account signs its devices out and preserves its archive. Additional accounts keep separate archives under `$NEX_DATA/users/` and use server-side models. The host console continues to manage the original archive and shared model configuration.

Use **Settings → Nex Account → Reset My Data** and confirm with your current password to permanently erase your personal server archive and saved connections. Nex clears your pending recording queue on the Mac or iPhone that submitted the reset and signs this account out on every device. The archive and live voice services briefly restart. Sign back in with the same account to open the empty workspace. Queued recordings from before the reset stay out of the new workspace.

An account can have one active Mac session and one active iPhone session. Signing in on another device of the same type revokes the previous session. Device sessions expire after 90 days. Apple Watch follows its iPhone's account. The service user can also manage accounts from the Linux terminal for host-side setup and recovery:

```bash
python3 -m nex --data "$NEX_DATA" account create alice --owner
python3 -m nex --data "$NEX_DATA" account server-link
python3 -m nex --data "$NEX_DATA" account list
python3 -m nex --data "$NEX_DATA" account reset-password alice
```

As an alternative to browser setup, run `python3 -m nex --data "$NEX_DATA" account setup-link` as the server user. That private, single-use link lets you create the owner directly in the Nex app and expires after 30 minutes.

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

- Back up the entire private archive with Nex stopped or take a consistent filesystem snapshot. Include any externally configured SSD audio directory. Include `accounts.sqlite` and the account archives under `users/`. Keep backups and account credentials outside the source checkout.
- The server uses a pinned self-signed certificate for the current tailnet IP. If the certificate expires or the IP changes, provision the current identity, restart both services, and give users a new server link. Drain device queues before a planned certificate change. For same-IP renewal, stop both services and move the old identity directory under `tailnet-tls/` to a private backup before running `provision-tailnet`.
- Check that the console opens through SSH, sign in on a client, test Chat, and upload a short recording. Enable Live Conversation separately and try it on a physical device.
- Run `python3.12 -m pip install -r requirements-test.txt` in a development virtual environment and `python3.12 -m unittest discover -s tests -v` from this directory. Tests that require Bubblewrap or other Linux-specific facilities can skip on macOS.
