#!/usr/bin/env python3
"""Smoke tests for share-it: index scan, parsers, renderer, redaction.

Runs against the real local transcript stores (read-only). No uploads.
Usage: python3 tests/smoke.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parsers
import render

FAILURES = []


def check(name, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def main():
    print("== index scan ==")
    cache = {}
    t0 = time.time()
    sessions = parsers.scan_sessions(cache)
    cold = time.time() - t0
    t0 = time.time()
    sessions = parsers.scan_sessions(cache)
    warm = time.time() - t0
    n_claude = sum(1 for s in sessions if s["source"] == "claude")
    n_codex = sum(1 for s in sessions if s["source"] == "codex")
    n_cowork = sum(1 for s in sessions if s["app"] == "cowork")
    untitled = sum(1 for s in sessions if s["title"] == "(untitled)")
    print(f"  {len(sessions)} sessions (claude={n_claude} codex={n_codex} cowork={n_cowork}), "
          f"cold={cold:.1f}s warm={warm:.2f}s, untitled={untitled}")
    check("finds sessions from both stores", n_claude > 0 and n_codex > 0)
    check("warm scan under 2s", warm < 2, f"{warm:.2f}s")
    check("every session has required fields",
          all(all(k in s for k in ("path", "source", "app", "title", "mtime", "size", "subagent"))
              for s in sessions))
    check("titles mostly resolved", untitled < len(sessions) * 0.3, f"{untitled}/{len(sessions)}")

    if not sessions:
        print("no local session stores found — skipping store-dependent checks")
        print("run tests/unit.py for hermetic coverage")
        sys.exit(0)

    print("== parsers ==")
    parsed = empty = errors = 0
    roles_seen = set()
    sample = [s for s in sessions if not s["subagent"]][:40] or sessions[:5]
    for s in sample:
        try:
            msgs = parsers.parse_session(s["path"])
        except Exception as e:
            errors += 1
            print(f"  parse error: {s['path']}: {e}")
            continue
        parsed += 1
        if not msgs:
            empty += 1
        roles_seen |= {m["role"] for m in msgs}
    check("parses 40 recent sessions without exceptions", errors == 0, f"{errors} errors")
    check("all roles observed", {"user", "assistant", "tool"} <= roles_seen, str(roles_seen))
    check("few empty parses", empty <= len(sample) * 0.2, f"{empty}/{len(sample)}")

    print("== renderer ==")
    big = max(sample, key=lambda s: s["size"])
    msgs = parsers.parse_session(big["path"])
    md_full = render.render_markdown(big, msgs)
    md_msgs = render.render_markdown(big, msgs, messages_only=True)
    md_think = render.render_markdown(big, msgs, include_thinking=True)
    check("renders largest sample", len(md_full) > 200, f"{len(md_full):,} chars")
    check("messages-only is smaller and tool-free",
          len(md_msgs) <= len(md_full) and "### Tool:" not in md_msgs)
    check("thinking mode adds content", len(md_think) >= len(md_full))
    check("no giant base64 runs survive", "base64," not in md_full)

    print("== redaction ==")
    fake = {"source": "claude", "title": "t", "project": "p", "mtime": time.time()}
    poisoned = [
        {"role": "user", "text": "my key is sk-abc123def456ghi789jkl012mno345 ok"},
        {"role": "assistant", "text": "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY and AKIAIOSFODNN7REALKEY"},
        {"role": "tool", "name": "Bash", "input": "curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U'",
         "output": "ghp_16C7e42F292c6912E7710c838347Ae178B4a token=hunter2secret"},
    ]
    md = render.render_markdown(fake, poisoned)
    for needle in ("sk-abc123", "wJalrXUtnFEMI", "AKIAIOSFODNN7REALKEY", "eyJhbGciOiJIUzI1NiJ9",
                   "ghp_16C7e42F292c6912E7710c838347Ae178B4a", "hunter2secret"):
        check(f"redacts {needle[:20]}…", needle not in md)
    md_raw = render.render_markdown(fake, poisoned, redact_secrets=False)
    check("unredacted mode keeps content", "AKIAIOSFODNN7REALKEY" in md_raw)
    check("fence safety", render._fence("hello ```test```").startswith("````"))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURES: {FAILURES}")
        sys.exit(1)
    print("all smoke tests passed")


if __name__ == "__main__":
    main()
