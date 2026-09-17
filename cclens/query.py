"""Read side of the index.

Every function takes a connection and returns plain dicts and lists, ready to
be serialised. Entry bodies are read from the original file on demand by
`body`, never from the index.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .index import SUBSTANTIVE

PAGE = 200
MAX_PAGE = 1000
BODY_LIMIT = 2_000_000

ENTRY_COLS = (
    "e.id, e.ts, e.session_id, e.agent_id, e.agent_type, e.stream, e.source_kind,"
    " e.kind, e.subtype, e.role, e.uuid, e.parent_uuid, e.tool_name, e.tool_use_id,"
    " e.model, e.permission_mode, e.duration_ms, e.elapsed_us, e.in_tok, e.out_tok,"
    " e.cache_r, e.cache_w, e.cost, e.is_sidechain, e.is_meta, e.has_error, e.summary,"
    " e.line_no, e.byte_offset, e.byte_len, e.file_id"
)

LATENCY_BUCKETS = """
  CASE
    WHEN elapsed_us < 50 THEN 'under 50us'
    WHEN elapsed_us < 200 THEN '50us to 200us'
    WHEN elapsed_us < 1000 THEN '200us to 1ms'
    WHEN elapsed_us < 100000 THEN '1ms to 100ms'
    ELSE 'over 100ms'
  END
"""


def _rows(cur: sqlite3.Cursor) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _one(db: sqlite3.Connection, sql: str, params: Sequence = ()) -> dict | None:
    row = db.execute(sql, params).fetchone()
    return dict(row) if row else None


def _clamp(value: Any, default: int, cap: int = MAX_PAGE) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, cap))


def _csv(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v)]
    return [part for part in str(value).split(",") if part]


def _in_clause(column: str, values: Sequence[str]) -> tuple[str, list]:
    marks = ", ".join("?" * len(values))
    return f"{column} IN ({marks})", list(values)


# --- overview -------------------------------------------------------------


def doctor(db: sqlite3.Connection) -> list[str]:
    lines = []
    for row in db.execute(
        "SELECT source_kind, COUNT(*) files, SUM(lines_ok) ok, SUM(lines_bad) bad,"
        " SUM(missing) missing FROM source_file GROUP BY source_kind ORDER BY source_kind"
    ):
        lines.append(f"{row['source_kind']:11} {row['files']:>4} file(s)"
                     f"  {row['ok'] or 0:>8} entries"
                     f"  {row['bad'] or 0:>5} unparseable"
                     f"  {row['missing'] or 0:>3} gone from disk")
    sessions = db.execute("SELECT COUNT(*) FROM session").fetchone()[0]
    agents = db.execute("SELECT COUNT(*) FROM agent").fetchone()[0]
    lines.append(f"{sessions} session(s), {agents} subagent(s)")
    for row in db.execute("SELECT path, skipped_reason FROM source_file"
                          " WHERE skipped_reason IS NOT NULL"):
        lines.append(f"skipped {row['path']}: {row['skipped_reason']}")
    return lines


def stats(db: sqlite3.Connection) -> dict:
    return {
        "totals": _one(db, """
            SELECT (SELECT COUNT(*) FROM entry) entries,
                   (SELECT COUNT(*) FROM session) sessions,
                   (SELECT COUNT(*) FROM agent) agents,
                   (SELECT COUNT(*) FROM source_file) files,
                   (SELECT SUM(size) FROM source_file) source_bytes,
                   (SELECT MIN(ts) FROM entry WHERE ts IS NOT NULL) first_ts,
                   (SELECT MAX(ts) FROM entry WHERE ts IS NOT NULL) last_ts,
                   (SELECT value FROM meta WHERE key = 'indexed_at') indexed_at
        """),
        "streams": _rows(db.execute(
            "SELECT stream, COUNT(*) n FROM entry GROUP BY stream ORDER BY n DESC")),
        "kinds": _rows(db.execute(
            "SELECT stream, kind, COUNT(*) n FROM entry GROUP BY stream, kind"
            " ORDER BY n DESC LIMIT 80")),
        # Every user has tool calls in the transcript; only a user who runs
        # tool hooks has timings for them.
        "tools": _rows(db.execute("""
            SELECT e.tool_name, COUNT(*) n,
              (SELECT SUM(COALESCE(h.has_error, 0)) FROM entry h
                WHERE h.tool_name = e.tool_name AND h.stream = 'hook') errors,
              (SELECT AVG(h.duration_ms) FROM entry h
                WHERE h.tool_name = e.tool_name AND h.stream = 'hook') avg_ms,
              (SELECT MAX(h.duration_ms) FROM entry h
                WHERE h.tool_name = e.tool_name AND h.stream = 'hook') max_ms
            FROM entry e WHERE e.tool_name IS NOT NULL AND e.kind = 'assistant'
            GROUP BY e.tool_name ORDER BY n DESC LIMIT 60""")),
        "activity": _rows(db.execute(
            "SELECT DATE(ts, 'unixepoch', 'localtime') day, stream, COUNT(*) n"
            " FROM entry WHERE ts IS NOT NULL GROUP BY day, stream"
            " ORDER BY day DESC LIMIT 400")),
        "hook_latency": _rows(db.execute(
            f"SELECT kind, {LATENCY_BUCKETS} bucket, COUNT(*) n FROM entry"
            " WHERE stream = 'hook' AND elapsed_us IS NOT NULL"
            " GROUP BY kind, bucket ORDER BY kind, MIN(elapsed_us)")),
        "models": _rows(db.execute(
            "SELECT model, COUNT(*) n, SUM(COALESCE(in_tok, 0)) in_tok,"
            " SUM(COALESCE(out_tok, 0)) out_tok, SUM(COALESCE(cache_r, 0)) cache_r"
            " FROM entry WHERE model IS NOT NULL AND kind = 'assistant'"
            " GROUP BY model ORDER BY n DESC")),
        "projects": _rows(db.execute(
            "SELECT COALESCE(project, '(no transcript)') project, COUNT(*) sessions,"
            " SUM(entries) entries, MAX(last_ts) last_ts FROM session"
            " GROUP BY project ORDER BY last_ts DESC")),
    }


# --- sessions -------------------------------------------------------------


def sessions(db: sqlite3.Connection, params: dict) -> dict:
    clauses, args = ["1=1"], []
    if params.get("project"):
        clause, extra = _in_clause("project", _csv(params["project"]))
        clauses.append(clause)
        args += extra
    if params.get("q"):
        clauses.append("(title LIKE ? OR session_id LIKE ? OR cwd LIKE ?"
                       " OR git_branch LIKE ?)")
        args += [f"%{params['q']}%"] * 4
    if params.get("since"):
        clauses.append("last_ts >= ?")
        args.append(float(params["since"]))
    if params.get("until"):
        clauses.append("first_ts <= ?")
        args.append(float(params["until"]))
    if params.get("errors"):
        clauses.append("errors > 0")
    if params.get("model"):
        clauses.append("models LIKE ?")
        args.append(f"%{params['model']}%")

    where = " AND ".join(clauses)
    order = {
        "recent": "last_ts DESC",
        "oldest": "last_ts ASC",
        "entries": "entries DESC",
        "cost": "cost DESC",
        "tools": "tool_calls DESC",
    }.get(str(params.get("order", "recent")), "last_ts DESC")
    limit = _clamp(params.get("limit"), 100)
    offset = max(0, int(params.get("offset") or 0))

    rows = _rows(db.execute(
        f"SELECT * FROM session WHERE {where} ORDER BY {order} NULLS LAST"
        f" LIMIT ? OFFSET ?", (*args, limit, offset)))
    total = db.execute(f"SELECT COUNT(*) FROM session WHERE {where}", args).fetchone()[0]
    return {
        "rows": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "projects": _rows(db.execute(
            "SELECT COALESCE(project, '(no transcript)') project, COUNT(*) n,"
            " MAX(last_ts) last_ts FROM session GROUP BY project"
            " ORDER BY last_ts DESC")),
    }


def session(db: sqlite3.Connection, session_id: str) -> dict | None:
    row = _one(db, "SELECT * FROM session WHERE session_id = ?", (session_id,))
    if row is None:
        return None
    row["files"] = _rows(db.execute(
        "SELECT id, source_kind, path, label, agent_id, size, lines_ok, lines_bad,"
        " missing, app_version FROM source_file WHERE session_id = ? ORDER BY id",
        (session_id,)))
    row["kinds"] = _rows(db.execute(
        "SELECT stream, kind, COUNT(*) n FROM entry WHERE session_id = ?"
        " GROUP BY stream, kind ORDER BY n DESC", (session_id,)))
    row["tools"] = _rows(db.execute(
        "SELECT tool_name, COUNT(*) n FROM entry WHERE session_id = ?"
        " AND tool_name IS NOT NULL GROUP BY tool_name ORDER BY n DESC",
        (session_id,)))
    row["agent_counts"] = agent_counts(db, session_id)
    row["sidecar"] = _rows(db.execute(
        f"SELECT {ENTRY_COLS} FROM entry e WHERE e.session_id = ? AND e.ts IS NULL"
        " ORDER BY e.kind, e.id DESC LIMIT 400", (session_id,)))
    return row


def agents(db: sqlite3.Connection, params: dict) -> dict:
    """The subagents of one session, with what each was asked to do.

    A subagent's own transcript opens with the prompt it was handed, so that
    first user record is the task. Most subagents outlive their transcript:
    the file is swept and only the stop record's closing message remains, which
    is why the transcript is reported as present or gone rather than assumed.
    """
    order = {
        "recent": "first_ts DESC",
        "oldest": "first_ts ASC",
        "entries": "entries DESC",
        "tools": "tool_calls DESC",
        "errors": "errors DESC, entries DESC",
    }.get(str(params.get("order", "recent")), "first_ts DESC")

    outer = ["1=1"]
    group = str(params.get("group", "subagents"))
    if group in ("subagents", "interface"):
        outer.append(f"substantive = {1 if group == 'subagents' else 0}")
    if params.get("kept"):
        outer.append("transcript_file IS NOT NULL AND transcript_missing = 0")
    if params.get("errors"):
        outer.append("errors > 0")

    sql = f"""
      SELECT * FROM (
        SELECT a.*, CASE WHEN {SUBSTANTIVE} THEN 1 ELSE 0 END substantive,
          (SELECT COUNT(*) FROM entry e
            WHERE e.agent_id = a.agent_id AND e.has_error = 1) errors,
          (SELECT SUM(COALESCE(e.out_tok, 0)) FROM entry e
            WHERE e.agent_id = a.agent_id) out_tok,
          (SELECT e.summary FROM entry e JOIN source_file f ON f.id = e.file_id
            WHERE e.agent_id = a.agent_id AND f.source_kind = 'transcript'
              AND e.role = 'user' ORDER BY e.line_no LIMIT 1) task,
          (SELECT e.id FROM entry e JOIN source_file f ON f.id = e.file_id
            WHERE e.agent_id = a.agent_id AND f.source_kind = 'transcript'
              AND e.role = 'user' ORDER BY e.line_no LIMIT 1) task_entry,
          (SELECT e.id FROM entry e WHERE e.agent_id = a.agent_id
              AND e.kind = 'subagent-stop' ORDER BY e.id DESC LIMIT 1) stop_entry,
          (SELECT f.id FROM source_file f
            WHERE f.agent_id = a.agent_id ORDER BY f.id LIMIT 1) transcript_file,
          (SELECT f.missing FROM source_file f
            WHERE f.agent_id = a.agent_id ORDER BY f.id LIMIT 1) transcript_missing,
          (SELECT f.path FROM source_file f
            WHERE f.agent_id = a.agent_id ORDER BY f.id LIMIT 1) transcript_path
        FROM agent a WHERE a.session_id = ?
      ) WHERE {' AND '.join(outer)} ORDER BY {order} NULLS LAST
    """
    rows = _rows(db.execute(sql, (params.get("session", ""),)))
    counts = agent_counts(db, params.get("session", ""))
    return {
        "rows": rows,
        "total": len(rows),
        "group": group,
        "subagents": counts["subagents"],
        "interface": counts["interface"],
        "kept": sum(1 for r in rows if r["transcript_file"] and not r["transcript_missing"]),
        "types": sorted({r["agent_type"] for r in rows if r["agent_type"]}),
    }


def agent_counts(db: sqlite3.Connection, session_id: str) -> dict:
    row = db.execute(
        f"SELECT SUM(CASE WHEN {SUBSTANTIVE} THEN 1 ELSE 0 END) subagents,"
        f" SUM(CASE WHEN {SUBSTANTIVE} THEN 0 ELSE 1 END) interface"
        " FROM agent a WHERE a.session_id = ?", (session_id,)).fetchone()
    return {"subagents": row["subagents"] or 0, "interface": row["interface"] or 0}


def agent(db: sqlite3.Connection, session_id: str, agent_id: str) -> dict | None:
    found = agents(db, {"session": session_id, "group": "all"})
    for row in found["rows"]:
        if row["agent_id"] == agent_id:
            return row
    return None


# --- entries --------------------------------------------------------------


def entries(db: sqlite3.Connection, params: dict) -> dict:
    clauses, args = [], []
    if params.get("session"):
        clauses.append("e.session_id = ?")
        args.append(params["session"])
    if params.get("agent"):
        clauses.append("e.agent_id = ?")
        args.append(params["agent"])
    if params.get("file"):
        clauses.append("e.file_id = ?")
        args.append(int(params["file"]))
    for field, column in (("stream", "e.stream"), ("kind", "e.kind"),
                          ("tool", "e.tool_name")):
        values = _csv(params.get(field))
        if values:
            clause, extra = _in_clause(column, values)
            clauses.append(clause)
            args += extra
    if params.get("tool_use_id"):
        clauses.append("e.tool_use_id = ?")
        args.append(params["tool_use_id"])
    if params.get("since"):
        clauses.append("e.ts >= ?")
        args.append(float(params["since"]))
    if params.get("until"):
        clauses.append("e.ts <= ?")
        args.append(float(params["until"]))
    if params.get("errors"):
        clauses.append("e.has_error = 1")
    if params.get("dated"):
        clauses.append("e.ts IS NOT NULL")
    if not params.get("meta"):
        clauses.append("COALESCE(e.is_meta, 0) = 0")
    if params.get("agents_only"):
        clauses.append("COALESCE(e.agent_id, '') <> ''")
    if not params.get("include_agents") and params.get("session"):
        # A source that records no agent at all belongs to the main lane.
        clauses.append("COALESCE(e.agent_id, '') = ''")

    descending = str(params.get("order", "asc")).lower() == "desc"
    cursor = params.get("cursor")
    if cursor:
        clauses.append(f"e.id {'<' if descending else '>'} ?")
        args.append(int(cursor))

    where = " AND ".join(clauses) or "1=1"
    limit = _clamp(params.get("limit"), PAGE)
    direction = "DESC" if descending else "ASC"
    rows = _rows(db.execute(
        f"SELECT {ENTRY_COLS} FROM entry e WHERE {where}"
        f" ORDER BY e.ts {direction} NULLS LAST, e.id {direction} LIMIT ?",
        (*args, limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "rows": rows,
        "next_cursor": rows[-1]["id"] if more and rows else None,
        "total": db.execute(f"SELECT COUNT(*) FROM entry e WHERE {where}",
                            args).fetchone()[0],
    }


def body(db: sqlite3.Connection, entry_id: int) -> dict | None:
    row = _one(db, f"SELECT {ENTRY_COLS}, f.path, f.missing, f.root, f.label,"
                   " f.source_kind AS file_kind FROM entry e"
                   " JOIN source_file f ON f.id = e.file_id WHERE e.id = ?",
               (entry_id,))
    if row is None:
        return None
    out = {"entry": row, "raw": None, "parsed": None, "error": None,
           "provenance": {"path": row.pop("path"), "root": row.pop("root"),
                          "label": row.pop("label"), "line": row["line_no"],
                          "byte_offset": row["byte_offset"],
                          "byte_len": row["byte_len"],
                          "missing": bool(row.pop("missing"))}}
    path = Path(out["provenance"]["path"])
    if out["provenance"]["missing"] or not path.is_file():
        out["error"] = "the source file is no longer on disk, only indexed fields remain"
        return out
    if row["byte_len"] > BODY_LIMIT:
        out["error"] = (f"line is {row['byte_len']} bytes, larger than the"
                        f" {BODY_LIMIT} byte view limit")
        return out
    try:
        with path.open("rb") as fh:
            fh.seek(row["byte_offset"])
            raw = fh.read(row["byte_len"])
    except OSError as exc:
        out["error"] = f"cannot read the source file: {exc}"
        return out
    out["raw"] = raw.decode("utf-8", "replace")
    try:
        out["parsed"] = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        out["error"] = "the line on disk no longer parses as JSON"
    return out


def related(db: sqlite3.Connection, entry_id: int) -> dict:
    row = _one(db, "SELECT * FROM entry WHERE id = ?", (entry_id,))
    if row is None:
        return {}
    out: dict[str, list[dict]] = {}
    if row["tool_use_id"]:
        out["same_tool_call"] = _rows(db.execute(
            f"SELECT {ENTRY_COLS} FROM entry e WHERE e.tool_use_id = ? AND e.id <> ?"
            " ORDER BY e.stream, e.id", (row["tool_use_id"], entry_id)))
    if row["parent_uuid"]:
        out["parent"] = _rows(db.execute(
            f"SELECT {ENTRY_COLS} FROM entry e WHERE e.uuid = ? LIMIT 5",
            (row["parent_uuid"],)))
    if row["uuid"]:
        out["children"] = _rows(db.execute(
            f"SELECT {ENTRY_COLS} FROM entry e WHERE e.parent_uuid = ? LIMIT 50",
            (row["uuid"],)))
    if row["agent_id"]:
        out["agent"] = _rows(db.execute(
            "SELECT * FROM agent WHERE agent_id = ?", (row["agent_id"],)))
    return out


def tool_call(db: sqlite3.Connection, tool_use_id: str) -> dict:
    """Everything recorded about one tool call, across all four sources."""
    rows = _rows(db.execute(
        f"SELECT {ENTRY_COLS}, f.source_kind AS file_kind FROM entry e"
        " JOIN source_file f ON f.id = e.file_id WHERE e.tool_use_id = ?"
        " ORDER BY e.ts NULLS LAST, e.id", (tool_use_id,)))
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["stream"], []).append(row)
    hook_ms = next((r["duration_ms"] for r in rows
                    if r["stream"] == "hook" and r["duration_ms"] is not None), None)
    return {
        "tool_use_id": tool_use_id,
        "streams": grouped,
        "count": len(rows),
        "tool_name": next((r["tool_name"] for r in rows if r["tool_name"]), None),
        "session_id": next((r["session_id"] for r in rows if r["session_id"]), None),
        "duration_ms": hook_ms,
        "denied": any(r["kind"] == "PermissionDenied" for r in rows),
        "failed": any(r["kind"] == "PostToolUseFailure" for r in rows),
    }


# --- hooks ----------------------------------------------------------------


def attach_context(db: sqlite3.Connection, rows: list[dict]) -> None:
    """Lend each row the summary of the tool call it belongs to.

    A hook record holds an envelope and a tool_use_id, so on its own a row of
    them reads as a column of event names. The tool call is recorded elsewhere,
    and the id is the join.
    """
    ids = sorted({r["tool_use_id"] for r in rows if r.get("tool_use_id")})
    if not ids:
        return
    found: dict[str, str] = {}
    for chunk in (ids[i:i + 400] for i in range(0, len(ids), 400)):
        marks = ", ".join("?" * len(chunk))
        for row in db.execute(
            f"SELECT tool_use_id, summary FROM entry WHERE tool_use_id IN ({marks})"
            " AND stream <> 'hook' AND summary IS NOT NULL AND summary <> ''"
            " GROUP BY tool_use_id", chunk
        ):
            found[row["tool_use_id"]] = row["summary"]
    for row in rows:
        row["context"] = found.get(row.get("tool_use_id") or "", "")


def hooks(db: sqlite3.Connection, params: dict) -> dict:
    params = dict(params)
    params["stream"] = "hook"
    params.setdefault("order", "desc")
    params["meta"] = True
    params["include_agents"] = True
    out = entries(db, params)
    attach_context(db, out["rows"])
    out["events"] = _rows(db.execute(
        "SELECT kind, COUNT(*) n, MAX(ts) last_ts, AVG(elapsed_us) avg_us,"
        " SUM(COALESCE(has_error, 0)) errors FROM entry WHERE stream = 'hook'"
        " GROUP BY kind ORDER BY n DESC"))
    return out


# --- search ---------------------------------------------------------------


def search(db: sqlite3.Connection, params: dict) -> dict:
    raw = str(params.get("q") or "").strip()
    if not raw:
        return {"rows": [], "total": 0, "query": raw, "error": None}
    limit = _clamp(params.get("limit"), 60, 300)
    clauses, args = ["entry_fts MATCH ?"], [raw]
    for field, column in (("stream", "e.stream"), ("kind", "e.kind"),
                          ("tool", "e.tool_name")):
        values = _csv(params.get(field))
        if values:
            clause, extra = _in_clause(column, values)
            clauses.append(clause)
            args += extra
    if params.get("session"):
        clauses.append("e.session_id = ?")
        args.append(params["session"])

    sql = (f"SELECT {ENTRY_COLS},"
           " snippet(entry_fts, 0, '<<', '>>', ' ... ', 14) AS snippet"
           " FROM entry_fts JOIN entry e ON e.id = entry_fts.rowid"
           f" WHERE {' AND '.join(clauses)} ORDER BY rank LIMIT ?")
    try:
        rows = _rows(db.execute(sql, (*args, limit)))
        return {"rows": rows, "total": len(rows), "query": raw, "error": None}
    except sqlite3.OperationalError:
        quoted = '"' + raw.replace('"', '""') + '"'
        try:
            rows = _rows(db.execute(sql, (quoted, *args[1:], limit)))
            return {"rows": rows, "total": len(rows), "query": quoted, "error": None}
        except sqlite3.OperationalError as exc:
            return {"rows": [], "total": 0, "query": raw, "error": str(exc)}
