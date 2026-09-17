"""The log sources cclens indexes.

A source is one `Source` in `SOURCES`. Its contract:

    kind        stable identifier, stored on every entry and shown in the UI
    stream      lane the entries belong to: transcript, hook, state, statusline
    discover    (Root) -> iterable of Found(path, label, session_id, agent_id)
    parse       (obj, found) -> dict of entry columns, or None to drop the line
    text        (obj) -> text to make searchable, "" to keep it out of search
    facts       (obj) -> file level facts, optional. Keys in `FACT_KEYS` are
                stored once per file instead of once per entry.

Adding a source means appending to `SOURCES`. Nothing else in cclens knows one
source from another: the indexer walks `discover`, stores what `parse` returns,
and feeds `text` to FTS.

Entry columns a parser may set are listed in `COLUMNS`. Anything it omits is
null, and a null timestamp means the source does not record one, in which case
the entry is ordered by its position in the file and placed on the time axis by
`index.correlate` if a matching tool call carries a timestamp.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Root

COLUMNS = (
    "ts", "session_id", "agent_id", "agent_type", "kind", "subtype", "role",
    "uuid", "parent_uuid", "tool_name", "tool_use_id", "model",
    "permission_mode", "duration_ms", "elapsed_us", "in_tok", "out_tok",
    "cache_r", "cache_w", "cost", "is_sidechain", "is_meta", "has_error",
    "summary",
)

FACT_KEYS = ("cwd", "git_branch", "app_version")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SUMMARY_CHARS = 220


@dataclass(frozen=True)
class Found:
    """A file to index, with what the path itself tells us about it."""

    path: Path
    label: str
    session_id: str = ""
    agent_id: str = ""
    parent_session_id: str = ""


@dataclass(frozen=True)
class Source:
    kind: str
    stream: str
    discover: Callable[[Root], Iterable[Found]]
    parse: Callable[[dict, Found], dict | None]
    text: Callable[[dict], str]
    facts: Callable[[dict], dict] | None = None


# --- shared helpers -------------------------------------------------------


def _clip(value: Any, limit: int = SUMMARY_CHARS) -> str:
    """One line, collapsed whitespace, clipped for list rendering."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _iso(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _epoch(value: Any, unit: float = 1.0) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) / unit
    return None


def _flag(value: Any) -> int | None:
    return None if value is None else int(bool(value))


SALIENT = (
    "command", "file_path", "pattern", "path", "url", "query", "prompt",
    "description", "message", "skill", "notebook_path", "subagent_type",
    "storyPublicId", "to", "text",
)


def tool_brief(name: str, tool_input: Any) -> str:
    """Compact rendering of a tool call: the tool plus its most telling field."""
    name = name or "tool"
    if not isinstance(tool_input, dict):
        return f"{name} {_clip(tool_input, 120)}".strip()
    for key in SALIENT:
        if isinstance(tool_input.get(key), str) and tool_input[key].strip():
            return f"{name}: {_clip(tool_input[key], 160)}"
    for key, value in tool_input.items():
        if isinstance(value, (str, int, float)) and str(value).strip():
            return f"{name}: {key}={_clip(value, 120)}"
    return name


def _blocks(message: Any) -> list[dict]:
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in _blocks(message):
        kind = block.get("type")
        if kind in ("text", "thinking"):
            parts.append(str(block.get(kind) or block.get("text") or ""))
        elif kind == "tool_result":
            inner = block.get("content")
            if isinstance(inner, str):
                parts.append(inner)
            elif isinstance(inner, list):
                for item in inner:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(str(item.get("text") or ""))
    return "\n".join(p for p in parts if p)


# --- transcripts ----------------------------------------------------------


def discover_transcripts(root: Root) -> Iterator[Found]:
    """Walk the transcript trees named in the configuration.

    One tree, `projects`, is what Claude Code writes. A host that ran before
    its projects directory was pointed at shared storage keeps the earlier
    transcripts in a sibling tree, which is read only when named.
    """
    for name in root.project_dirs:
        projects = root.path / name
        if not projects.is_dir():
            continue
        for project in sorted(p for p in projects.iterdir() if p.is_dir()):
            for path in sorted(project.glob("*.jsonl")):
                yield Found(path=path, label=project.name, session_id=path.stem)
            for sub in sorted(project.glob("*/subagents/*.jsonl")):
                session = sub.parent.parent.name
                agent = sub.stem[6:] if sub.stem.startswith("agent-") else sub.stem
                yield Found(
                    path=sub,
                    label=project.name,
                    session_id=session,
                    agent_id=agent,
                    parent_session_id=session,
                )
        for stray in sorted(projects.glob("*.jsonl")):
            yield Found(path=stray, label="(projects root)", session_id="")


TITLE_KEYS = {
    "ai-title": "aiTitle",
    "custom-title": "customTitle",
    "agent-name": "agentName",
    "last-prompt": "lastPrompt",
    "mode": "mode",
    "permission-mode": "permissionMode",
}


def _transcript_summary(obj: dict, kind: str, message: Any) -> str:
    if kind in TITLE_KEYS:
        return _clip(obj.get(TITLE_KEYS[kind]))
    if kind == "attachment":
        att = obj.get("attachment")
        if isinstance(att, dict):
            # The attachment type is already carried as the entry's subtype.
            for key in ("content", "path", "hookName", "rendered", "stdout"):
                if isinstance(att.get(key), str) and att[key].strip():
                    return _clip(att[key])
            return ""
        return ""
    if kind == "system":
        return _clip(obj.get("content") or obj.get("subtype") or "system")
    if kind == "cost-state":
        cost = obj.get("totalCostUSD")
        added, removed = obj.get("totalLinesAdded"), obj.get("totalLinesRemoved")
        if isinstance(cost, (int, float)):
            return f"${cost:.4f}  +{added}/-{removed}"
        return "cost state"
    if kind == "pr-link":
        return _clip(obj.get("prUrl"))
    if kind == "frame-link":
        return _clip(f"{obj.get('title') or ''} {obj.get('frameUrl') or ''}")
    if kind == "queue-operation":
        return _clip(f"{obj.get('operation') or ''} {obj.get('content') or ''}")
    if kind == "file-history-delta":
        return _clip(obj.get("trackingPath"))
    if kind == "file-history-snapshot":
        snapshot = obj.get("snapshot")
        n = len(snapshot) if isinstance(snapshot, (list, dict)) else 0
        return f"snapshot of {n} file(s)"

    calls = [b for b in _blocks(message) if b.get("type") == "tool_use"]
    if calls:
        return " | ".join(tool_brief(b.get("name", ""), b.get("input")) for b in calls[:3])
    results = [b for b in _blocks(message) if b.get("type") == "tool_result"]
    body = _clip(_message_text(message))
    if results and not body:
        return "tool result"
    if body:
        return body
    return _describe_blocks(message) or kind


def _describe_blocks(message: Any) -> str:
    """Name the blocks when none of them carry text.

    Streamed thinking arrives as a block holding a signature and no text at
    all, so the alternative is a row that looks empty for no stated reason.
    """
    counts: dict[str, int] = {}
    for block in _blocks(message):
        counts[str(block.get("type"))] = counts.get(str(block.get("type")), 0) + 1
    if not counts:
        return ""
    parts = [f"{n} {name} block{'s' if n > 1 else ''}" for name, n in counts.items()]
    return f"{', '.join(parts)}, no text recorded"


def parse_transcript(obj: dict, found: Found) -> dict | None:
    kind = str(obj.get("type") or "unknown")
    message = obj.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    usage = usage if isinstance(usage, dict) else {}

    tool_name = tool_use_id = ""
    has_error = None
    for block in _blocks(message):
        btype = block.get("type")
        if btype == "tool_use":
            tool_name = str(block.get("name") or "")
            tool_use_id = str(block.get("id") or "")
        elif btype == "tool_result":
            tool_use_id = str(block.get("tool_use_id") or tool_use_id)
            has_error = _flag(block.get("is_error"))

    subtype = obj.get("subtype")
    if kind == "attachment" and isinstance(obj.get("attachment"), dict):
        subtype = obj["attachment"].get("type")

    if obj.get("isApiErrorMessage") or obj.get("error") or obj.get("apiErrorStatus"):
        has_error = 1

    cost = obj.get("totalCostUSD") if kind == "cost-state" else None

    return {
        "ts": _iso(obj.get("timestamp")) or _epoch(obj.get("startTime"), 1000.0),
        "session_id": str(obj.get("sessionId") or obj.get("session_id") or found.session_id),
        "agent_id": found.agent_id,
        "agent_type": "",
        "kind": kind,
        "subtype": _clip(subtype, 60) or None,
        "role": (message or {}).get("role") if isinstance(message, dict) else None,
        "uuid": obj.get("uuid"),
        "parent_uuid": obj.get("parentUuid"),
        "tool_name": tool_name or None,
        "tool_use_id": tool_use_id or None,
        "model": (message or {}).get("model") if isinstance(message, dict) else None,
        "permission_mode": obj.get("permissionMode"),
        "duration_ms": obj.get("durationMs"),
        "elapsed_us": None,
        "in_tok": usage.get("input_tokens"),
        "out_tok": usage.get("output_tokens"),
        "cache_r": usage.get("cache_read_input_tokens"),
        "cache_w": usage.get("cache_creation_input_tokens"),
        "cost": cost,
        "is_sidechain": _flag(obj.get("isSidechain")) or (1 if found.agent_id else 0),
        "is_meta": _flag(obj.get("isMeta")),
        "has_error": has_error,
        "summary": _transcript_summary(obj, kind, message),
    }


def text_transcript(obj: dict) -> str:
    kind = obj.get("type")
    parts = [_message_text(obj.get("message"))]
    for block in _blocks(obj.get("message")):
        if block.get("type") == "tool_use":
            parts.append(str(block.get("name") or ""))
            parts.append(json.dumps(block.get("input"), default=str))
    if kind == "attachment":
        att = obj.get("attachment")
        if isinstance(att, dict):
            parts.append(str(att.get("type") or ""))
            for key in ("content", "path", "stdout", "rendered"):
                value = att.get(key)
                if isinstance(value, str):
                    parts.append(value)
    if kind == "system":
        parts.append(str(obj.get("content") or ""))
    for key in ("aiTitle", "customTitle", "lastPrompt", "agentName", "prUrl"):
        if isinstance(obj.get(key), str):
            parts.append(obj[key])
    return "\n".join(p for p in parts if p)


def facts_transcript(obj: dict) -> dict:
    return {
        "cwd": obj.get("cwd"),
        "git_branch": obj.get("gitBranch"),
        "app_version": obj.get("version"),
    }


# --- hook audit -----------------------------------------------------------


def discover_audit(root: Root) -> Iterator[Found]:
    audit = root.path / "audit"
    if not audit.is_dir():
        return
    for path in sorted(audit.glob("*.jsonl")):
        yield Found(path=path, label=path.stem)


AUDIT_SKIP = {"_ts", "_pid", "_elapsed_us", "_elapsed_ms", "session_id", "agent_id",
              "agent_type", "tool_name", "tool_use_id", "permission_mode", "duration_ms"}


def _audit_summary(detail: dict, error: Any) -> str:
    """Hook records carry whichever of forty optional keys applied, so the
    summary is the keys that were actually present, in the order written."""
    if error:
        return _clip(error)
    pairs = [f"{k}={v}" for k, v in detail.items()
             if not isinstance(v, (dict, list)) and v != ""]
    # A record with nothing but the envelope says all it has to say through its
    # event name and tool, both of which the entry already carries.
    return _clip(" ".join(pairs))


def parse_audit(obj: dict, found: Found) -> dict | None:
    detail = {k: v for k, v in obj.items() if k not in AUDIT_SKIP}
    error = obj.get("error")
    return {
        "ts": _epoch(obj.get("_ts"), 1000.0),
        "session_id": str(obj.get("session_id") or ""),
        "agent_id": str(obj.get("agent_id") or ""),
        "agent_type": str(obj.get("agent_type") or ""),
        "kind": found.label,
        "subtype": _clip(obj.get("reason") or obj.get("source") or obj.get("trigger")
                         or obj.get("notification_type"), 60) or None,
        "role": None,
        "uuid": None,
        "parent_uuid": None,
        "tool_name": obj.get("tool_name"),
        "tool_use_id": obj.get("tool_use_id"),
        "model": obj.get("pricing_model"),
        "permission_mode": obj.get("permission_mode"),
        "duration_ms": obj.get("duration_ms") or obj.get("_elapsed_ms"),
        "elapsed_us": obj.get("_elapsed_us"),
        "in_tok": obj.get("in_tok"),
        "out_tok": obj.get("out_tok"),
        "cache_r": obj.get("cr_tok"),
        "cache_w": obj.get("cw_1h"),
        "cost": obj.get("cost"),
        "is_sidechain": 1 if obj.get("agent_id") else 0,
        "is_meta": None,
        "has_error": 1 if error else None,
        "summary": _audit_summary(detail, error),
    }


def text_audit(obj: dict) -> str:
    return json.dumps(obj, default=str)


# --- state dumps written by the other hooks -------------------------------


STATE_FILES = {
    "tool-calls.jsonl": "tool-call",
    "tool-results.jsonl": "tool-result",
    "subagent-stops.jsonl": "subagent-stop",
    "quarantined-agents.jsonl": "quarantine",
}


def discover_state(root: Root) -> Iterator[Found]:
    state = root.path / "state"
    if not state.is_dir():
        return
    for name, label in STATE_FILES.items():
        path = state / name
        if path.is_file():
            yield Found(path=path, label=label)


def _response_head(response: Any) -> str:
    if isinstance(response, dict):
        for key in ("stdout", "content", "text", "filePath", "file"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return _clip(value)
        return _clip(response)
    return _clip(response)


def parse_state(obj: dict, found: Found) -> dict | None:
    if found.label == "quarantine":
        return {
            "ts": _epoch(obj.get("epoch")),
            "session_id": "",
            "agent_id": str(obj.get("agent_id") or ""),
            "kind": "quarantine",
            "tool_use_id": obj.get("tool_use_id"),
            "summary": f"quarantined agent {obj.get('agent_id')}",
            "is_sidechain": 1,
        }

    tool_input = obj.get("tool_input")
    brief = tool_brief(str(obj.get("tool_name") or ""), tool_input)
    if found.label == "subagent-stop":
        brief = _clip(obj.get("last_assistant_message")) or f"agent {obj.get('agent_id')} stopped"
    elif found.label == "tool-result":
        brief = f"{brief}  ->  {_response_head(obj.get('tool_response'))}"

    response = obj.get("tool_response")
    stderr = response.get("stderr") if isinstance(response, dict) else None
    interrupted = response.get("interrupted") if isinstance(response, dict) else None

    return {
        "ts": None,
        "session_id": str(obj.get("session_id") or ""),
        "agent_id": str(obj.get("agent_id") or ""),
        "agent_type": str(obj.get("agent_type") or ""),
        "kind": found.label,
        "subtype": str(obj.get("hook_event_name") or "") or None,
        "tool_name": obj.get("tool_name"),
        "tool_use_id": obj.get("tool_use_id"),
        "permission_mode": obj.get("permission_mode"),
        "duration_ms": obj.get("duration_ms"),
        "is_sidechain": 1 if obj.get("agent_id") else 0,
        "has_error": 1 if (stderr or interrupted) else None,
        "summary": brief,
    }


def text_state(obj: dict) -> str:
    parts = [
        str(obj.get("tool_name") or ""),
        json.dumps(obj.get("tool_input"), default=str) if obj.get("tool_input") else "",
        _response_head(obj.get("tool_response")) if obj.get("tool_response") else "",
        str(obj.get("last_assistant_message") or ""),
    ]
    return "\n".join(p for p in parts if p)


# --- statusline snapshots -------------------------------------------------


def discover_statusline(root: Root) -> Iterator[Found]:
    path = root.path / "statusline-debug.jsonl"
    if path.is_file():
        yield Found(path=path, label="statusline")


def parse_statusline(obj: dict, found: Found) -> dict | None:
    model = obj.get("model") if isinstance(obj.get("model"), dict) else {}
    cost = obj.get("cost") if isinstance(obj.get("cost"), dict) else {}
    window = obj.get("context_window") if isinstance(obj.get("context_window"), dict) else {}
    used = window.get("used_percentage")
    total = cost.get("total_cost_usd")
    bits = [str(model.get("display_name") or model.get("id") or "")]
    if isinstance(total, (int, float)):
        bits.append(f"${total:.4f}")
    if isinstance(used, (int, float)):
        bits.append(f"{used:.0f}% context")
    return {
        "ts": _epoch(obj.get("_ts")),
        "session_id": str(obj.get("session_id") or ""),
        "kind": "statusline",
        "subtype": str(obj.get("version") or "") or None,
        "model": model.get("id"),
        "in_tok": window.get("total_input_tokens"),
        "out_tok": window.get("total_output_tokens"),
        "cost": total,
        "duration_ms": cost.get("total_api_duration_ms"),
        "summary": "  ".join(b for b in bits if b),
    }


def text_statusline(obj: dict) -> str:
    """Snapshots repeat once per statusline render, so they stay out of search."""
    return ""


SOURCES = (
    Source("transcript", "transcript", discover_transcripts, parse_transcript,
           text_transcript, facts_transcript),
    Source("audit", "hook", discover_audit, parse_audit, text_audit),
    Source("state", "state", discover_state, parse_state, text_state),
    Source("statusline", "statusline", discover_statusline, parse_statusline, text_statusline),
)
