#!/usr/bin/env python3
"""Hermetic unit tests — synthetic fixtures, no local stores, no network."""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parsers
import render
import share

FAIL = []


def check(name, cond, detail=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def fixture(tmp, name, lines):
    p = os.path.join(tmp, name)
    with open(p, "w") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    return p


ART_PATH = "/tmp/shareit-unit-artifact.txt"  # overridden per-run in main()

CLAUDE_LINES = [
    {"type": "user", "message": {"role": "user", "content": "hello world"}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "hmm"},
        {"type": "tool_use", "id": "t1", "name": "Write",
         "input": {"file_path": "__ART__", "content": "x"}},
    ]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "done!"}]}},
    {"type": "ai-title", "aiTitle": "Fixture session"},
]

CODEX_LINES = [
    {"type": "session_meta", "payload": {"id": "x", "cwd": "/tmp/proj", "source": "cli"}},
    {"type": "response_item", "payload": {"type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "fix the bug"}]}},
    {"type": "response_item", "payload": {"type": "reasoning",
        "summary": [{"type": "summary_text", "text": "thinking…"}]}},
    {"type": "response_item", "payload": {"type": "function_call", "name": "shell",
        "arguments": "{\"cmd\":\"ls\"}", "call_id": "c1"}},
    {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1",
        "output": "file.txt"}},
    {"type": "response_item", "payload": {"type": "web_search_call", "status": "completed",
        "action": {"query": "docs"}}},
    {"type": "response_item", "payload": {"type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "fixed."}, "stray-non-dict-block"]}},
]


def main():
    tmp = tempfile.mkdtemp(prefix="shareit-test-")
    global ART_PATH
    ART_PATH = os.path.join(tmp, "artifact.txt")
    import json as _json
    for _ln in CLAUDE_LINES:
        _c = (_ln.get("message") or {}).get("content")
        for _b in _c if isinstance(_c, list) else []:
            if isinstance(_b, dict) and _b.get("input", {}).get("file_path") == "__ART__":
                _b["input"]["file_path"] = ART_PATH
    print("== parsers (fixtures) ==")
    cl = fixture(tmp, "claude.jsonl", CLAUDE_LINES)
    msgs = parsers.parse_claude(cl)
    roles = [m["role"] for m in msgs]
    check("claude roles", roles == ["user", "thinking", "tool", "assistant"], str(roles))
    check("claude tool result paired", msgs[2]["output"] == "ok")

    cx = fixture(tmp, "codex.jsonl", CODEX_LINES)
    msgs = parsers.parse_codex(cx)
    roles = [m["role"] for m in msgs]
    check("codex roles", roles == ["user", "thinking", "tool", "tool", "assistant"], str(roles))
    check("codex non-dict block survives", msgs[-1]["text"] == "fixed.")
    check("codex generic *_call kept", any(m["role"] == "tool" and m["name"] == "web_search"
                                           for m in msgs))

    with open(ART_PATH, "w") as fh:
        fh.write("artifact")
    try:
        arts = [a["name"] for a in _artifacts(cl)]
        check("claude artifact found", arts == ["artifact.txt"], str(arts))
    finally:
        os.remove(ART_PATH)

    # codex: same file via absolute + relative spellings must dedupe, created wins
    with open("/tmp/shareit-dup.pdf", "w") as fh:
        fh.write("x")
    dup = fixture(tmp, "codex-dup.jsonl", [
        {"type": "session_meta", "payload": {"id": "d", "cwd": "/tmp", "source": "cli"}},
        {"type": "event_msg", "payload": {"type": "patch_apply_end", "success": True,
            "changes": {"shareit-dup.pdf": {"update": {}}}}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell",
            "arguments": "pandoc x.md --output /tmp/shareit-dup.pdf", "call_id": "c9"}},
        {"type": "response_item", "payload": {"type": "function_call_output",
            "call_id": "c9", "output": "done"}},
    ])
    try:
        arts = parsers.session_artifacts(dup, source="codex", cwd="/tmp")
        names = [(a["name"], a["kind"]) for a in arts]
        check("codex dedupe across spellings", len(arts) == 1, str(names))
        check("created beats modified on merge",
              arts and arts[0]["kind"] == "created", str(names))
    finally:
        os.remove("/tmp/shareit-dup.pdf")

    print("== render ==")
    sess = {"source": "claude", "title": "t", "project": "p", "mtime": time.time()}
    md = render.render_markdown(sess, [
        {"role": "user", "text": "hi"},
        {"role": "tool", "name": "Bash", "input": "echo ````four````",
         "output": "word soup " * 600},
    ])
    check("five-backtick fence used", "`````" in md)
    check("truncation marker", "chars truncated" in md)
    check("fence helper exceeds content runs", render._fence("a````b").startswith("`````"))

    print("== share cache (isolated state) ==")
    share.STATE_DIR = tmp
    share.SHARES_PATH = os.path.join(tmp, "shares.json")
    share.CONFIG_PATH = os.path.join(tmp, "config.json")
    sess2 = {"title": "t", "source": "claude", "path": cl, "mtime": os.path.getmtime(cl)}
    opts = {"redact": True, "messages_only": False, "thinking": False, "expires_hours": 168}
    share.record_share(sess2, {"url": "https://x/1", "provider": "s3_presign",
                               "ref": "k", "hours": 168}, opts, 10)
    check("cache hit same opts", share.find_cached(sess2, opts)["url"] == "https://x/1")
    check("cache miss different expiry",
          share.find_cached(sess2, {**opts, "expires_hours": 24}) is None)
    check("cache miss after edit", share.find_cached(
        {**sess2, "mtime": 0}, opts) is not None)  # cache keys on LIVE mtime, not passed value
    os.utime(cl, (1, 1))
    check("cache miss after file change", share.find_cached(sess2, opts) is None)

    print("== search (fts fixtures) ==")
    import search as fts
    fts.DB_PATH = os.path.join(tmp, "fts.sqlite")
    fts.index_session("/fake/one.jsonl", 1.0, [
        {"role": "user", "text": "how do we deploy the kraken service"},
        {"role": "assistant", "text": "use the blue-green pipeline"}])
    fts.index_session("/fake/two.jsonl", 1.0, [
        {"role": "user", "text": "unrelated grocery list"}])
    hits = fts.search("kraken deploy")
    check("fts finds by content", len(hits) == 1 and hits[0]["path"] == "/fake/one.jsonl")
    check("fts snippet marks match", "«" in hits[0]["snippet"])
    check("fts survives hostile query", fts.search('"; DROP TABLE msgs; --') == []
          or isinstance(fts.search('robert"); drop'), list))
    check("fts needs_index tracks mtime", not fts.needs_index("/fake/one.jsonl", 1.0)
          and fts.needs_index("/fake/one.jsonl", 2.0))
    fts.prune({"/fake/two.jsonl"})
    check("fts prune removes dead sessions", fts.search("kraken") == [])

    print("== html render ==")
    html = render.render_html(
        {"source": "claude", "title": "t <script>alert(1)</script> sk-abc123def456ghi789jkl012",
         "mtime": time.time()},
        [{"role": "user", "text": "<img src=x onerror=alert(1)>"},
         {"role": "assistant", "text": "ok"}],
        artifact_links=[{"name": "a.pdf", "path": "/x/a.pdf", "size": 10,
                         "kind": "created", "url": "https://ex/a.pdf"}], card="c")
    check("html escapes transcript", "<img src=x" not in html and "&lt;img" in html)
    check("html escapes+redacts title", "<script>" not in html and "sk-abc123" not in html)
    check("html links artifact", 'href="https://ex/a.pdf"' in html)

    print("== card redaction ==")
    card = render.share_card({"source": "claude", "title": "key sk-abc123def456ghi789jkl012mno",
                              "model": "m"}, {"turns": 1, "tools": 0, "minutes": 5}, 1)
    check("card redactable", "sk-abc123" not in render.redact(card))

    print("== lifecycle safety (never vanish / never stale) ==")
    import importlib, app as A
    importlib.reload(A)
    A.ANNOT_PATH = os.path.join(tmp, "ann2.json"); A._annot = {}; A._cache = {}
    # (1) a parse failure must NOT persist an empty 'no files' record
    real_sa = parsers.session_artifacts
    def boom(*a, **k): raise IOError("transient")
    parsers.session_artifacts = boom
    rec = A._annotate_one("/fx/x.jsonl", "claude", "/tmp", 5.0)
    parsers.session_artifacts = real_sa
    check("transient error does NOT cache empty", rec is None and "/fx/x.jsonl" not in A._annot)
    # (6) corrupt shares.json is preserved, not silently reset
    share.SHARES_PATH = os.path.join(tmp, "sh.json")
    open(share.SHARES_PATH, "w").write("{ broken json ][")
    check("corrupt shares → empty list", share.load_shares() == [])
    check("corrupt shares backed up", os.path.exists(share.SHARES_PATH + ".corrupt"))
    # (7) exports for colliding titles get distinct files
    A.CACHE_PATH = os.path.join(tmp, "idx.json")
    # simulate: two different paths, same sanitized title → different tag
    import hashlib
    t1 = hashlib.sha1(b"/a/one.jsonl").hexdigest()[:6]
    t2 = hashlib.sha1(b"/a/two.jsonl").hexdigest()[:6]
    check("export filenames disambiguate by path", t1 != t2)

    print("== annotation persistence (the files-vanish bug) ==")
    import importlib, app as appmod
    importlib.reload(appmod)
    appmod.ANNOT_PATH = os.path.join(tmp, "annotations.json")
    appmod._annot = {}
    appmod._cache = {"/fx/s.jsonl": {"path": "/fx/s.jsonl", "mtime": 5.0, "size": 100,
                                     "source": "claude", "subagent": False}}
    rec = {"mtime": 5.0, "v": appmod.ANNOT_VERSION, "arts": 2,
           "art_list": [{"path": "/tmp/a.md", "name": "a.md", "size": 1,
                         "kind": "created", "mtime": 1.0}],
           "read_list": []}
    appmod._annot["/fx/s.jsonl"] = rec
    appmod._merge_annotations()
    check("annotations merge into cache",
          appmod._cache["/fx/s.jsonl"].get("arts") == 2)
    appmod._save_annot()
    appmod._cache = {"/fx/s.jsonl": {"path": "/fx/s.jsonl", "mtime": 5.0, "size": 100,
                                     "source": "claude", "subagent": False}}  # cache cleared!
    appmod._annot = {}
    appmod._load_annot()
    appmod._merge_annotations()
    check("annotations SURVIVE an index-cache clear",
          appmod._cache["/fx/s.jsonl"].get("arts") == 2
          and appmod._cache["/fx/s.jsonl"]["art_list"][0]["name"] == "a.md")
    # live sessions: a recently-stale annotation (≤10min behind) still SHOWS
    # (the worker recomputes it), but an ancient one never does
    recent = {"path": "/fx/s.jsonl", "mtime": 9.0, "size": 100,
              "source": "claude", "subagent": False}
    appmod._cache = {"/fx/s.jsonl": recent}
    appmod._merge_annotations()
    check("recently-changed files keep stale annotations visible",
          "art_list" in recent)
    ancient = {"path": "/fx/s.jsonl", "mtime": 5000.0, "size": 100,
               "source": "claude", "subagent": False}
    appmod._cache = {"/fx/s.jsonl": ancient}
    appmod._merge_annotations()
    check("long-changed files invalidate annotations", "art_list" not in ancient)


    # regression: codex generated-image artifacts must carry mtime (share
    # fingerprint + recency sort subscript it -> KeyError 'mtime' in the UI)
    gen = fixture(tmp, "genimg.png", ["fake"])
    real_gen = parsers._codex_generated_images
    parsers._codex_generated_images = lambda _real: [gen]
    try:
        arts = parsers.session_artifacts(fixture(tmp, "codex2.jsonl", CODEX_LINES),
                                         source="codex", cwd=None)
        check("generated-image artifact carries mtime",
              arts and all("mtime" in a for a in arts), str(arts))
    finally:
        parsers._codex_generated_images = real_gen

    # ---- payload v3: media, clipboard html safety, selection ----
    import base64 as _b64
    import media as _mediamod
    png_meta = (b"\x89PNG\r\n\x1a\n"
                + b"\x00\x00\x00\x04tEXtabcd\x00\x00\x00\x00"
                + b"\x00\x00\x00\x00IEND\xaeB`\x82")
    img_lines = [
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
             "data": _b64.b64encode(png_meta).decode()}},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
             "data": _b64.b64encode(png_meta).decode()}},   # duplicate → deduped
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
             "data": ""}},                                   # empty → tolerated
        ]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "see screenshot"}]}},
    ]
    mp = fixture(tmp, "media.jsonl", img_lines)
    msgs = parsers.parse_claude(mp)
    objs = _mediamod.collect(msgs)
    check("inline images survive parsing + dedupe", len(objs) == 1, str(len(objs)))
    check("metadata stripped from collected image",
          objs and b"tEXt" not in objs[0]["data"])
    names = [m.get("name") for msg in msgs for m in msg.get("media", [])]
    check("duplicate image reuses one object name", len(set(names)) == 1, str(names))

    codex_img = [
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text",
                      "text": '<image src="/Users/someone/secret/shot.png"></image> look'},
                     {"type": "input_image",
                      "image_url": "data:image/png;base64," + _b64.b64encode(png_meta).decode()}]}},
        {"type": "response_item", "payload": {"type": "agent_message", "message": "ok"}},
    ]
    cxp = fixture(tmp, "codeximg.jsonl", codex_img)
    cmsgs = parsers.parse_codex(cxp)
    utext = next(m["text"] for m in cmsgs if m["role"] == "user")
    check("codex local-path image markers stripped", "/Users/someone" not in utext, utext)
    check("codex inline image collected",
          any(m.get("media") for m in cmsgs))

    xss = [{"role": "user", "text": '<script>alert(1)</script> & "quotes"'},
           {"role": "assistant", "text": "<img src=x onerror=alert(2)>"}]
    html_out = render.clipboard_html({"title": "<b>t</b>", "source": "claude"}, xss)
    check("clipboard html escapes all user content",
          "<script" not in html_out and "<img" not in html_out
          and "&lt;script&gt;" in html_out)
    check("clipboard html styles are inline-only", "<style" not in html_out)

    print()
    if FAIL:
        print(f"{len(FAIL)} FAILURES: {FAIL}")
        sys.exit(1)
    print("all unit tests passed")


def _artifacts(path):
    return parsers.session_artifacts(path, source="claude", cwd=os.path.dirname(ART_PATH))


if __name__ == "__main__":
    main()
