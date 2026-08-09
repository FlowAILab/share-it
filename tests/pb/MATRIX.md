# Send result pasteboard matrix (Milestone Zero — spike evidence)

Payload: ONE write → item0 {public.html, public.rtf, plain} + one public.file-url item per artifact.
Written with prepareForNewContents(.currentHostOnly).

| Target | Takes | Evidence |
|---|---|---|
| Terminal / plain text fields | message (plain) | pbpaste + plain NSTextView probe — verified |
| TextEdit / Notes / Mail compose (NSTextView) | rich message, 0 attachments inserted | headless NSTextView.paste() probe — verified |
| Finder / Slack / attachment-aware apps | the files | empirically established (v1 copy-answer hijack bug: file items win) |
| Browser textareas (Gmail/LinkedIn/X) | message (text/html flavor) | standard WebKit/Blink paste path — expected, spot-check on ship |

Verdict: no destructive/empty combination in any tested target → gate PASSED,
⌘R = multi-item (message + files). Toast wording: "observed in tested apps".

## E2E re-verification with real session payloads (v2 endpoints)

- copy_context (pointer mode, 188MB Claude session): 1 text-only item,
  `public.utf8-plain-text`, pbpaste = the handoff prompt with bundle path,
  29 screenshots copied into media/. Build time 0.9s.
- /api/result (real answer + 3 real artifacts): 4 items — html+rtf+plain
  + 3 file-urls. NSTextView rich paste = formatted message, 0 attachments;
  readObjects(NSURL) = the 3 files; pbpaste = markdown answer.
- Interrupted sessions (Claude "[Request interrupted]", Codex unanswered tail)
  → 409 "No completed result", verified on real transcripts of both clients.
