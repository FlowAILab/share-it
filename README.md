<h1 align="center">Share-It</h1>

<p align="center">
  <b>Get a shareable link to any AI-coding session — in one click.</b><br>
  <sub>Your whole session, at a URL. Works with Claude Code · Codex · Cursor · Cowork.</sub>
</p>

<p align="center">
  <a href="https://github.com/FlowAILab/share-it/releases/latest/download/Share-It.dmg">
    <b>⬇︎  Download for macOS</b>
  </a>
  &nbsp;·&nbsp; Drag to Applications, press <b>⌥S</b>. No setup, no account.
</p>
<p align="center">
  <sub>First launch: right-click the app → <b>Open</b> (not yet notarized — signed builds coming).</sub>
</p>

<p align="center"><sub>▶ demo video coming soon</sub></p>

---

Press **⌥S**, find your session — that's it. The link holds the **complete session**:
the whole conversation and the files it produced. Send it to a teammate, or paste it
into another agent to pick up right where you left off. ✨

## Core

- **🔗 Share a link** — press **⏎**. A short URL to the full session lands on your clipboard.
- **📋 Copy the session** — press **⌘C**. The whole conversation as a file, no upload.
- **📎 Copy / share the files** — the artifacts a session made (PDFs, images, code)
  straight to Finder, Slack, or email — real files, not screenshots.

## Also handy

- **🧠 Deep mode** — bundle the reasoning, full tool output, and *every* file the session
  touched, so another agent can truly continue the work.
- **📖 Reader page** — a clean, human-friendly web page instead of raw markdown.
- **🔎 Full-text search** — find any session by what was actually said inside it.
- **🛟 Never lose a session** — Claude Code deletes transcripts after ~30 days; Share-It
  keeps a local copy so your history stays yours.
- **↩️ Resume** — reopen a session in its original app or terminal.
- **⏳ Expiry your way** — links live forever or 1 / 3 / 7 days, and delete in one click.

## 🔒 Private by default

Nothing leaves your machine until you press share. Your data is **never used for
training** — it's yours, on your own storage. Secrets are scrubbed automatically and
the preview shows exactly what goes out. Links are unguessable and expire on your terms.

## Build from source

```bash
./macos/build.sh          # → ~/Applications/share-it.app
```

Local app (stdlib Python + native panel); shares upload to a Cloudflare R2 worker
(`worker/`). Add a new harness in one small class — see `adapters.py`.

<sub>MIT licensed · built by <a href="https://github.com/FlowAILab">FlowAILab</a></sub>
