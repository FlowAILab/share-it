# Send result pasteboard matrix (Milestone Zero — spike evidence)

Payload: ONE write → item0 {public.html, public.rtf, plain} + one public.file-url item per artifact.
Written with prepareForNewContents(.currentHostOnly).

| Target | Takes | Evidence class |
|---|---|---|
| Terminal / plain text fields | message (plain) | VERIFIED — pbpaste + plain NSTextView probe |
| TextEdit / Notes / Mail compose | rich message, 0 attachments inserted | MACHINERY-VERIFIED — headless NSTextView.paste(), the framework those apps embed |
| Finder + readObjects(NSURL) consumers (Slack-class) | the files | API-VERIFIED (readObjects probe) + REPORTED (v1 copy-answer hijack bug in Slack) |
| Browser textareas (Gmail/LinkedIn/X) | message (text/html flavor) | EXPECTED (standard engine paste path) — not yet exercised against the live products |

Honest gate status: no destructive/empty combination in anything we could
drive headlessly; the live Slack/Gmail/LinkedIn/X product surfaces were NOT
automated (would require operating the user's real accounts). ⌘R ships
multi-item per the product decision, with ⌥⌘R = message-only as the
deterministic escape hatch, and the toast claims only "apps take the message
or the files".

## E2E re-verification with real session payloads (v2 endpoints)

- copy_context (pointer mode, 188MB Claude session): 1 text-only item,
  `public.utf8-plain-text`, pbpaste = the handoff prompt with bundle path,
  29 screenshots copied into media/. Build time 0.9s.
- /api/result (real answer + 3 real artifacts): 4 items — html+rtf+plain
  + 3 file-urls. NSTextView rich paste = formatted message, 0 attachments;
  readObjects(NSURL) = the 3 files; pbpaste = markdown answer.
- Interrupted sessions (Claude "[Request interrupted]", Codex unanswered tail)
  → 409 "No completed result", verified on real transcripts of both clients.
