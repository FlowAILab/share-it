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
shell **generates** the token and a separate readiness nonce, passes both to the Python
backend it spawns (`SHAREIT_TOKEN`, `SHAREIT_READY` env vars), and injects the token into
its own WKWebView out-of-band (never over HTTP, so it can't be read from the page source).

The shell does **not** send the token to whatever answers on the port. Instead the backend
writes the readiness nonce to `~/.shareit/ready` (mode `0600`) **only after it successfully
binds the port**, and the shell waits for that exact nonce before loading the UI or enabling
the native bridge. A process that squats port 8749 first makes the real backend's bind fail,
so the ready nonce is never written and the app bails out rather than trusting the squatter —
and a *different* user can neither read the spawned process's env nor write the `0600` file.
The bridge additionally ignores messages from any frame whose origin isn't loopback. A token
file (`~/.shareit/session_token`, `0600`) is written for direct `python3 app.py` dev use,
where the page is injected the token in-process.

## Rendered content

User-supplied HTML/SVG artifacts — and the reader page itself — are served with
`Content-Security-Policy: sandbox`, rendering them in an opaque origin so no shared bundle
can execute script against the worker domain. `X-Content-Type-Options: nosniff` is always
set. Transcript text is treated as untrusted data, HTML-escaped at render time.

## Bundle deletion

Deleting a hosted **bundle** share (the default for every session share) requires a
per-share `delKey` returned only to the creator at upload time — the fleet upload token
alone cannot delete someone else's bundle. Legacy single-file `/p/` shares (older links and
the standalone "share one file" action) are still token-gated only; they are being migrated
to bundles.

## Known limitations (pre-1.0)

- **Shared hosted upload token.** The DMG bundles one upload token for the hosted worker.
  Anyone who extracts it can upload content (bounded by 25 MB/object, 100 MB/bundle,
  64-object, and expiry caps) — but not delete others' shares (see above) and not script
  the worker origin (sandboxed). Per-install capability credentials are the planned fix;
  self-hosters set their own worker `SHARE_TOKEN` + `default_config.json`.
- **Attached files ship as-is.** Secret scrubbing covers transcript *text*, not the bytes
  of files you attach — review the selected files (shown as named chips) before sharing.
