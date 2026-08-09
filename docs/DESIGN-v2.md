# Share-It v2 — verb redesign (design review, pre-code) — REV 3

## Intent

(unchanged) Share-It is a macOS palette app (⌥S) indexing local AI-coding sessions
(Claude Code, Codex, Cursor, +8). User priorities shifted: (1) copy one agent's context
into another local agent, (2) share a session's result with a human (Slack/LinkedIn),
(3) hosted links — less focus. Redesign around 3 primary verbs + 1 utility, remove the
top options row, rebuild copying around an on-disk bundle. Claude's own /export ships
~1% of a session (rendered scrollback, no tool output, `[Image #N]` with no path); we
parse the raw JSONL and can ship full tool I/O, artifact paths, and actual image files.

## Plan (revised)

### 1. Verbs, names, shortcuts (one visible action row)

| Verb | Shortcut | Audience | Payload |
|---|---|---|---|
| **Copy context** | ⏎, ⌘C | agent, this Mac | local bundle on disk; clipboard = inline md (small) or handoff-prompt + path (large). TEXT-ONLY. |
| **Share link** | ⌘L | agent, remote/other device | in-memory remote render uploaded (session.md + flattened media/files objects); clipboard = URL. Positioned as the REMOTE agent handoff — humans get Send result (R2-9: per explicit user decision the human reader page is retired; UI copy and README say "context link for agents / other devices", so the raw markdown link is coherent) |
| **Send result** | ⌘R | human | last assistant message rich text + curated artifact file items, ONE multi-item write (see §5) |
| **Copy files** | ⇧⌘C | anywhere | artifact file objects; **no files → disabled + "No files" toast** (current silent fallback to last-answer removed) |

Overflow "⋯" (⌘K): Open in client (⌘O), deep toggle (⌘D), link expiry, shares list,
delete link. **Visible state chips** next to the action bar: a `deep` chip when deep is
on; the expiry value (∞/1d/3d/7d) rendered inside the Share link button itself (e.g.
"Share link ∞") so persisted hidden state can't surprise (addresses R1-12). Shortcut
hints (⌘L/⌘R/…) render ONLY in the native shell; browser mode shows clickable buttons
without those hint labels since browser chrome owns ⌘L/⌘R (addresses R1-13).

Naming: "Send result" kept over "Copy result" — matches the user's stated mental model;
tooltip: "copies a ready-to-send message + files". Considered R1-3's rename; the
determinism concern is handled by mechanics + testing (§5), not naming.

### 2. Bundle: LOCAL = on-disk dir; REMOTE = in-memory objects (no shared dir)

Resolves R1-2 (conflation) by construction: the disk directory serves ONLY Copy context.
Share link keeps today's in-memory `{name, data}` object pipeline (share.upload_bundle),
reusing the same *renderer*, never the same directory.

Local bundle — IMMUTABLE GENERATIONS (R2-1, R2-2):
```
~/.shareit/exports/<source>-<sha1(canonical session id/path)[:12]>/<gen>/
  session.md    media/NNN.png …
```
- `<gen>` = 12-hex random nonce from `os.urandom` (R3-3 — no timestamp collisions
  under concurrency). Every copy builds into a fresh `<gen>.tmp-<pid>` and publishes
  with ONE atomic `os.rename` to `<gen>/` — no missing-path window, crash leaves only
  a tmp dir that GC removes. The PASTED pointer is the generation path, immutable for
  its retention lifetime (≥ the 7-day lease; R3-4): a recipient can never receive
  altered content — deep/normal are simply different generations. Pointer-mode text
  notes the lifetime ("kept at least 7 days").
- Convenience symlink `<key>/latest` updated via symlink+rename (atomic); never used
  in pasted payloads.
- Dir 0700, files 0600. Key = source + hash of the FULL canonical id (no 8-char
  collisions; SQLite `#` ids hash cleanly). Refuse symlinked build targets (lstat).
- Retention: generations younger than a 7-DAY LEASE are never GC'd; beyond the lease,
  LRU to 200 generations / 2GB. Quota enforced after each build AND at launch (R2-2).

Remote objects: flattened names (`media_001.png`, `file_report.pdf`) because the worker
sanitizes `/`; session.md links use the existing absolute `media_base/b/<id>/…` URL
pattern render.py already emits (R1-1). No worker changes.

Memory model (R1-5, honest version): messages are fully materialized by adapters (the
existing architecture; ~tens of MB worst case, fine). session.md is written
incrementally to disk; local mode NEVER reads artifact bytes; remote mode keeps the
existing in-memory objects with the existing 25MB/object + 100MB/bundle + 64-object
caps as the memory bound. No claim of end-to-end streaming.

### 3. Copy context clipboard payload

- Threshold measured on the FINAL COMPOSED clipboard payload in UTF-8 bytes (header +
  transcript + artifact paths + footer), constant 16 KiB (R1-8).
- ≤ threshold → inline: header + full markdown + artifact absolute paths + footer
  "full bundle: <path>". Media links in the INLINE rendering are rewritten to absolute
  bundle paths (`/Users/…/exports/<key>/media/001.png`) so pastes work from any cwd;
  the on-disk session.md keeps relative links (R1-8).
- > threshold → pointer: ~10-line handoff prompt (client/title/date, absolute
  session.md path, media note, artifact paths "work on these directly", "read
  session.md first, then continue"). Paths shell-quoted.
- Pasteboard TEXT-ONLY (no file items — known hijack bug class). Copy-context writes
  (inline AND pointer) are marked `.currentHostOnly` — the payload is private
  transcript text and host-only paths, so it must not ride Universal Clipboard;
  the native `copyText` gains a `localOnly` flag. Share-link URL copies remain
  Universal-Clipboard eligible (R2-4).

### 4. session.md content + truncation algorithm (rewritten per R1-6/7)

All limits are UTF-8 bytes. Pipeline order: parse → **redact (always)** → truncate →
compose, so a secret can never straddle a truncation boundary (R1-6).

Per-message budgets (default | deep):
- user / assistant text: 20 KiB | 64 KiB, middle-truncated above budget.
- Write tool input: 8 KiB | 64 KiB. Edit: old/new shown as a diff-style hunk pair,
  4 KiB each | full (R1-7).
- Read / Grep outputs: 1.5 KiB | 16 KiB, marker `[truncated — re-read at <path>]`
  (marker only for tools with a re-readable path).
- Bash: 2 KiB | 16 KiB — no "re-read" marker (not re-derivable, possibly unsafe to
  re-run). Every tool call gets a structured one-line header: tool, ok/exit status,
  command or path, cwd when it differs (R1-7).
- Failed tool calls (nonzero exit / error) get 4× their budget; the LAST failed call
  gets 8× (the thing the next agent most needs) (R1-7).
- Recency: the final 10 tool calls get 2× budget. Multipliers COMBINE
  multiplicatively, capped at 8× base per call (R2-3).
- Thinking: deep only.

Global allocation — TIERED, so the cap can never discard the task itself (R2-3):
- Tier A (reserved first): the INITIAL user request, the LAST 3 user requests, the
  final 5 assistant turns, and every failed-tool structured header line — kept at
  full per-message budget.
- Tier A overflow policy (R3-1): remaining (middle) user messages are Tier A' — kept
  at full budget while room remains, else middle-truncated to 4 KiB each, else
  1-line summaries; if Tier A alone would exceed the 20 MiB cap, Tier A' degrades
  first, then final assistant turns tighten to 8 KiB — the initial request and last
  user request are the last things standing. Tier-A-overflow unit test required.
- Tier B (next): remaining assistant texts, newest-first.
- Tier C (last): tool call bodies, newest-first with the multipliers above.
When the cap is hit, un-granted Tier B/C items degrade to their structured one-line
headers (`[earlier: 41 tool calls, 12 files written — paths above]`). Deep =
expanded budgets, same cap and tiers.

Header block: client, title, date, cwd, resume command, artifact list, reads manifest.
REMOTE variant (R1-9, R2-8): omits resume command; keeps the REAL cwd internally for
`_rel()` relativization but DISPLAYS only its basename; read/artifact paths inside the
workspace render relative to cwd; paths outside the workspace render as basename +
"(outside workspace)" — absolute paths never appear in remote output.

### 5. Send result mechanics (revised per R1-3/4, R2-5/6/10)

**Which message is "the result" (R2-5):** parsers preserve the harness's phase metadata
where it exists — Codex distinguishes `commentary` vs `final_answer`; the parser
currently drops it. UNIFORM COMPLETION RULE for every client with metadata (R4-1): the
chosen result must be a completed answer (Codex `final_answer`, Claude `end_turn`)
occurring AFTER the latest non-injected user request — a stale prior-turn answer is
never sent. Send result prefers that answer; if a session
has ONLY commentary (interrupted run), it errors "No completed result — session looks
interrupted" rather than shipping a progress note. Claude (R3-2): `parse_claude`
preserves `stop_reason`; a result requires a completed assistant `end_turn` AFTER the
latest user request — an interrupted tool run (`stop_reason: "tool_use"` tail) errors
the same way instead of shipping a pre-tool explanation. Real completed AND
interrupted fixtures for both clients. Clients with no completion metadata at all:
last non-empty assistant message, documented as best-effort.

**Milestone ZERO — pasteboard spike (R2-6):** before any surrounding flow is built, a
spike writes the multi-item payload and pastes into the named product targets: Slack
desktop, Slack web, Gmail compose, Mail.app, LinkedIn post box, X compose, Notes,
TextEdit, iTerm. Pass criteria: in every target, ONE paste produces a useful result
(message text OR attached files) and never a destructive/empty insert. Fail in any
major target → ⌘R ships text-only and text+files moves to ⌥⌘R. The action table's
multi-item ⌘R is CONTINGENT on this spike; implementation order enforces it.

One `copyRich` write (spike-permitting): item 1 = the result message rendered by a
result-specific markdown→HTML renderer with RTF + plain fallbacks; items 2..n =
curated artifact file URLs. No artifacts → single rich-text item.

**Renderer safety contract (R2-10):** escape-first (markdown source is untrusted);
no raw-HTML passthrough; link schemes allowlisted to https/http/mailto; images are
NOT emitted into the clipboard HTML (no remote or file resource loading at paste or
RTF-conversion time); RTF is derived from the sanitized HTML only, with resource
loading disabled. Title and all header metadata pass the same redaction as bodies.
Tests: hostile raw HTML, `javascript:` links, remote `<img>`, `file://` img source.

Toast: until the spike produces evidence, neutral — "Result + 2 files copied." After
the spike, the toast may state observed behavior (from the README matrix), not
assumptions (R2-6 accepted for pre-evidence; post-evidence specificity retained —
users deserve the documented matrix once it EXISTS).

Swift `copyRich`/`copyFiles`/`copyText` return the `writeObjects` Bool to JS via the
handler reply; failure toasts as failure (R1-4). File-bearing writes use
`prepareForNewContents(with: .currentHostOnly)` (R1-4).

`POST /api/result` selection contract (R1-10, R2-7): `files` OMITTED → server
computes the default primary set; `files: []` → intentionally attach nothing; the UI
sends the explicit array (possibly empty) whenever `_selTouched` is true. Explicitly
selected files that are missing → 400 with the missing list; auto-selected missing
files → skipped + `skipped:[…]` in the response, and the success toast surfaces it
("Result + 1 file copied — 1 missing file skipped").

### 6. Share link (reduced focus)

- Upload = remote render: session.md + flattened media/file objects; link resolves to
  session.md (markdown). Human reader HTML removed from UI (code kept, unexposed).
  Stat card UI removed. "for agent" per-share button removed (default payload is
  agent-ready). Clipboard after ⌘L = URL only.
- Expiry: control in ⋯, current value visible on the Share button (§1). Persisted.
- `EXPORT_SCHEMA_VERSION` bumped; share-cache key covers mode(=agent|deep), selected
  file fingerprint, media, and builder version — verified in implementation, extended
  if any dimension is missing (R1-15).

### 7. Redaction & compat

- redact always true, enforced SERVER-SIDE in `_render_opts` (client flag ignored) —
  applies to all routes incl. legacy (R1-12). No scrub messaging in the UI (explicit
  user requirement; README keeps the "artifact files ship as-is" note).
- `/api/copy_chat` keeps its EXACT current response contract as a deprecated alias for
  one release; the new UI only calls `/api/copy_context` (R1-15).

### 8. Media extraction (scoped honestly per R1-11)

- v2 ships: existing inline-base64 images (media.collect) + Claude image-cache file
  references — parser emits cache refs; bundle copies them with: containment check
  (only from the known cache roots), content sniff, 10MB/image cap, 40-image cap.
- Codex file-backed image markers: best-effort behind the same checks where the format
  is recognized. Unrecognized/uncopyable images are NOT silent: session.md gets an
  `[image unavailable]` marker in place, and copy/share responses return
  copied/skipped image counts surfaced in the toast (R2-11).

### 9. UI removals

Top options row deleted (human/agent/deep segment, expiry segment, stat card, adv).
Deep survives as ⋯ toggle + chip. "Copy last answer" button replaced by Send result.
Search, source legend, list, peek, artifact checkbox row, shares view stay.

### 10. Verification (expanded per R1-16 + user requirement)

- Unit: threshold boundary at 16 KiB with multi-byte unicode; truncation (fail-boost,
  last-fail 8×, recency, global-cap newest-first); redact-before-truncate secret
  boundary; local/remote header asymmetry; path quoting; atomic rebuild under
  concurrent calls + interrupted build (tmp dir left behind → GC'd); media containment
  (path traversal attempt fixture).
- Pasteboard: small AppKit inspector (swift script) asserting item count, types per
  item, RTF/HTML presence, file URLs — run after each copy verb.
- ONE authoritative real-target paste matrix (R3-5), identical to Milestone Zero's:
  Slack desktop, Slack web, Gmail compose, Mail.app, LinkedIn post box, X compose,
  Notes, TextEdit, iTerm/Terminal, generic browser textarea. Post-spike toast wording
  is scoped "observed in tested apps".
- E2E on REAL sessions: a real Claude Code session AND a real Codex session AND one
  SQLite-backed client (Cursor); every verb both sides of the threshold; share-link
  fetch of session.md + every media/file object link (R1-1 regression check).
- No shipping on fixtures alone.

## Complexity (self-assessed)

complex — up to 3 rounds.

## Revision notes (round 1)

1. **Fixed** — remote no longer uses nested names: flattened object names + the
   existing absolute media_base URL pattern; no worker migration needed (§2).
2. **Fixed** — disk dir is local-only; remote stays in-memory; tmp-sibling atomic swap
   + per-key lock + symlink refusal (§2).
3. **Accepted with mechanics, not rename** — no either/or contract claimed; exact-payload
   toast, empirical target matrix pre-ship, and an evidence gate that demotes ⌘R to
   text-only (files on ⌥⌘R) if the matrix shows destructive behavior (§5). Name stays
   "Send result" per explicit user preference; NSSharingServicePicker rejected as
   heavyweight for v2.
4. **Fixed** — acknowledged native writes (Bool → JS), result-specific md→HTML renderer,
   `.currentHostOnly` for file payloads (§5).
5. **Fixed** — streaming claim replaced with the honest memory model; remote bounded by
   existing caps (§2).
6. **Fixed** — UTF-8 byte limits, newest-first global allocation under a 20 MiB cap,
   deep = expanded-but-capped, redact strictly before truncate (§4).
7. **Fixed** — structured per-call header (tool/status/command/cwd), failed-call 4× and
   last-failed 8× boosts, Edit as diff hunks, Bash keeps no re-read marker (§4).
8. **Fixed** — threshold on final composed payload bytes; inline media links rewritten
   to absolute bundle paths (no forced pointer mode needed) (§3).
9. **Fixed** — remote header scrubbed (no resume cmd, basename cwd, $HOME-stripped read
   paths); no auto-upload of read files; explicitly checked files may ship (§4, §6).
10. **Fixed** — /api/result takes files[] or computes server-side default;
    explicit-missing → 400, auto-missing → skipped+reported; ⇧⌘C no-files → disabled
    with toast, fallback removed (§1, §5).
11. **Fixed** — media scoped to base64 + Claude cache refs with containment/sniff/caps;
    Codex file-backed best-effort; fixtures required (§8).
12. **Partially accepted** — deep chip + expiry value visible on the Share button;
    server-side redaction enforcement. The persistent "files not scrubbed" reminder is
    NOT added — explicit user instruction ("don't add msg about it"); README retains it.
13. **Fixed** — shortcut hints native-shell-only; browser mode gets buttons (§1).
14. **Fixed** — hash-based dir key, 0700/0600, symlink-safe rebuild, LRU GC (§2).
15. **Fixed** — EXPORT_SCHEMA_VERSION bump + cache-key dimensions verified; copy_chat
    alias keeps its exact old contract (§6, §7).
16. **Fixed** — AppKit pasteboard inspector, real-target matrix, worker link-follow
    test, unicode boundary, interrupted/concurrent build tests, + Cursor e2e (§10).

## Revision notes (round 2)

1. **Fixed via your alternative** — dropped the rename dance for immutable generation
   dirs: one atomic rename publishes, pasted pointers are immutable forever, crash
   leaves only a GC-able tmp (§2). renameatx_np unnecessary under this scheme.
2. **Fixed** — generations make mode changes new paths by construction; 7-day GC lease
   + quota enforcement post-build and at launch (§2).
3. **Fixed** — tiered allocation: Tier A (all user msgs, final 5 assistant turns,
   failed-call headers) reserved before newest-first tool detail; multipliers defined
   as multiplicative, capped 8× (§4).
4. **Fixed** — Copy context (inline and pointer) is `.currentHostOnly` via a localOnly
   flag on copyText; Share-link URLs stay Universal-Clipboard eligible (§3).
5. **Fixed** — parser preserves Codex phase; Send result prefers final_answer, errors
   on commentary-only sessions, conservative last-assistant fallback for phase-less
   clients (§5).
6. **Fixed with one retained point** — spike is milestone zero with pass/fail criteria
   and the named product targets; ⌘R's multi-item form is contingent on it. Toast is
   neutral pre-evidence; I retain post-evidence specificity — once the matrix is
   measured and documented, the toast states observed behavior. A permanently vague
   toast serves no one (§5).
7. **Fixed** — omitted vs [] contract; UI sends explicit array when _selTouched;
   skipped surfaced in toast (§5).
8. **Fixed** — real cwd kept for relativization, display basename, outside-workspace
   marker; no absolute paths in remote output (§4).
9. **Resolved as labeling, not a reader revival** — the user explicitly retired the
   human reader page; Share link is repositioned in UI copy + README as the remote
   AGENT/device handoff, humans are routed to Send result (§1 table). Rebuilding a
   reader contradicts the product decision this redesign implements.
10. **Fixed** — full renderer safety contract: escape-first, scheme allowlist, no raw
    HTML, no images in clipboard HTML, RTF from sanitized HTML with resource loading
    disabled, header metadata redacted, hostile-input tests (§5).
11. **Fixed** — `[image unavailable]` markers + copied/skipped counts in responses and
    toasts (§8).

## Revision notes (round 3)

1. **Fixed** — Tier A bounded: initial + last-3 user requests and final assistant
   turns reserved; middle user turns (Tier A') degrade first (4 KiB → summaries);
   overflow test required (§4).
2. **Fixed** — parse_claude preserves stop_reason; result requires end_turn after the
   latest user request; interrupted fixtures for both clients (§5).
3. **Fixed** — generation id is a 12-hex urandom nonce (§2).
4. **Fixed** — "immutable for its retention lifetime (≥7-day lease)", surfaced in
   pointer text (§2).
5. **Fixed** — single authoritative matrix incl. Gmail/LinkedIn/X/Slack-web shared by
   Milestone Zero and §10; toast scoped "observed in tested apps" (§5, §10).

## Revision notes (round 4)

1. **Fixed** — uniform completion rule: for BOTH parsers the chosen result must be a
   completed answer occurring after the latest non-injected user request; otherwise
   "No completed result." Multi-turn interrupted fixtures required for both (§5).
