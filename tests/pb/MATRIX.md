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
