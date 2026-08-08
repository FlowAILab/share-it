# Security

Share-It is local-first: session data never leaves your machine until you press share.

- **Report a vulnerability:** open a private security advisory on this repo
  (Security → Advisories → Report a vulnerability), not a public issue.
- **Never commit** `default_config.json` (holds the hosted upload token) — it is gitignored.
- The Cloudflare worker's `SHARE_TOKEN` is a server-side secret set via
  `wrangler secret put`, never in source.
- Shares are unguessable bearer URLs with configurable expiry; treat them as public.

## Local server

The backend binds to `127.0.0.1:8749`. `/api/*` requires a per-launch token. The macOS
shell **generates** the token, passes it to the Python backend it spawns via the
`SHAREIT_TOKEN` environment variable, and injects it into its own WKWebView out-of-band
(never over HTTP, so it can't be read from the page source). The shell then calls
`/api/verify` with that token and only loads the UI / enables the native bridge if the
server proves it holds the same token — so a process that squats port 8749 before launch
cannot impersonate the backend. The bridge also ignores messages from any frame that isn't
loopback. A token file (`~/.shareit/session_token`, mode `0600`) is written for dev/CLI use.

## Rendered content

User-supplied HTML/SVG artifacts — and the reader page itself — are served with
`Content-Security-Policy: sandbox`, rendering them in an opaque origin so no shared bundle
can execute script against the worker domain. `X-Content-Type-Options: nosniff` is always
set. Transcript text is treated as untrusted data, HTML-escaped at render time.

## Bundle deletion

Deleting a hosted share requires a per-share `delKey` returned only to the creator at
upload time — possession of the fleet upload token is **not** sufficient to delete someone
else's link.

## Known limitations (pre-1.0)

- **Shared hosted upload token.** The DMG bundles one upload token for the hosted worker.
  Anyone who extracts it can upload content (bounded by 25 MB/object, 100 MB/bundle,
  64-object, and expiry caps) — but not delete others' shares (see above) and not script
  the worker origin (sandboxed). Per-install capability credentials are the planned fix;
  self-hosters set their own worker `SHARE_TOKEN` + `default_config.json`.
- **Attached files ship as-is.** Secret scrubbing covers transcript *text*, not the bytes
  of files you attach — review the selected files (shown as named chips) before sharing.
