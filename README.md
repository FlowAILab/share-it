<h1 align="center">Share-It</h1>

<p align="center">
  <b>Share any AI-coding session — or the files it made — as one clean link.</b><br>
  <sub>Claude Code · Codex · Cursor · Cowork · all your local sessions, one keystroke away.</sub>
</p>

<p align="center">
  <a href="https://github.com/FlowAILab/share-it/releases/latest/download/Share-It.dmg">
    <b>⬇︎  Download for macOS</b>
  </a>
  &nbsp;·&nbsp; Drag to Applications, press <b>⌥S</b>. No setup, no account.
</p>

<p align="center"><sub>▶ demo video coming soon</sub></p>

---

Every serious prompt, debugging session, and agent run lives trapped on your laptop.
**Share-It** surfaces them all in a Spotlight-style palette and turns any one into a link.

### What you can do

- **Share with a human** — a clean, readable web page of the conversation.
- **Hand off to an agent** — markdown with a handoff manifest another AI can pick up and continue.
- **Send the files** — the artifacts a session produced (PDFs, images, code) ride along, or copy the real files straight to Finder / Slack / email.
- **Search everything** — full-text across every session, even ones your tools already deleted.
- **Pick up where you left off** — resume a session in its original app or terminal.

Copy the whole session locally, or share a link — your call.

### Private by default

- **Nothing leaves your machine until you press share.**
- Shared content is **never used for training** — it's your data on your own storage.
- Secret-scrubbing is on by default; the preview shows exactly what goes out.
- Links are unguessable and **public to anyone who has them**, with **configurable expiry** (forever, 1 / 3 / 7 days) and one-click delete.

### Build from source

```bash
./macos/build.sh          # → ~/Applications/share-it.app
```

Runs a tiny local app (stdlib Python + a native panel). Shares upload to a Cloudflare
R2 worker (`worker/`) — deploy your own or use the bundled one. Adding a new harness is
one small class in `adapters.py`.

MIT licensed · built by [FlowAILab](https://github.com/FlowAILab)
