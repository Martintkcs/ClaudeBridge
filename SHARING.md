# Sharing And Production Guide

## Recommended Sharing Model

For the current version, share Claude Bridge as source code on GitHub and run it privately through Tailscale.

Recommended setup:

- GitHub repository for the code.
- Tailscale for private access.
- `ClaudeBridge.cmd` as the Windows launcher.
- `ClaudeBridge.sh` as the macOS launcher.
- No public router port forwarding.
- One local Claude Bridge instance per trusted machine.

## What To Commit

Commit:

- `app.py`
- `README.md`
- `SHARING.md`
- `SECURITY_REVIEW.md`
- `TAILSCALE.md`
- `TESTING.md`
- `docs/index.html`
- `.gitignore`
- `*.cmd`
- `*.ps1`
- `*.sh`

Do not commit:

- `config.json`
- `claude_bridge.sqlite3`
- `uploads/`
- `__pycache__/`
- `*.log`

## GitHub Pages

The repository contains a landing page in:

```text
docs/index.html
```

Enable it in GitHub:

1. Repository Settings.
2. Pages.
3. Source: Deploy from a branch.
4. Branch: `main`.
5. Folder: `/docs`.

## Custom URL

With Tailscale, the private URL usually looks like:

```text
http://machine-name.tailnet-name.ts.net/
```

You can rename the machine in the Tailscale admin console to get a cleaner URL.

For detailed setup steps, see [TAILSCALE.md](TAILSCALE.md).

For a real public custom domain, use Cloudflare Tunnel or another authenticated reverse proxy. Do not expose Claude Bridge directly to the internet with only a shared token.

## Production Checklist

Before public or team-wide production use:

- Add per-user accounts or per-user tokens.
- Add rate limiting.
- Add audit logging.
- Add token rotation and revocation.
- Add workspace/session allowlists.
- Restrict dangerous permission modes.
- Add a stronger approval broker for shell commands and file edits.
- Run behind HTTPS.
- Consider Cloudflare Access, Tailscale, or another identity-aware proxy.

## Important Limitation

Claude Bridge uses the Claude CLI on the machine where it runs. Whoever can access the Bridge may be able to act through that machine's Claude Code environment. Treat access as sensitive.

## Teammate Testing Checklist

Before sending it to teammates:

- Make sure `README.md`, `TESTING.md`, `TAILSCALE.md`, and `SECURITY_REVIEW.md` are committed.
- Make sure `config.json`, `claude_bridge.sqlite3`, and `uploads/` are not committed.
- Tell testers to run their own local instance.
- Tell testers not to share tokens in screenshots or bug reports.
- Ask testers to include OS, browser, Claude Code version, and terminal output when reporting issues.
