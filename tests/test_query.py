from cclens import query

from .conftest import AGENT, INTERFACE, SESSION, SWEPT, TOOL_USE


def test_sessions_are_listed_newest_first_with_their_project(indexed):
    result = query.sessions(indexed, {})
    assert result["total"] == 1
    assert result["rows"][0]["title"] == "Indexing spike"
    assert result["projects"][0]["project"] == "-work-repo"


def test_the_session_total_is_the_corpus_not_the_page(indexed):
    """The list pages by offset while `total` keeps counting the whole match, so
    a caller can tell it is holding less than everything."""
    page = query.sessions(indexed, {"limit": 1})
    assert len(page["rows"]) == 1
    assert page["total"] == 1
    beyond = query.sessions(indexed, {"limit": 1, "offset": 1})
    assert beyond["rows"] == []
    assert beyond["total"] == 1
    assert beyond["offset"] == 1


def test_doctor_names_a_source_that_found_nothing(transcripts_only):
    """An enabled source with no files says so. Silence would leave a reader
    unable to tell an absent directory from one never looked in."""
    lines = query.doctor(transcripts_only, ("transcript", "audit", "state", "statusline"))
    assert any(line.startswith("transcript") and "entries" in line for line in lines)
    for absent in ("audit", "state", "statusline"):
        assert any(line.startswith(absent) and "looked and found none" in line
                   for line in lines), absent


def test_doctor_stays_quiet_about_a_source_that_was_excluded(transcripts_only):
    lines = query.doctor(transcripts_only, ("transcript",))
    assert not any(line.startswith(("audit", "state", "statusline")) for line in lines)


def test_doctor_reports_every_source_that_did_find_files(indexed):
    lines = query.doctor(indexed, ("transcript", "audit", "state", "statusline"))
    assert not any("looked and found none" in line for line in lines)


def test_a_session_reports_its_files_agents_and_undated_records(indexed):
    found = query.session(indexed, SESSION)
    assert {f["source_kind"] for f in found["files"]} == {"transcript"}
    assert found["agent_counts"] == {"subagents": 2, "interface": 1}
    assert {r["kind"] for r in found["sidecar"]} >= {"ai-title", "mode"}


def test_a_subagent_reports_the_task_it_was_handed(indexed):
    """A subagent transcript opens with the prompt, so that record is the task."""
    rows = query.agents(indexed, {"session": SESSION})["rows"]
    found = next(r for r in rows if r["agent_id"] == AGENT)
    assert found["task"] == "count the widgets in stock"
    assert found["task_entry"] is not None
    assert found["transcript_file"] is not None
    assert found["transcript_missing"] == 0


def test_a_subagent_whose_transcript_is_gone_is_still_a_subagent(indexed):
    rows = query.agents(indexed, {"session": SESSION})["rows"]
    found = next(r for r in rows if r["agent_id"] == SWEPT)
    assert found["substantive"] == 1
    assert found["transcript_file"] is None
    assert found["task"] is None
    assert found["summary"] == "counted the widgets twice"
    assert found["stop_entry"] is not None


def test_an_id_that_only_ever_stopped_is_not_counted_as_a_subagent(indexed):
    """Progress lines, suggested replies and session summaries stop like a
    subagent but declare no type and do no work of their own."""
    listed = query.agents(indexed, {"session": SESSION})
    assert INTERFACE not in {r["agent_id"] for r in listed["rows"]}
    assert listed["subagents"] == 2
    assert listed["interface"] == 1

    other = query.agents(indexed, {"session": SESSION, "group": "interface"})
    assert [r["agent_id"] for r in other["rows"]] == [INTERFACE]
    assert other["rows"][0]["summary"] == "Reading widgets.txt"


def test_the_session_agent_count_counts_only_subagents(indexed):
    assert query.session(indexed, SESSION)["agent_counts"]["subagents"] == 2


def test_subagents_can_be_ordered_by_how_much_they_did(indexed):
    rows = query.agents(indexed, {"session": SESSION, "order": "entries"})["rows"]
    assert [r["agent_id"] for r in rows] == [AGENT, SWEPT]


def test_the_kept_filter_leaves_out_subagents_with_no_transcript(indexed):
    result = query.agents(indexed, {"session": SESSION, "kept": 1})
    assert [r["agent_id"] for r in result["rows"]] == [AGENT]
    assert query.agents(indexed, {"session": SESSION})["kept"] == 1


def test_the_timeline_leaves_out_records_without_a_timestamp(indexed):
    dated = query.entries(indexed, {"session": SESSION, "dated": 1, "meta": 1})
    assert all(row["ts"] is not None for row in dated["rows"])
    everything = query.entries(indexed, {"session": SESSION, "meta": 1})
    assert everything["total"] > dated["total"]


def test_the_main_session_lane_leaves_out_subagent_records(indexed):
    main = query.entries(indexed, {"session": SESSION, "dated": 1})
    assert not any(row["agent_id"] for row in main["rows"])
    with_agents = query.entries(indexed, {"session": SESSION, "dated": 1,
                                          "include_agents": 1})
    assert with_agents["total"] > main["total"]


def test_a_source_that_records_no_agent_stays_in_the_main_lane(indexed):
    rows = query.entries(indexed, {"session": SESSION, "dated": 1, "meta": 1,
                                   "kind": "statusline"})["rows"]
    assert len(rows) == 4
    assert not any(row["agent_id"] for row in rows)


def test_paging_walks_forward_without_repeating_a_row(indexed):
    first = query.entries(indexed, {"session": SESSION, "dated": 1, "limit": 2})
    assert first["next_cursor"] is not None
    second = query.entries(indexed, {"session": SESSION, "dated": 1, "limit": 2,
                                     "cursor": first["next_cursor"]})
    assert not ({r["id"] for r in first["rows"]} & {r["id"] for r in second["rows"]})


def test_one_tool_call_gathers_every_source_that_mentions_it(indexed):
    joined = query.tool_call(indexed, TOOL_USE)
    assert set(joined["streams"]) == {"transcript", "hook", "state"}
    assert joined["tool_name"] == "Bash"
    assert joined["duration_ms"] == 9
    assert joined["failed"] is False


def test_a_failed_tool_call_is_flagged_as_failed(indexed):
    assert query.tool_call(indexed, "tu-two")["failed"] is True


def test_a_body_read_returns_the_bytes_that_are_on_disk(indexed):
    row = indexed.execute("SELECT id FROM entry WHERE uuid = 'u-1'").fetchone()
    found = query.body(indexed, row["id"])
    assert found["error"] is None
    assert found["parsed"]["message"]["content"] == "count the widgets"
    assert found["raw"].startswith("{")
    assert found["provenance"]["line"] == 3
    assert found["provenance"]["byte_len"] == len(found["raw"])


def test_a_body_read_says_so_when_the_file_is_gone(indexed, claude_home, tmp_path):
    row = indexed.execute("SELECT id FROM entry WHERE uuid = 'u-1'").fetchone()
    source = claude_home / "projects" / "-work-repo" / f"{SESSION}.jsonl"
    source.rename(tmp_path / "carried-off.jsonl")
    found = query.body(indexed, row["id"])
    assert "no longer on disk" in found["error"]
    assert found["entry"]["uuid"] == "u-1"


def test_related_records_follow_the_parent_and_the_tool_call(indexed):
    row = indexed.execute("SELECT id FROM entry WHERE uuid = 'u-2'").fetchone()
    related = query.related(indexed, row["id"])
    assert [r["uuid"] for r in related["parent"]] == ["a-1"]
    assert {r["stream"] for r in related["same_tool_call"]} >= {"hook", "state"}


def test_hook_rows_borrow_the_summary_of_their_tool_call(indexed):
    rows = query.hooks(indexed, {"kind": "PreToolUse"})["rows"]
    assert rows and "wc -l widgets.txt" in rows[0]["context"]


def test_a_hook_record_holding_only_an_envelope_has_no_summary_of_its_own(indexed):
    row = indexed.execute(
        "SELECT summary FROM entry WHERE kind = 'PostToolUse'").fetchone()
    assert row["summary"] == ""


def test_search_returns_a_snippet_around_the_hit(indexed):
    result = query.search(indexed, {"q": "widgets"})
    assert result["error"] is None
    assert any("<<widgets>>" in row["snippet"] for row in result["rows"])


def test_search_falls_back_to_a_phrase_when_the_syntax_is_rejected(indexed):
    result = query.search(indexed, {"q": 'widgets AND ("'})
    assert result["error"] is None
    assert result["query"].startswith('"')


def test_an_empty_query_searches_for_nothing(indexed):
    assert query.search(indexed, {"q": "  "})["rows"] == []


def test_stats_counts_the_sources_tools_and_hook_timings(indexed):
    stats = query.stats(indexed)
    assert stats["totals"]["sessions"] == 1
    assert {row["stream"] for row in stats["streams"]} == {"transcript", "hook",
                                                           "state", "statusline"}
    assert any(row["tool_name"] == "Bash" for row in stats["tools"])
    assert {row["bucket"] for row in stats["hook_latency"]} <= {
        "under 50us", "50us to 200us", "200us to 1ms", "1ms to 100ms", "over 100ms"}


def test_the_slow_hook_lands_in_the_slow_bucket(indexed):
    rows = [r for r in query.stats(indexed)["hook_latency"] if r["kind"] == "PostToolUse"]
    assert [r["bucket"] for r in rows] == ["over 100ms"]
