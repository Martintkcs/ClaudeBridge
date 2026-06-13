# Claude Bridge

Claude Bridge is a lightweight local web app for controlling Claude Code / Claude CLI sessions from desktop or mobile.

It is designed for people who want a simple Codex-like remote control surface for their own Claude Code sessions: select a session, send a message, attach files, schedule a prompt, and watch the result from a browser.

## What It Does

- Lists local Claude Code sessions automatically.
- Groups sessions by project folder, similar to Codex.
- Sends messages to an existing Claude session or starts a new non-interactive run.
- Supports scheduled messages.
- Shows Claude transcript history in the browser.
- Supports file uploads and image previews.
- Lets you paste screenshots from the clipboard on desktop.
- Works on mobile through Tailscale.
- Supports model selection: default, Sonnet, Opus.
- Supports Claude permission modes and allowed tools.
- Streams run events into a small timeline.
- Provides a Stop button for running jobs.

## How It Works

Claude Bridge does not type into the Claude Desktop window. It runs Claude Code locally through the Claude CLI:

```powershell
claude -p "your message" --resume SESSION_ID
```

The app reads local Claude session metadata and transcript files, then presents them in a mobile-friendly web UI.

## Quick Start

Requirements:

- Windows or macOS
- Python 3.10+
- Claude Code / Claude CLI installed and authenticated
- Tailscale for remote mobile access

For teammate testing, each tester should run Claude Bridge on their own computer with their own Claude Code login. Do not share your local `config.json`, token, database, or uploads folder. See [TESTING.md](TESTING.md).

Start on Windows:

```powershell
cd "<path-to-ClaudeBridge>"
.\ClaudeBridge.cmd
```

Start on macOS:

```bash
cd "<path-to-ClaudeBridge>"
bash ./ClaudeBridge.sh
```

Open locally:

```text
http://127.0.0.1:8765/
```

On first launch, Claude Bridge creates `config.json`. That file contains the login token. The token is ignored by Git and should never be committed.

## Mobile Access

Recommended setup is Tailscale Serve. For a full Windows and macOS step-by-step guide, see [TAILSCALE.md](TAILSCALE.md).

On Windows:

```powershell
.\ClaudeBridge.cmd
```

On macOS:

```bash
bash ./ClaudeBridge.sh
```

Then open the Tailscale URL on your phone:

```text
http://YOUR-MACHINE.YOUR-TAILNET.ts.net/
```

Replace `YOUR-MACHINE` and `YOUR-TAILNET` with your own Tailscale machine name and tailnet name.

The token is entered once in the login screen. It is then stored in the browser and sent via the `X-Claude-Bridge-Token` header, so it does not stay visible in the URL.

If Tailscale Serve cannot be configured from a normal terminal, run this once from an Administrator PowerShell:

```powershell
.\setup-tailscale-serve.cmd
```

After that, the normal daily startup command is still:

```powershell
.\ClaudeBridge.cmd
```

Daily usage is intentionally simple: start `ClaudeBridge.cmd` on Windows or `ClaudeBridge.sh` on macOS, then open your Tailscale MagicDNS URL from the phone.

## Testing With Teammates

Send teammates:

- the GitHub repository link
- [README.md](README.md)
- [TESTING.md](TESTING.md)
- [TAILSCALE.md](TAILSCALE.md)

Each tester should create their own local token by starting the app. They should not reuse your token.

## Permissions And Tools

Claude Bridge can pass these options to Claude CLI:

- `--model`
- `--permission-mode`
- `--allowedTools`

For code execution or file edits, choose a permission mode and allowed tools, for example:

```text
Read Write Edit Bash(git *) Bash(python *)
```

Be careful with broad permissions like `bypassPermissions` or `Bash(*)`. Use them only in trusted local projects.

## Local Data

These files are local-only and ignored by Git:

- `config.json`
- `claude_bridge.sqlite3`
- `uploads/`
- logs and Python cache files

## Security Status

Claude Bridge is suitable as a private Tailscale-only MVP. It is not yet ready to expose directly to the public internet.

Before public production use, add:

- per-user authentication
- rate limiting
- audit logs
- token rotation
- workspace allowlists
- stricter tool permission policies
- HTTPS or an authenticated reverse proxy

See [SECURITY_REVIEW.md](SECURITY_REVIEW.md) for details.

## GitHub Pages Landing Page

A static landing page is included in:

```text
docs/index.html
```

To enable it on GitHub:

1. Open repository Settings.
2. Go to Pages.
3. Set source to `Deploy from a branch`.
4. Choose branch `main` and folder `/docs`.
5. Save.

## License

Private prototype unless you add a license file.
