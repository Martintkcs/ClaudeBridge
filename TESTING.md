# Tester Guide

Use this guide when you send Claude Bridge to a teammate for testing.

## Recommended Test Model

Each tester should run Claude Bridge on their own computer.

Do not share your own `config.json`, token, SQLite database, uploads folder, or Tailscale device URL with other people.

Each tester needs:

- their own Claude Code / Claude CLI installation
- their own authenticated Claude account
- their own local Claude sessions
- optional Tailscale account/device for mobile testing

## Before Testing

Check these first:

1. Claude Code works from a terminal:

```bash
claude --version
```

2. Python works:

```bash
python --version
```

or on macOS:

```bash
python3 --version
```

3. The repository is cloned or downloaded.

4. The tester is inside the Claude Bridge folder.

## Start Locally

Windows:

```powershell
.\ClaudeBridge.cmd
```

macOS:

```bash
bash ./ClaudeBridge.sh
```

Then open:

```text
http://127.0.0.1:8765/
```

Copy the token printed in the terminal into the login screen.

## Test Checklist

Run through these checks:

- The login screen accepts the token.
- Sessions appear in the left sidebar.
- Sessions are grouped by project.
- Opening a session shows recent messages.
- The chat scrolls to the latest message when first opened.
- The selected session automatically fills model and permission mode when available.
- Sending a simple message works.
- Scheduling a message works.
- Scheduled messages can be deleted.
- File attachment works.
- Image attachment preview works.
- Clicking an image opens the large preview.
- Pasting a screenshot from the clipboard works on desktop.
- The Stop button appears for running jobs.
- Mobile layout works through Tailscale.

## Mobile Test With Tailscale

Use the detailed setup in [TAILSCALE.md](TAILSCALE.md).

Minimum checks:

1. Computer is connected in Tailscale.
2. Phone is connected in the same tailnet.
3. The launcher configured Tailscale Serve.
4. The phone opens:

```text
http://YOUR-MACHINE.YOUR-TAILNET.ts.net/
```

Use `http://`, not `https://`, unless you later configure HTTPS separately.

## What To Report

Ask testers to report:

- operating system and version
- browser and device
- Claude Code version
- whether they used local URL or Tailscale URL
- screenshot of the error
- terminal output from the Claude Bridge launcher
- the session title/project where the problem happened

Do not ask testers to send their token or `config.json`.

## Known Limitations

- This is a private Tailscale MVP, not a public internet service.
- Authentication is currently a single local token.
- There are no per-user accounts yet.
- Broad Claude permission modes can allow shell commands and file edits.
- Claude Bridge uses Claude CLI locally; it does not type into the Claude Desktop UI.
- Some Claude CLI internals may change over time, especially session metadata fields.
