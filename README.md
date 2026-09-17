# cclens

A local browser for Claude Code transcripts and hook logs. It indexes the JSONL
files under your `.claude` directories and serves a single page on localhost
where you can read a session, watch the hook events interleave with it, and open
any record down to the bytes it occupies on disk.

Stdlib only. No install, no build step, no network.

## Getting started on a Mac

You do not need to install anything. macOS already ships the Python this needs.

**1. Open Terminal.** Press Command and Space, type `Terminal`, press Return. A
window opens where you type commands and press Return after each one.

**2. Check Python is there.** Paste this and press Return:

```
python3 -c "import sqlite3, sys; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)'); print(sys.version)"
```

You should see a version number, 3.9 or higher. If macOS offers to install
developer tools, accept, then run the line again. If it prints an error
mentioning `fts5`, stop here and say so: search will not work on that Mac.

**3. Get cclens.** On the repository page on GitHub, click the green **Code**
button, then **Download ZIP**. Double-click the downloaded file to unzip it.
You will have a folder called `cclens-main` in your Downloads.

**4. Point Terminal at that folder.** Type `cd ` (with the space), then drag the
`cclens-main` folder from Finder onto the Terminal window, which fills in its
location, then press Return.

**5. Read your logs.** This looks through the Claude Code files already on your
Mac and builds an index of them:

```
python3 -m cclens index
```

It prints what it found. A few seconds is normal, and a minute is normal if you
have used Claude Code heavily.

**6. Open it.**

```
python3 -m cclens serve --open
```

Your browser opens on the page. Leave the Terminal window alone while you use
it; closing it stops the server. Press Control and C in Terminal when you are
finished.

To see anything new later, run step 5 again, then step 6.

### Nothing leaves your Mac

cclens reads the files Claude Code already wrote in your home folder, and
writes one index file under `~/.cache/cclens`. It makes no network requests of
any kind, and the page it serves is reachable only from your own machine.

### If something goes wrong

| What you see | What to do |
|---|---|
| `command not found: python3` | Run `xcode-select --install`, accept the prompt, wait for it to finish, then try again. |
| `No module named cclens` | Terminal is pointed at the wrong folder. Redo step 4, and check the folder you drag in is the one containing a folder called `cclens`. |
| `Address already in use` | Something else is on that port. Use `python3 -m cclens serve --open --port 8899`. |
| The page opens but there are no sessions | Run `python3 -m cclens doctor`. It prints where it looked. If it shows 0 transcript files, Claude Code has not written anything on this Mac yet. |

## Design intent

Four separate logs describe the same work, and each is useless alone. A
transcript says what was said. The hook audit says which hooks fired and how
long they took. The state dumps written by tool hooks hold the full input and
output of every tool call. The statusline log holds the running cost. They share
four keys, and those keys are the whole reason this exists:

| Key | Ties together |
|---|---|
| `session_id` | every source, for one session |
| `tool_use_id` | a tool call's transcript blocks, its hook records and its state dumps |
| `agent_id` | a subagent's own transcript, its start and stop records, its quarantine |
| `uuid` / `parentUuid` | a record and the record it answers to |

So a hook row shows the command it fired for, a tool call opens as one page
listing every source that mentions it, and a session timeline puts hooks and
messages on one axis.

## How it works

The index holds metadata and a byte offset, never a body. Each entry stores its
file, line number, offset and length; a body is read by seeking back into the
original file when you open the record. The index stays a fraction of the corpus,
the raw view is exactly the bytes on disk, and nothing is ever copied out of
`.claude`.

Source files are append only, so a run reads from `bytes_ingested` forward and
takes only whole lines. A line still being written is left for the next run. A
file whose size went backwards is read again from zero. A file that has left the
disk keeps its entries and is marked, because the metadata is still worth having
after a retention sweep takes the transcript.

Indexing 475 files and 1.1 GB takes about 25 seconds. Re-running with nothing
new takes under a second.

```
$ python3 -m cclens index
indexing into ~/.cache/cclens/index.db
  root host: ~/.claude

624696 entries from 475 changed file(s) of 475 seen, 1.1 GB read in 24.6s
101 unparseable line(s)
473 file(s) skipped as the same file reached through more than one root
skipped ~/.claude/projects/2026-05.jsonl: not JSONL: every line so far failed to parse
index is 395.5 MB
```

Tables: `source_file` (one row per file, with what has been read), `entry` (one
row per line), `entry_fts` (an FTS5 projection of the searchable text, capped
per entry), `session` and `agent` (rollups rebuilt on every run).

## Views

- **Sessions.** Every session with its project, size, tool count, subagent
  count, errors and cost.
- **Session.** Transcript, or a timeline with the hook events mixed in, or the
  hooks alone. `Meta records` reveals the attachment and system records the real
  UI hides. `Session records` holds the ones with no timestamp: title history,
  mode changes, latch state.
- **Subagents.** Every subagent of the session, each showing the task it was
  handed, what it did, and whether its transcript is still on disk. A subagent
  opens with the prompt it was given, so that first record is the task. Open one
  for its full prompt, its full closing message, and its own records.

  `SubagentStop` also fires for the short model calls behind the progress lines,
  the suggested replies and the session summaries. Those declare no type, call
  no tool, keep no transcript and stop immediately, and they outnumber the real
  subagents by twenty to one, so they are counted and listed apart under
  Interface agents rather than inflating the subagent count. An agent counts as
  a subagent when it declared a type, kept a transcript, called a tool, or was
  seen starting.
- **Hooks.** Every event type with counts, mean hook time and failures, each row
  labelled with the tool call it fired for.
- **Search.** FTS5 over message text, thinking, tool inputs and tool output
  heads. Operators work: `foo AND bar`, `"exact phrase"`, `pre*`,
  `NEAR(a b, 5)`. A query FTS rejects is retried as a phrase.
- **Stats.** Entries per day by source, and the distribution of each hook's own
  runtime.
- **Inspector**, in every view: the indexed fields, the parsed body as a
  collapsible tree with copy-value and copy-path on every node, the raw line
  with its path, line and byte offset, and the records related by tool call,
  parent or agent.

Keys: `/` search, `j` and `k` move, `1` to `4` switch views, `Esc` closes the
inspector. Every view is addressable, so `#/session/<id>?tab=timeline&e=<entry>`
is a link to one record in context.

## Configuration

Read from the environment, all optional.

| Variable | Default | Meaning |
|---|---|---|
| `CCLENS_CLAUDE_HOME` | `~/.claude` | roots to index, colon separated, each optionally `name=path` |
| `CCLENS_DB` | `$XDG_CACHE_HOME/cclens/index.db` | where the index lives |
| `CCLENS_HOST` | `127.0.0.1` | bind address. A loopback bind answers only to a loopback `Host` header |
| `CCLENS_PORT` | `8731` | bind port |
| `CCLENS_FTS_CHARS` | `4000` | per entry cap on indexed search text |
| `CCLENS_EXCLUDE` | none | source kinds to skip: `transcript`, `audit`, `state`, `statusline` |
| `CCLENS_PROJECT_DIRS` | `projects` | transcript directories to read under each root, comma separated |

`--claude-home`, `--db`, `--exclude`, `--project-dirs`, `--host` and `--port`
override the environment. Roots are deduplicated by real path, so a `.claude`
reached through a symlink from another host is one corpus, not two.

Only `projects` is read unless you say otherwise. A host that ran Claude Code
before its projects directory was pointed at shared storage keeps the earlier
transcripts in a sibling tree; `doctor` names any such tree it can see and
prints the flag that would read it.

Two hosts sharing a corpus through symlinks, where only the statusline log
differs:

```
python3 -m cclens --claude-home "host=/mnt/host/.claude:local=$HOME/.claude" \
  --project-dirs projects,projects_linux index
```

`python3 -m cclens doctor` prints the configuration, what each source found, and
anything skipped.

## Sources

A source is one `Source` in `cclens/sources.py`, declaring how to find its
files, how to turn a line into entry columns, and what text to make searchable.
That module's docstring is the entire contract. Adding one is a change to that
file and nothing else.

| Kind | Reads | Notes |
|---|---|---|
| `transcript` | `<tree>/*/*.jsonl` and `<tree>/*/*/subagents/*.jsonl`, for each tree in `CCLENS_PROJECT_DIRS` | 19 record types; subagent files carry the agent id in the name |
| `audit` | `audit/*.jsonl` | one file per hook event, uniform envelope plus whichever of forty keys applied |
| `state` | `state/tool-calls.jsonl`, `tool-results.jsonl`, `subagent-stops.jsonl`, `quarantined-agents.jsonl` | full tool inputs and outputs; these carry no clock, so they borrow the timestamp of their tool call |
| `statusline` | `statusline-debug.jsonl` | one snapshot per render; kept out of search because it repeats. Its `total_cost_usd` is cumulative per run and restarts at zero both on resume and on a skill fork, so a session reports the highest single figure any run reached, and the number of runs beside it |

Files at the root of `projects/` are grep style dumps rather than JSONL. The
indexer detects that from the parse failures and skips them by name in the
report rather than dropping the lines quietly.

## Development

```
python3 -m pytest
python3 -m ruff check .
```

Tests run against synthetic fixtures built in a temporary directory, and pass on
the Python macOS ships, which is the floor the project declares.

## Security

`serve` binds loopback, answers only to a loopback `Host` header, sets no CORS
headers, makes no outbound request and needs no credentials. Your transcripts
are not anonymous and neither is the index. See `SECURITY.md`.
