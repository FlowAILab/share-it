# share-it — PRD (v2, shipped)

## One-liner
A Raycast-style Mac palette (⌥S) that lists every Claude Code / Cowork / Codex session on
your machine and turns any of them — or any file an agent wrote during them — into a
shareable link in one click. Links serve raw markdown so "paste it into an LLM" just works.

## Problem
Agent sessions are trapped in `~/.claude/projects` and `~/.codex/sessions`. There's no
unified browser and no way to hand a session to a person or another LLM short of exporting
files. Hosted share features (Claude artifacts, ChatGPT links) have burned users: public
indexing, no expiry, no revoke. Codex has no sharing at all.

## Product principles
1. **Local-first, explicit egress** — server binds 127.0.0.1; nothing uploads until Share.
2. **Safe by default** — redaction on (pattern-based, labeled best-effort), unguessable
   URLs, revocable, expiry options; unredacted is an explicit warned choice.
3. **LLM-readable output** — links serve raw markdown with an untrusted-content preamble.
4. **One click** — global sticky options; share button on every row; link auto-copied;
   repeat shares of identical content+options reuse the cached link instantly.
5. **Minimal, monochrome, native** — vibrancy glass panel, no chrome, keyboard-first.

## Shipped (v0.2)
- Unified index: Claude Code (CLI/desktop/IDE), Cowork-tagged sessions, Codex
  (CLI+Desktop via state_*.sqlite + rollout scan incl. archived), subagents toggleable.
  Cached (`~/.shareit/index.json`, schema-versioned), refreshes on focus.
- Peek: first messages + files-the-agent-wrote (artifacts), inline per row.
- Share: session→markdown (full/messages-only, thinking opt-in, redaction) or artifact→raw
  file. Providers: S3 short public link (default, `p/<token>`, expiry via tag-based
  lifecycle: ∞/1d/3d/7d) → S3 presigned (≤7d, pre-public-setup fallback) → dpaste (≤1MB)
- Shares drawer: provider, time left, copy, delete-now (S3).
- Native shell: Swift NSPanel + WKWebView, ⌥S Carbon hotkey, health-checked backend
  launch with Retry/Quit alert. `macos/build.sh` → ~/Applications/share-it.app.
- Tests: hermetic unit fixtures + real-store smoke. MIT licensed.

## Known simplifications (accepted)
- Redaction is regex-shaped; the peek + preview is the human control.
- Share links are bearer URLs; anyone with the link reads until expiry/delete.
- Tag-lifecycle expiry granularity is ~1 day; presign fallback caps at 7d.
- v.gd shortener only in the presign fallback path, best-effort (long URLs often fail).

## Roadmap (researched Aug 2026 — merged Claude + Codex + market evidence)
See README "Roadmap"; headline candidates: full-text search across sessions, 30-day-purge
rescue archive, share cards (stats block), per-session cost analytics, resume-from-link
bundles, reader mode for shared pages.
