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

<p align="center"><sub>▶ demo video coming soon</sub></p>

---

Press **⌥S**, find your session — that's it. The link holds the **complete session**:
the whole conversation and the files it produced. Send it to a teammate, or paste it
into another agent to pick up right where you left off.

### Core

- **🔗 Share a link** — press **⏎**. A short URL to the full session lands on your clipboard.
- **📋 Copy the session** — press **⌘C**. The whole conversation as a file, no upload.
- **📎 Copy the files** — the artifacts a session made (PDFs, images, code) straight to
  Finder, Slack, or email — real files, not screenshots.

### Also handy

Reader-page mode for humans · resume a session in its original app · full-text search ·
keeps sessions your tools auto-delete · link expiry (∞ / 1d / 3d / 7d).

### Private by default

Nothing leaves your machine until you press share. Your data is **never used for
training** — it's yours, on your own storage. Secrets are scrubbed automatically and
the preview shows exactly what goes out. Links are unguessable, expire on your terms,
and delete in one click.

### Build from source

```bash
./macos/build.sh          # → ~/Applications/share-it.app
```

Local app (stdlib Python + native panel); shares upload to a Cloudflare R2 worker
(`worker/`). Add a new harness in one small class — see `adapters.py`.

MIT licensed · built by [FlowAILab](https://github.com/FlowAILab)
