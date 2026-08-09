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

## Layer 3 — index quality
(human review pending — see below)

## Layer 4 — pasteboard
See tests/pb/MATRIX.md — copy_context 1 text item (localOnly); send result
html+rtf+plain + N file-urls; NSTextView rich paste = message/0 attachments;
readObjects(NSURL) = the files. PASS.

## Layers 5–6 — real app + cross-agent
(manual pending — app builds; browser+native flows to run)
