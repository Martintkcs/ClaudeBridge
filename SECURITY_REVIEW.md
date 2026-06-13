# Claude Bridge Security Review

## Current Status

Claude Bridge is acceptable as a private MVP when used through Tailscale.

It is not yet production-ready for direct public internet exposure.

## What Is Already In Place

- Token is not kept in the URL.
- API requests use `X-Claude-Bridge-Token`.
- Local secrets are ignored by Git:
  - `config.json`
  - `claude_bridge.sqlite3`
  - `uploads/`
- Uploads are constrained to the local `uploads/` folder.
- File size limit is enforced.
- Tailscale Serve can expose the app privately without router port forwarding.
- Basic HTTP security headers are set:
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: no-referrer`
  - `X-Frame-Options: DENY`
  - `Cache-Control: no-store`
  - basic Content Security Policy

## Main Risks

- A shared token is not enough for a public multi-user deployment.
- Broad Claude tool permissions can allow shell commands and file edits.
- `bypassPermissions` is dangerous outside a trusted local environment.
- Uploaded files are not scanned.
- There is no per-user audit trail yet.
- There is no rate limiting for bad token attempts.

## Before Production

Add:

- per-user authentication
- per-user tokens or SSO
- rate limiting
- audit logs
- token rotation and revocation
- workspace allowlists
- session allowlists
- stricter allowed-tools presets
- approval broker for shell commands and file edits
- HTTPS
- automated service startup
- log rotation

## Deployment Recommendation

For now:

```text
Tailscale-only access + trusted users + local machine
```

Avoid:

```text
public router port forward + shared token + broad tool permissions
```
