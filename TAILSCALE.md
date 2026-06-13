# Tailscale Setup Guide

This guide explains how to make Claude Bridge reachable from your phone or another trusted device without opening a public router port.

The recommended setup is:

- Claude Bridge runs on your computer.
- Tailscale creates a private VPN between your computer and phone.
- Tailscale Serve exposes Claude Bridge inside that private VPN.
- Claude Bridge still requires its own login token.

Do not expose Claude Bridge directly to the public internet.

## 1. Install Tailscale On The Computer

Install Tailscale on the computer where Claude Code and Claude Bridge run.

### Windows

Recommended install:

```powershell
winget install --id Tailscale.Tailscale -e
```

You can also download it from:

```text
https://tailscale.com/download/windows
```

After installation, open Tailscale and log in with your Tailscale account.

### macOS

Install Tailscale from the official macOS download page or the Mac App Store:

```text
https://tailscale.com/download/mac
```

The official macOS download page says macOS Monterey 12.0 or later is required.

After installation:

1. Open the Tailscale app.
2. Log in with your Tailscale account.
3. Make sure the Mac shows as connected.
4. Make sure the `tailscale` command is available in Terminal.

Check the CLI:

```bash
tailscale status
```

If Terminal says `command not found: tailscale`, install or enable the Tailscale CLI first. Claude Bridge can still run locally, but it cannot configure Tailscale Serve automatically until the `tailscale` command works.

## 2. Install Tailscale On The Phone

Install the Tailscale app from the iOS App Store or Google Play.

Log in with the same Tailscale account.

In the Tailscale app, both devices should appear as connected:

- your Windows computer or Mac
- your phone

## 3. Check The Computer Address

Open the Tailscale app or admin page and select your computer.

You should see two useful addresses:

- MagicDNS name, for example:

```text
desktop-name.tailnet-name.ts.net
```

- Tailscale IPv4 address, for example:

```text
100.x.y.z
```

For Claude Bridge, the cleanest URL is usually the MagicDNS name:

```text
http://desktop-name.tailnet-name.ts.net/
```

## 4. Start Claude Bridge

In the Claude Bridge folder, run:

### Windows

```powershell
.\ClaudeBridge.cmd
```

### macOS

```bash
bash ./ClaudeBridge.sh
```

This one launcher tries to do all daily startup steps:

- creates `config.json` if needed
- prints the login token
- checks Tailscale where possible
- configures Tailscale Serve for Claude Bridge
- starts the local web app on port `8765`

What the launcher does not do:

- install Tailscale
- create a Tailscale account
- log in to your tailnet
- bypass OS permission prompts
- expose the app publicly

Local browser URL:

```text
http://127.0.0.1:8765/
```

Phone URL through Tailscale:

```text
http://YOUR-MACHINE.YOUR-TAILNET.ts.net/
```

Replace `YOUR-MACHINE.YOUR-TAILNET.ts.net` with the MagicDNS name shown in Tailscale.

## 5. If Tailscale Serve Needs Manual Setup

Sometimes the app can run locally, but Tailscale Serve configuration needs a manual one-time setup.

### Windows

If `ClaudeBridge.cmd` prints an Access denied message for Tailscale Serve, run this once from an Administrator PowerShell:

```powershell
.\setup-tailscale-serve.cmd
```

After this succeeds, daily startup is still:

```powershell
.\ClaudeBridge.cmd
```

### macOS

If `ClaudeBridge.sh` cannot configure Tailscale Serve, first check that the CLI works:

```bash
tailscale status
```

Then run the one-time setup:

```bash
bash ./setup-tailscale-serve.sh
```

Or run the Tailscale command directly:

```bash
tailscale serve --yes --http 80 --bg 8765
```

After this succeeds, daily startup is still:

```bash
bash ./ClaudeBridge.sh
```

## 6. Log In From The Phone

Open the Tailscale URL on your phone:

```text
http://YOUR-MACHINE.YOUR-TAILNET.ts.net/
```

Claude Bridge will ask for a token.

The token is printed in the terminal when the app starts. It is also stored in:

```text
config.json
```

Enter the token once. The browser stores it locally and sends it in a request header, so it does not stay visible in the URL.

## 7. Daily Use

Normal daily flow:

1. Make sure the computer is awake and online.
2. Make sure Tailscale is connected on the computer.
3. Run the launcher.

Windows:

```powershell
.\ClaudeBridge.cmd
```

macOS:

```bash
bash ./ClaudeBridge.sh
```

4. Open the Tailscale URL from your phone.
5. Choose a Claude session and send a message.

If Windows or macOS starts Tailscale automatically, you do not need to manually open Tailscale every time.

## 8. Troubleshooting

### The local URL works, but the phone URL does not

Check these:

- Tailscale is connected on the computer.
- Tailscale is connected on the phone.
- Both devices are logged into the same tailnet.
- The phone is opening `http://...`, not `https://...`.
- `ClaudeBridge.cmd` or `ClaudeBridge.sh` is still running.
- Tailscale Serve was configured successfully.

If needed, run once as Administrator:

```powershell
.\setup-tailscale-serve.cmd
```

On macOS, run:

```bash
bash ./setup-tailscale-serve.sh
```

### Safari keeps loading forever

Try these:

- disconnect and reconnect Tailscale on the phone
- disconnect and reconnect Tailscale on Windows
- restart Claude Bridge with `Ctrl+C`, then `.\ClaudeBridge.cmd` on Windows or `bash ./ClaudeBridge.sh` on macOS
- check that the phone URL uses the computer MagicDNS name

### Access denied while setting up Tailscale Serve

Run PowerShell as Administrator, then:

```powershell
.\setup-tailscale-serve.cmd
```

### The token screen appears again

Copy the current token from the terminal or from `config.json`.

If `config.json` was deleted, Claude Bridge generated a new token.

### I want a cleaner URL

Rename the computer in the Tailscale admin console. The MagicDNS URL will use that machine name.

Example:

```text
http://claude-bridge.tailnet-name.ts.net/
```

## 9. Security Notes

Treat Claude Bridge access as sensitive.

Anyone who can access Claude Bridge and knows the token can send prompts to Claude Code on that computer.

Recommended rules:

- use Tailscale only
- do not forward router ports
- do not expose it directly to the public internet
- do not commit `config.json`
- share access only with trusted people
- be careful with broad Claude permissions such as `bypassPermissions` or `Bash(*)`

For team or public production use, add per-user authentication, audit logs, rate limiting, token rotation, and stricter permission policies.
