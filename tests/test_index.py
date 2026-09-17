import json

import pytest

from cclens import index

from .conftest import AGENT, SESSION, TOOL_USE, transcript_records, write


def kinds(db, source_kind):
    return {row["kind"]: row["n"] for row in db.execute(
        "SELECT kind, COUNT(*) n FROM entry WHERE source_kind = ? GROUP BY kind",
        (source_kind,))}


def test_every_source_reaches_the_index(indexed):
    streams = dict(indexed.execute("SELECT stream, COUNT(*) FROM entry GROUP BY stream"))
    assert set(streams) == {"transcript", "hook", "state", "statusline"}


def test_a_tool_call_is_recorded_from_the_transcript_and_the_hooks(indexed):
    streams = {row["stream"] for row in indexed.execute(
        "SELECT DISTINCT stream FROM entry WHERE tool_use_id = ?", (TOOL_USE,))}
    assert streams == {"transcript", "hook", "state"}


def test_a_subagent_transcript_is_attributed_to_its_agent_and_session(indexed):
    row = indexed.execute(
        "SELECT session_id, agent_id, is_sidechain FROM entry WHERE uuid = 'ag-1'").fetchone()
    assert (row["session_id"], row["agent_id"], row["is_sidechain"]) == (SESSION, AGENT, 1)


def test_the_agent_rollup_takes_its_type_from_the_stop_record(indexed):
    row = indexed.execute("SELECT agent_type, summary FROM agent WHERE agent_id = ?",
                          (AGENT,)).fetchone()
    assert row["agent_type"] == "explorer"
    assert "stock holds 42" in row["summary"]


def test_a_file_of_unparseable_lines_is_skipped_with_a_reason(indexed):
    row = indexed.execute(
        "SELECT skipped_reason, lines_ok FROM source_file WHERE path LIKE '%2026-01.jsonl'"
    ).fetchone()
    assert row["lines_ok"] == 0
    assert "not JSONL" in row["skipped_reason"]


def test_a_second_run_reads_only_the_new_lines(cfg, claude_home):
    first = index.index_all(cfg)
    again = index.index_all(cfg)
    assert again.lines_ok == 0

    path = claude_home / "audit" / "PreToolUse.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"_ts": 1767348100000, "session_id": SESSION,
                             "tool_name": "Read", "tool_use_id": "tu-four"}) + "\n")
    third = index.index_all(cfg)
    assert third.lines_ok == 1
    assert first.lines_ok > 1


def test_a_half_written_last_line_waits_for_its_newline(cfg, claude_home):
    path = claude_home / "audit" / "Stop.jsonl"
    path.write_text('{"_ts": 1767348200000, "session_id": "session-one"}\n{"_ts": 176',
                    encoding="utf-8")
    index.index_all(cfg)
    db = index.connect(cfg.db_path)
    assert db.execute("SELECT COUNT(*) FROM entry WHERE kind = 'Stop'").fetchone()[0] == 1
    db.close()

    with path.open("a", encoding="utf-8") as fh:
        fh.write('7348201000, "session_id": "session-one"}\n')
    index.index_all(cfg)
    db = index.connect(cfg.db_path)
    assert db.execute("SELECT COUNT(*) FROM entry WHERE kind = 'Stop'").fetchone()[0] == 2
    db.close()


def test_a_file_that_shrank_is_read_again_from_the_start(cfg, claude_home):
    index.index_all(cfg)
    path = claude_home / "audit" / "PreToolUse.jsonl"
    write(path, [{"_ts": 1767348300000, "session_id": SESSION, "tool_name": "Glob"}])
    index.index_all(cfg)
    db = index.connect(cfg.db_path)
    tools = [r[0] for r in db.execute(
        "SELECT tool_name FROM entry WHERE kind = 'PreToolUse'")]
    assert tools == ["Glob"]
    db.close()


def test_a_vanished_file_keeps_its_entries_and_is_marked(cfg, claude_home, tmp_path):
    index.index_all(cfg)
    moved = claude_home / "projects" / "-work-repo" / f"{SESSION}.jsonl"
    moved.rename(tmp_path / "carried-off.jsonl")

    report = index.index_all(cfg)
    assert report.files_missing == 1
    db = index.connect(cfg.db_path)
    assert db.execute("SELECT missing FROM source_file WHERE path = ?",
                      (str(moved),)).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM entry WHERE uuid = 'u-1'").fetchone()[0] == 1
    db.close()


def test_only_the_named_transcript_trees_are_read(claude_home, tmp_path):
    """A second tree beside projects is read when it is named, not before."""
    from cclens import config

    extra = claude_home / "projects_linux" / "-work-repo"
    write(extra / "other-session.jsonl", transcript_records())

    def index_with(project_dirs, db_name):
        cfg = config.from_env({
            "HOME": str(tmp_path),
            "CCLENS_CLAUDE_HOME": f"test={claude_home}",
            "CCLENS_DB": str(tmp_path / db_name),
            **({"CCLENS_PROJECT_DIRS": project_dirs} if project_dirs else {}),
        })
        index.index_all(cfg)
        db = index.connect(cfg.db_path)
        found = db.execute(
            "SELECT COUNT(*) FROM source_file WHERE path LIKE '%projects_linux%'"
        ).fetchone()[0]
        db.close()
        return found

    assert index_with(None, "default.db") == 0
    assert index_with("projects,projects_linux", "both.db") > 0


def test_a_corpus_shared_between_roots_through_a_symlink_is_read_once(claude_home, tmp_path):
    """A VM whose .claude symlinks the host's projects is one corpus, not two."""
    from cclens import config

    other = tmp_path / "vm-claude"
    other.mkdir()
    (other / "projects").symlink_to(claude_home / "projects")

    cfg = config.from_env({
        "HOME": str(tmp_path),
        "CCLENS_CLAUDE_HOME": f"mac={claude_home}:vm={other}",
        "CCLENS_DB": str(tmp_path / "shared.db"),
    })
    report = index.index_all(cfg)
    assert report.files_duplicate == 3

    db = index.connect(cfg.db_path)
    assert db.execute("SELECT COUNT(*) FROM entry WHERE uuid = 'u-1'").fetchone()[0] == 1
    db.close()


def test_records_without_a_clock_borrow_one_from_their_tool_call(indexed):
    row = indexed.execute(
        "SELECT ts FROM entry WHERE source_kind = 'state' AND tool_use_id = ?",
        (TOOL_USE,)).fetchone()
    assert row["ts"] is not None


def test_a_state_record_with_no_tool_call_keeps_no_timestamp(indexed):
    row = indexed.execute(
        "SELECT ts FROM entry WHERE kind = 'quarantine'").fetchone()
    assert row["ts"] == 1767348020.0


def test_transcript_sidecar_records_have_no_timestamp(indexed):
    undated = {row["kind"] for row in indexed.execute(
        "SELECT DISTINCT kind FROM entry WHERE ts IS NULL")}
    assert {"ai-title", "mode", "cost-state"} <= undated


def test_the_session_rollup_carries_the_title_project_and_cost(indexed):
    row = indexed.execute("SELECT * FROM session WHERE session_id = ?", (SESSION,)).fetchone()
    assert row["title"] == "Indexing spike"
    assert row["project"] == "-work-repo"
    assert row["cost"] == pytest.approx(0.9)
    assert row["cost_runs"] == 2
    assert row["cwd"] == "/work/repo"
    assert row["git_branch"] == "main"
    assert row["errors"] >= 1


def test_the_search_projection_covers_thinking_and_tool_input(indexed):
    hits = indexed.execute(
        "SELECT COUNT(*) FROM entry_fts WHERE entry_fts MATCH 'widgets'").fetchone()[0]
    assert hits >= 2


def test_a_cost_that_restarts_from_zero_is_counted_as_another_run(indexed):
    """The reported cost resets both on resume and on a skill fork, so the runs
    are counted and the highest single figure is what the session reports."""
    row = indexed.execute("SELECT cost, cost_runs FROM session WHERE session_id = ?",
                          (SESSION,)).fetchone()
    assert row["cost_runs"] == 2
    assert row["cost"] == pytest.approx(0.9)


def test_statusline_snapshots_stay_out_of_search(indexed):
    row = indexed.execute(
        "SELECT id FROM entry WHERE source_kind = 'statusline'").fetchone()
    assert indexed.execute("SELECT COUNT(*) FROM entry_fts WHERE rowid = ?",
                           (row["id"],)).fetchone()[0] == 0


def test_an_excluded_source_is_not_read(cfg, claude_home):
    trimmed = cfg.__class__(**{**cfg.__dict__, "exclude": frozenset({"statusline"})})
    index.index_all(trimmed)
    db = index.connect(cfg.db_path)
    assert db.execute("SELECT COUNT(*) FROM entry WHERE source_kind = 'statusline'"
                      ).fetchone()[0] == 0
    db.close()
