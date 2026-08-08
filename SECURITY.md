# Security

Share-It is local-first: session data never leaves your machine until you press share.

- **Report a vulnerability:** open a private security advisory on this repo
  (Security → Advisories → Report a vulnerability), not a public issue.
- **Never commit** `default_config.json` (holds the hosted upload token) — it is gitignored.
- The Cloudflare worker's `SHARE_TOKEN` is a server-side secret set via
  `wrangler secret put`, never in source.
- Shares are unguessable bearer URLs with configurable expiry; treat them as public.

## Local server

The backend binds to `127.0.0.1:8749`. `/api/*` requires a per-launch token written
to `~/.shareit/session_token` (mode `0600`) and injected into the app's own page — this
stops a *different* logged-in user on the same Mac from driving the API. A process running
as **you** can read that file, but such a process already has your filesystem access, so
this is not a new exposure.

## Known limitations (by design, pre-1.0)

- **Shared hosted token.** The DMG bundles one upload token for the hosted worker, so
  anyone who extracts it can upload content or delete a share URL they know. Abuse is
  bounded by a 25 MB/object, 100 MB/bundle, 64-object cap and expiry. Per-install
  capability credentials are the planned fix. Self-hosters should set their own worker
  `SHARE_TOKEN` and `default_config.json`.
- **Native bridge trusts the loopback page.** The macOS shell loads whatever answers on
  8749; a process that squats the port before launch could serve the UI. Mitigated by the
  per-launch token (a squatter can't mint valid `/api` calls) but not fully closed until
  the shell spawns and pins its own backend.
- **Attached files ship as-is.** Secret scrubbing covers transcript *text*, not the bytes
  of files you attach — review the selected files before sharing.
