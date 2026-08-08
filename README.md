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
  <sub>First launch: right-click the app → <b>Open</b> (signed builds coming).</sub>
</p>

<p align="center"><sub>▶ demo video coming soon</sub></p>

---

Hit **⌥S**, find your session, done. The link carries the **whole thing** — the
conversation and every file it made. Send it to a friend, or drop it into another
agent and it picks up right where you left off. 🚀

## Core

- 🔗 **Share a link** — hit **⏎**, a short URL is on your clipboard.
- 📋 **Copy the session** — hit **⌘C**, the full chat as a file. Stays on your machine.
- 📦 **Grab the files** — the PDFs, images, and code it made, ready to drop into Finder,
  Slack, or email. Real files, not screenshots.

## Nice extras

- 🧠 **Deep mode** — pack in the reasoning, tool output, and every file, so another agent
  can truly take over.
- 🛟 **Nothing gets lost** — Claude wipes old sessions after ~30 days; Share-It quietly
  keeps them, and lets you search everything you've ever done.
- 📖 **Reader mode** — a clean web page for humans, not raw markdown.
- ⏳ **Your rules** — links last forever or 1 / 3 / 7 days, and vanish in one click.

## 🔒 Yours, always

Nothing leaves your Mac until you say so. Your sessions are **never used for training** —
they're yours, on your own storage. Secrets get scrubbed automatically, and you see
exactly what's in the link before it goes out.

## Build from source

```bash
./macos/build.sh          # → ~/Applications/share-it.app
```

Tiny local app (Python + a native panel). Shares go to a Cloudflare R2 worker
(`worker/`). Adding a new tool? One small class in `adapters.py`.

<sub>MIT licensed · built by <a href="https://github.com/FlowAILab">FlowAILab</a></sub>
