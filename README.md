<h1 align="center">Share-It</h1>

<p align="center">
  <b>Hand off any AI-coding session — with its output files — to another agent in one click.</b><br>
  <sub>Works across Claude Code · Codex · Cursor · Cowork. Your sessions, one keystroke away.</sub>
</p>

<p align="center">
  <a href="https://github.com/FlowAILab/share-it/releases/latest/download/Share-It.dmg">
    <b>⬇︎  Download for macOS</b>
  </a>
  &nbsp;·&nbsp; Drag to Applications, press <b>⌥S</b>. No setup, no account.
</p>

<p align="center"><sub>▶ demo video coming soon</sub></p>

---

Your best prompts and agent runs are trapped on your laptop. Share-It makes them
one keystroke away — and one click to pass on.

### Core

- **🤝 Agent handoff** — turn any session into a clean context bundle another agent
  can continue from: the conversation, the decisions, the last open request, **and the
  files it produced**. Paste the link into Claude, Codex, or Cursor and it picks up
  exactly where you left off. Works across all of them.
- **🔗 One-click link** — press ⏎, get a short public link on your clipboard. Send it
  to a teammate or drop it into any chat.
- **📎 One-click files** — copy the real artifacts a session created (PDFs, images,
  code) straight to Finder, Slack, or email — actual files, not screenshots.

That's it: **⌥S → find it → share it.** No exporting, no zipping, no hunting for paths.

### Also

Reader-page mode for humans · resume a session in its original app · full-text search ·
keeps sessions your tools auto-delete · configurable link expiry.

### Private by default

Nothing leaves your machine until you press share. Your data is **never used for
training** — it's yours, on your own storage. Secrets are scrubbed automatically and
the preview shows exactly what goes out. Links are unguessable, expire on your terms
(∞ / 1d / 3d / 7d), and delete in one click.

### Build from source

```bash
./macos/build.sh          # → ~/Applications/share-it.app
```

Local app (stdlib Python + native panel); shares upload to a Cloudflare R2 worker
(`worker/`). Add a new harness in one small class — see `adapters.py`.

MIT licensed · built by [FlowAILab](https://github.com/FlowAILab)
