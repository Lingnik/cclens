// cclens front end. No build step, no dependencies, no outbound requests.

const $ = (sel) => document.querySelector(sel);
const nav = $("#nav");
const rail = $("#rail");
const content = $("#content");
const inspector = $("#inspector");
const frame = $(".frame");
const omni = $("#omni-input");
const toastEl = $("#toast");

const STREAMS = ["transcript", "hook", "state", "statusline"];
const BUCKETS = ["under 50us", "50us to 200us", "200us to 1ms", "1ms to 100ms", "over 100ms"];
const PAGE = 200;

const state = { rows: [], entry: null, expanded: new Set(), showAllStrings: new Set() };

// --- small helpers --------------------------------------------------------

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function num(value) {
  if (value === null || value === undefined || value === "") return "";
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString() : String(value);
}

function bytes(value) {
  let n = Number(value || 0);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${i === 0 ? n : n.toFixed(1)} ${units[i]}`;
}

function money(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return "";
  return `$${n.toFixed(n < 1 ? 4 : 2)}`;
}

function micros(value) {
  if (value === null || value === undefined || value === "") return "";
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  if (n < 1000) return `${Math.round(n)}us`;
  if (n < 1e6) return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}ms`;
  return `${(n / 1e6).toFixed(1)}s`;
}

function millis(value) {
  if (value === null || value === undefined || value === "") return "";
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  if (n < 1000) return `${Math.round(n)}ms`;
  if (n < 60000) return `${(n / 1000).toFixed(1)}s`;
  if (n < 3600000) return `${Math.floor(n / 60000)}m ${Math.round((n % 60000) / 1000)}s`;
  if (n < 86400000) return `${Math.floor(n / 3600000)}h ${Math.round((n % 3600000) / 60000)}m`;
  return `${Math.floor(n / 86400000)}d ${Math.round((n % 86400000) / 3600000)}h`;
}

function clock(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}

function stamp(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${d.toLocaleTimeString([], { hour12: false })}`;
}

function ago(ts) {
  if (!ts) return "";
  const secs = Date.now() / 1000 - ts;
  const steps = [[60, "s"], [3600, "m"], [86400, "h"], [604800, "d"]];
  if (secs < 60) return `${Math.round(secs)}s ago`;
  for (let i = 1; i < steps.length; i += 1) {
    if (secs < steps[i][0]) return `${Math.floor(secs / steps[i - 1][0])}${steps[i][1]} ago`;
  }
  const days = Math.floor(secs / 86400);
  return days < 365 ? `${days}d ago` : `${(days / 365).toFixed(1)}y ago`;
}

function toast(message) {
  toastEl.textContent = message;
  toastEl.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { toastEl.hidden = true; }, 1600);
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("copied");
  } catch {
    toast("the browser refused clipboard access");
  }
}

async function api(path, params = {}) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
  }
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  const body = await res.json().catch(() => ({ error: `${res.status} ${res.statusText}` }));
  if (!res.ok) throw new Error(body.error || `${res.status}`);
  return body;
}

// --- routing --------------------------------------------------------------

function route() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [pathPart, queryPart] = raw.split("?");
  const parts = pathPart.split("/").filter(Boolean);
  const q = Object.fromEntries(new URLSearchParams(queryPart || ""));
  return { view: parts[0] || "sessions", id: parts[1] ? decodeURIComponent(parts[1]) : "", q };
}

function href(view, id, q = {}) {
  const clean = Object.entries(q).filter(([, v]) => v !== undefined && v !== null && v !== "");
  const query = clean.length ? `?${new URLSearchParams(clean)}` : "";
  return `#/${view}${id ? `/${encodeURIComponent(id)}` : ""}${query}`;
}

function go(patch, replace = false) {
  const r = route();
  const q = { ...r.q, ...patch };
  for (const [k, v] of Object.entries(q)) if (v === null || v === "") delete q[k];
  const target = href(patch.view || r.view, "view" in patch ? (patch.id || "") : r.id, q);
  if (replace) history.replaceState(null, "", target);
  else location.hash = target;
  if (replace) render();
}

function link(view, id, q) {
  return esc(href(view, id, q));
}

// --- shared rendering -----------------------------------------------------

function chip(text, cls = "") {
  return `<span class="chip ${cls}">${esc(text)}</span>`;
}

function railGroup(title, items, activeValue, param) {
  const rows = items.map((item) => `
    <button class="rail-item ${String(activeValue || "") === String(item.value) ? "on" : ""}"
            data-rail="${esc(param)}" data-value="${esc(item.value)}" title="${esc(item.title || item.label)}">
      <span class="label">${esc(item.label)}</span>
      <span class="count">${item.count === undefined ? "" : num(item.count)}</span>
    </button>`).join("");
  return `<h3>${esc(title)}</h3>${rows}`;
}

function entryRow(row, options = {}) {
  const classes = ["row", `kind-${row.kind}`];
  if (row.has_error) classes.push("is-error");
  if (row.is_meta) classes.push("is-meta");
  if (row.agent_id) classes.push("row-agent");
  if (String(state.selected) === String(row.id)) classes.push("sel");
  const lane = [
    chip(row.kind, `stream-${row.stream}`),
    row.role === "user" && row.kind !== "user" ? chip("user", "role-user") : "",
  ].filter(Boolean).join("");
  const meta = row.elapsed_us ? micros(row.elapsed_us)
    : row.duration_ms ? millis(row.duration_ms)
    : row.out_tok ? `${num(row.out_tok)}t` : "";
  const borrowed = !row.summary && row.context;
  const summary = options.snippet && row.snippet
    ? esc(row.snippet).replace(/&lt;&lt;/g, "<mark>").replace(/&gt;&gt;/g, "</mark>")
    : esc(row.summary || row.context || "");
  return `<div class="${classes.join(" ")}" data-entry="${row.id}" role="button" tabindex="0">
    <span class="time">${esc(options.dateToo ? stamp(row.ts) : clock(row.ts))}</span>
    <span class="lane">${lane}</span>
    <span class="lane">${row.tool_name ? chip(row.tool_name, "tool") : ""}</span>
    <span class="what dim">${esc(options.second || row.subtype || "")}</span>
    <span class="what summary ${borrowed ? "borrowed" : ""}"
          ${borrowed ? 'title="from the tool call this hook fired for"' : ""}>${summary}</span>
    <span class="meta">${esc(meta)}</span>
  </div>`;
}

function rowsBlock(rows, options = {}) {
  if (!rows.length) return `<div class="empty">Nothing matches.</div>`;
  state.rows = rows.map((r) => r.id);
  return `<div class="rows ${options.dateToo ? "dated" : ""}">
    ${rows.map((r) => entryRow(r, options)).join("")}</div>`;
}

function card(label, value, small = false) {
  return `<div class="card"><div class="k">${esc(label)}</div>
    <div class="v ${small ? "small" : ""}">${value === "" || value === null || value === undefined ? "&mdash;" : esc(value)}</div></div>`;
}

// --- view: sessions -------------------------------------------------------

async function viewSessions(r) {
  const data = await api("/api/sessions", {
    project: r.q.project, q: r.q.sq, order: r.q.order || "recent",
    errors: r.q.errors, limit: 150,
  });
  const projects = [
    { value: "", label: "All projects", count: data.projects.reduce((a, p) => a + p.n, 0) },
    ...data.projects.map((p) => ({ value: p.project, label: p.project, count: p.n })),
  ];
  rail.innerHTML = railGroup("Projects", projects, r.q.project, "project");

  const orders = [["recent", "Recent"], ["entries", "Entries"], ["cost", "Cost"], ["tools", "Tool calls"]];
  content.innerHTML = `<div class="page">
    <div class="page-head">
      <h1>Sessions</h1>
      <span class="sub">${num(data.total)} indexed${r.q.project ? ` in ${esc(r.q.project)}` : ""}</span>
      <span class="spacer"></span>
      <div class="controls">
        <input type="search" id="session-q" placeholder="filter title, cwd, id" value="${esc(r.q.sq || "")}">
        ${orders.map(([v, l]) => `<button data-order="${v}" class="${(r.q.order || "recent") === v ? "on" : ""}">${l}</button>`).join("")}
        <button data-toggle="errors" class="${r.q.errors ? "on" : ""}">Errors only</button>
      </div>
    </div>
    <table class="grid">
      <colgroup>
        <col class="w-when"><col><col class="w-project">
        <col class="w-num"><col class="w-num"><col class="w-num">
        <col class="w-num"><col class="w-cost"><col class="w-models">
      </colgroup>
      <thead><tr>
        <th class="nowrap">When</th><th>Title</th><th>Project</th>
        <th class="num">Entries</th><th class="num">Tools</th><th class="num">Agents</th>
        <th class="num">Errors</th><th class="num">Cost</th><th>Models</th>
      </tr></thead>
      <tbody>${data.rows.map((s) => `
        <tr data-session="${esc(s.session_id)}">
          <td class="nowrap dim" title="${esc(stamp(s.last_ts))}">${esc(ago(s.last_ts))}</td>
          <td class="trunc title" title="${esc(s.title || s.session_id)}">${esc(s.title || s.session_id)}</td>
          <td class="trunc dim" title="${esc(s.project || "")}">${esc(s.project || "")}</td>
          <td class="num">${num(s.entries)}</td>
          <td class="num">${num(s.tool_calls)}</td>
          <td class="num">${s.agents ? num(s.agents) : ""}</td>
          <td class="num ${s.errors ? "" : "dim"}">${s.errors ? num(s.errors) : ""}</td>
          <td class="num">${money(s.cost)}</td>
          <td class="trunc dim" title="${esc(s.models || "")}">${esc((s.models || "").replace(/claude-/g, ""))}</td>
        </tr>`).join("")}
      </tbody>
    </table>
    ${data.rows.length ? "" : `<div class="empty">No sessions match.</div>`}
  </div>`;

  content.querySelectorAll("tr[data-session]").forEach((tr) => {
    tr.addEventListener("click", () => { location.hash = href("session", tr.dataset.session); });
  });
  content.querySelectorAll("button[data-order]").forEach((b) => {
    b.addEventListener("click", () => go({ order: b.dataset.order }));
  });
  const toggle = content.querySelector('button[data-toggle="errors"]');
  toggle.addEventListener("click", () => go({ errors: r.q.errors ? "" : "1" }));
  const filter = content.querySelector("#session-q");
  filter.addEventListener("change", () => go({ sq: filter.value }));
}

// --- view: one session ----------------------------------------------------

const SESSION_TABS = [
  ["transcript", "Transcript"],
  ["timeline", "Timeline"],
  ["hooks", "Hooks"],
  ["subagents", "Subagents"],
  ["records", "Session records"],
  ["files", "Files"],
];

async function viewSession(r) {
  const s = await api(`/api/sessions/${encodeURIComponent(r.id)}`);
  // A session whose transcript was swept still has its hook and statusline
  // records, so it opens on the timeline rather than on an empty tab.
  const tab = r.q.tab || (s.transcript_entries ? "transcript" : "timeline");

  rail.innerHTML = [
    railGroup("Record kinds", [{ value: "", label: "All kinds", count: s.entries }].concat(
      s.kinds.map((k) => ({ value: k.kind, label: k.kind, count: k.n, title: `${k.stream} / ${k.kind}` }))),
      r.q.kind, "kind"),
    s.tools.length ? railGroup("Tools", [{ value: "", label: "All tools" }].concat(
      s.tools.map((t) => ({ value: t.tool_name, label: t.tool_name, count: t.n }))), r.q.tool, "tool") : "",
  ].join("");

  const span = s.first_ts && s.last_ts ? millis((s.last_ts - s.first_ts) * 1000) : "";
  content.innerHTML = `<div class="page">
    <div class="page-head">
      <h1>${esc(s.title || s.session_id)}</h1>
      ${s.transcript_entries ? "" : chip("no transcript indexed", "bad")}
      <span class="spacer"></span>
      <div class="controls">
        <button data-toggle="meta" class="${r.q.meta ? "on" : ""}" title="show records the transcript view normally hides">Meta records</button>
        <button data-toggle="agents" class="${r.q.agents ? "on" : ""}">Subagent lanes</button>
        <button data-toggle="errors" class="${r.q.errors ? "on" : ""}">Errors only</button>
      </div>
    </div>
    <div class="cards">
      ${card("Entries", num(s.entries))}
      ${card("Transcript", num(s.transcript_entries))}
      ${card("Hook events", num(s.hook_entries))}
      ${card("Tool calls", num(s.tool_calls))}
      ${card("Subagents", num(s.agent_counts.subagents))}
      ${card("Errors", num(s.errors))}
      ${card("Cost", s.cost_runs > 1 ? `${money(s.cost)} of ${s.cost_runs} runs`
        : money(s.cost) || "not recorded", true)}
      ${card("Span", span, true)}
    </div>
    <div class="panel">
      <dl class="kv">
        <dt>session</dt><dd>${esc(s.session_id)}</dd>
        <dt>project</dt><dd>${esc(s.project || "")}</dd>
        <dt>cwd</dt><dd>${esc(s.cwd || "")}</dd>
        <dt>branch</dt><dd>${esc(s.git_branch || "")}</dd>
        <dt>version</dt><dd>${esc(s.app_version || "")}</dd>
        <dt>first seen</dt><dd>${esc(stamp(s.first_ts))}</dd>
        <dt>last seen</dt><dd>${esc(stamp(s.last_ts))}</dd>
        <dt>tokens</dt><dd>${num(s.in_tok)} in / ${num(s.out_tok)} out</dd>
      </dl>
    </div>
    <div class="controls" id="session-tabs">
      ${SESSION_TABS.map(([v, l]) => `<button data-tab="${v}" class="${tab === v ? "on" : ""}">${l}${
        v === "subagents" && s.agent_counts.subagents
          ? ` <span class="tab-count">${num(s.agent_counts.subagents)}</span>` : ""
      }</button>`).join("")}
    </div>
    <div id="session-body"></div>
  </div>`;

  content.querySelectorAll("button[data-tab]").forEach((b) => {
    b.addEventListener("click", () => go({ tab: b.dataset.tab, cursor: "" }));
  });
  ["meta", "agents", "errors"].forEach((key) => {
    content.querySelector(`button[data-toggle="${key}"]`)
      .addEventListener("click", () => go({ [key]: r.q[key] ? "" : "1" }));
  });

  const body = $("#session-body");
  if (tab === "subagents") {
    await viewSubagents(body, r);
    return;
  }
  if (!s.transcript_entries && (tab === "transcript" || tab === "records")) {
    body.innerHTML = `<div class="panel">
      <h2>No transcript indexed</h2>
      <p class="rail-note">No transcript for this session reached the index.
      Either it was swept from disk, or it lives in a tree the index was not
      pointed at. Check the roots under Stats, then run the indexer again.</p>
      <p class="rail-note">What survives is what the hooks and the statusline
      wrote elsewhere: ${num(s.hook_entries)} hook event(s) and
      ${num(s.entries - s.transcript_entries - s.hook_entries)} other record(s),
      which the Timeline holds${s.cost ? `, along with the ${money(s.cost)} it
      billed` : ""}.</p>
      <p><button data-tab="timeline">Open the timeline</button></p>
    </div>`;
    body.querySelector("button[data-tab]")
      .addEventListener("click", () => go({ tab: "timeline" }));
    return;
  }
  if (tab === "files") {
    body.innerHTML = `<table class="grid">
      <thead><tr><th>Source</th><th>Path</th><th class="nowrap">Size</th>
        <th class="nowrap">Entries</th><th class="nowrap">Bad lines</th><th>State</th></tr></thead>
      <tbody>${s.files.map((f) => `<tr data-file="${f.id}">
        <td class="nowrap">${chip(f.source_kind)}</td>
        <td class="trunc mono" title="${esc(f.path)}">${esc(f.path)}</td>
        <td class="num">${bytes(f.size)}</td>
        <td class="num">${num(f.lines_ok)}</td>
        <td class="num ${f.lines_bad ? "" : "dim"}">${f.lines_bad ? num(f.lines_bad) : ""}</td>
        <td class="nowrap">${f.missing ? chip("gone from disk", "bad") : chip("present")}</td>
      </tr>`).join("")}</tbody></table>`;
    body.querySelectorAll("tr[data-file]").forEach((tr) => {
      tr.addEventListener("click", () => go({ tab: "timeline", file: tr.dataset.file }));
    });
    return;
  }
  if (tab === "records") {
    body.innerHTML = `<p class="rail-note">Records with no timestamp of their own: the
      session's title history, mode changes and latch state. They are not timeline events.</p>
      ${rowsBlock(s.sidecar, { second: "" })}`;
    wireRows(body);
    return;
  }

  const params = {
    session: r.id, limit: PAGE, meta: r.q.meta, errors: r.q.errors,
    kind: r.q.kind, tool: r.q.tool, dated: 1,
  };
  if (r.q.agent) { params.agent = r.q.agent; params.include_agents = 1; }
  else if (r.q.agents) params.include_agents = 1;
  if (r.q.file) params.file = r.q.file;
  if (tab === "transcript") params.stream = "transcript";
  if (tab === "hooks") params.stream = "hook";

  await paginate(body, params, { dateToo: tab === "timeline" });
}

// --- subagents ------------------------------------------------------------

function messageText(parsed) {
  const content = parsed && parsed.message && parsed.message.content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((b) => b && (b.type === "text" || b.type === "thinking"))
    .map((b) => b.text || b.thinking || "")
    .join("\n\n");
}

function transcriptChip(row) {
  if (!row.transcript_file) return chip("no transcript kept", "bad");
  if (row.transcript_missing) return chip("transcript swept", "bad");
  return chip("transcript kept");
}

async function viewSubagents(target, r) {
  const data = await api("/api/agents", {
    session: r.id, group: "subagents", order: r.q.aorder || "recent",
    kept: r.q.kept, errors: r.q.aerrors,
  });
  if (r.q.agent) {
    await subagentDetail(target, r);
    return;
  }

  const orders = [["recent", "Recent"], ["entries", "Entries"],
                  ["tools", "Tool calls"], ["errors", "Errors"]];
  target.innerHTML = `
    <div class="page-head">
      <span class="sub">${num(data.total)} subagent${data.total === 1 ? "" : "s"},
        ${num(data.kept)} still ${data.kept === 1 ? "has its" : "have their"} transcript</span>
      <span class="spacer"></span>
      <div class="controls">
        ${orders.map(([v, l]) => `<button data-aorder="${v}"
          class="${(r.q.aorder || "recent") === v ? "on" : ""}">${l}</button>`).join("")}
        <button data-atoggle="kept" class="${r.q.kept ? "on" : ""}">With transcript</button>
        <button data-atoggle="aerrors" class="${r.q.aerrors ? "on" : ""}">Errors only</button>
      </div>
    </div>
    ${data.rows.length ? `<div class="agents">${data.rows.map(agentCard).join("")}</div>`
      : `<div class="empty">No subagent matches.</div>`}
    ${data.interface ? interfacePanel(r, data.interface) : ""}`;

  target.querySelectorAll("button[data-aorder]").forEach((b) => {
    b.addEventListener("click", () => go({ aorder: b.dataset.aorder }));
  });
  target.querySelectorAll("button[data-atoggle]").forEach((b) => {
    b.addEventListener("click", () => go({ [b.dataset.atoggle]: r.q[b.dataset.atoggle] ? "" : "1" }));
  });
  target.querySelectorAll(".agent-card").forEach((el) => {
    el.addEventListener("click", () => go({ agent: el.dataset.agent }));
  });

  const toggle = target.querySelector("[data-iface]");
  if (toggle) toggle.addEventListener("click", () => go({ iface: r.q.iface ? "" : "1" }));
  if (r.q.iface) {
    const other = await api("/api/agents", { session: r.id, group: "interface" });
    $("#iface-rows").innerHTML = other.rows.map((row) => `
      <button class="iface-row" data-agent="${esc(row.agent_id)}">
        <span class="time">${esc(stamp(row.first_ts))}</span>
        <span class="msg">${esc(row.summary || "no message recorded")}</span>
      </button>`).join("");
    $("#iface-rows").querySelectorAll(".iface-row").forEach((el) => {
      el.addEventListener("click", () => go({ agent: el.dataset.agent }));
    });
  }
}

function interfacePanel(r, count) {
  return `<div class="panel">
    <h2>Interface agents &middot; ${num(count)}</h2>
    <p class="rail-note">The short model calls behind the progress lines, the
    suggested replies and the session summaries. They declare no type, call no
    tools and keep no transcript, and each one stops immediately, so they are
    counted apart from the session's subagents.</p>
    <button data-iface class="${r.q.iface ? "on" : ""}">${r.q.iface ? "Hide" : "Show"} them</button>
    <div id="iface-rows"></div>
  </div>`;
}

function agentSpan(row) {
  if (!row.first_ts || !row.last_ts || row.last_ts <= row.first_ts) return "";
  return millis((row.last_ts - row.first_ts) * 1000);
}

function agentCard(row) {
  const span = agentSpan(row);
  const body = row.task || row.summary || "";
  const label = row.task ? "asked to" : row.summary ? "ended saying" : "";
  return `<button class="agent-card" data-agent="${esc(row.agent_id)}">
    <div class="agent-line">
      ${chip(row.agent_type || "type not recorded", row.agent_type ? "tool" : "")}
      <span class="agent-when">${esc(stamp(row.first_ts))}</span>
      ${span ? `<span class="agent-when dim">${esc(span)}</span>` : ""}
      <span class="spacer"></span>
      ${row.quarantined ? chip("quarantined", "bad") : ""}
      ${transcriptChip(row)}
    </div>
    <div class="agent-body">
      ${label ? `<span class="agent-label">${label}</span>` : ""}
      <span class="agent-text">${esc(body) || '<span class="dim">nothing recorded</span>'}</span>
    </div>
    <div class="agent-stats">
      <span>${num(row.entries)} records</span>
      <span>${num(row.tool_calls || 0)} tool calls</span>
      ${row.errors ? `<span class="bad-text">${num(row.errors)} errors</span>` : ""}
      ${row.out_tok ? `<span>${num(row.out_tok)} output tokens</span>` : ""}
      <span class="mono dim">${esc(row.agent_id)}</span>
    </div>
  </button>`;
}

async function subagentDetail(target, r) {
  const all = await api("/api/agents", { session: r.id, group: "all" });
  const row = all.rows.find((a) => a.agent_id === r.q.agent);
  if (!row) {
    target.innerHTML = `<div class="empty">No such agent in this session.</div>`;
    return;
  }
  const span = agentSpan(row);
  const stats = row.substantive
    ? `${card("Type", row.agent_type || "not recorded", true)}
       ${card("Records", num(row.entries))}
       ${card("Tool calls", num(row.tool_calls || 0))}
       ${card("Errors", num(row.errors))}
       ${card("Output tokens", num(row.out_tok || 0))}
       ${card("Span", span || "a single instant", true)}
       ${card("Started", stamp(row.first_ts) || "not recorded", true)}`
    : `${card("Records", num(row.entries))}
       ${card("Started", stamp(row.first_ts) || "not recorded", true)}`;

  const task = row.substantive
    ? `<div class="panel">
         <h2>Asked to</h2>
         <div id="agent-task" class="prose">${row.task_entry ? "loading"
           : `<span class="dim">Its transcript is not on disk, so the prompt it was given was not kept.</span>`}</div>
         ${row.task_entry ? `<button data-open="${row.task_entry}">Open this record</button>` : ""}
       </div>`
    : `<div class="panel">
         <h2>What this is</h2>
         <p class="rail-note">An interface agent: one of the short model calls
         behind the progress lines, the suggested replies and the session
         summaries. It declared no type, called no tool and kept no transcript.
         Its one message is below.</p>
       </div>`;

  target.innerHTML = `
    <div class="page-head">
      <button data-back>&larr; All subagents</button>
      <span class="spacer"></span>
      <div class="controls">
        ${row.substantive ? transcriptChip(row) : chip("interface agent")}
        ${row.quarantined ? chip("quarantined", "bad") : ""}</div>
    </div>
    <div class="cards">${stats}</div>
    ${task}
    ${row.summary ? `<div class="panel">
      <h2>${row.substantive ? "Ended saying" : "Its message"}</h2>
      <div id="agent-said" class="prose">${esc(row.summary)}</div>
      ${row.stop_entry ? `<button data-open="${row.stop_entry}">Open this record</button>` : ""}
    </div>` : ""}
    <div class="panel">
      <dl class="kv">
        <dt>agent</dt><dd>${esc(row.agent_id)}</dd>
        <dt>transcript</dt><dd>${esc(row.transcript_path || "never indexed")}</dd>
      </dl>
    </div>
    <div id="agent-rows"></div>`;

  target.querySelector("[data-back]").addEventListener("click", () => go({ agent: "" }));

  target.querySelectorAll("button[data-open]").forEach((b) => {
    b.addEventListener("click", () => select(b.dataset.open));
  });

  // The index keeps a clipped summary for list rendering; the panels show the
  // whole thing, read back from the file.
  if (row.task_entry) {
    const found = await api(`/api/entries/${row.task_entry}`);
    $("#agent-task").textContent = messageText(found.parsed) || row.task || "";
  }
  if (row.stop_entry) {
    const found = await api(`/api/entries/${row.stop_entry}`);
    const said = (found.parsed || {}).last_assistant_message;
    if (said) $("#agent-said").textContent = said;
  }

  await paginate($("#agent-rows"), {
    session: r.id, agent: row.agent_id, include_agents: 1, meta: r.q.meta,
    limit: PAGE,
  }, { dateToo: true });
}

// --- paged entry lists ----------------------------------------------------

async function paginate(target, params, options = {}) {
  const data = await api("/api/entries", params);
  const hidden = [
    params.meta ? "" : "meta records hidden",
    params.dated ? "records without a timestamp are under Session records" : "",
    params.include_agents ? "" : "subagent lanes hidden",
  ].filter(Boolean).join(", ");
  target.innerHTML = `<div class="page-head">
      <span class="sub">${num(data.total)} matching entries</span>
      ${hidden ? `<span class="sub dim">${esc(hidden)}</span>` : ""}</div>
    <div id="page-rows">${rowsBlock(data.rows, options)}</div>
    ${data.next_cursor ? `<button class="load-more" data-cursor="${data.next_cursor}">Load ${PAGE} more</button>` : ""}`;
  wireRows(target);
  const more = target.querySelector(".load-more");
  if (more) {
    more.addEventListener("click", async () => {
      more.disabled = true;
      more.textContent = "loading";
      const next = await api("/api/entries", { ...params, cursor: more.dataset.cursor });
      const holder = target.querySelector("#page-rows .rows") || target.querySelector("#page-rows");
      holder.insertAdjacentHTML("beforeend", next.rows.map((row) => entryRow(row, options)).join(""));
      state.rows = state.rows.concat(next.rows.map((row) => row.id));
      wireRows(target);
      if (next.next_cursor) {
        more.dataset.cursor = next.next_cursor;
        more.disabled = false;
        more.textContent = `Load ${PAGE} more`;
      } else {
        more.remove();
      }
    });
  }
}

function wireRows(scope) {
  scope.querySelectorAll(".row[data-entry]").forEach((el) => {
    if (el.dataset.wired) return;
    el.dataset.wired = "1";
    const open = () => select(el.dataset.entry);
    el.addEventListener("click", open);
    el.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
    });
  });
}

function select(entryId) {
  state.selected = entryId;
  document.querySelectorAll(".row.sel").forEach((el) => el.classList.remove("sel"));
  const el = document.querySelector(`.row[data-entry="${entryId}"]`);
  if (el) el.classList.add("sel");
  const r = route();
  history.replaceState(null, "", href(r.view, r.id, { ...r.q, e: entryId }));
  renderInspector(entryId);
}

// --- view: hooks ----------------------------------------------------------

async function viewHooks(r) {
  const data = await api("/api/hooks", {
    kind: r.q.kind, tool: r.q.tool, session: r.q.session,
    errors: r.q.errors, limit: PAGE, cursor: r.q.cursor,
  });
  const total = data.events.reduce((a, e) => a + e.n, 0);
  rail.innerHTML = railGroup("Hook events",
    [{ value: "", label: "All events", count: total }].concat(
      data.events.map((e) => ({ value: e.kind, label: e.kind, count: e.n }))),
    r.q.kind, "kind");

  const selected = data.events.find((e) => e.kind === r.q.kind);
  content.innerHTML = `<div class="page">
    <div class="page-head">
      <h1>${esc(r.q.kind || "Hook events")}</h1>
      <span class="sub">${num(data.total)} matching</span>
      <span class="spacer"></span>
      <div class="controls">
        <button data-toggle="errors" class="${r.q.errors ? "on" : ""}">Errors only</button>
        ${r.q.session ? `<button data-clear="session">session ${esc(r.q.session.slice(0, 8))} &times;</button>` : ""}
        ${r.q.tool ? `<button data-clear="tool">tool ${esc(r.q.tool)} &times;</button>` : ""}
      </div>
    </div>
    ${data.events.length ? "" : `<div class="panel">
      <h2>No hook events indexed</h2>
      <p class="rail-note">Claude Code can run a command of your choosing on each
      hook event, before a tool runs, after it returns, when a turn ends. This
      view reads whatever those commands wrote into the audit directory beside
      your transcripts.</p>
      <p class="rail-note">Nothing here means no such commands are set up, which
      is the normal case. Everything else in cclens works without them.</p>
    </div>`}
    ${selected ? `<div class="cards">
      ${card("Fired", num(selected.n))}
      ${card("Mean hook time", micros(selected.avg_us), true)}
      ${card("With an error", num(selected.errors))}
      ${card("Last fired", ago(selected.last_ts), true)}
    </div>` : ""}
    <div id="hook-rows"></div>
  </div>`;
  content.querySelector('button[data-toggle="errors"]')
    .addEventListener("click", () => go({ errors: r.q.errors ? "" : "1" }));
  content.querySelectorAll("button[data-clear]").forEach((b) => {
    b.addEventListener("click", () => go({ [b.dataset.clear]: "" }));
  });

  const target = $("#hook-rows");
  if (data.events.length) {
    target.innerHTML = rowsBlock(data.rows, { dateToo: true });
    wireRows(target);
  }
}

// --- view: search ---------------------------------------------------------

async function viewSearch(r) {
  rail.innerHTML = `<h3>Search</h3>
    <p class="rail-note">FTS5 over message text, thinking, tool inputs and tool
    output heads, capped per entry at index time. Statusline snapshots are not
    indexed for search.</p>
    <p class="rail-note">Operators work: <code>foo AND bar</code>,
    <code>"exact phrase"</code>, <code>pre*</code>, <code>NEAR(a b, 5)</code>.</p>`;

  if (!r.q.q) {
    content.innerHTML = `<div class="page"><div class="empty">Type a query above.</div></div>`;
    return;
  }
  const data = await api("/api/search", { q: r.q.q, stream: r.q.stream, limit: 120 });
  content.innerHTML = `<div class="page">
    <div class="page-head">
      <h1>Search</h1>
      <span class="sub">${data.error ? "" : `${num(data.total)} hits for ${esc(data.query)}`}</span>
      <span class="spacer"></span>
      <div class="controls">
        ${["", ...STREAMS].map((s) => `<button data-stream="${s}" class="${(r.q.stream || "") === s ? "on" : ""}">${s || "all streams"}</button>`).join("")}
      </div>
    </div>
    ${data.error ? `<div class="err">FTS rejected that query: ${esc(data.error)}</div>` : ""}
    <div id="search-rows">${data.error ? "" : rowsBlock(data.rows, { snippet: true, dateToo: true })}</div>
  </div>`;
  content.querySelectorAll("button[data-stream]").forEach((b) => {
    b.addEventListener("click", () => go({ stream: b.dataset.stream }));
  });
  wireRows($("#search-rows"));
}

// --- view: one tool call --------------------------------------------------

async function viewTool(r) {
  const data = await api(`/api/tool/${encodeURIComponent(r.id)}`);
  rail.innerHTML = `<h3>Tool call</h3>
    <p class="rail-note">Every record that mentions this tool_use_id, across the
    transcript, the hook audit and the state dumps.</p>`;
  const groups = Object.entries(data.streams);
  content.innerHTML = `<div class="page">
    <div class="page-head">
      <h1>${esc(data.tool_name || "tool call")}</h1>
      <span class="sub mono">${esc(data.tool_use_id)}</span>
    </div>
    <div class="cards">
      ${card("Records", num(data.count))}
      ${card("Tool time", millis(data.duration_ms) || "not recorded", true)}
      ${card("Permission", data.denied ? "denied" : "allowed", true)}
      ${card("Outcome", data.failed ? "failed" : "completed", true)}
      ${data.session_id ? card("Session", data.session_id.slice(0, 8), true) : ""}
    </div>
    ${data.session_id ? `<p><a href="${link("session", data.session_id, { tab: "timeline" })}">Open the session timeline</a></p>` : ""}
    ${groups.map(([stream, rows]) => `<div class="panel">
      <h2>${esc(stream)} &middot; ${rows.length}</h2>
      <div class="rows dated">${rows.map((row) => entryRow(row, { dateToo: true })).join("")}</div>
    </div>`).join("") || `<div class="empty">Nothing indexed for that id.</div>`}
  </div>`;
  state.rows = groups.flatMap(([, rows]) => rows.map((row) => row.id));
  wireRows(content);
}

// --- view: stats ----------------------------------------------------------

async function viewStats() {
  const data = await api("/api/stats");
  const t = data.totals || {};
  rail.innerHTML = railGroup("Projects", data.projects.map((p) => ({
    value: p.project, label: p.project, count: p.sessions,
  })), "", "project-stats");

  content.innerHTML = `<div class="page">
    <div class="page-head"><h1>Overview</h1>
      <span class="sub">${t.indexed_at ? `indexed ${ago(Number(t.indexed_at))}` : ""}</span></div>
    <div class="cards">
      ${card("Entries", num(t.entries))}
      ${card("Sessions", num(t.sessions))}
      ${card("Subagents", num(t.agents))}
      ${card("Source files", num(t.files))}
      ${card("Source bytes", bytes(t.source_bytes), true)}
      ${card("Oldest", stamp(t.first_ts), true)}
      ${card("Newest", stamp(t.last_ts), true)}
    </div>

    <div class="panel">
      <h2>Entries per day by source</h2>
      <div id="activity"></div>
    </div>

    <div class="panel">
      <h2>Hook time per event</h2>
      <p class="rail-note">How long each hook itself took to run, from the audit
      record's own elapsed measurement.</p>
      <div id="latency"></div>
    </div>

    <div class="panel">
      <h2>Tools</h2>
      <table class="grid">
      <colgroup><col><col class="w-num"><col class="w-num"><col class="w-num"><col class="w-num"></colgroup>
      <thead><tr><th>Tool</th><th class="num">Calls</th>
        <th class="num">Errors</th><th class="num">Mean</th><th class="num">Slowest</th></tr></thead>
      <tbody>${data.tools.map((row) => `<tr data-tool="${esc(row.tool_name)}">
        <td>${esc(row.tool_name)}</td><td class="num">${num(row.n)}</td>
        <td class="num ${row.errors ? "" : "dim"}">${row.errors ? num(row.errors) : ""}</td>
        <td class="num">${millis(row.avg_ms)}</td><td class="num">${millis(row.max_ms)}</td>
      </tr>`).join("")}</tbody></table>
    </div>

    <div class="panel">
      <h2>Models</h2>
      <table class="grid">
      <colgroup><col><col class="w-num"><col class="w-num"><col class="w-num"><col class="w-num"></colgroup>
      <thead><tr><th>Model</th><th class="num">Assistant turns</th>
        <th class="num">Input</th><th class="num">Output</th><th class="num">Cache read</th></tr></thead>
      <tbody>${data.models.map((row) => `<tr>
        <td class="mono">${esc(row.model)}</td><td class="num">${num(row.n)}</td>
        <td class="num">${num(row.in_tok)}</td><td class="num">${num(row.out_tok)}</td>
        <td class="num">${num(row.cache_r)}</td>
      </tr>`).join("")}</tbody></table>
    </div>

    <div class="panel">
      <h2>Record kinds</h2>
      <table class="grid">
      <colgroup><col class="w-kind"><col><col class="w-num"></colgroup>
      <thead><tr><th>Source</th><th>Kind</th><th class="num">Entries</th></tr></thead>
      <tbody>${data.kinds.map((row) => `<tr data-kind="${esc(row.kind)}">
        <td class="nowrap">${chip(row.stream, `stream-${row.stream}`)}</td>
        <td class="mono">${esc(row.kind)}</td><td class="num">${num(row.n)}</td>
      </tr>`).join("")}</tbody></table>
    </div>
  </div>`;

  drawActivity($("#activity"), data.activity);
  drawLatency($("#latency"), data.hook_latency);
  content.querySelectorAll("tr[data-tool]").forEach((tr) => {
    tr.addEventListener("click", () => { location.hash = href("hooks", "", { tool: tr.dataset.tool }); });
  });
  content.querySelectorAll("tr[data-kind]").forEach((tr) => {
    tr.addEventListener("click", () => { location.hash = href("hooks", "", { kind: tr.dataset.kind }); });
  });
}

// --- charts ---------------------------------------------------------------

let tip;

function showTip(event, html) {
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "chart-tip";
    document.body.appendChild(tip);
  }
  tip.innerHTML = html;
  tip.hidden = false;
  const box = tip.getBoundingClientRect();
  const x = Math.min(event.clientX + 12, window.innerWidth - box.width - 8);
  const y = Math.max(8, event.clientY - box.height - 12);
  tip.style.setProperty("left", `${x}px`);
  tip.style.setProperty("top", `${y}px`);
}

function hideTip() { if (tip) tip.hidden = true; }

function legend(items) {
  return `<div class="legend">${items.map((item) => `<span>
    <span class="sw" data-swatch="${esc(item.color)}"></span>${esc(item.label)}
    ${item.count === undefined ? "" : `<span class="dim">${num(item.count)}</span>`}</span>`).join("")}</div>`;
}

function paintSwatches(scope) {
  scope.querySelectorAll(".sw[data-swatch]").forEach((el) => {
    el.style.setProperty("background", el.dataset.swatch);
  });
}

function drawActivity(target, activity) {
  const byDay = new Map();
  for (const row of activity) {
    if (!row.day) continue;
    const bucket = byDay.get(row.day) || {};
    bucket[row.stream] = row.n;
    byDay.set(row.day, bucket);
  }
  const days = [...byDay.keys()].sort().slice(-90);
  if (!days.length) { target.innerHTML = `<div class="empty">No dated entries.</div>`; return; }

  const present = STREAMS.filter((s) => days.some((d) => byDay.get(d)[s]));
  const totals = days.map((d) => present.reduce((a, s) => a + (byDay.get(d)[s] || 0), 0));
  const peak = Math.max(...totals, 1);
  const W = 940, H = 190, PAD_L = 46, PAD_B = 22, PAD_T = 8;
  const plotW = W - PAD_L - 8, plotH = H - PAD_B - PAD_T;
  const slot = plotW / days.length;
  const barW = Math.max(2, Math.min(14, slot - 2));

  const ticks = [0, 0.5, 1].map((f) => Math.round(peak * f));
  const gridlines = ticks.map((v) => {
    const y = PAD_T + plotH - (v / peak) * plotH;
    return `<line class="gridline" x1="${PAD_L}" y1="${y}" x2="${W - 8}" y2="${y}"></line>
            <text class="axis" x="${PAD_L - 6}" y="${y + 3}" text-anchor="end">${num(v)}</text>`;
  }).join("");

  const bars = days.map((day, i) => {
    const counts = byDay.get(day);
    let y = PAD_T + plotH;
    const segments = present.map((stream, si) => {
      const n = counts[stream] || 0;
      if (!n) return "";
      const h = Math.max(1, (n / peak) * plotH - 2);
      y -= h + 2;
      return `<rect class="mark" x="${PAD_L + i * slot + (slot - barW) / 2}" y="${y}"
        width="${barW}" height="${h}" data-fill="var(--series-${si + 1})"></rect>`;
    }).join("");
    const label = `<div class="t">${esc(day)}</div>` + present.map((stream, si) =>
      `<div class="r"><span><span class="sw" data-swatch="var(--series-${si + 1})"></span>${stream}</span><b>${num(counts[stream] || 0)}</b></div>`).join("");
    return `<g data-tip="${esc(label)}">
      <rect x="${PAD_L + i * slot}" y="${PAD_T}" width="${slot}" height="${plotH}" fill="transparent"></rect>
      ${segments}</g>`;
  }).join("");

  const every = Math.ceil(days.length / 10);
  const axis = days.map((day, i) => (i % every === 0
    ? `<text class="axis" x="${PAD_L + i * slot + slot / 2}" y="${H - 6}" text-anchor="middle">${esc(day.slice(5))}</text>`
    : "")).join("");

  target.innerHTML = `${legend(present.map((s, i) => ({
    label: s, color: `var(--series-${i + 1})`,
    count: days.reduce((a, d) => a + (byDay.get(d)[s] || 0), 0),
  })))}
    <svg class="chart" viewBox="0 0 ${W} ${H}" role="img"
         aria-label="entries per day by source, last ${days.length} days">
      ${gridlines}${bars}${axis}
    </svg>
    <details><summary class="muted">Table of the same numbers</summary>
      <table class="grid"><thead><tr><th>Day</th>${present.map((s) => `<th class="nowrap">${esc(s)}</th>`).join("")}<th class="nowrap">Total</th></tr></thead>
      <tbody>${days.slice().reverse().map((day) => `<tr><td class="mono nowrap">${esc(day)}</td>
        ${present.map((s) => `<td class="num">${num(byDay.get(day)[s] || 0)}</td>`).join("")}
        <td class="num">${num(present.reduce((a, s) => a + (byDay.get(day)[s] || 0), 0))}</td></tr>`).join("")}
      </tbody></table></details>`;

  target.querySelectorAll("rect[data-fill]").forEach((el) => {
    el.style.setProperty("fill", el.dataset.fill);
  });
  target.querySelectorAll("g[data-tip]").forEach((g) => {
    g.addEventListener("mousemove", (event) => { showTip(event, g.dataset.tip); paintSwatches(tip); });
    g.addEventListener("mouseleave", hideTip);
  });
  paintSwatches(target);
}

function dominant(row) {
  let best = BUCKETS[0];
  for (const bucket of BUCKETS) {
    if ((row.counts[bucket] || 0) > (row.counts[best] || 0)) best = bucket;
  }
  const share = Math.round(((row.counts[best] || 0) / (row.total || 1)) * 100);
  return `${share}% ${best}`;
}

function drawLatency(target, rows) {
  const byKind = new Map();
  for (const row of rows) {
    const bucket = byKind.get(row.kind) || {};
    bucket[row.bucket] = row.n;
    byKind.set(row.kind, bucket);
  }
  const kinds = [...byKind.entries()]
    .map(([kind, counts]) => {
      const total = BUCKETS.reduce((a, b) => a + (counts[b] || 0), 0);
      const slow = (counts["1ms to 100ms"] || 0) + (counts["over 100ms"] || 0);
      return { kind, counts, total, slow };
    })
    .sort((a, b) => b.slow / (b.total || 1) - a.slow / (a.total || 1) || b.total - a.total);
  if (!kinds.length) { target.innerHTML = `<div class="empty">No hook timings indexed.</div>`; return; }

  target.innerHTML = `${legend(BUCKETS.map((b, i) => ({ label: b, color: `var(--ord-${i + 1})` })))}
    <table class="grid">
    <colgroup><col class="w-project"><col class="w-num"><col></colgroup>
    <thead><tr><th>Event</th><th class="num">Fired</th>
      <th>Distribution of the hook's own runtime</th></tr></thead>
    <tbody>${kinds.map((row) => `<tr data-kind="${esc(row.kind)}">
      <td class="mono nowrap">${esc(row.kind)}</td>
      <td class="num">${num(row.total)}</td>
      <td><div class="strip-row">
        <div class="strip">${BUCKETS.map((b, i) => {
          const n = row.counts[b] || 0;
          if (!n) return "";
          return `<span data-fill="var(--ord-${i + 1})" data-w="${(n / row.total) * 100}"
                   data-tip="${esc(`<div class='t'>${row.kind}</div><div class='r'><span>${b}</span><b>${num(n)}</b></div>`)}"></span>`;
        }).join("")}</div>
        <span class="strip-label">${esc(dominant(row))}</span>
        ${row.slow ? `<span class="strip-slow">${num(row.slow)} over 1ms</span>` : ""}
      </div></td></tr>`).join("")}
    </tbody></table>`;

  target.querySelectorAll(".strip span[data-fill]").forEach((el) => {
    el.style.setProperty("background", el.dataset.fill);
    el.style.setProperty("width", `${el.dataset.w}%`);
    el.addEventListener("mousemove", (event) => showTip(event, el.dataset.tip));
    el.addEventListener("mouseleave", hideTip);
  });
  target.querySelectorAll("tr[data-kind]").forEach((tr) => {
    tr.addEventListener("click", () => { location.hash = href("hooks", "", { kind: tr.dataset.kind }); });
  });
  paintSwatches(target);
}

// --- inspector ------------------------------------------------------------

const INSPECTOR_TABS = [["fields", "Fields"], ["json", "JSON"], ["raw", "Raw"], ["related", "Related"]];

async function renderInspector(entryId) {
  const r = route();
  const id = entryId || r.q.e;
  if (!id) {
    inspector.hidden = true;
    frame.classList.remove("with-inspector");
    return;
  }
  inspector.hidden = false;
  frame.classList.add("with-inspector");
  if (!state.entry || String(state.entry.entry.id) !== String(id)) {
    inspector.innerHTML = `<div class="insp-body muted">loading</div>`;
    try {
      state.entry = await api(`/api/entries/${encodeURIComponent(id)}`);
    } catch (err) {
      inspector.innerHTML = `<div class="insp-body"><div class="err">${esc(err.message)}</div></div>`;
      return;
    }
    state.expanded = new Set(["", "/message", "/attachment", "/tool_input"]);
    state.showAllStrings = new Set();
  }
  drawInspector();
}

function drawInspector() {
  const data = state.entry;
  const e = data.entry;
  const p = data.provenance;
  const tab = route().q.it || "fields";

  inspector.innerHTML = `
    <div class="insp-head">
      <div class="line1">
        ${chip(e.stream, `stream-${e.stream}`)}
        <h2>${esc(e.kind)}</h2>
        ${e.has_error ? chip("error", "bad") : ""}
        <button class="close" data-act="close" title="close (Esc)">&times;</button>
      </div>
      <div class="where">${esc(p.path)}<br>line ${num(p.line)} &middot; byte ${num(p.byte_offset)} &middot; ${bytes(p.byte_len)}
        ${p.missing ? " &middot; file gone from disk" : ""}</div>
    </div>
    <div class="insp-tabs">
      ${INSPECTOR_TABS.map(([v, l]) => `<button data-it="${v}" class="${tab === v ? "on" : ""}">${l}</button>`).join("")}
    </div>
    <div class="insp-body" id="insp-body"></div>`;

  inspector.querySelector('[data-act="close"]').addEventListener("click", closeInspector);
  inspector.querySelectorAll("button[data-it]").forEach((b) => {
    b.addEventListener("click", () => { go({ it: b.dataset.it }, true); });
  });

  const body = $("#insp-body");
  if (data.error && tab !== "fields") {
    body.innerHTML = `<div class="err">${esc(data.error)}</div>`;
    if (!data.raw) return;
  }
  if (tab === "fields") drawFields(body, data);
  else if (tab === "json") drawJson(body, data);
  else if (tab === "raw") drawRaw(body, data);
  else drawRelated(body, data);
}

function closeInspector() {
  const r = route();
  const q = { ...r.q };
  delete q.e;
  state.entry = null;
  history.replaceState(null, "", href(r.view, r.id, q));
  renderInspector();
}

const FIELD_LABELS = {
  ts: "timestamp", session_id: "session", agent_id: "agent", agent_type: "agent type",
  source_kind: "source", tool_use_id: "tool_use_id", in_tok: "input tokens",
  out_tok: "output tokens", cache_r: "cache read", cache_w: "cache write",
  duration_ms: "duration", elapsed_us: "hook time", is_sidechain: "sidechain",
  is_meta: "meta", has_error: "error", line_no: "line", byte_offset: "byte offset",
  byte_len: "byte length", file_id: "file",
};

function drawFields(target, data) {
  const e = data.entry;
  const rows = Object.entries(e)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => {
      let shown = v;
      if (k === "ts") shown = `${stamp(v)}  (${v})`;
      else if (k === "elapsed_us") shown = micros(v);
      else if (k === "duration_ms") shown = millis(v);
      else if (k === "cost") shown = money(v);
      else if (typeof v === "number" && k.endsWith("_tok")) shown = num(v);
      return `<dt>${esc(FIELD_LABELS[k] || k)}</dt><dd>${esc(shown)}</dd>`;
    }).join("");
  const links = [];
  if (e.session_id) links.push(`<a href="${link("session", e.session_id, { tab: "timeline" })}">session timeline</a>`);
  if (e.tool_use_id) links.push(`<a href="${link("tool", e.tool_use_id)}">this tool call</a>`);
  if (e.kind && e.stream === "hook") links.push(`<a href="${link("hooks", "", { kind: e.kind })}">all ${esc(e.kind)}</a>`);
  target.innerHTML = `${data.error ? `<div class="err">${esc(data.error)}</div>` : ""}
    <dl class="kv">${rows}</dl>
    ${links.length ? `<p class="muted">${links.join(" &middot; ")}</p>` : ""}
    <p><button data-copy-fields>Copy these fields as JSON</button></p>`;
  target.querySelector("[data-copy-fields]")
    .addEventListener("click", () => copy(JSON.stringify(e, null, 2)));
}

function typeOf(value) {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  return typeof value;
}

function getPath(root, path) {
  if (!path) return root;
  let node = root;
  for (const part of path.split("/").slice(1)) {
    const key = part.replace(/~1/g, "/").replace(/~0/g, "~");
    node = Array.isArray(node) ? node[Number(key)] : node?.[key];
  }
  return node;
}

function encodeSegment(key) {
  return String(key).replace(/~/g, "~0").replace(/\//g, "~1");
}

const STRING_PREVIEW = 600;

function renderNode(key, value, path, depth) {
  const kind = typeOf(value);
  const label = key === null ? "" : `<span class="key">${esc(key)}</span><span class="eq">:</span>`;
  const copyBtn = `<button class="copy" data-copy="${esc(path)}" title="copy this value">copy</button>`;
  const pathBtn = `<button class="copy" data-copy-path="${esc(path)}" title="copy the path">path</button>`;

  if (kind === "object" || kind === "array") {
    const keys = kind === "array" ? value.map((_, i) => i) : Object.keys(value);
    const open = state.expanded.has(path);
    const brace = kind === "array" ? ["[", "]"] : ["{", "}"];
    const head = `<div class="line">
      <button class="tw" data-toggle-path="${esc(path)}">${open ? "▾" : "▸"}</button>
      ${label}<span class="kindhint">${brace[0]}${keys.length}${brace[1]} ${esc(kind)}</span>
      ${pathBtn}${copyBtn}</div>`;
    if (!open || !keys.length) return head;
    const children = keys.map((k) =>
      renderNode(k, kind === "array" ? value[k] : value[k], `${path}/${encodeSegment(k)}`, depth + 1)).join("");
    return `${head}<div class="node">${children}</div>`;
  }

  let shown;
  if (kind === "string") {
    const long = value.length > STRING_PREVIEW && !state.showAllStrings.has(path);
    const text = long ? value.slice(0, STRING_PREVIEW) : value;
    shown = `<span class="s">${esc(text)}${long ? "…" : ""}</span>`
      + (value.length > STRING_PREVIEW
        ? ` <button class="tw big" data-show-all="${esc(path)}">${long ? `show all ${num(value.length)} chars` : "collapse"}</button>`
        : "");
  } else if (kind === "number") shown = `<span class="n">${esc(value)}</span>`;
  else if (kind === "boolean") shown = `<span class="b">${esc(value)}</span>`;
  else shown = `<span class="nul">null</span>`;

  return `<div class="line"><span class="tw"></span>${label}${shown}${pathBtn}${copyBtn}</div>`;
}

function drawJson(target, data) {
  if (!data.parsed) {
    target.innerHTML = `<div class="empty">No parsed body for this entry.</div>`;
    return;
  }
  target.innerHTML = `<div class="filter-row">
      <input type="search" id="json-filter" placeholder="filter keys and values">
      <button data-expand-all>Expand all</button>
      <button data-collapse-all>Collapse</button>
    </div>
    <div class="tree" id="tree"></div>`;

  const paint = () => {
    const needle = ($("#json-filter").value || "").toLowerCase();
    const source = needle ? filterTree(data.parsed, needle) : data.parsed;
    $("#tree").innerHTML = renderNode(null, source, "", 0);
  };
  state.expanded.add("");
  paint();

  target.querySelector("#json-filter").addEventListener("input", paint);
  target.querySelector("[data-expand-all]").addEventListener("click", () => {
    collectPaths(data.parsed, "", state.expanded, 0);
    paint();
  });
  target.querySelector("[data-collapse-all]").addEventListener("click", () => {
    state.expanded = new Set([""]);
    paint();
  });

  $("#tree").addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-toggle-path]");
    if (toggle) {
      const path = toggle.dataset.togglePath;
      if (state.expanded.has(path)) state.expanded.delete(path);
      else state.expanded.add(path);
      paint();
      return;
    }
    const showAll = event.target.closest("[data-show-all]");
    if (showAll) {
      const path = showAll.dataset.showAll;
      if (state.showAllStrings.has(path)) state.showAllStrings.delete(path);
      else state.showAllStrings.add(path);
      paint();
      return;
    }
    const copyValue = event.target.closest("[data-copy]");
    if (copyValue) {
      const value = getPath(data.parsed, copyValue.dataset.copy);
      copy(typeof value === "string" ? value : JSON.stringify(value, null, 2));
      return;
    }
    const copyPath = event.target.closest("[data-copy-path]");
    if (copyPath) copy(copyPath.dataset.copyPath || "/");
  });
}

function collectPaths(value, path, into, depth) {
  if (depth > 12) return;
  into.add(path);
  const kind = typeOf(value);
  if (kind === "object") {
    for (const key of Object.keys(value)) collectPaths(value[key], `${path}/${encodeSegment(key)}`, into, depth + 1);
  } else if (kind === "array") {
    value.forEach((item, i) => collectPaths(item, `${path}/${i}`, into, depth + 1));
  }
}

function filterTree(value, needle) {
  const kind = typeOf(value);
  if (kind === "object") {
    const out = {};
    for (const [key, inner] of Object.entries(value)) {
      if (key.toLowerCase().includes(needle)) { out[key] = inner; continue; }
      const kept = filterTree(inner, needle);
      if (kept !== undefined) out[key] = kept;
    }
    return Object.keys(out).length ? out : undefined;
  }
  if (kind === "array") {
    const out = value.map((item) => filterTree(item, needle)).filter((item) => item !== undefined);
    return out.length ? out : undefined;
  }
  return String(value).toLowerCase().includes(needle) ? value : undefined;
}

function drawRaw(target, data) {
  if (!data.raw) {
    target.innerHTML = `<div class="empty">The line could not be read from disk.</div>`;
    return;
  }
  target.innerHTML = `<p class="muted">The bytes on disk at that offset, unchanged.</p>
    <p><button data-copy-raw>Copy the raw line</button>
       <button data-copy-pretty>Copy it pretty printed</button></p>
    <pre class="raw">${esc(data.raw)}</pre>`;
  target.querySelector("[data-copy-raw]").addEventListener("click", () => copy(data.raw));
  target.querySelector("[data-copy-pretty]").addEventListener("click", () =>
    copy(data.parsed ? JSON.stringify(data.parsed, null, 2) : data.raw));
}

const RELATED_TITLES = {
  same_tool_call: "Same tool call",
  parent: "Parent record",
  children: "Records that answer to this one",
  agent: "Subagent",
};

function drawRelated(target, data) {
  const groups = Object.entries(data.related || {}).filter(([, rows]) => rows && rows.length);
  if (!groups.length) {
    target.innerHTML = `<div class="empty">Nothing else references this record.</div>`;
    return;
  }
  target.innerHTML = groups.map(([name, rows]) => `<div class="rel-group">
    <h3>${esc(RELATED_TITLES[name] || name)}</h3>
    ${rows.map((row) => (name === "agent"
      ? `<div class="rel-item"><span class="t">${esc(row.agent_type || "agent")} ${esc(row.agent_id)}</span>
           <span class="s">${esc(row.summary || "")}</span></div>`
      : `<button class="rel-item" data-entry="${row.id}">
           <span class="t">${esc(row.stream)} &middot; ${esc(row.kind)} &middot; ${esc(clock(row.ts))}</span>
           <span class="s">${esc(row.summary || "")}</span></button>`)).join("")}
  </div>`).join("");
  target.querySelectorAll("button[data-entry]").forEach((b) => {
    b.addEventListener("click", () => select(b.dataset.entry));
  });
}

// --- shell ----------------------------------------------------------------

const VIEWS = {
  sessions: viewSessions, session: viewSession, hooks: viewHooks,
  search: viewSearch, stats: viewStats, tool: viewTool,
};

async function render() {
  const r = route();
  nav.querySelectorAll("a").forEach((a) => {
    a.classList.toggle("active", a.dataset.route === r.view
      || (r.view === "session" && a.dataset.route === "sessions"));
  });
  omni.value = r.view === "search" ? (r.q.q || "") : "";
  const view = VIEWS[r.view] || viewSessions;
  state.selected = r.q.e;
  try {
    await view(r);
  } catch (err) {
    content.innerHTML = `<div class="page"><div class="err">${esc(err.message)}</div></div>`;
  }
  wireRail();
  await renderInspector();
}

function wireRail() {
  rail.querySelectorAll("button[data-rail]").forEach((b) => {
    b.addEventListener("click", () => {
      const key = b.dataset.rail;
      const value = b.dataset.value;
      if (key === "project-stats") { location.hash = href("sessions", "", { project: value }); return; }
      go({ [key]: value, cursor: "" });
    });
  });
}

async function freshness() {
  try {
    const info = await api("/api/stats");
    const t = info.totals || {};
    $("#freshness").textContent =
      `${num(t.entries)} entries  ${t.indexed_at ? `indexed ${ago(Number(t.indexed_at))}` : ""}`;
  } catch {
    $("#freshness").textContent = "no index";
  }
}

// --- theme -----------------------------------------------------------------

const THEMES = ["system", "dark", "light"];

function applyTheme(name) {
  if (name === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = name;
  const button = $("#theme");
  if (button) button.textContent = name;
}

function initTheme() {
  let stored = "system";
  try {
    stored = localStorage.getItem("cclens.theme") || "system";
  } catch {
    stored = "system";
  }
  if (!THEMES.includes(stored)) stored = "system";
  const button = document.createElement("button");
  button.id = "theme";
  button.className = "theme-btn";
  button.title = "theme: system, dark or light";
  $(".topbar").insertBefore(button, $("#freshness"));
  button.addEventListener("click", () => {
    const next = THEMES[(THEMES.indexOf(button.textContent) + 1) % THEMES.length];
    try {
      localStorage.setItem("cclens.theme", next);
    } catch {
      /* a browser with site data blocked still gets the theme for this page */
    }
    applyTheme(next);
  });
  applyTheme(stored);
}

$("#omni").addEventListener("submit", (event) => {
  event.preventDefault();
  location.hash = href("search", "", { q: omni.value });
});

document.addEventListener("keydown", (event) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName);
  if (event.key === "/" && !typing) { event.preventDefault(); omni.focus(); return; }
  if (event.key === "Escape") {
    if (typing) { event.target.blur(); return; }
    closeInspector();
    return;
  }
  if (typing || event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === "j" || event.key === "k") {
    const ids = state.rows;
    if (!ids.length) return;
    const at = ids.findIndex((id) => String(id) === String(state.selected));
    const next = event.key === "j" ? Math.min(ids.length - 1, at + 1) : Math.max(0, at - 1);
    select(ids[next]);
    const el = document.querySelector(".row.sel");
    if (el) el.scrollIntoView({ block: "nearest" });
  }
  const tabs = ["sessions", "hooks", "search", "stats"];
  if (/^[1-4]$/.test(event.key)) location.hash = href(tabs[Number(event.key) - 1]);
});

window.addEventListener("hashchange", render);
initTheme();
render();
freshness();
