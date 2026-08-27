"""Cross-model review: a second model reads a session's work as it happens.

A Codex session is reviewed by Claude; a Claude session is reviewed by Codex.
The reviewer runs as a real agent with repo access — not a diff reader — and is
told to verify before reporting, because an unverified finding is worse than a
missed one: the user acts on it.

Two planes:
  * JSON is the control plane — {review_needed, severity, summary[], file}.
  * The review itself accumulates in a markdown file next to the session's repo.

Anything the reviewer says that is not that JSON object is treated as a human
talking to it, and ignored. That keeps the channel usable for conversation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone

STATE_DIR = os.path.expanduser("~/.shareit/review")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

# Who reviews whom. The value is always the *other* model.
# keyed by the session list's `source`, which says "claude" for Claude Code
REVIEWER_OF = {"codex": "claude", "claude": "codex", "claude-code": "codex"}
LABEL = {"claude": "Claude", "codex": "Codex"}

POLL_SECONDS = 5
DEFAULT_EVERY = 5          # agent turns between checks
MIN_GAP_SECONDS = 300      # ...and never more often than this, however fast they arrive
MAX_CONCURRENT = 2         # reviewers thinking at once; the rest wait a tick
# How close together banners may land. The finding always reaches the panel —
# these only govern whether it also makes a noise.
NOTIFY_QUIET = 600         # 10 min: inside this, only something worse gets through
NOTIFY_FLOOR = 120         # 2 min: never two banners closer than this, any watch
_LAST_NOTIFY = {"at": 0.0}  # across every watch, so three reviewers cannot pile on
_LOCK = threading.RLock()
_STARTED = False

# One watch per session under review. The panel is a list of reviewers, so the
# state has to be a list too — a single global watch could only ever show one.
STATE: dict = {"watches": {}}          # session path -> watch

_WFIELDS = ("path", "source", "reviewer", "title", "cwd", "every", "watermark",
            "file", "pending", "history", "stats", "reviewer_session",
            "reviewer_started", "focus", "last", "force", "new", "busy",
            "reviewer_session_real", "asks", "paused", "added", "last_at", "once",
            "last_notify_at", "last_notify_sev")


def _blank_watch(path: str, source: str, title: str, cwd: str) -> dict:
    return {"path": path, "source": source, "reviewer": REVIEWER_OF.get(source),
            "title": title, "cwd": cwd, "every": DEFAULT_EVERY, "watermark": 0,
            "new": 0, "busy": False, "busy_since": None, "last": None,
            "file": None, "pending": None, "history": [], "focus": "",
            "reviewer_session": None, "reviewer_started": False, "force": False,
            "reviewer_session_real": False, "asks": [], "paused": False,
            "added": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stats": {"checks": 0, "quiet": 0, "recorded": 0,
                      "raised": 0, "sent": 0, "ignored": 0}}


def _w(path: str) -> dict | None:
    return STATE["watches"].get(path)


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

def _save() -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"watches": {k: {f: w.get(f) for f in _WFIELDS}
                                   for k, w in STATE["watches"].items()}}, fh, indent=1)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def _load() -> None:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(saved.get("watches"), dict):
        for path, w in saved["watches"].items():
            if not w.get("path"):
                continue
            base = _blank_watch(path, w.get("source") or "", w.get("title") or "",
                                w.get("cwd") or "")
            base.update({k: v for k, v in w.items() if k in _WFIELDS and v is not None})
            base["busy"] = False           # nothing survives a restart mid-check
            STATE["watches"][path] = base
    elif saved.get("path"):                # migrate the old single-watch file
        base = _blank_watch(saved["path"], saved.get("source") or "",
                            saved.get("title") or "", saved.get("cwd") or "")
        base.update({k: v for k, v in saved.items() if k in _WFIELDS and v is not None})
        base["busy"] = False
        STATE["watches"][saved["path"]] = base
    _unshare_note_files()


# --------------------------------------------------------------------------
# reading the watched session
# --------------------------------------------------------------------------

NOTES_DIR = os.path.join(STATE_DIR, "notes")


def _slug(text: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return out[:40] or "session"


def _note_path(path: str, source: str, title: str) -> str:
    """One file per watched session, under share-it's own directory.

    Not the repo and never $HOME: two reviewers in one repo collided, and a
    session launched from home dropped REVIEW-NOTES.md straight into it."""
    m = re.search(r"([0-9a-f]{8})", os.path.basename(path))
    os.makedirs(NOTES_DIR, exist_ok=True)
    return os.path.join(NOTES_DIR,
                        f"{_slug(title)}-{m.group(1) if m else source}.md")


def _unshare_note_files() -> None:
    """Migrate anything still pointing at a repo or home directory."""
    for path, w in STATE["watches"].items():
        f = w.get("file") or ""
        if f.startswith(NOTES_DIR):
            continue
        new = _note_path(path, w.get("source") or "", w.get("title") or "")
        if f and os.path.exists(f) and not os.path.exists(new):
            try:
                os.makedirs(NOTES_DIR, exist_ok=True)
                shutil.copyfile(f, new)    # keep what the reviewer already wrote
            except OSError:
                pass
        w["file"] = new


def _turns_full(path: str) -> list[tuple[str, str]]:
    """(role, text) for user and assistant turns, in order. The reviewer needs
    the user's steering too — a finding the user already answered in their own
    words would otherwise be invisible and get raised again."""
    out: list[tuple[str, str]] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"assistant"' not in line and '"user"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("payload", rec)
                if not isinstance(msg, dict):
                    continue
                inner = msg.get("message", msg)
                msg = inner if isinstance(inner, dict) else msg
                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue
                content = msg.get("content") or []
                text = (content if isinstance(content, str) else
                        " ".join(c.get("text", "") for c in content
                                 if isinstance(c, dict)))
                text = re.sub(r"\s+", " ", text).strip()
                # tool results arrive as user-role records; they are not steering
                if text and not (role == "user" and text.startswith(("<", "[Request",
                                                                    "Caveat:", "tool_use"))):
                    out.append((role, text))
    except OSError:
        pass
    return out


def _window(path: str, mark: int) -> str:
    """Everything said since the watermark, both sides, in order."""
    full = _turns_full(path)
    seen, start = 0, len(full)
    for i, (role, _) in enumerate(full):
        if role == "assistant":
            seen += 1
            if seen > mark:
                start = i
                break
    else:
        start = len(full)
    # pick up any user turns immediately preceding the first new assistant turn
    while start > 0 and full[start - 1][0] == "user" and seen > mark:
        start -= 1
    return "\n\n".join(("You: " if r == "user" else "") + t[:1500]
                        for r, t in full[start:])


def _turns(path: str) -> list[str]:
    """Assistant turns only — this is what the watermark counts."""
    out: list[str] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"assistant"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("payload", rec)
                msg = msg.get("message", msg)
                if msg.get("role") != "assistant":
                    continue
                content = msg.get("content") or []
                text = (content if isinstance(content, str) else
                        " ".join(c.get("text", "") for c in content
                                 if isinstance(c, dict)))
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    out.append(text)
    except OSError:
        pass
    return out


def _dirty(cwd: str) -> str:
    if not cwd or not os.path.isdir(cwd):
        return ""
    try:
        r = subprocess.run(["git", "status", "--short"], cwd=cwd,
                           capture_output=True, text=True, timeout=20)
        return "\n".join(r.stdout.splitlines()[:25])
    except (OSError, subprocess.SubprocessError):
        return ""


# --------------------------------------------------------------------------
# the reviewer
# --------------------------------------------------------------------------

BRIEF = """\
{peer} has been working in this repo. You are reviewing its work — you are a
different model, and that difference is the point. You are not the implementer.

This is check #{check_no}. You last looked at turn {from_turn}; {peer} is now at
turn {to_turn}. Review only what happened in between, in the context of what you
already concluded — do not start over.

What was said since your last check — {peer}'s turns {from_turn}–{to_turn},
with the user's own messages marked "You:" (tool calls are omitted):

{delta}

The text above omits {peer}'s tool calls. Its full session transcript is:
  {session_path}
  thread {thread_id}
That file is JSONL, one record per line — read it directly when you need to see
what {peer} actually ran, not just what it said it did. Checking the claim
against the tool calls is often where the real finding is.

Uncommitted files right now:
{changed}

Your running review file is {review_file}
Read it first — it holds everything you have already concluded. Never repeat a
point already in it unless you have new evidence. Append new findings there;
keep it in a form your next check can pick up from.

You have shell and file access. Use it. VERIFY BEFORE YOU WRITE ANYTHING DOWN:
run the test, query the database, read the file. Do not record a finding you
could not confirm — an unverified finding is worse than a missed one, because
the user will act on it.

Review holistically. Defects first, but anything that would make this
materially better counts:

- correctness: does it do what it claims? check the claim, not the prose
- contract drift: code, tests, docs and config disagreeing with each other
- failure modes: errors swallowed, partial writes, no rollback, silent fallbacks
- irreversibility: anything destroying data or evidence that cannot be remade
- security: secrets, permissions, injection, over-broad access, credential paths
- tests: do they cover the claim, or pass regardless of it?
- performance: obvious waste — repeated work, N+1, unbounded reads, hot loops
- simplicity: a materially smaller or clearer way to do the same thing
- process: work being redone, evidence not captured before it is destroyed,
  a manual step that keeps recurring, missing guardrails
- recurrence: for anything you find, ask what would have caught it without a
  human - a test, an assertion, a type, a lint rule, a CI step, a script, a
  note in the repo's agent instructions, a reusable skill. Name the cheapest
  one that would actually have fired
- consistency with the patterns already in this repo

Ignore formatting, naming and cosmetic style. Substance only.
{outstanding}

Quality-of-life and process suggestions are welcome but they are LOW PRIORITY
and they travel as passengers. Attach them to a finding you are already
reporting rather than raising them alone, and never let one drive an interrupt
by itself - a missing guardrail can wait for the next thing that genuinely
cannot. If you have several small ones, club them into a single line at the end
of the review file rather than spreading them across checks.

YOU ARE NOT THE IMPLEMENTER. Never edit, create, move or delete any file in
{peer}'s project, however obvious the fix looks and however sure you are. The
only file you write is your own review file. Changing the code out from under
{peer} mid-task corrupts its work and destroys the independence that makes this
review worth anything. If something should change, say so in the review.
Reading, grepping and running read-only checks is exactly what you should do.
{focus}

Two separate decisions. Do not mix them.

A. What to REPORT. Everything you looked at and concluded. Always fill this in.
   It goes on a status panel and into the review file. Reporting is free.

B. Whether to INTERRUPT {peer}. Expensive - it costs the user an action and
   costs {peer} its train of thought. Interrupt only when {peer} is about to
   rely on something that will not hold: a test asserting a guarantee nothing
   enforces, a gate that cannot fire, a check passing for the wrong reason, or
   something about to become irreversible (a scored run, a deploy, a deletion).

   "Nothing changed since last check", "still blocked on the same thing",
   "this earlier finding is still open" are STATUS. They never interrupt,
   however true they are. If the only new thing is that time passed,
   should_interrupt is false. Neither do process, tooling or quality-of-life
   suggestions, however good - those ride along with something else.

Could not verify a claim you would otherwise interrupt over? Say so and
interrupt anyway - name what you could not run and what would settle it.

BE CURRENT. Your notes and your open asks are a record of what was true when
you wrote them, not what is true now. {peer} has been working since. Before you
mention anything you have said before, re-read the actual file or re-run the
actual check - a finding you raised two checks ago has very often already been
fixed, and repeating a dead one costs you the user's trust in all the others.
When something turns out to be handled, say so once, put its id in "resolved",
and never raise it again.

Do not repeat yourself. If a point is already in your review file and nothing
has changed, it is finished - not "still open", not "worth restating", not
"reiterating for completeness". Say something new or say nothing. The user
reads every check; a check that only re-litigates the last one wastes them.

Reply with ONE JSON object and nothing else:

{{"should_interrupt": false, "severity": 0,
  "summary": ["short bullet", "another bullet"],
  "headline": "",
  "resolved": [],
  "file": "{review_file}", "prompt": ""}}

summary: 1-4 terse bullets. Status - what changed, what you checked, what you
concluded. This is the report, not the ask.
headline: one line naming the single thing worth acting on, or "" if there is
none. Not a summary of the summary.
prompt: the message {peer} will actually read. AT MOST 4 SHORT LINES. It is
pasted into {peer}'s window mid-task, so length costs it the very context the
interruption was meant to protect. One line on what looks wrong and where
(file:line), one on what would confirm or refute it. The reasoning, the
evidence and the alternatives go in the review file, not here - the message
already carries a pointer to it. Suggest, never instruct: you may be wrong and
{peer} can see things you cannot. "X at f.py:12 looks like it may Y - running Z
would settle it" beats a paragraph.

resolved: ids from the "still open" list above that you verified are now
handled. Omit anything you did not check. [] when there is nothing open.

severity, and be strict - most real findings are 1 or 2:
  0  nothing worth saying
  1  worth recording. {peer} loses nothing by never hearing it
  2  should fix EVENTUALLY. Real, but the next step is safe without it -
     cleanup, a missing test for something that currently works, a latent
     edge case, anything that can ride along with the next change
  3  should fix IMMEDIATELY. The next real step is unsafe until it is fixed:
     it makes a scored run, a deploy or a deletion wrong or unrecoverable,
     or something is about to pass for the wrong reason

should_interrupt may ONLY be true when severity is 3, and 3 is rare - expect
most checks to raise nothing at all. If you are weighing 2 against 3, it is a 2;
only reach for 3 when you can name the specific next action that becomes wrong
or unrecoverable, and you have verified it rather than inferred it. A severity 3
finding {peer} already knows about, or that the user has already been told about
in an earlier check, does not interrupt either. Interrupting on something that
turns out to be a 2 is worse than staying quiet on something that was a 3: the
user stops trusting the interruptions.
"""


def reviewer_app_link(w: dict) -> str | None:
    """Deep link to the reviewer's own session, using each app's scheme."""
    rv, sid = w.get("reviewer"), w.get("reviewer_session")
    if not sid:
        return None
    if rv == "claude" and os.path.isdir("/Applications/Claude.app"):
        return f"claude://resume?session={sid}"
    if rv == "codex" and w.get("reviewer_session_real") \
            and os.path.isdir("/Applications/ChatGPT.app"):
        return f"codex://threads/{sid}"
    return None


def reviewer_resume_command(w: dict) -> str | None:
    sid = w.get("reviewer_session")
    if not sid:
        return None
    if w.get("reviewer") == "claude":
        return f"claude --resume {sid}"
    return f"codex exec resume {sid}" if w.get("reviewer_session_real") else None


def _extract_verdict(raw: str) -> dict | None:
    """Pull the control object out of a reply that may also contain prose.

    Brace-balanced rather than regex: the object nests (findings is an array of
    objects), so a non-greedy match stops at the first inner `}` and the whole
    verdict gets thrown away.
    """
    if not raw:
        return None
    for start in (i for i, ch in enumerate(raw) if ch == "{"):
        depth, in_str, esc = 0, False, False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = raw[start:i + 1]
                    if '"should_interrupt"' not in chunk and '"review_needed"' not in chunk:
                        break
                    try:
                        val = json.loads(chunk)
                    except json.JSONDecodeError:
                        break
                    return val if isinstance(val, dict) else None
    return None


def _ask(w: dict, reviewer: str, peer: str, cwd: str, delta: str, changed: str,
         review_file: str, *, session_path: str = "", thread_id: str = "",
         check_no: int = 1, from_turn: int = 0, to_turn: int = 0,
         once: str = "") -> dict | None:
    with _LOCK:
        focus = (w.get("focus") or "").strip()
    with _LOCK:
        asks = w.get("asks") or []
        open_asks = [a for a in asks if not a.get("resolved_at")]
        declined = [a for a in asks if a.get("rejected")][-6:]
    raised = [a for a in open_asks if not a.get("carried")]
    carried = [a for a in open_asks if a.get("carried")][-8:]
    outstanding = ""
    if raised:
        lines = "\n".join(
            f"  [{a['id']}] sev {a.get('severity') or 0} raised {a['at'][11:16]}Z"
            f"{' · sent to ' + LABEL.get(peer, peer) if a.get('sent_at') else ' · NOT YET SENT'}"
            f": {a.get('headline') or ''}" for a in raised)
        outstanding += (
            "\nStill open from your earlier checks:\n" + lines + "\n\n"
            "Before anything new, settle these. For each one say plainly whether "
            f"{LABEL.get(peer, peer)} has now addressed it, with the evidence you "
            "checked. List the ids you are confident are handled in \"resolved\" "
            "and they stop being carried forward. An id you do not list stays "
            "open and comes back next check, so do not list one you did not "
            "verify. If an ask was never sent, the user may simply not have seen "
            "it - restate it briefly in your prompt rather than assuming it was "
            "ignored.\n")
    if carried:
        clines = "\n".join(
            f"  [{a['id']}] sev {a.get('severity') or 0} from {a['at'][11:16]}Z"
            f": {a.get('headline') or ''}" for a in carried)
        outstanding += (
            "\nCarried quietly - you filed these as \"fix eventually\" and the user "
            "was never interrupted with them:\n" + clines + "\n\n"
            "These are already on the record. Do NOT re-file them as new findings "
            "and do not spend a prompt on them on their own - that is the "
            "repetition you are told to avoid. What to do with them: check "
            "whether each is now handled and put the ids you verified in "
            "\"resolved\", which drops them for good. If one has genuinely become "
            "urgent - the next real step now depends on it - raise it properly as "
            "a 3 and say what changed. Otherwise leave them be; they ride along "
            "until they are fixed or stop mattering.\n")
    if declined:
        outstanding += (
            "\nThe user has already declined these - do not raise them again "
            "unless you have genuinely new evidence, and say what is new if you do:\n"
            + "\n".join(f"  - {a.get('headline') or a['id']}" for a in declined) + "\n")
    prompt = BRIEF.format(peer=LABEL.get(peer, peer), delta=delta, outstanding=outstanding,
                          changed=changed or "(clean)", review_file=review_file,
                          session_path=session_path, thread_id=thread_id or "(unknown)",
                          check_no=check_no, from_turn=from_turn, to_turn=to_turn,
                          focus=(
                              (f"\nWhat the user is most concerned about right now:\n{focus}\n"
                               if focus else "")
                              + (f"\nThe user asked for this check specifically:\n  {once}\n"
                                 "Answer that first and directly, in your summary and in your\n"
                                 "prompt if it warrants one. Then carry on with the normal\n"
                                 "review. This request applies to this check only.\n"
                                 if once else "")))
    with _LOCK:
        sid = w.get("reviewer_session")
        started = w.get("reviewer_started")
    if reviewer == "claude":
        # one pinned session: the reviewer remembers what it already raised,
        # and the user can open that same conversation from the Reviews tab
        cmd = ["claude", "-p", prompt, "--output-format", "json",
               "--permission-mode", "auto", "--add-dir", NOTES_DIR,
               "--resume" if started else "--session-id", sid]
    else:
        # parity with claude's --permission-mode auto: no approvals, no sandbox.
        # codex mints its own thread id, so the first run has none to resume —
        # it is captured from the rollout it writes and reused from then on.
        # auto mode, not a bypass: the bypass let a reviewer edit the peer's
        # source mid-task even with the brief telling it not to. The flag has to
        # precede `resume`, and --add-dir is rejected by the resume form, so the
        # notes directory is opened with a config override that both forms take.
        cmd = ["codex", "exec", "--approve-for-me"]
        if started and sid and w.get("reviewer_session_real"):
            cmd += ["resume", sid]
        cmd += ["--json", "--skip-git-repo-check",
                "-c", "sandbox_workspace_write.writable_roots="
                      + json.dumps([NOTES_DIR]), prompt]
    with _LOCK:
        if reviewer == "claude":
            w["reviewer_started"] = True
            _save()
    def _run(argv):
        return subprocess.run(argv, cwd=cwd or None, capture_output=True,
                              text=True, timeout=1800, stdin=subprocess.DEVNULL)
    try:
        proc = _run(cmd)
        # `codex exec resume` refuses when anything else holds that thread's
        # writer lock — a leftover process, or the same thread open elsewhere.
        # It exits 1 with empty stdout, and the whole check used to be thrown
        # away. Continuity is worth less than the check: start a fresh thread
        # and let the notes file carry the history forward.
        if (reviewer == "codex" and proc.returncode != 0
                and "already has an active writer" in (proc.stderr or "")
                and "resume" in cmd):
            i = cmd.index("resume")
            _log(f"codex thread {sid} locked by another writer — "
                 f"retrying on a fresh thread")
            with _LOCK:
                w["reviewer_session_real"] = False   # stop resuming a dead lock
                _save()
            proc = _run(cmd[:i] + cmd[i + 2:])       # drop `resume <sid>`
    except (OSError, subprocess.SubprocessError) as exc:
        return {"should_interrupt": False, "review_needed": False, "severity": 0,
                "prompt": "", "headline": "",
                "summary": [f"reviewer unavailable: {exc}"], "file": review_file}
    raw = (proc.stdout or "").strip()
    if reviewer == "codex":
        # --json is JSONL: the first event carries the real thread id, so later
        # checks can resume that exact reviewer instead of starting over, and
        # "open Codex" points at a thread that exists
        tid_seen, msgs = None, []
        for line in raw.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "thread.started" and ev.get("thread_id"):
                tid_seen = ev["thread_id"]
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                msgs.append(item["text"])
        if msgs:
            raw = msgs[-1]
        with _LOCK:
            if tid_seen:
                # only an id codex actually reported can be resumed; anything
                # else was ours to invent and would fail on the next check
                w["reviewer_session"] = tid_seen
                w["reviewer_session_real"] = True
            w["reviewer_started"] = True
            _save()
    else:
        try:                               # claude -p --output-format json
            raw = json.loads(raw).get("result", raw)
        except json.JSONDecodeError:
            pass
    out = _extract_verdict(raw)
    if out is None:
        _log(f"verdict unparsed | {reviewer} rc={proc.returncode} "
             f"stdout={len(proc.stdout or '')}b stderr={(proc.stderr or '')[:200]!r} "
             f"tail={raw[-200:]!r}")
        return None                        # a human was talking to it — ignore
    if "should_interrupt" not in out:      # older replies used review_needed
        out["should_interrupt"] = bool(out.get("review_needed"))
    out["review_needed"] = bool(out["should_interrupt"])
    out.setdefault("severity", 0)
    out.setdefault("summary", [])
    out.setdefault("headline", "")
    out.setdefault("prompt", "")
    out.setdefault("resolved", [])
    out.setdefault("file", review_file)
    return out


# --------------------------------------------------------------------------
# notification
# --------------------------------------------------------------------------

def _add_ask(w: dict, ctl: dict, sev: int, carried: bool) -> None:
    """Put a finding on the record so later checks have to settle it.

    carried=True means it was never shown to the user - it exists only so the
    reviewer must verify it went away instead of quietly re-discovering it.
    The same finding restated check after check must not pile up, so an open
    ask with the same headline absorbs the repeat."""
    head = (ctl.get("headline") or summary_line(ctl) or "").strip()
    key = " ".join(head.lower().split())[:80]
    for a in w.get("asks") or []:
        if a.get("resolved_at") or not key:
            continue
        if " ".join((a.get("headline") or "").lower().split())[:80] == key:
            a["at"] = w["last"]                       # still true as of now
            a["severity"] = max(int(a.get("severity") or 0), sev)
            if not carried:
                a["carried"] = False                  # promoted to a real ask
            return
    w.setdefault("asks", []).append({
        "id": f"A{len(w.get('asks') or []) + 1}", "at": w["last"],
        "headline": head, "severity": sev, "sent_at": None,
        "resolved_at": None, "carried": carried})


def summary_line(ctl: dict) -> str:
    v = ctl.get("summary") or []
    return " · ".join(v) if isinstance(v, list) else str(v)


def _log(line: str) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "notify.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass


def _should_notify(w: dict, sev: int) -> tuple[bool, str]:
    """Whether this finding is worth a noise, given what was already announced.

    Nothing is dropped by saying no here — the ask still appears in the panel
    and still carries forward. This is only about interrupting the room."""
    now = time.time()
    if now - _LAST_NOTIFY["at"] < NOTIFY_FLOOR:
        return False, "another banner seconds ago"
    gap = now - (w.get("last_notify_at") or 0)
    prev = int(w.get("last_notify_sev") or 0)
    # Inside the quiet window only something strictly worse interrupts again.
    # Past it, a new sev 3 is a new sev 3 — an hour-old banner is not a reason
    # to sit on the next one. Repetition is prevented upstream: the reviewer is
    # told not to restate, and identical findings collapse onto one ask.
    if gap < NOTIFY_QUIET and sev <= prev:
        return False, (f"quiet window, {int(NOTIFY_QUIET - gap)}s left and "
                       f"nothing worse than sev {prev}")
    return True, ""


def _notify(reviewer: str, peer: str, text: str, session: str = "") -> None:
    """Sound first: Notification Center is silenced by Focus, afplay is not.
    Failures are logged rather than swallowed - a notification that silently
    does not fire is indistinguishable from a reviewer that said nothing."""
    log = []
    # `beep` rides the alert-volume channel; afplay rides output volume, which
    # is often turned right down. Do both so it is audible either way.
    if shutil.which("osascript"):
        try:
            subprocess.Popen(["osascript", "-e", "beep 2"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.append("beep ok")
        except OSError as exc:
            log.append(f"beep failed: {exc}")
    aiff = "/System/Library/Sounds/Glass.aiff"
    if os.path.exists(aiff) and shutil.which("afplay"):
        try:
            subprocess.Popen(["afplay", aiff], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            log.append("sound ok")
        except OSError as exc:
            log.append(f"sound failed: {exc}")
    title = f"{LABEL.get(reviewer, reviewer)} → {LABEL.get(peer, peer)}: {session or 'review'}"
    # osascript is the only path that reliably displays without a signed app
    # bundle. It posts under Script Editor's identity, so the banner shows and
    # makes noise but clicking it opens Script Editor rather than share-it —
    # that is the trade for not requiring a signing identity.
    if shutil.which("osascript"):
        # AppleScript strings are not JSON: json.dumps escapes non-ASCII as
        # \uXXXX, which osascript parses as literal backslash-u and rejects.
        # Keep the characters, escape only what AppleScript itself needs.
        def _as(v: str) -> str:
            v = "".join(ch for ch in str(v) if ch >= " " or ch == " ")
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
        script = (f"display notification {_as(text[:180])} "
                  f"with title {_as(title)} "
                  f"subtitle {_as(session or 'share-it review')} "
                  f"sound name \"Glass\"")
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        log.append(f"osascript rc={r.returncode} {r.stderr.strip()[:120]}")
    else:
        log.append("no notifier available")
    _log(" | ".join(log))


# --------------------------------------------------------------------------
# control surface
# --------------------------------------------------------------------------

def _trim_prompt(role: str, text: str) -> str:
    """The user turn is the whole brief; show only the part that varies."""
    if role != "user":
        return text[:1200]
    marker = "turns since your last check:"
    if marker in text:
        text = text.split(marker, 1)[1]
        text = text.split("Uncommitted files right now", 1)[0]
        return "what changed: " + text.strip()[:600]
    return text[:400]


def _tool_line(name: str, inp: dict) -> str:
    """One readable line per tool call — what it looked at, not the whole payload."""
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        return os.path.basename(str(inp.get("file_path") or "")) or name
    if name == "Bash":
        return re.sub(r"\s+", " ", str(inp.get("command") or ""))[:160]
    if name in ("Grep", "Glob"):
        return f'{inp.get("pattern") or inp.get("query") or ""} {inp.get("path") or ""}'.strip()
    if name in ("WebFetch", "WebSearch"):
        return str(inp.get("url") or inp.get("query") or "")[:120]
    first = next((v for v in inp.values() if isinstance(v, str)), "")
    return re.sub(r"\s+", " ", first)[:140]


def transcript(path: str, limit: int = 40) -> list[dict]:
    """The reviewer's own conversation, so its reasoning is inspectable."""
    with _LOCK:
        w = _w(path) or {}
        rv, sid = w.get("reviewer"), w.get("reviewer_session")
    if not sid:
        return []
    if rv == "codex":
        hit = None
        for dirpath, _, files in os.walk(os.path.expanduser("~/.codex/sessions")):
            for f in files:
                if sid in f and f.endswith(".jsonl"):
                    hit = os.path.join(dirpath, f)
                    break
            if hit:
                break
    else:
        root = os.path.expanduser("~/.claude/projects")
        hit = None
        for dirpath, _, files in os.walk(root):
            if f"{sid}.jsonl" in files:
                hit = os.path.join(dirpath, f"{sid}.jsonl")
                break
    if not hit:
        return []
    out: list[dict] = []
    try:
        with open(hit, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message") or {}
                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue
                at = rec.get("timestamp", "")
                c = msg.get("content")
                if isinstance(c, str):
                    t = re.sub(r"\s+", " ", c).strip()
                    if t:
                        out.append({"role": role, "text": _trim_prompt(role, t), "at": at})
                    continue
                for b in c or []:
                    if not isinstance(b, dict):
                        continue
                    kind = b.get("type")
                    if kind == "text":
                        t = re.sub(r"\s+", " ", b.get("text", "")).strip()
                        if t:
                            out.append({"role": role, "text": _trim_prompt(role, t), "at": at})
                    elif kind == "tool_use":
                        out.append({"role": "tool", "at": at,
                                    "tool": b.get("name", "tool"),
                                    "text": _tool_line(b.get("name", ""),
                                                       b.get("input") or {})})
    except OSError:
        return []
    return out[-limit:]


def set_focus(path: str, text: str) -> dict:
    with _LOCK:
        w = _w(path)
        if not w:
            return {"ok": False}
        w["focus"] = (text or "").strip()[:2000]
        _save()
    return {"ok": True}


def notes(path: str, limit: int = 20000) -> str:
    with _LOCK:
        w = _w(path)
        f = w and w.get("file")
    if not f or not os.path.exists(f):
        return ""
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""
    if len(text) <= limit:
        return text
    tail = text[-limit:]
    nl = tail.find("\n")                   # never start a view mid-word
    return "\u2026" + (tail[nl + 1:] if 0 <= nl < 400 else tail)


def review_now(path: str, ask: str = "") -> dict:
    """On demand. A force flag, not a watermark rewind - rewinding made the
    progress counter claim more new turns than the session actually has."""
    with _LOCK:
        w = _w(path)
        if not w:
            return {"ok": False, "message": "nothing is being reviewed"}
        if w.get("busy"):
            return {"ok": False, "message": "a review is already running"}
        w["force"] = True
        w["once"] = (ask or "").strip()[:1000]   # this check only, then forgotten
        _save()
    return {"ok": True, "message": ("Asked — starting within a few seconds." if ask
                                    else "Review queued — starting within a few seconds.")}


_COUNT_CACHE: dict[str, tuple[str, int]] = {}


def _turn_count(path: str) -> int:
    """Turn count with a stat-keyed cache. status() runs on every 2.5s poll and
    was re-parsing the whole session file each time — on a 300MB rollout that
    pinned the request for tens of seconds and wedged the whole panel."""
    try:
        st = os.stat(path)
        fp = f"{int(st.st_mtime)}:{st.st_size}"
    except OSError:
        return 0
    hit = _COUNT_CACHE.get(path)
    if hit and hit[0] == fp:
        return hit[1]
    n = len(_turns(path))
    _COUNT_CACHE[path] = (fp, n)
    return n


def _public(w: dict) -> dict:
    out = dict(w)
    out["reviewer_link"] = reviewer_app_link(w)
    out["reviewer_cmd"] = reviewer_resume_command(w)
    out["reviewer_label"] = LABEL.get(w.get("reviewer") or "", "")
    out["peer_label"] = LABEL.get(w.get("source") or "", "")
    if w.get("path") and os.path.exists(w["path"]):
        # recompute here so the UI's progress actually moves between checks
        w["new"] = max(0, _turn_count(w["path"]) - int(w.get("watermark") or 0))
        out["new"] = w["new"]
    return out


def status() -> dict:
    """Every watch, in the order they were started. Sorting by activity made the
    cards swap places under the cursor every time a reviewer woke up."""
    with _LOCK:
        ws = [_public(w) for w in STATE["watches"].values()]
    ws.sort(key=lambda w: (w.get("added") or "", w.get("title") or ""))
    return {"watches": ws}


def enable(path: str, source: str, title: str = "", cwd: str = "") -> dict:
    """Start reviewing one session. Baselines at the live end, not the history."""
    reviewer = REVIEWER_OF.get(source)
    if not reviewer:
        return {"error": f"no reviewer defined for {source}"}
    if not shutil.which(reviewer):
        return {"error": f"{reviewer} CLI not found on PATH"}
    repo = cwd if cwd and os.path.isdir(cwd) else os.path.dirname(path)
    with _LOCK:
        prior = STATE["watches"].get(path)
        if prior:
            was = prior.get("paused")
            prior["paused"] = False
            if cwd and os.path.isdir(cwd) and cwd != prior.get("cwd"):
                prior["cwd"] = cwd         # correcting the root keeps the history
            _save()
            return {"ok": True, "reviewer": prior.get("reviewer"),
                    "message": (f"{LABEL.get(prior['reviewer'], 'Reviewer')} is watching again "
                                f"— {len(prior.get('history') or [])} earlier checks kept."
                                if was else "already being reviewed")}
        w = _blank_watch(path, source, title, repo)
        # baseline one window back, so the very first check has real work to
        # read instead of an empty delta
        w["watermark"] = max(0, len(_turns(path)) - DEFAULT_EVERY)
        # claude accepts a supplied id; codex mints its own, so leave it blank
        # until its first run reports one
        w["reviewer_session"] = str(uuid.uuid4()) if reviewer == "claude" else None
        w["file"] = _note_path(path, source, title)
        w["force"] = True                  # first look should be immediate
        STATE["watches"][path] = w
        _unshare_note_files()
        _save()
    return {"ok": True, "reviewer": reviewer,
            "message": f"{LABEL[reviewer]} is now reviewing this session."}


def disable(path: str) -> dict:
    """Pause, not forget. Throwing away the watermark, the pinned reviewer
    session and everything already concluded would make the next start relearn
    the whole session from scratch."""
    with _LOCK:
        w = _w(path)
        if not w:
            return {"ok": False, "message": "not being reviewed"}
        w["paused"] = True
        w["busy"] = False
        w["force"] = False
        proc = w.pop("_proc", None)
        _save()
    if proc is not None:                   # an in-flight reviewer would keep burning
        try:
            proc.terminate()
        except (OSError, ValueError):
            pass
    return {"ok": True, "message": "Paused — it keeps everything it has learned."}


def resume(path: str) -> dict:
    with _LOCK:
        w = _w(path)
        if not w:
            return {"ok": False, "message": "not being reviewed"}
        w["paused"] = False
        _save()
    return {"ok": True, "message": f"{LABEL.get(w['reviewer'], 'Reviewer')} is watching again."}


def peer_app_link(w: dict) -> str | None:
    """Deep link to the session under review."""
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                  w.get("path") or "")
    if not m:
        return None
    if w.get("source") == "codex" and os.path.isdir("/Applications/ChatGPT.app"):
        return f"codex://threads/{m.group(1)}"
    if w.get("source") in ("claude", "claude-code") and os.path.isdir("/Applications/Claude.app"):
        return f"claude://resume?session={m.group(1)}"
    return None


def peer_message(w: dict, entry: dict) -> str:
    """What actually gets pasted. Short on purpose: the peer can open the file.

    A wall of prose costs the peer the same context the interruption was meant
    to save, so this is a headline, the ask, and where to read the rest."""
    who = LABEL.get(w.get("reviewer") or "", "A reviewer")
    sev = int(entry.get("severity") or 0)
    head = (entry.get("headline") or "").strip()
    body = (entry.get("prompt") or "").strip()
    if not body:
        body = "\n".join(f"- {b}" for b in (entry.get("summary") or []))
    parts = [f"[{who} reviewed your last few turns · severity {sev}]"]
    if head:
        parts.append(head)
    parts.append(body)
    if entry.get("file"):
        parts.append(f"Full notes, with what would confirm or refute this:\n{entry['file']}")
    return "\n\n".join(parts)


def send_to_peer(path: str, i: int = 0) -> dict:
    """Clipboard, then open the peer's own session so the paste is one keystroke.

    Not `codex exec resume` / `claude -p --resume`: those do append to the
    rollout file, but a live interactive session never sees the append, and the
    CLI runs a full autonomous turn in the repo to get there. Handing the user
    a loaded clipboard in the right window is the honest version."""
    with _LOCK:
        w = _w(path)
        hist = (w or {}).get("history") or []
        if not w or not (0 <= i < len(hist)):
            return {"ok": False, "message": "no such review"}
        entry = hist[i]
        peer = LABEL.get(w.get("source") or "", "the agent")
    if shutil.which("pbcopy"):
        subprocess.run(["pbcopy"], input=peer_message(w, entry), text=True)
    link, opened, pasted = peer_app_link(w), False, False
    if link:
        opened = subprocess.run(["open", link], capture_output=True,
                                timeout=10).returncode == 0
    if opened:
        time.sleep(1.4)                    # the app needs to focus its input first
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to keystroke "v" using command down'],
            capture_output=True, text=True, timeout=15)
        pasted = r.returncode == 0
        if not pasted:                     # almost always a missing Accessibility grant
            _log(f"paste failed rc={r.returncode} {r.stderr.strip()[:140]}")
    with _LOCK:
        entry["sent_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry["send_state"] = ("pasted" if pasted else "opened" if opened else "copied")
        for a in w.get("asks") or []:      # the next check verifies what was sent
            if a["at"] == entry.get("at"):
                a["sent_at"] = entry["sent_at"]
        w["stats"]["sent"] += 1
        if (w.get("pending") or {}).get("at") == entry.get("at"):
            w["pending"] = None
        _save()
    if pasted:
        return {"ok": True, "message": f"Pasted into {peer} — press Enter to send"}
    return {"ok": True, "message": (f"Copied — {peer} is open, press ⌘V" if opened
                                    else f"Copied — paste it to {peer}")}


def reject(path: str, i: int = 0) -> dict:
    """The user says no. The ask stops being carried forward and the reviewer is
    told it was declined, so it does not simply raise the same thing next check."""
    with _LOCK:
        w = _w(path)
        hist = (w or {}).get("history") or []
        if not w or not (0 <= i < len(hist)):
            return {"ok": False, "message": "no such review"}
        entry = hist[i]
        entry["rejected_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for a in w.get("asks") or []:
            if a["at"] == entry.get("at"):
                a["resolved_at"] = entry["rejected_at"]
                a["rejected"] = True
        if (w.get("pending") or {}).get("at") == entry.get("at"):
            w["pending"] = None
        _save()
    return {"ok": True, "message": "Declined — the reviewer will not raise it again."}


def copy_review(path: str, i: int = 0) -> dict:
    """Any recorded finding is copyable, not only the ones worth interrupting for."""
    with _LOCK:
        w = _w(path)
        hist = (w or {}).get("history") or []
        if not w or not (0 <= i < len(hist)):
            return {"ok": False, "message": "no such review"}
        entry = hist[i]
        text = peer_message(w, entry)
        peer = LABEL.get(w.get("source") or "", "the agent")
        entry.setdefault("sent_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        entry.setdefault("send_state", "copied")
        for a in w.get("asks") or []:      # copying it counts as handling it
            if a["at"] == entry.get("at"):
                a["sent_at"] = entry["sent_at"]
        if (w.get("pending") or {}).get("at") == entry.get("at"):
            w["pending"] = None
        w["stats"]["sent"] += 1
        _save()
    if shutil.which("pbcopy"):
        subprocess.run(["pbcopy"], input=text, text=True)
    return {"ok": True, "message": f"Copied — paste it to {peer}"}


def resolve(path: str, action: str) -> dict:
    """send → hand it to the peer; dismiss → keep it in the file only."""
    with _LOCK:
        w = _w(path)
        p = w and w.get("pending")
        if not p:
            return {"ok": False, "message": "nothing pending"}
        if action != "send":
            w["pending"] = None
            _save()
            return {"ok": True, "message": "Left in the review file."}
        idx = next((i for i, h in enumerate(w["history"])
                    if h.get("at") == p.get("at")), 0)
    return send_to_peer(path, idx)


# --------------------------------------------------------------------------
# loop
# --------------------------------------------------------------------------

def _check(path: str) -> None:
    """One reviewer pass. Runs on its own thread so a slow reviewer on one
    session never holds up the reviewers on the others."""
    with _LOCK:
        w = _w(path)
        if not w:
            return
        cwd, reviewer, peer = w["cwd"], w["reviewer"], w["source"]
        mark, rf = w["watermark"], w["file"]
        check_no = w["stats"]["checks"] + 1
        once = w.pop("once", "")           # consumed by this check and no other
    turns = _turns(path)
    delta = _window(path, mark)
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                  os.path.basename(path))
    ctl = _ask(w, reviewer, peer, cwd, delta, _dirty(cwd), rf,
               session_path=path, thread_id=m.group(1) if m else "",
               check_no=check_no, from_turn=mark, to_turn=len(turns), once=once)

    with _LOCK:
        w = _w(path)
        if not w:                          # stopped while the reviewer was thinking
            return
        w["busy"] = False
        w["watermark"] = len(turns)
        w["new"] = 0
        w["last"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        w["last_at"] = time.time()
        w["stats"]["checks"] += 1
        if ctl is None:
            w["stats"]["ignored"] += 1
            _save()
            return
        ctl["at"] = w["last"]
        ctl["reviewer"] = reviewer
        ctl["peer"] = peer
        ctl["title"] = w["title"]
        w["history"].insert(0, ctl)
        del w["history"][60:]
        done = {str(x).strip().upper() for x in (ctl.get("resolved") or [])}
        for a in w.get("asks") or []:
            if a["id"].upper() in done and not a.get("resolved_at"):
                a["resolved_at"] = w["last"]
        sev = int(ctl.get("severity") or 0)
        if ctl.get("should_interrupt") and ctl.get("prompt"):
            w["stats"]["raised"] += 1
            w["pending"] = ctl
            _add_ask(w, ctl, sev, carried=False)
            # only the "fix before the next step" tier is worth a banner, and
            # only if the last one has had time to land
            if sev >= 3:
                ok, why = _should_notify(w, sev)
                waiting = sum(1 for a in w["asks"]
                              if not a.get("resolved_at") and not a.get("sent_at")
                              and not a.get("carried"))
                if ok:
                    head = ctl.get("headline") or summary_line(ctl) or "review ready"
                    if waiting > 1:
                        head = f"{head}  (+{waiting - 1} still waiting)"
                    w["last_notify_at"] = time.time()
                    w["last_notify_sev"] = sev
                    _notify(reviewer, peer, head, w.get("title") or "")
                    _LAST_NOTIFY["at"] = time.time()
                else:
                    _log(f"banner held back ({why}) — sev {sev}, {waiting} waiting: "
                         f"{(ctl.get('headline') or '')[:60]}")
        elif sev >= 2:
            # "fix eventually" earns no banner, but dropping it is worse than
            # repeating it: it rides along until the reviewer verifies it is gone
            w["stats"]["recorded"] += 1
            _add_ask(w, ctl, sev, carried=True)
        elif sev > 0:
            w["stats"]["recorded"] += 1
        else:
            w["stats"]["quiet"] += 1
        _save()


def _tick() -> None:
    due = []
    with _LOCK:
        for path, w in list(STATE["watches"].items()):
            if w.get("busy"):
                since = w.get("busy_since") or 0
                if since and time.time() - since > 2100:   # 35 min: wedged
                    w["busy"] = False
                    w["stats"]["ignored"] += 1
                else:
                    continue
            if w.get("paused") or w.get("pending") or not os.path.exists(path):
                continue
            w["new"] = max(0, _turn_count(path) - int(w.get("watermark") or 0))
            forced = bool(w.pop("force", False))
            if not forced and (w["new"] < w["every"] or w["new"] == 0):
                continue
            if not forced and time.time() - (w.get("last_at") or 0) < MIN_GAP_SECONDS:
                w["cooling"] = True        # turns are there, the clock is not
                continue
            w["cooling"] = False
            if sum(1 for x in STATE["watches"].values() if x.get("busy")) >= MAX_CONCURRENT:
                w["force"] = forced        # try again next tick, keep the request
                continue
            w["busy"] = True
            w["busy_since"] = time.time()
            due.append(path)
        if due:
            _save()
    for path in due:
        threading.Thread(target=_check, args=(path,), daemon=True).start()


def _loop() -> None:
    while True:
        time.sleep(POLL_SECONDS)
        try:
            _tick()
        except Exception:                  # a reviewer fault must not kill share-it
            with _LOCK:
                for w in STATE["watches"].values():
                    w["busy"] = False


def start() -> None:
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    _load()
    threading.Thread(target=_loop, name="shareit-review", daemon=True).start()
