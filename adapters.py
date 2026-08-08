"""Harness adapters — the pluggable seam between share-it and each agent CLI.

To add a new harness (OpenCode, Pi, aider, …): subclass Adapter, implement the
five methods against its on-disk session format, append an instance to ADAPTERS.
Everything else — indexing, search, peek, sharing, artifacts, the UI legend —
picks it up from the registry.
"""
import os

import cursor as _cursor
import parsers
import pi as _pi
import opencode as _opencode
import goose as _goose
import continuedev as _continue
import cline as _cline
import copilot as _copilot


class Adapter:
    id = ""            # stable key; also the UI badge/glyph key
    label = ""         # human name shown in the app
    note = ""          # one-liner for the sources legend

    def roots(self):
        """Directories this adapter may read transcripts from."""
        raise NotImplementedError

    def owns(self, real_path):
        # ids may be "<file>" or "<file>#<subid>" (multi-session SQLite stores)
        base = real_path.split("#", 1)[0]
        for r in self.roots():
            rr = os.path.realpath(r)
            if base == rr or base.startswith(rr + os.sep):
                return True
        return False

    def parse(self, path):
        """path → unified message list (role: user|assistant|thinking|tool)."""
        raise NotImplementedError

    def artifacts(self, path, cwd=None):
        """Files the agent successfully wrote → [{path,name,size,kind}]."""
        return []

    def reads(self, path, cwd=None, limit=40):
        """Files the agent read (context manifest) → [{path,name,size}]."""
        return []

    def resume_command(self, path, cwd=None):
        """Shell command that reopens this session in its harness, or None."""
        return None

    def discover(self):
        """Non-file harnesses (SQLite-backed) yield [{id, title, cwd}] here.
        File-globbing adapters (Claude/Codex) return [] — scan_sessions walks roots()."""
        return []

    def app_link(self, path):
        """Deep link that opens this session in the harness's desktop app, or None."""
        return None


class ClaudeAdapter(Adapter):
    id = "claude"
    label = "Claude Code"
    note = "CLI · desktop · IDE"

    def roots(self):
        return [parsers.CLAUDE_ROOT, parsers.RESCUE_ROOT]

    def parse(self, path):
        return parsers.parse_claude(os.path.realpath(path))

    def artifacts(self, path, cwd=None):
        return parsers.session_artifacts(path, source="claude", cwd=cwd)

    def reads(self, path, cwd=None, limit=40):
        return parsers.session_reads(path, source="claude", cwd=cwd, limit=limit)

    def resume_command(self, path, cwd=None):
        import shlex
        sid = os.path.splitext(os.path.basename(path))[0]
        if sid.startswith("agent-"):
            return None  # subagent transcripts aren't resumable
        prefix = f"cd {shlex.quote(cwd)} && " if cwd else ""
        return f"{prefix}claude --resume {sid}"

    def app_link(self, path):
        sid = os.path.splitext(os.path.basename(path))[0]
        if sid.startswith("agent-") or not os.path.isdir("/Applications/Claude.app"):
            return None
        # verified against Claude.app: claude://resume?session=<id> imports the CLI session
        return f"claude://resume?session={sid}"


class CodexAdapter(Adapter):
    id = "codex"
    label = "Codex"
    note = "CLI · desktop"

    def roots(self):
        return [parsers.CODEX_ROOT, parsers.CODEX_ARCHIVE]

    def parse(self, path):
        return parsers.parse_codex(os.path.realpath(path))

    def artifacts(self, path, cwd=None):
        return parsers.session_artifacts(path, source="codex", cwd=cwd)

    @staticmethod
    def _thread_id(path):
        import re
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
                      r"\.jsonl$", os.path.basename(path))
        return m.group(1) if m else None

    def resume_command(self, path, cwd=None):
        import shlex
        tid = self._thread_id(path)
        if not tid:
            return None
        prefix = f"cd {shlex.quote(cwd)} && " if cwd else ""
        return f"{prefix}codex resume {tid}"

    def app_link(self, path):
        tid = self._thread_id(path)
        if not tid or not os.path.isdir("/Applications/ChatGPT.app"):
            return None
        # observed (undocumented) route in ChatGPT.app's codex:// scheme; may change —
        # the endpoint always falls back to the terminal command if it fails
        return f"codex://threads/{tid}"


class CursorAdapter(Adapter):
    id = "cursor"
    label = "Cursor"
    note = "editor (SQLite)"

    def roots(self):
        return [_cursor.CURSOR_ROOT]

    def owns(self, real_path):
        base = real_path.split("#", 1)[0]
        return "#" in real_path and (base == _cursor.CURSOR_ROOT
                                     or base.startswith(_cursor.CURSOR_ROOT + os.sep))

    def discover(self):
        out = []
        for s in _cursor.discover():   # one DB scan for the named majority
            title = s.get("title")
            if not title:              # unnamed → derive from first message (rare)
                title, _ = _cursor.meta_for(s["id"])
            out.append({"id": s["id"], "title": title or "(untitled)", "cwd": "",
                        "ts": s.get("ts") or 0})
        return out

    def parse(self, path):
        return _cursor.parse(path)


class PiAdapter(Adapter):
    id = "pi"; label = "Pi"; note = "coding agent (JSONL)"

    def roots(self):
        return [_pi.ROOT]

    def parse(self, path):
        return _pi.parse(path)

    def discover(self):
        return _pi.discover()


class OpenCodeAdapter(Adapter):
    id = "opencode"; label = "OpenCode"; note = "coding agent (SQLite)"

    def roots(self):
        return _opencode.data_dirs()

    def parse(self, path):
        return _opencode.parse(path)

    def discover(self):
        return _opencode.discover()


class GooseAdapter(Adapter):
    id = "goose"; label = "Goose"; note = "coding agent (SQLite)"

    def roots(self):
        return [_goose._DIR]

    def parse(self, path):
        return _goose.parse(path)

    def discover(self):
        return _goose.discover()


class ContinueAdapter(Adapter):
    id = "continue"; label = "Continue"; note = "editor extension"

    def roots(self):
        return [_continue._SESS]

    def parse(self, path):
        return _continue.parse(path)

    def discover(self):
        return _continue.discover()


class _ClineFamily(Adapter):
    ext = ""

    def roots(self):
        rs = [os.path.join(b, self.ext, "tasks") for b in _cline._BASES]
        if self.ext == "saoudrizwan.claude-dev":
            rs.append(os.path.join(os.environ.get("CLINE_DATA_DIR")
                                   or os.path.expanduser("~/.cline/data"), "tasks"))
        return rs

    def parse(self, path):
        return _cline.parse(path)

    def discover(self):
        return _cline.discover(self.ext)


class ClineAdapter(_ClineFamily):
    id = "cline"; label = "Cline"; note = "editor extension"; ext = "saoudrizwan.claude-dev"


class RooAdapter(_ClineFamily):
    id = "roo"; label = "Roo Code"; note = "editor extension"; ext = "rooveterinaryinc.roo-cline"


class CopilotAdapter(Adapter):
    id = "copilot"; label = "Copilot"; note = "VS Code chat"

    def roots(self):
        return _copilot._BASES

    def parse(self, path):
        return _copilot.parse(path)

    def discover(self):
        return _copilot.discover()


ADAPTERS = [ClaudeAdapter(), CodexAdapter()]
if _cursor.available():           # only surface a client when it's installed
    ADAPTERS.append(CursorAdapter())
if _pi.available():
    ADAPTERS.append(PiAdapter())
if _opencode.available():
    ADAPTERS.append(OpenCodeAdapter())
if _goose.available():
    ADAPTERS.append(GooseAdapter())
if _continue.available():
    ADAPTERS.append(ContinueAdapter())
if _cline.available("saoudrizwan.claude-dev"):
    ADAPTERS.append(ClineAdapter())
if _cline.available("rooveterinaryinc.roo-cline"):
    ADAPTERS.append(RooAdapter())
if _copilot.available():
    ADAPTERS.append(CopilotAdapter())


def by_id(source):
    for a in ADAPTERS:
        if a.id == source:
            return a
    raise KeyError(f"no adapter for source {source!r}")


def for_path(path):
    real = os.path.realpath(path)
    for a in ADAPTERS:
        if a.owns(real):
            return a
    raise ValueError("path outside every adapter's transcript roots")


def allowed_roots():
    out = []
    for a in ADAPTERS:
        out.extend(a.roots())
    return out
