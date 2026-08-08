# Security

Share-It is local-first: session data never leaves your machine until you press share.

- **Report a vulnerability:** open a private security advisory on this repo
  (Security → Advisories → Report a vulnerability), not a public issue.
- **Never commit** `default_config.json` (holds the hosted upload token) — it is gitignored.
- The Cloudflare worker's `SHARE_TOKEN` is a server-side secret set via
  `wrangler secret put`, never in source.
- Shares are unguessable bearer URLs with configurable expiry; treat them as public.
