"""Synthetic fixtures.

Nothing here comes from a real .claude directory, and the identifiers are
deliberately not shaped like real session or tool ids so that
`scripts/check_publishable.py` can treat those shapes as leaks.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cclens import config, index

SESSION = "session-one"
AGENT = "seven"
SWEPT = "swept-agent"
INTERFACE = "interface-agent"
TOOL_USE = "tu-one"


def write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def transcript_records() -> list[dict]:
    return [
        {"type": "ai-title", "sessionId": SESSION, "aiTitle": "Indexing spike"},
        {"type": "mode", "sessionId": SESSION, "mode": "auto"},
        {"type": "user", "sessionId": SESSION, "uuid": "u-1", "parentUuid": None,
         "timestamp": "2026-01-02T10:00:00.000Z", "cwd": "/work/repo",
         "gitBranch": "main", "version": "2.1.0",
         "message": {"role": "user", "content": "count the widgets"}},
        {"type": "assistant", "sessionId": SESSION, "uuid": "a-1", "parentUuid": "u-1",
         "timestamp": "2026-01-02T10:00:05.000Z", "cwd": "/work/repo",
         "gitBranch": "main", "version": "2.1.0",
         "message": {"role": "assistant", "model": "test-model",
                     "usage": {"input_tokens": 11, "output_tokens": 22,
                               "cache_read_input_tokens": 33,
                               "cache_creation_input_tokens": 44},
                     "content": [{"type": "thinking", "thinking": "the widgets live in stock"},
                                 {"type": "tool_use", "id": TOOL_USE, "name": "Bash",
                                  "input": {"command": "wc -l widgets.txt"}}]}},
        {"type": "user", "sessionId": SESSION, "uuid": "u-2", "parentUuid": "a-1",
         "timestamp": "2026-01-02T10:00:06.000Z",
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": TOOL_USE,
                                  "content": "42 widgets.txt"}]}},
        {"type": "attachment", "sessionId": SESSION, "uuid": "at-1",
         "timestamp": "2026-01-02T10:00:07.000Z",
         "attachment": {"type": "hook_success", "hookName": "PostToolUse:Bash",
                        "content": "hook ran clean"}},
        {"type": "assistant", "sessionId": SESSION, "uuid": "a-2", "parentUuid": "u-2",
         "timestamp": "2026-01-02T10:00:08.000Z", "isApiErrorMessage": True,
         "message": {"role": "assistant", "model": "test-model",
                     "content": [{"type": "text", "text": "the request failed"}]}},
        {"type": "assistant", "sessionId": SESSION, "uuid": "a-3",
         "timestamp": "2026-01-02T10:00:09.000Z",
         "message": {"role": "assistant", "model": "test-model",
                     "content": [{"type": "thinking", "thinking": "", "signature": "op4que"}]}},
        {"type": "cost-state", "sessionId": SESSION, "totalCostUSD": 0.5,
         "totalLinesAdded": 3, "totalLinesRemoved": 1},
    ]


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    root = tmp_path / "claude"
    project = root / "projects" / "-work-repo"

    write(project / f"{SESSION}.jsonl", transcript_records())
    write(project / SESSION / "subagents" / f"agent-{AGENT}.jsonl", [
        {"type": "user", "sessionId": SESSION, "uuid": "ag-0", "parentUuid": None,
         "timestamp": "2026-01-02T10:00:09.000Z", "isSidechain": True,
         "message": {"role": "user", "content": "count the widgets in stock"}},
        {"type": "assistant", "sessionId": SESSION, "uuid": "ag-1",
         "timestamp": "2026-01-02T10:00:10.000Z", "isSidechain": True,
         "message": {"role": "assistant", "model": "test-model",
                     "content": [{"type": "text", "text": "subagent looked at stock"}]}},
    ])

    # A grep style dump: each line is a path, a colon, then JSON.
    dump = root / "projects" / "2026-01.jsonl"
    dump.parent.mkdir(parents=True, exist_ok=True)
    dump.write_text("\n".join(
        f'-work-repo/{SESSION}.jsonl:{{"type": "user", "uuid": "u-{i}"}}'
        for i in range(60)) + "\n", encoding="utf-8")

    write(root / "audit" / "SessionStart.jsonl", [
        {"_ts": 1767348000000, "_pid": 10, "session_id": SESSION,
         "source": "startup", "_elapsed_us": 30},
    ])
    write(root / "audit" / "PreToolUse.jsonl", [
        {"_ts": 1767348005000, "_pid": 11, "session_id": SESSION, "tool_name": "Bash",
         "tool_use_id": TOOL_USE, "permission_mode": "auto", "_elapsed_us": 40},
    ])
    write(root / "audit" / "PostToolUse.jsonl", [
        {"_ts": 1767348006000, "_pid": 12, "session_id": SESSION, "tool_name": "Bash",
         "tool_use_id": TOOL_USE, "duration_ms": 9, "permission_mode": "auto",
         "_elapsed_us": 120000},
    ])
    write(root / "audit" / "PostToolUseFailure.jsonl", [
        {"_ts": 1767348007000, "_pid": 13, "session_id": SESSION, "tool_name": "Bash",
         "tool_use_id": "tu-two", "error": "Exit code 2", "_elapsed_us": 50},
    ])

    write(root / "state" / "tool-calls.jsonl", [
        {"session_id": SESSION, "tool_name": "Bash", "tool_use_id": TOOL_USE,
         "hook_event_name": "PreToolUse", "permission_mode": "auto",
         "tool_input": {"command": "wc -l widgets.txt"}},
    ])
    write(root / "state" / "tool-results.jsonl", [
        {"session_id": SESSION, "tool_name": "Bash", "tool_use_id": TOOL_USE,
         "hook_event_name": "PostToolUse", "duration_ms": 9,
         "tool_input": {"command": "wc -l widgets.txt"},
         "tool_response": {"stdout": "42 widgets.txt", "stderr": "", "interrupted": False}},
    ])
    write(root / "state" / "subagent-stops.jsonl", [
        {"session_id": SESSION, "agent_id": AGENT, "agent_type": "explorer",
         "hook_event_name": "SubagentStop", "stop_hook_active": False,
         "agent_transcript_path": str(project / SESSION / "subagents" / f"agent-{AGENT}.jsonl"),
         "last_assistant_message": "stock holds 42"},
        {"session_id": SESSION, "agent_id": SWEPT, "agent_type": "general-purpose",
         "hook_event_name": "SubagentStop", "stop_hook_active": False,
         "agent_transcript_path": "/gone/agent-swept.jsonl",
         "last_assistant_message": "counted the widgets twice"},
        {"session_id": SESSION, "agent_id": INTERFACE, "agent_type": "",
         "hook_event_name": "SubagentStop", "stop_hook_active": False,
         "last_assistant_message": "Reading widgets.txt"},
    ])
    write(root / "state" / "quarantined-agents.jsonl", [
        {"epoch": 1767348020, "agent_id": "agent-eight", "tool_use_id": "tu-three"},
    ])

    def snapshot(ts, total):
        return {"_ts": ts, "session_id": SESSION, "version": "2.1.0",
                "model": {"id": "test-model", "display_name": "Test Model"},
                "cost": {"total_cost_usd": total, "total_api_duration_ms": 1200},
                "context_window": {"total_input_tokens": 100, "total_output_tokens": 50,
                                   "used_percentage": 12.5}}

    # Two runs: the session was resumed, and the second run bills from zero.
    write(root / "statusline-debug.jsonl", [
        snapshot(1767348030, 0.2), snapshot(1767348040, 0.9),
        snapshot(1767348050, 0.1), snapshot(1767348060, 0.4),
    ])
    return root


@pytest.fixture
def cfg(claude_home: Path, tmp_path: Path) -> config.Config:
    return config.from_env({
        "HOME": str(tmp_path),
        "CCLENS_CLAUDE_HOME": f"test={claude_home}",
        "CCLENS_DB": str(tmp_path / "index.db"),
    })


@pytest.fixture
def indexed(cfg: config.Config):
    index.index_all(cfg)
    db = index.connect(cfg.db_path)
    yield db
    db.close()


@pytest.fixture
def transcripts_only(claude_home: Path, tmp_path: Path):
    """What a host with no tool hooks looks like: transcripts and nothing else."""
    cfg = config.from_env({
        "HOME": str(tmp_path),
        "CCLENS_CLAUDE_HOME": f"test={claude_home}",
        "CCLENS_DB": str(tmp_path / "bare.db"),
        "CCLENS_EXCLUDE": "audit,state,statusline",
    })
    index.index_all(cfg)
    db = index.connect(cfg.db_path)
    yield db
    db.close()
