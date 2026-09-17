"""Incremental index over the log files.

Every source file is append only, so a file is read from `bytes_ingested`
forward and only whole lines are taken; a line still being written is left for
the next run. A file whose size went backwards is read again from zero.

Bodies are not copied into the index. Each entry stores the file, byte offset
and length of its line, and `query.body` seeks that back out of the original
file, so what the inspector shows is the bytes on disk.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .sources import COLUMNS, SOURCES, Found, Source

# An agent did work of its own if it declared a type, kept a transcript, called
# a tool, or was seen starting. Everything else is an id that only ever stopped:
# the short model calls behind progress lines, suggested replies and session
# summaries, which run without tools and leave no transcript.
SUBSTANTIVE = """
  COALESCE(a.agent_type, '') <> ''
  OR COALESCE(a.tool_calls, 0) > 0
  OR EXISTS (SELECT 1 FROM source_file f WHERE f.agent_id = a.agent_id)
  OR EXISTS (SELECT 1 FROM entry e2 WHERE e2.agent_id = a.agent_id
               AND e2.kind = 'SubagentStart')
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_file (
  id INTEGER PRIMARY KEY,
  root TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  stream TEXT NOT NULL,
  path TEXT NOT NULL UNIQUE,
  label TEXT,
  session_id TEXT,
  agent_id TEXT,
  parent_session_id TEXT,
  size INTEGER,
  mtime REAL,
  bytes_ingested INTEGER NOT NULL DEFAULT 0,
  lines_ok INTEGER NOT NULL DEFAULT 0,
  lines_bad INTEGER NOT NULL DEFAULT 0,
  skipped_reason TEXT,
  missing INTEGER NOT NULL DEFAULT 0,
  cwd TEXT,
  git_branch TEXT,
  app_version TEXT,
  indexed_at REAL
);

CREATE TABLE IF NOT EXISTS entry (
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES source_file(id),
  line_no INTEGER NOT NULL,
  byte_offset INTEGER NOT NULL,
  byte_len INTEGER NOT NULL,
  stream TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  ts REAL,
  session_id TEXT,
  agent_id TEXT,
  agent_type TEXT,
  kind TEXT,
  subtype TEXT,
  role TEXT,
  uuid TEXT,
  parent_uuid TEXT,
  tool_name TEXT,
  tool_use_id TEXT,
  model TEXT,
  permission_mode TEXT,
  duration_ms REAL,
  elapsed_us REAL,
  in_tok INTEGER,
  out_tok INTEGER,
  cache_r INTEGER,
  cache_w INTEGER,
  cost REAL,
  is_sidechain INTEGER,
  is_meta INTEGER,
  has_error INTEGER,
  summary TEXT
);

CREATE INDEX IF NOT EXISTS entry_session ON entry(session_id, ts);
CREATE INDEX IF NOT EXISTS entry_file ON entry(file_id, line_no);
CREATE INDEX IF NOT EXISTS entry_tool_use ON entry(tool_use_id);
CREATE INDEX IF NOT EXISTS entry_ts ON entry(ts);
CREATE INDEX IF NOT EXISTS entry_kind ON entry(kind, ts);
CREATE INDEX IF NOT EXISTS entry_tool ON entry(tool_name, ts);
CREATE INDEX IF NOT EXISTS entry_uuid ON entry(uuid);
CREATE INDEX IF NOT EXISTS entry_agent ON entry(agent_id, ts);

CREATE VIRTUAL TABLE IF NOT EXISTS entry_fts
  USING fts5(text, tokenize='unicode61 remove_diacritics 2');

CREATE TABLE IF NOT EXISTS session (
  session_id TEXT PRIMARY KEY,
  project TEXT,
  title TEXT,
  cwd TEXT,
  git_branch TEXT,
  app_version TEXT,
  first_ts REAL,
  last_ts REAL,
  entries INTEGER,
  transcript_entries INTEGER,
  hook_entries INTEGER,
  tool_calls INTEGER,
  errors INTEGER,
  agents INTEGER,
  models TEXT,
  in_tok INTEGER,
  out_tok INTEGER,
  cost REAL
);

CREATE TABLE IF NOT EXISTS agent (
  agent_id TEXT PRIMARY KEY,
  session_id TEXT,
  agent_type TEXT,
  first_ts REAL,
  last_ts REAL,
  entries INTEGER,
  tool_calls INTEGER,
  quarantined INTEGER,
  summary TEXT
);

CREATE INDEX IF NOT EXISTS agent_session ON agent(session_id);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Columns added after a table first shipped. An index is expensive to rebuild,
# so it is widened in place instead.
ADDED_COLUMNS = {"session": {"cost_runs": "INTEGER"}}

# Claude Code reports a cumulative cost per run, and the number resets to zero
# both when a session is resumed and when a skill forks. Counting the resets is
# useful on its own; adding up what each run reached is only correct for the
# first case, so `cost` stays the highest single figure reported until the two
# can be told apart.
SESSION_COST = """
  WITH samples AS (
    SELECT session_id, kind, cost, ts, id FROM entry
     WHERE cost IS NOT NULL AND session_id IS NOT NULL AND session_id <> ''
       AND kind IN ('statusline', 'cost-state')
  ),
  ordered AS (
    SELECT session_id, kind, cost, ts, id,
           LAG(cost) OVER (PARTITION BY session_id, kind ORDER BY ts, id) prev
      FROM samples
  ),
  marked AS (
    SELECT session_id, kind, cost,
           SUM(CASE WHEN prev IS NULL OR cost < prev THEN 1 ELSE 0 END)
             OVER (PARTITION BY session_id, kind ORDER BY ts, id) run
      FROM ordered
  ),
  runs AS (
    SELECT session_id, kind, run, MAX(cost) reached
      FROM marked GROUP BY session_id, kind, run
  ),
  per_kind AS (
    SELECT session_id, kind, SUM(reached) total, COUNT(*) run_count
      FROM runs GROUP BY session_id, kind
  )
  SELECT session_id,
         COALESCE(MAX(CASE WHEN kind = 'statusline' THEN total END),
                  MAX(CASE WHEN kind = 'cost-state' THEN total END)) cost,
         COALESCE(MAX(CASE WHEN kind = 'statusline' THEN run_count END),
                  MAX(CASE WHEN kind = 'cost-state' THEN run_count END)) run_count
    FROM per_kind GROUP BY session_id
"""

ENTRY_FIELDS = ("file_id", "line_no", "byte_offset", "byte_len", "stream",
                "source_kind", *COLUMNS)
INSERT_ENTRY = (
    f"INSERT INTO entry (id, {', '.join(ENTRY_FIELDS)}) "
    f"VALUES ({', '.join('?' * (len(ENTRY_FIELDS) + 1))})"
)
BATCH = 2000
UNPARSEABLE_LIMIT = 50


@dataclass
class FileReport:
    path: str
    source_kind: str
    lines_ok: int = 0
    lines_bad: int = 0
    bytes_read: int = 0
    reindexed: bool = False
    skipped_reason: str | None = None


@dataclass
class Report:
    files_seen: int = 0
    files_changed: int = 0
    files_missing: int = 0
    files_duplicate: int = 0
    lines_ok: int = 0
    lines_bad: int = 0
    bytes_read: int = 0
    skipped: list[FileReport] = field(default_factory=list)
    elapsed_s: float = 0.0
    db_bytes: int = 0

    def add(self, item: FileReport) -> None:
        self.lines_ok += item.lines_ok
        self.lines_bad += item.lines_bad
        self.bytes_read += item.bytes_read
        if item.lines_ok or item.lines_bad:
            self.files_changed += 1
        if item.skipped_reason:
            self.skipped.append(item)


def connect(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if read_only and db_path.exists():
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    else:
        db = sqlite3.connect(db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    for table, columns in ADDED_COLUMNS.items():
        present = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in present:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    db.commit()


def _walk(config: Config) -> Iterator[tuple[Source, str, Found]]:
    for root in config.roots:
        for source in SOURCES:
            if not config.enabled(source.kind):
                continue
            for found in source.discover(root):
                yield source, root.name, found


def index_all(
    config: Config,
    *,
    full: bool = False,
    progress: Callable[[FileReport], None] | None = None,
) -> Report:
    started = time.time()
    db = connect(config.db_path)
    ensure_schema(db)
    report = Report()
    seen: list[int] = []
    seen_real: set[str] = set()

    for source, root_name, found in _walk(config):
        # One root's directory may be a symlink into another's, which is how a
        # VM shares a corpus with its host. The same file is one file.
        try:
            real = str(found.path.resolve())
        except OSError:
            real = str(found.path)
        if real in seen_real:
            report.files_duplicate += 1
            continue
        seen_real.add(real)
        report.files_seen += 1
        item, file_id = _ingest(db, config, source, root_name, found, full=full)
        seen.append(file_id)
        report.add(item)
        if progress and (item.lines_ok or item.lines_bad or item.skipped_reason):
            progress(item)

    report.files_missing = _mark_missing(db, seen)
    derive(db)
    correlate(db)
    db.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('indexed_at', ?)",
        (str(time.time()),),
    )
    db.commit()
    db.execute("PRAGMA optimize")
    if full:
        # A full reread leaves the pages of the old entries free but the file
        # the same size.
        db.execute("VACUUM")
    db.close()
    report.elapsed_s = time.time() - started
    report.db_bytes = config.db_path.stat().st_size if config.db_path.exists() else 0
    return report


def _ingest(
    db: sqlite3.Connection,
    config: Config,
    source: Source,
    root_name: str,
    found: Found,
    *,
    full: bool,
) -> tuple[FileReport, int]:
    path = found.path
    item = FileReport(path=str(path), source_kind=source.kind)
    try:
        stat = path.stat()
    except OSError as exc:
        item.skipped_reason = f"stat failed: {exc}"
        return item, -1

    row = db.execute(
        "SELECT id, bytes_ingested, skipped_reason FROM source_file WHERE path = ?",
        (str(path),),
    ).fetchone()

    if row is None:
        cur = db.execute(
            "INSERT INTO source_file (root, source_kind, stream, path, label, session_id,"
            " agent_id, parent_session_id, size, mtime) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (root_name, source.kind, source.stream, str(path), found.label,
             found.session_id, found.agent_id, found.parent_session_id,
             stat.st_size, stat.st_mtime),
        )
        file_id, start = int(cur.lastrowid), 0
    else:
        file_id = int(row["id"])
        start = int(row["bytes_ingested"])
        if full or stat.st_size < start:
            db.execute("DELETE FROM entry_fts WHERE rowid IN "
                       "(SELECT id FROM entry WHERE file_id = ?)", (file_id,))
            db.execute("DELETE FROM entry WHERE file_id = ?", (file_id,))
            db.execute("UPDATE source_file SET lines_ok = 0, lines_bad = 0,"
                       " skipped_reason = NULL WHERE id = ?", (file_id,))
            start = 0
            item.reindexed = True
        elif row["skipped_reason"]:
            return item, file_id

    db.execute("UPDATE source_file SET missing = 0, size = ?, mtime = ? WHERE id = ?",
               (stat.st_size, stat.st_mtime, file_id))

    if start >= stat.st_size:
        return item, file_id

    line_no = int(db.execute(
        "SELECT COALESCE(MAX(line_no), 0) FROM entry WHERE file_id = ?", (file_id,)
    ).fetchone()[0])
    next_id = int(db.execute("SELECT COALESCE(MAX(id), 0) FROM entry").fetchone()[0]) + 1

    rows: list[tuple] = []
    texts: list[tuple[int, str]] = []
    facts: dict[str, str] = {}
    offset = start

    with path.open("rb") as fh:
        fh.seek(start)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            here, offset = offset, offset + len(raw)
            line_no += 1
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                item.lines_bad += 1
                if item.lines_bad >= UNPARSEABLE_LIMIT and item.lines_ok == 0:
                    item.skipped_reason = "not JSONL: every line so far failed to parse"
                    offset = stat.st_size
                    break
                continue
            if not isinstance(obj, dict):
                item.lines_bad += 1
                continue

            cols = source.parse(obj, found)
            if cols is None:
                continue
            if source.facts is not None:
                facts.update({k: v for k, v in source.facts(obj).items() if v})

            entry_id = next_id
            next_id += 1
            rows.append((entry_id, file_id, line_no, here, len(raw), source.stream,
                         source.kind, *(cols.get(c) for c in COLUMNS)))
            body = source.text(obj)
            if body:
                texts.append((entry_id, body[: config.fts_chars]))
            item.lines_ok += 1

            if len(rows) >= BATCH:
                _flush(db, rows, texts)
                rows, texts = [], []

    _flush(db, rows, texts)
    item.bytes_read = offset - start
    db.execute(
        "UPDATE source_file SET bytes_ingested = ?, lines_ok = lines_ok + ?,"
        " lines_bad = lines_bad + ?, skipped_reason = COALESCE(?, skipped_reason),"
        " cwd = COALESCE(?, cwd), git_branch = COALESCE(?, git_branch),"
        " app_version = COALESCE(?, app_version), indexed_at = ? WHERE id = ?",
        (offset, item.lines_ok, item.lines_bad, item.skipped_reason,
         facts.get("cwd"), facts.get("git_branch"), facts.get("app_version"),
         time.time(), file_id),
    )
    db.commit()
    return item, file_id


def _flush(db: sqlite3.Connection, rows: list[tuple], texts: list[tuple[int, str]]) -> None:
    if rows:
        db.executemany(INSERT_ENTRY, rows)
    if texts:
        db.executemany("INSERT INTO entry_fts (rowid, text) VALUES (?, ?)", texts)


def _mark_missing(db: sqlite3.Connection, seen: list[int]) -> int:
    """Flag files that were indexed once and are no longer on disk.

    Their entries stay queryable; only body reads fail, which the UI shows.
    """
    db.execute("CREATE TEMP TABLE IF NOT EXISTS seen_file (id INTEGER PRIMARY KEY)")
    db.execute("DELETE FROM seen_file")
    db.executemany("INSERT OR IGNORE INTO seen_file (id) VALUES (?)",
                   [(i,) for i in seen if i > 0])
    cur = db.execute(
        "UPDATE source_file SET missing = 1 "
        "WHERE missing = 0 AND id NOT IN (SELECT id FROM seen_file)"
    )
    return cur.rowcount or 0


def derive(db: sqlite3.Connection) -> None:
    """Rebuild the session and agent rollups from the entries."""
    db.execute("DELETE FROM session")
    db.execute("""
        INSERT INTO session (session_id, first_ts, last_ts, entries, transcript_entries,
                             hook_entries, tool_calls, errors, in_tok, out_tok)
        SELECT session_id, MIN(ts), MAX(ts), COUNT(*),
               SUM(stream = 'transcript'), SUM(stream = 'hook'),
               SUM(tool_name IS NOT NULL AND kind = 'assistant'),
               SUM(COALESCE(has_error, 0)),
               SUM(COALESCE(in_tok, 0)), SUM(COALESCE(out_tok, 0))
        FROM entry WHERE session_id IS NOT NULL AND session_id <> ''
        GROUP BY session_id
    """)
    db.execute("""
        UPDATE session SET
          project = (SELECT f.label FROM source_file f
                     WHERE f.session_id = session.session_id AND f.source_kind = 'transcript'
                     ORDER BY f.agent_id <> '', f.id LIMIT 1),
          cwd = (SELECT f.cwd FROM source_file f
                 WHERE f.session_id = session.session_id AND f.cwd IS NOT NULL
                 ORDER BY f.id DESC LIMIT 1),
          git_branch = (SELECT f.git_branch FROM source_file f
                        WHERE f.session_id = session.session_id AND f.git_branch IS NOT NULL
                        ORDER BY f.id DESC LIMIT 1),
          app_version = (SELECT f.app_version FROM source_file f
                         WHERE f.session_id = session.session_id AND f.app_version IS NOT NULL
                         ORDER BY f.id DESC LIMIT 1),
          title = COALESCE(
            (SELECT e.summary FROM entry e WHERE e.session_id = session.session_id
             AND e.kind = 'custom-title' AND e.summary <> '' ORDER BY e.id DESC LIMIT 1),
            (SELECT e.summary FROM entry e WHERE e.session_id = session.session_id
             AND e.kind = 'ai-title' AND e.summary <> '' ORDER BY e.id DESC LIMIT 1),
            (SELECT e.summary FROM entry e WHERE e.session_id = session.session_id
             AND e.kind = 'last-prompt' AND e.summary <> '' ORDER BY e.id LIMIT 1)),


          models = (SELECT GROUP_CONCAT(m, ' ') FROM
                    (SELECT DISTINCT e.model AS m FROM entry e
                     WHERE e.session_id = session.session_id AND e.model IS NOT NULL
                       AND e.kind = 'assistant' ORDER BY m))
    """)
    db.execute("DELETE FROM agent")
    db.execute("""
        INSERT INTO agent (agent_id, session_id, agent_type, first_ts, last_ts,
                           entries, tool_calls, quarantined, summary)
        SELECT agent_id,
               (SELECT e2.session_id FROM entry e2 WHERE e2.agent_id = e.agent_id
                 AND e2.session_id <> '' LIMIT 1),
               (SELECT e3.agent_type FROM entry e3 WHERE e3.agent_id = e.agent_id
                 AND e3.agent_type IS NOT NULL AND e3.agent_type <> '' LIMIT 1),
               MIN(ts), MAX(ts), COUNT(*),
               SUM(tool_name IS NOT NULL AND kind = 'assistant'),
               MAX(kind = 'quarantine'),
               (SELECT e4.summary FROM entry e4 WHERE e4.agent_id = e.agent_id
                 AND e4.kind = 'subagent-stop' ORDER BY e4.id DESC LIMIT 1)
        FROM entry e WHERE agent_id IS NOT NULL AND agent_id <> ''
        GROUP BY agent_id
    """)
    db.execute(f"""
        UPDATE session SET
          cost = (SELECT MAX(e.cost) FROM entry e
                   WHERE e.session_id = session.session_id),
          cost_runs = (SELECT c.run_count FROM ({SESSION_COST}) c
                        WHERE c.session_id = session.session_id)
    """)
    db.commit()

    # Counted once the agent table exists, because it counts only the agents
    # that did work of their own.
    db.execute(f"""
        UPDATE session SET agents = (SELECT COUNT(*) FROM agent a
          WHERE a.session_id = session.session_id AND ({SUBSTANTIVE}))
    """)
    db.commit()


def correlate(db: sqlite3.Connection) -> None:
    """Place timestamp free entries on the time axis.

    The state dumps record a tool_use_id but no clock, so they borrow the
    timestamp of the hook or transcript entry for the same tool call.
    """
    db.execute("""
        UPDATE entry SET ts = (
          SELECT MIN(other.ts) FROM entry other
          WHERE other.tool_use_id = entry.tool_use_id AND other.ts IS NOT NULL
        )
        WHERE ts IS NULL AND tool_use_id IS NOT NULL AND tool_use_id <> ''
    """)
    db.commit()
