// Cited admin dashboard. All data is inserted with textContent (never innerHTML),
// because questions, URLs and error messages come from untrusted users and feeds.
"use strict";

const KEY = "cited_admin_token";
const LEVEL_NAMES = { high: "High", medium: "Moderate", low: "Limited" };
const LEVEL_COLORS = { high: "#27ae60", medium: "#e67e22", low: "#e74c3c" };
let timer = null;

function getToken() { try { return sessionStorage.getItem(KEY) || ""; } catch { return ""; } }
function setToken(t) { try { t ? sessionStorage.setItem(KEY, t) : sessionStorage.removeItem(KEY); } catch {} }

function el(tag, attrs, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "style") n.style.cssText = v;
    else n.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}
const pill = (status, text) => el("span", { class: `pill s-${status}` }, text ?? status);

function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
const when = (ts) => el("span", { title: new Date(ts * 1000).toLocaleString() }, ago(ts));
const fmtUptime = (s) => s < 3600 ? `${Math.round(s / 60)} min` : s < 86400 ? `${(s / 3600).toFixed(1)} h` : `${(s / 86400).toFixed(1)} d`;

async function api(path, body) {
  const opts = { headers: { Authorization: `Bearer ${getToken()}` }, cache: "no-store" };
  if (body) { opts.method = "POST"; opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  if (r.status === 401 || r.status === 404) { throw Object.assign(new Error("auth"), { auth: true, status: r.status }); }
  if (r.status === 429) { throw Object.assign(new Error("Too many failed sign-in attempts. Wait 15 minutes."), { auth: true }); }
  if (!r.ok && r.status !== 503) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

// ── Rendering ───────────────────────────────────────────────────────────────

function renderOverview(o) {
  const h = o.health;
  document.getElementById("hdr-status").replaceChildren(pill(h.status));
  document.getElementById("status-pill").replaceChildren(pill(h.status, h.status.toUpperCase()));
  document.getElementById("status-summary").textContent = h.summary || "";
  document.getElementById("status-meta").textContent =
    `Version ${h.version}${h.commit ? ` (${h.commit})` : ""} · up ${fmtUptime(h.uptime_seconds)} · ` +
    `models: ${h.prewarm.state}${h.prewarm.error ? ` (${h.prewarm.error})` : ""}`;

  const st = o.searches, ev = o.events, sec = o.security || {}, fx = o.fixes || {}, af = o.autofix || {};
  const af24 = af.stats_24h || {};
  const errs24 = ((ev.by_level || {}).ERROR || 0) + ((ev.by_level || {}).CRITICAL || 0);
  const warns24 = (ev.by_level || {}).WARNING || 0;
  const mem = h.checks.memory || {};
  const rl = (ev.by_kind || {}).rate_limited || 0;
  const tiles = [
    ["Searches (24h)", st ? st.searches : "—", st ? `${st.levels.high} high · ${st.levels.medium} moderate` : "history off"],
    ["Limited coverage (24h)", st ? `${st.low_confidence_pct}%` : "—", st ? `${st.low_confidence} searches` : ""],
    ["Errors (24h)", errs24, `${warns24} warnings`],
    ["Open bugs", fx.enabled ? fx.open_errors : "—", "error types not marked fixed (30 d)"],
    ["Bugs fixed (30d)", fx.enabled ? fx.fixed : "—",
      fx.enabled ? `${fx.holding} holding · ${fx.regressed} regressed${fx.auto_fixed ? ` · ${fx.auto_fixed} auto` : ""}` : ""],
    ["Threats blocked (24h)", sec.enabled ? sec.total_24h : "—", sec.enabled ? `${sec.total_30d} in 30 days` : ""],
    ["Auto-fixes (24h)", af.enabled ? af24.applied : "off", af.enabled ? `${af24.worked} worked · ${af24.learned_fixes} remembered` : "set CITED_ADMIN_TOKEN"],
    ["Memory", mem.rss_mb != null ? `${Math.round(mem.rss_mb)} MB` : "—", mem.limit_mb ? `of ${Math.round(mem.limit_mb)} MB (${mem.used_pct}%)` : "no container limit found"],
    ["Uptime", fmtUptime(h.uptime_seconds), `since ${new Date(h.started_at).toLocaleString()}`],
  ];
  document.getElementById("tiles").replaceChildren(...tiles.map(([k, v, sub]) =>
    el("div", { class: "tile" }, el("div", { class: "k" }, k), el("div", { class: "v" }, v), el("div", { class: "sub" }, sub))));

  const rows = Object.entries(h.checks).map(([name, c]) => {
    const facts = Object.entries(c)
      .filter(([k]) => !["status", "label", "detail", "meaning", "action", "cached"].includes(k))
      .map(([k, v]) => `${k.replace(/_/g, " ")}: ${v ?? "—"}`).join(" · ");
    return el("tr", {},
      el("td", {}, c.label || name),
      el("td", {}, pill(c.status)),
      el("td", {},
        c.detail ? el("div", {}, c.detail) : null,
        facts ? el("div", { class: "muted" }, facts) : null,
        c.meaning ? el("div", { class: "explain" }, el("b", {}, "What it means: "), c.meaning) : null,
        c.action ? el("div", { class: "explain" }, el("b", {}, "What to do: "), c.action) : null));
  });
  document.querySelector("#checks tbody").replaceChildren(...rows);
  renderConfidence(st);
  renderThreats(sec);
  renderAutofix(af);
  renderDigest(o.digest || {});
  renderGrowth(o.growth);
  renderLibrary(o.library || {});
}

const LIB_STATE = { ready: "Ready", building: "Building…", refreshing: "Refreshing…", error: "Error",
                    stale: "Stale — last refresh failed" };

function renderLibrary(lib) {
  const panel = document.getElementById("library-panel");
  panel.hidden = !lib.enabled;
  if (!lib.enabled) return;
  const body = document.querySelector("#library tbody");
  body.replaceChildren(...(lib.sources.length ? lib.sources.map((s) => {
    const btn = el("button", { type: "button" }, "Refresh now");
    btn.disabled = s.state === "refreshing";
    btn.addEventListener("click", async () => {
      btn.disabled = true; btn.textContent = "Started…";
      try { await api("/admin/api/library/refresh", { key: s.key }); setTimeout(load, 1500); }
      catch (e) { btn.disabled = false; btn.textContent = "Refresh now"; alert(`Could not start: ${e.message}`); }
    });
    return el("tr", {},
      el("td", {}, el("b", {}, s.name), el("div", { class: "muted" }, s.url),
        s.error ? el("div", { style: "color:var(--bad);font-size:13px" }, s.error) : null),
      el("td", {}, pill(s.state, LIB_STATE[s.state] || s.state)),
      el("td", { class: "num" }, s.posts), el("td", { class: "num" }, s.chunks),
      el("td", {}, s.last_ok ? when(s.last_ok) : "never"),
      el("td", { class: "num" }, s.last_added),
      el("td", {}, btn));
  }) : [el("tr", {}, el("td", { colspan: 7, class: "empty" }, "No newsletters in library.json yet."))]));
  const pending = document.getElementById("library-pending");
  pending.replaceChildren(...(lib.pending.length ? [
    el("b", {}, "Needs a URL: "), lib.pending.map((p) => p.name).join(", "),
    " — add each newsletter's address to library.json and redeploy."] : []));
}

const SVG = "http://www.w3.org/2000/svg";
function svg(tag, attrs) {
  const n = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
  return n;
}
function niceMax(v) {
  if (v <= 4) return 4;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}
const fmtDay = (d) => new Date(d + "T00:00:00Z").toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });

function renderGrowth(g) {
  const panel = document.getElementById("growth-panel");
  panel.hidden = !g;
  if (!g) return;
  const w = g.week;
  const chg = (pct) => pct == null ? el("span", { class: "muted" }, "no prior data")
    : pct === 0 ? el("span", { class: "muted" }, "no change vs prior week")
    : el("span", { class: pct > 0 ? "chg-up" : "chg-down" }, `${pct > 0 ? "▲" : "▼"} ${Math.abs(pct)}% vs prior week`);
  document.getElementById("growth-tiles").replaceChildren(
    ...[["Searches (7 days)", w.searches, w.searches_change_pct, `${w.searches_prev} the week before`],
        ["Newsletters searched (7 days)", w.newsletters, w.newsletters_change_pct, `${w.newsletters_prev} the week before`],
        ["Avg daily clients (approx.)", g.clients_enabled ? w.avg_daily_clients : "—", w.clients_change_pct,
         `${w.avg_daily_clients_prev} the week before`]]
      .map(([k, v, pct, sub]) => el("div", { class: "tile" }, el("div", { class: "k" }, k),
        el("div", { class: "v" }, v), el("div", { class: "sub" }, chg(pct), " · ", sub))));

  // Bar chart: one series (searches/day), single hue, rounded data-ends, 2px gaps, recessive grid.
  const box = document.getElementById("growth-chart"), tip = document.getElementById("growth-tip");
  const days = g.days, W = Math.max(320, box.clientWidth || 800), H = 200;
  const padL = 34, padB = 22, padT = 8, plotW = W - padL, plotH = H - padB - padT;
  const max = niceMax(Math.max(...days.map((d) => d.searches), 1));
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none" });
  for (const f of [0, 0.5, 1]) {
    const y = padT + plotH * (1 - f);
    s.append(svg("line", { x1: padL, x2: W, y1: y, y2: y, stroke: "var(--grid)", "stroke-width": 1 }));
    const t = svg("text", { x: padL - 6, y: y + 4, "text-anchor": "end" }); t.textContent = Math.round(max * f); s.append(t);
  }
  const gap = 2, bw = (plotW - gap * (days.length - 1)) / days.length;
  days.forEach((d, i) => {
    const x = padL + i * (bw + gap), h = (d.searches / max) * plotH, y = padT + plotH - h, r = Math.min(4, bw / 2, h);
    const hit = svg("rect", { class: "hit", x, y: padT, width: bw + gap, height: plotH });
    const bar = svg("path", { class: "bar", d: h <= 0 ? "" :
      `M${x},${padT + plotH} V${y + r} Q${x},${y} ${x + r},${y} H${x + bw - r} Q${x + bw},${y} ${x + bw},${y + r} V${padT + plotH} Z` });
    hit.addEventListener("mousemove", (ev) => {
      tip.hidden = false;
      tip.replaceChildren(el("b", {}, fmtDay(d.day)), el("div", {}, `${d.searches} searches`),
        el("div", {}, `${d.newsletters} newsletters`), el("div", { class: "muted" }, `${d.clients} clients (approx.)`));
      tip.style.left = `${Math.min(ev.clientX + 12, window.innerWidth - 170)}px`;
      tip.style.top = `${ev.clientY + 12}px`;
      bar.classList.add("on");
    });
    hit.addEventListener("mouseleave", () => { tip.hidden = true; bar.classList.remove("on"); });
    s.append(hit, bar);
    if ((days.length - 1 - i) % 7 === 0) {
      const t = svg("text", { x: x + bw / 2, y: H - 6, "text-anchor": "middle" }); t.textContent = fmtDay(d.day); s.append(t);
    }
  });
  s.append(svg("line", { x1: padL, x2: W, y1: padT + plotH, y2: padT + plotH, stroke: "var(--muted)", "stroke-width": 1 }));
  box.replaceChildren(s);

  document.querySelector("#growth-table tbody").replaceChildren(...days.slice().reverse().map((d) => el("tr", {},
    el("td", {}, fmtDay(d.day)), el("td", { class: "num" }, d.searches),
    el("td", { class: "num" }, d.newsletters), el("td", { class: "num" }, d.clients))));
}

const RESULT_LABEL = { applied: "Applied", worked: "Worked", failed: "Didn't work", skipped: "Skipped" };

function renderAutofix(af) {
  const hint = document.getElementById("af-hint"), sum = document.getElementById("af-summary");
  const pb = document.querySelector("#playbook tbody"), rec = document.querySelector("#af-recent tbody");
  if (!af.enabled) {
    hint.textContent = "Off";
    sum.replaceChildren(el("div", { class: "empty" }, "The auto-fix agent is off (needs CITED_ADMIN_TOKEN; CITED_AUTOHEAL=0 disables it)."));
    pb.replaceChildren(); rec.replaceChildren(); return;
  }
  hint.textContent = `Safe fixes only — never changes code. Last check ${af.last_cycle ? ago(af.last_cycle) : "pending"}.`;
  const s = af.stats_24h || {}, s30 = af.stats_30d || {};
  sum.replaceChildren(...[
    el("div", {}, `Last 24 h: ${s.applied} applied · ${s.worked} worked · ${s.failed} didn't work · ${s.skipped} skipped. `,
       `Last 30 days: ${s30.applied} applied, ${s30.worked} worked.`),
    af.paused_sources.length ? el("div", { class: "muted" }, "Paused feeds: ",
      af.paused_sources.map((p) => `${p.host} (${Math.max(1, Math.round(p.seconds_left / 60))} min left)`).join(", ")) : null,
    af.blocked_clients ? el("div", { class: "muted" }, `${af.blocked_clients} client(s) temporarily blocked.`) : null,
    af.pending.length ? el("div", { class: "muted" }, `Checking whether ${af.pending.length} fix(es) worked…`) : null,
  ].filter(Boolean));
  pb.replaceChildren(...(af.playbook.length ? af.playbook.map((p) => {
    const forget = el("button", { type: "button" }, "Forget");
    forget.addEventListener("click", async () => {
      if (!confirm("Forget this fix? The agent will diagnose this error from scratch next time.")) return;
      await api("/admin/api/forget", { fingerprint: p.fingerprint }); await load();
    });
    return el("tr", {},
      el("td", { class: "q" }, p.message || p.fingerprint),
      el("td", {}, p.action_name, p.trusted ? "" : el("div", { class: "muted" }, "not trusted yet")),
      el("td", { class: "num" }, p.successes), el("td", { class: "num" }, p.failures),
      el("td", {}, p.last_applied ? when(p.last_applied) : "—"),
      el("td", {}, forget));
  }) : [el("tr", {}, el("td", { colspan: 6, class: "empty" }, "No fixes learned yet."))]));
  rec.replaceChildren(...(af.recent.length ? af.recent.map((a) => el("tr", {},
    el("td", {}, when(a.ts)),
    el("td", {}, a.action_name, a.from_playbook ? el("div", { class: "muted" }, "from memory") : null),
    el("td", { class: "q" }, a.problem, a.detail ? el("div", { class: "muted" }, a.detail) : null),
    el("td", {}, el("span", { class: `pill r-${a.result}` }, RESULT_LABEL[a.result] || a.result))))
    : [el("tr", {}, el("td", { colspan: 4, class: "empty" }, "No actions yet."))]));
}

function renderDigest(d) {
  document.getElementById("digest-status").textContent = d.enabled
    ? `On — sent daily around ${String(d.hour_utc).padStart(2, "0")}:00 UTC via ${d.provider === "resend" ? "Resend" : "SMTP"}.`
    : "Off — set CITED_DIGEST_TO plus RESEND_API_KEY (or SMTP_HOST/SMTP_USER/SMTP_PASSWORD) on the server.";
  document.getElementById("digest-test").disabled = !d.enabled;
}

function renderThreats(sec) {
  const body = document.querySelector("#threats tbody");
  if (!sec.enabled) {
    body.replaceChildren(el("tr", {}, el("td", { colspan: 5, class: "empty" }, "Event logging is off."))); return;
  }
  body.replaceChildren(...sec.types.map((t) => el("tr", {},
    el("td", {}, el("b", {}, t.name)),
    el("td", { class: "muted" }, t.description),
    ...["24h", "7d", "30d"].map((k) => el("td", { class: "num", style: t[k] ? "font-weight:700" : "" }, t[k])))),
    el("tr", {}, el("td", {}, el("b", {}, "Total")), el("td", {}),
      ...["24h", "7d", "30d"].map((k) => el("td", { class: "num" }, el("b", {}, sec[`total_${k}`])))));
}

function renderConfidence(st) {
  const bar = document.getElementById("conf-bar"), legend = document.getElementById("conf-legend");
  const body = document.querySelector("#sources tbody");
  if (!st) {
    document.getElementById("conf-hint").textContent = "Search history is off (set CITED_ADMIN_TOKEN to enable).";
    bar.replaceChildren(); legend.replaceChildren(); body.replaceChildren(); return;
  }
  document.getElementById("conf-hint").textContent =
    `Last ${st.hours} h · limits from ThinkWithMe: High = best ≥ 62% and avg ≥ 59%; Moderate = best ≥ 55%; Limited = lower`;
  const total = st.searches || 0;
  bar.replaceChildren(...["high", "medium", "low"].map((lv) =>
    el("div", { style: `width:${total ? (st.levels[lv] / total) * 100 : 0}%;background:${LEVEL_COLORS[lv]}`,
                title: `${LEVEL_NAMES[lv]}: ${st.levels[lv]}` })));
  legend.replaceChildren(...["high", "medium", "low"].map((lv) =>
    el("span", {}, el("i", { style: `background:${LEVEL_COLORS[lv]}` }),
       `${LEVEL_NAMES[lv]}: ${st.levels[lv]}${total ? ` (${Math.round(st.levels[lv] / total * 100)}%)` : ""}`)),
    el("span", { class: "muted" }, `Average best match: ${st.avg_confidence != null ? Math.round(st.avg_confidence * 100) + "%" : "—"}`));
  body.replaceChildren(...(st.by_source.length ? st.by_source.map((s) => el("tr", {},
    el("td", {}, s.label),
    el("td", { class: "num" }, s.searches),
    el("td", { class: "num" }, s.avg_confidence != null ? `${Math.round(s.avg_confidence * 100)}%` : "—"),
    el("td", { class: "num" }, s.levels.high), el("td", { class: "num" }, s.levels.medium),
    el("td", { class: "num", style: s.levels.low ? "color:var(--bad);font-weight:700" : "" }, s.levels.low)))
    : [el("tr", {}, el("td", { colspan: 6, class: "empty" }, "No searches yet."))]));
}

function renderSearches(rows) {
  const body = document.querySelector("#searches tbody");
  if (!rows.length) {
    body.replaceChildren(el("tr", {}, el("td", { colspan: 4, class: "empty" }, "Nothing here yet."))); return;
  }
  body.replaceChildren(...rows.map((r) => el("tr", {},
    el("td", {}, when(r.ts)),
    el("td", {}, r.label),
    el("td", { class: "q" }, r.question),
    el("td", {}, r.level ? pill(r.level, `${LEVEL_NAMES[r.level]} · ${Math.round((r.confidence || 0) * 100)}%`)
                          : el("span", { class: "muted" }, `${Math.round((r.confidence || 0) * 100)}%`)))));
}

const STATE_LABEL = { open: "Open", fixed: "Fixed", regressed: "Regressed" };

function renderErrors(data) {
  const box = document.getElementById("errors");
  if (!data.enabled) { box.replaceChildren(el("div", { class: "empty" }, "Error logging is off (set CITED_ADMIN_TOKEN on the server).")); return; }
  const wanted = document.getElementById("err-state").value;
  const groups = data.groups.filter((g) => !wanted || g.state === wanted);
  if (!groups.length) { box.replaceChildren(el("div", { class: "empty" }, "Nothing in this period. 🎉")); return; }
  box.replaceChildren(...groups.map((g) => {
    const ctx = Object.entries(g.context || {}).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join("\n");
    const isBug = g.category === "error";
    const actions = [];
    if (isBug && g.state !== "fixed") {
      const input = el("input", { type: "text", maxlength: "500", placeholder: "What fixed it? (e.g. 'Bumped feed timeout in v1.0.1')" });
      const btn = el("button", { class: "primary", type: "button" }, g.state === "regressed" ? "Mark fixed again" : "Mark fixed");
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try { await api("/admin/api/resolve", { fingerprint: g.fingerprint, note: input.value }); await load(); }
        catch (e) { btn.disabled = false; alert(`Could not save: ${e.message}`); }
      });
      actions.push(el("div", { class: "fix-form" }, input, btn));
    }
    if (g.resolved_at) {
      const reopen = el("button", { type: "button" }, "Reopen");
      reopen.addEventListener("click", async () => { await api("/admin/api/reopen", { fingerprint: g.fingerprint }); await load(); });
      actions.push(el("div", { class: "note" },
        el("b", {}, g.state === "regressed" ? "Regressed — " : "Fixed — "),
        `marked ${g.resolved_by === "auto" ? "by the auto-fix agent" : "by admin"} ${ago(g.resolved_at)}`,
        g.regressed_at ? `, happened again ${ago(g.regressed_at)}` : "",
        g.note ? el("div", {}, `Note: ${g.note}`) : null,
        el("div", { style: "margin-top:6px" }, reopen)));
    }
    return el("details", {},
      el("summary", {},
        isBug ? el("span", { class: `pill st-${g.state}` }, STATE_LABEL[g.state]) : pill(g.level),
        el("span", { style: "font-weight:700" }, g.name),
        el("span", { class: "msg" }, g.message),
        el("span", { class: "muted" }, `×${g.count} · last `, when(g.last_seen))),
      el("div", { class: "muted", style: "margin-top:8px" },
        `${g.description} · severity ${g.level} · source ${g.source || "—"} · first seen ${new Date(g.first_seen * 1000).toLocaleString()}`),
      ctx ? el("pre", {}, ctx) : null,
      g.detail ? el("pre", {}, g.detail) : null,
      ...actions);
  }));
}

// ── Loading ─────────────────────────────────────────────────────────────────

async function load() {
  const searchLevel = document.getElementById("search-filter").value;
  const category = document.getElementById("err-category").value;
  const hours = document.getElementById("err-hours").value;
  try {
    const [overview, searches, errors] = await Promise.all([
      api("/admin/api/overview"),
      api(`/admin/api/searches?limit=50${searchLevel ? `&level=${searchLevel}` : ""}`),
      api(`/admin/api/errors?limit=300&hours=${hours}${category ? `&category=${category}` : ""}`),
    ]);
    showApp();
    renderOverview(overview);
    renderSearches(searches.searches || []);
    renderErrors(errors);
    document.getElementById("refreshed").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    if (e.auth) { signOut(e.message !== "auth" ? e.message : (getToken() ? "That token didn't work." : "")); return; }
    document.getElementById("refreshed").textContent = `Refresh failed: ${e.message}`;
  }
}

function showApp() {
  document.getElementById("login").hidden = true;
  document.getElementById("app").hidden = false;
}

function signOut(message) {
  setToken("");
  clearInterval(timer); timer = null;
  document.getElementById("app").hidden = true;
  document.getElementById("login").hidden = false;
  document.getElementById("login-error").textContent = message || "";
  document.getElementById("hdr-status").replaceChildren();
}

function schedule() {
  clearInterval(timer);
  if (document.getElementById("auto").checked) timer = setInterval(load, 30000);
}

document.getElementById("login-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  setToken(document.getElementById("token").value.trim());
  document.getElementById("token").value = "";
  load().then(schedule);
});
document.getElementById("signout").addEventListener("click", () => signOut());
document.getElementById("refresh").addEventListener("click", load);
document.getElementById("auto").addEventListener("change", schedule);
document.getElementById("digest-test").addEventListener("click", async () => {
  const out = document.getElementById("digest-result");
  out.textContent = "Sending…";
  try {
    const r = await fetch("/admin/api/digest/test", { method: "POST", headers: {
      Authorization: `Bearer ${getToken()}`, "Content-Type": "application/json" }, body: "{}" });
    const body = await r.json();
    out.textContent = r.ok ? `Sent (${body.result}).` : body.error;
  } catch (e) { out.textContent = `Failed: ${e.message}`; }
});
for (const id of ["search-filter", "err-category", "err-state", "err-hours"]) document.getElementById(id).addEventListener("change", load);

if (getToken()) { load().then(schedule); } else { signOut(); }
