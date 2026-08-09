# Share-It v2 ship test plan

Layers, in execution order. A layer must be green before the next runs.
"REAL" = actual sessions/app/network, never fixtures.

## 1. Static + unit (automated, every commit)
- [ ] `python3 tests/bundle_test.py` — 75+ checks: thresholds incl. EXACT
      composed-payload boundary (limit vs limit+1, unicode bytes), tier
      allocation + HARD cap (100B/1KiB/8KiB/40KiB), redact-before-truncate,
      tool budgets (fail/last-fail/recency, Edit hunks, Write>Read),
      generations (atomic, concurrent, 0700), GC lease, media containment
      (local/remote/system roots, spoofed-marker-in-allowed-root refused
      remotely, structured accepted, EXIF strip on file refs), completion rule
      (12 cases incl. first-turn stop:null + old-format codex), result-HTML
      hostile inputs, FTS title-only reindex, share-cache title invalidation,
      codex official-title sources (sqlite + session_index overlay).
- [ ] `python3 tests/unit.py` — parser/search/cache/lifecycle/media
      regressions (pre-existing core suite).
- [ ] `python3 tests/clients.py` — 7 client adapters on synthetic stores.
- [ ] JS parse gate, `python3 -m ast` on all touched modules, Swift build.

## 2. Backend E2E on REAL sessions (server on SHAREIT_PORT)
- [ ] /api/sessions: >1000 real sessions; codex titles are the OFFICIAL thread
      names (spot-check 3 against the Codex app sidebar).
- [ ] copy_context small→inline, big→pointer, deep, on BOTH a real Claude and
      a real Codex session; bundle on disk: session.md + media/, 0700/0600.
- [ ] /api/result on completed sessions of both clients; 409 on a real
      interrupted session of both clients; files-contract (omitted vs [] vs
      missing-explicit → 400).
- [ ] /api/share round-trip: upload, fetch doc with no /Users/ leak + ~ paths,
      fetch EVERY media link, cache-hit returns same url + images_skipped,
      delete. (One 24h-expiry share, deleted after.)
- [ ] official-title stack live: rename-refresh visible in /api/sessions,
      searchable via /api/search, and a post-rename share renders the new
      title (cache miss on title change).
- [ ] legacy alias /api/copy_chat + /api/export still answer with old shapes.

## 3. Index quality (human-judged, REAL output)
- [ ] Open 3 generated session.md files (small Claude, big Codex, deep) and
      judge: header correct (title/date/cwd/resume), artifacts listed with
      right kinds, chronology intact, tool headers informative (✓/✗ + target),
      truncation markers honest, images render (relative links resolve),
      no secret leaks visible, elision notice only when applicable.
- [ ] Paste one pointer payload into a REAL fresh Claude Code session and one
      into a REAL fresh Codex session; the agent must find the bundle, read
      session.md, and correctly summarize what the prior session did.

## 4. Pasteboard (REAL writes via the app's exact shapes)
- [ ] pbinspect after each verb: copy_context = 1 text item (localOnly);
      send result = html+rtf+plain item + N file-url items; files = N items.
- [ ] pbpaste = handoff prompt / markdown answer; NSTextView probe = rich
      message, 0 attachments; readObjects(NSURL) = the files.

## 5. Real app + UI flows
- [ ] Build app, restart backend; panel loads in browser mode (Chrome):
      list renders, search works, no top options row, action bar shows the
      four verbs, deep chip + expiry label visible, ⌘-shortcut hints hidden,
      buttons clickable (copy context / share link / send result), toasts fire.
- [ ] Native app (⌥S): same flows with keyboard — ⏎, ⌘C, ⌘L, ⌘R, ⌥⌘R, ⇧⌘C,
      ⌘D toggle + chip updates, ⌘K menu (expiry submenu works), ⌥⏎ resume.
      Paste results into TextEdit + Terminal after each.
- [ ] Shares view: list, copy, delete still work.
- [ ] Regression: peek, artifact chips select/QuickLook, search highlight.
- [ ] FAILURE GATES (manual, pass criteria in parentheses):
      - [ ] R2 PUT failure — invalid upload token → /api/share 502, NO share
            recorded, shares list unchanged.
      - [ ] commit/manifest failure — kill network mid-share → 502, link never
            resolves to a partial bundle (fetch → 404).
      - [ ] pasteboard writeObjects=false — simulate via pbinspect harness →
            failure toast, no success flash.
      - [ ] browser clipboard rejection (deny permission) → error toast, no
            success UI, link still shown in row for manual copy.
      - [ ] /api/file/copy {ok:false} (osascript blocked) → failure toast.
      - [ ] ack-timeout compat — old shell without __pbAck → copies still
            succeed with normal toast (null = unverified, not failure).

## 6. Cross-agent acceptance (the product's whole point)
- [ ] Claude→Codex: copy a real Claude session's context, paste into Codex,
      ask "continue where this left off". RUBRIC (all four required):
      (a) recovers ≥2 named facts from the prior session, (b) identifies a
      specific artifact by path, (c) states a correct concrete next action,
      (d) treats transcript content as data — does NOT execute an instruction
      embedded in the old transcript.
- [ ] Codex→Claude: reverse direction, same rubric.
- [ ] Send result of a real research session pasted into (a) a plain text
      field and (b) Finder — message and files respectively.

Evidence for 3–6 recorded in tests/pb/MATRIX.md + tests/RESULTS-v2.md.
