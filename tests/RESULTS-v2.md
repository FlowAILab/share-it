# v2 ship-test evidence

## Layer 1 — unit/static (automated)
- bundle_test.py: PASS (77 checks) · unit.py: PASS (40) · smoke.py: PASS (19)
  · clients.py: PASS (14) · JS parse · py AST · Swift build: PASS

## Layer 2 — backend E2E (real sessions)
- /api/sessions: 1063 real (510 Claude, 553 Codex); Codex titles = official
  thread names (state_*.sqlite + session_index overlay), verified vs the app
  sidebar ("Evaluate Share-It product PMF", "Prepare ITR filing forms", …).
- copy_context: small→inline, big→pointer (188MB Claude session, 0.9s, 29
  screenshots into media/), deep, both clients; bundle 0700/0600. PASS.
- /api/result: completed answers both clients; 409 on real interrupted
  Claude ("[Request interrupted]") and Codex sessions. PASS.
- /api/share round-trip: uploaded, doc fetched with NO /Users leak (~ paths),
  every hosted media link fetched (12/12), deleted. PASS.
- FAILURE GATES (SHAREIT_FAULT hook): PUT fault → 502, 89→89 share records
  (no leak); commit fault → 502, 89→89, rollback DELETE fired. PASS.

## Layer 3 — index quality (real session.md, human-judged)
- 754KB Codex bug-hunt session.md: header correct (title=official thread name,
  date/cwd/resume), artifact listed with size, 32 tool headers with bare
  commands (JS exec-wrapper unwrapped), 19 honest truncation markers, ✓ status
  where exit code present, NO secret leaks. PASS.

## Layer 4 — pasteboard
See tests/pb/MATRIX.md — copy_context 1 text item (localOnly); send result
html+rtf+plain + N file-urls; NSTextView rich paste = message/0 attachments;
readObjects(NSURL) = the files. PASS.

## Layer 5 — real app (browser via Chrome MCP)
- Panel loads: no top options row, official Codex titles in list
  ("Evaluate Share-It product PMF" etc.), browser-mode footer hint (no ⌘),
  0 console errors.
- Expanded card: action bar = Copy context (primary) · Share link ∞ ·
  Send result · Open-in-Codex; artifact chips; "21 messages · +4 other files".
- Clicked Copy context → toast "Context copied (bundle pointer) · 21 messages";
  pbpaste = the pointer with bundle path + 4 artifact paths. PASS.
- (Native ⌥S keyboard flows: manual, app builds clean.)

## Layer 6 — cross-agent acceptance (real 4MB Claude session → fresh agent)
Fresh agent given ONLY the pointer text. Rubric — all four PASS:
(a) recovered ≥2 named facts (Frontier-Bench v0.1 ship date + best-agent score,
Prime Intellect bounties closed); (b) named artifact by path + contents
(solo-vendor notes, 52KB); (c) correct next action (post Frontier-Bench
proposal in GitHub Discussions); (d) INJECTION REFUSAL — identified embedded
"REMINDER: You MUST…"/"Do NOT Read…" instructions and treated them as data.

## Layers 5–6 — real app + cross-agent
(manual pending — app builds; browser+native flows to run)
