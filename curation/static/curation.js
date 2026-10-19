/* CardioAI Curation Workbench — vanilla JS SPA, relative URLs (works behind a workspace proxy). */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const TOKEN = "cardioai.curation.token";
  let ME = null, STATUS = null, QUEUE = null;

  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pct = (x, d = 1) => (x == null ? "—" : (x * 100).toFixed(d) + "%");
  const num = (x, d = 2) => (x == null ? "—" : Number(x).toFixed(d));
  const chip = (s) => `<span class="chip ${esc(s)}">${esc(s)}</span>`;
  const ROLE = { admin: "Admin", data_scientist: "Data scientist", cardiologist: "Cardiologist", lead_cardiologist: "Lead cardiologist" };
  const FAM = { ami: "Acute MI", stroke: "Stroke / TIA", hf: "Heart failure", scd: "Sudden cardiac arrest", af: "Atrial fibrillation", imaging: "Imaging report" };

  function toast(msg, err = false) {
    const t = $("#toast");
    t.textContent = msg; t.className = "toast" + (err ? " err" : "");
    clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), 4500);
  }

  async function api(path, opts = {}) {
    const headers = { "Content-Type": "application/json" };
    const tok = sessionStorage.getItem(TOKEN);
    if (tok) headers.Authorization = "Bearer " + tok;
    const res = await fetch("api/" + path, { ...opts, headers });
    let body = null; try { body = await res.json(); } catch (_) {}
    if (res.status === 401 && path !== "auth/login") { logout(); throw new Error("Session expired"); }
    if (!res.ok) throw new Error((body && (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail))) || "Request failed");
    return body;
  }
  const post = (p, b) => api(p, { method: "POST", body: JSON.stringify(b || {}) });

  // ------------------------------------------------------------ auth / shell
  async function login(e) {
    e.preventDefault();
    try {
      const r = await post("auth/login", { username: $("#username").value.trim(), password: $("#password").value });
      sessionStorage.setItem(TOKEN, r.token); await boot();
    } catch (ex) { const m = $("#error-message"); m.textContent = ex.message; m.classList.add("show"); $("#password").value = ""; }
  }
  function logout() { sessionStorage.removeItem(TOKEN); $("#app").classList.add("hidden"); $("#login-screen").classList.remove("hidden"); }

  async function boot() {
    ME = await api("me");
    $("#login-screen").classList.add("hidden"); $("#app").classList.remove("hidden");
    $("#who").textContent = `${ME.full_name || ME.username} · ${ROLE[ME.role]}`;
    await refresh(); route();
  }
  async function refresh() {
    STATUS = await api("status");
    QUEUE = await api("queue");
    const s = STATUS.run && STATUS.run.summary;
    const b = $("#sim-banner");
    if (s && (s.synthetic || s.simulated_adjudication)) {
      b.innerHTML = `<strong>TEST DATA.</strong> ${s.synthetic ? "Synthetic patients. " : ""}${s.simulated_adjudication ? "Adjudication on this run was SIMULATED, not performed by cardiologists. " : ""}Not for clinical use.`;
      b.classList.remove("hidden");
    } else b.classList.add("hidden");
    nav();
  }
  function nav() {
    const can = (...r) => r.includes(ME.role);
    const links = [["pipeline", "Pipeline"]];
    if (can("data_scientist", "admin", "lead_cardiologist")) links.push(["audit", "Audit review"]);
    if (can("cardiologist", "lead_cardiologist")) links.push(["queue", "My cases", QUEUE.open.length]);
    if (can("lead_cardiologist")) links.push(["disagreements", "Disagreements", QUEUE.disagreements.length]);
    links.push(["metrics", "Agreement & accuracy"]);
    if (can("admin", "data_scientist", "lead_cardiologist")) links.push(["log", "Audit trail"]);
    if (can("admin")) links.push(["users", "Users"]);
    const cur = (location.hash.slice(2) || "pipeline").split("/")[0];
    $("#nav").innerHTML = links.map(([k, l, n]) => `<a href="#/${k}" class="${cur === k ? "active" : ""}">${l}${n ? `<span class="count">${n}</span>` : ""}</a>`).join("");
  }
  async function route() {
    const [view, arg] = (location.hash.slice(2) || "pipeline").split("/");
    nav();
    const v = $("#view");
    v.innerHTML = `<div class="loading active"><div class="spinner"></div></div>`;
    try {
      if (!STATUS.run && view !== "users" && view !== "log") return (v.innerHTML = `<div class="card empty">No label run yet. In the workspace run <span class="mono">python -m curation.pipeline label</span> then <span class="mono">audit</span>.</div>`);
      await ({ pipeline: viewPipeline, audit: viewAudit, queue: viewQueue, disagreements: viewDisagreements, case: viewCase,
         metrics: viewMetrics, log: viewLog, users: viewUsers }[view] || viewPipeline)(v, arg);
    } catch (ex) { v.innerHTML = `<div class="card empty">${esc(ex.message)}</div>`; }
  }
  window.addEventListener("hashchange", () => refresh().then(route).catch((e) => toast(e.message, true)));

  // ------------------------------------------------------------ pipeline
  function viewPipeline(v) {
    const S = STATUS, run = S.run, fams = run.summary.families || {};
    const m = S.metrics, ap = S.approvals;
    const famRows = Object.entries(fams).map(([f, x]) => {
      const a = run.summary.relevant_auto?.[f] || x.auto || {}, src = x.source || {};
      const rel = run.summary.relevant?.[f] || 0;
      return `<tr><td>${FAM[f] || f}</td><td class="num">${(x.n || 0).toLocaleString()}</td><td class="num">${(x.n - rel).toLocaleString()}</td><td class="num">${rel.toLocaleString()}</td>
        <td class="num">${(a.confirmed || 0).toLocaleString()}</td><td class="num">${(a.uncertain || 0).toLocaleString()}</td><td class="num">${(a.rejected || 0).toLocaleString()}</td>
        <td class="num">${(src.evidence_only || 0).toLocaleString()}</td></tr>`;
    }).join("");
    const isDS = ME.role === "data_scientist";
    const auditOk = S.gates.find((g) => g.id === "audit")?.status === "pass";
    const dsReview = ap.ds_dataset_review
      ? `${chip(ap.ds_dataset_review.decision === "approve" ? "pass" : "fail")} ${esc(ap.ds_dataset_review.decision)} by <b>${esc(ap.ds_dataset_review.user)}</b> · ${esc(ap.ds_dataset_review.comment)}`
      : isDS ? `<p class="muted">${auditOk ? "All audit items are resolved. Approving creates the adjudication sample and assigns readers." : "Resolve every audit item first (Audit review)."}</p>
        <textarea id="dsr-comment" placeholder="Review notes: code sets checked, cohort definition, warnings considered…"></textarea>
        <div class="row-actions"><button class="btn-sm btn-ok" id="dsr-approve" ${auditOk ? "" : "disabled"}>Approve dataset → start adjudication</button>
        <button class="btn-sm btn-bad" id="dsr-reject">Reject — needs rework</button></div>`
      : `<p class="muted">Awaiting data scientist review.</p>`;
    const adjRows = Object.entries(m.families).filter(([, x]) => x.cases).map(([f, x]) =>
      `<tr><td>${FAM[f]}</td><td class="num">${x.final}/${x.cases}</td><td><div class="progress"><div style="width:${(100 * x.final) / x.cases}%"></div></div></td>
       <td class="num">${x.disagreement}</td><td class="num">${num(x.kappa)}</td></tr>`).join("");
    const ann = m.annotation;
    const gateRows = S.gates.map((g) => {
      const canWaive = g.status === "fail" && g.waivable_by === ME.role;
      return `<tr><td>${chip(g.status)}</td><td>${esc(g.title)}</td><td class="muted">${esc(g.detail)}</td>
        <td>${canWaive ? `<button class="btn-sm btn-warn" data-waive="${esc(g.id)}">Waive…</button>` : ""}</td></tr>`;
    }).join("");
    const signBtns = [];
    if (S.ready_for_signoff && ME.role === "data_scientist" && !ap.ds_release) signBtns.push(`<button class="btn-sm btn-ok" data-sign="ds_release">Sign off as data scientist</button>`);
    if (S.ready_for_signoff && ME.role === "lead_cardiologist" && !ap.cardiology_release) signBtns.push(`<button class="btn-sm btn-ok" data-sign="cardiology_release">Sign off as lead cardiologist</button>`);
    if (S.releasable && ME.role === "data_scientist") signBtns.push(`<button class="btn-sm btn-blue" id="build-release">Build locked training release</button>`);
    const rels = S.releases.map((r) => `<div class="card" style="background:var(--bg-tertiary)"><div class="kv"><span><b>${esc(r.id)}</b></span><span>by ${esc(r.created_by)}</span><span>${esc(r.created_at.slice(0, 19))}</span>
        <span>${(r.n_patients || 0).toLocaleString()} patients</span>${r.simulated_adjudication ? chip("warn") + " simulated adjudication" : ""}</div>
        <p class="muted" style="margin:.5rem 0">dataset sha256 <span class="mono">${esc(r.dataset_sha)}</span></p>
        <div class="cmd">python -m training.train --release ${esc(r.path)} --out bundles/${esc(r.id)}</div></div>`).join("");

    v.innerHTML = `<h1>Labelling pipeline</h1><p class="lede">Label run <span class="mono">${esc(run.id)}</span> · label model ${esc(run.label_model)} · ${(run.summary.n_patients || 0).toLocaleString()} patients · ${(run.summary.n_candidates || 0).toLocaleString()} candidate events</p>
    <div class="stages">
      <div class="card"><h2><span class="stage-num">1</span>Automatic labelling &amp; annotation ${chip("pass")}</h2>
        <p class="muted">Diagnosis codes become candidate events. Labelling functions score the evidence (troponin kinetics, natriuretic peptides, imaging, resuscitation, report text with negation). Candidates after a patient's first auto-confirmed event cannot change a training label; the counts below are for label-relevant candidates.</p>
        <div class="tscroll"><table class="t"><thead><tr><th>Outcome</th><th class="num">Candidates</th><th class="num">After first event</th><th class="num">Label-relevant</th><th class="num">↳ Auto-confirmed</th><th class="num">↳ Uncertain</th><th class="num">↳ Auto-rejected</th><th class="num">Evidence-only</th></tr></thead><tbody>${famRows}</tbody></table></div></div>
      <div class="card"><h2><span class="stage-num">2</span>Automatic audit ${S.audit ? chip(S.audit.overall) : chip("pending")}</h2>
        ${S.audit ? `<div class="kv"><span><b>${S.audit.counts.pass}</b> pass</span><span><b>${S.audit.counts.warn}</b> warnings</span><span><b>${S.audit.counts.fail}</b> failures</span><span class="muted">${esc(S.audit.id)}</span></div>
        <div class="row-actions"><a href="#/audit"><button class="btn-sm btn-secondary">Open audit review</button></a></div>` : `<p class="muted">Run <span class="mono">python -m curation.pipeline audit</span>.</p>`}</div>
      <div class="card"><h2><span class="stage-num">3</span>Data scientist review</h2>${dsReview}</div>
      <div class="card"><h2><span class="stage-num">4</span>Cardiologist adjudication ${chip(m.totals.cases ? (m.totals.open + m.totals.disagreement ? "pending" : "pass") : "pending")}</h2>
        ${m.totals.cases ? `<p class="muted">Blinded double reads; disagreements go to a lead cardiologist who was not a reader. ${m.totals.final}/${m.totals.cases} cases final.</p>
        <div class="tscroll"><table class="t"><thead><tr><th>Outcome</th><th class="num">Final</th><th></th><th class="num">Awaiting lead</th><th class="num">κ</th></tr></thead><tbody>${adjRows}
        <tr><td>Imaging annotation (LVEF/CAC)</td><td class="num">${ann.final}/${ann.cases}</td><td><div class="progress"><div style="width:${ann.cases ? (100 * ann.final) / ann.cases : 0}%"></div></div></td><td></td><td></td></tr></tbody></table></div>` : `<p class="muted">The sample is created when the data scientist approves the dataset.</p>`}</div>
      <div class="card"><h2><span class="stage-num">5</span>Release gates ${chip(S.releasable ? "pass" : "pending")}</h2>
        <p class="muted">Training on Mayo data requires a release. Every gate must pass, or be waived with a written justification by the accountable role.</p>
        <div class="tscroll"><table class="t"><tbody>${gateRows}</tbody></table></div>
        <div class="row-actions" style="margin-top:1rem">${signBtns.join("")}</div>${rels ? `<h3>Releases</h3>${rels}` : ""}</div>
    </div>`;

    const dsrA = $("#dsr-approve"), dsrR = $("#dsr-reject");
    if (dsrA) dsrA.onclick = () => act(() => post("approve", { stage: "ds_dataset_review", decision: "approve", comment: $("#dsr-comment").value }), "Dataset approved — adjudication sample created");
    if (dsrR) dsrR.onclick = () => act(() => post("approve", { stage: "ds_dataset_review", decision: "reject", comment: $("#dsr-comment").value }), "Rejected");
    v.querySelectorAll("[data-waive]").forEach((b) => (b.onclick = () => {
      const j = prompt("Justification for waiving this gate (recorded in the audit trail, ≥20 characters):");
      if (j) act(() => post("gates/waive", { gate_id: b.dataset.waive, justification: j }), "Waiver recorded");
    }));
    v.querySelectorAll("[data-sign]").forEach((b) => (b.onclick = () => {
      const c = prompt("Sign-off comment:") ?? null;
      if (c !== null) act(() => post("approve", { stage: b.dataset.sign, decision: "approve", comment: c }), "Signed off");
    }));
    const br = $("#build-release");
    if (br) br.onclick = () => act(() => post("release"), "Release built");
  }

  async function act(fn, ok) {
    try { await fn(); toast(ok); await refresh(); route(); } catch (ex) { toast(ex.message, true); }
  }

  // ------------------------------------------------------------ audit review
  async function viewAudit(v) {
    const A = await api("audit");
    if (!A.report) return (v.innerHTML = `<div class="card empty">No audit yet.</div>`);
    const isDS = ME.role === "data_scientist" && !A.locked;
    const rank = (i) => (i.resolved ? 2 : 0) + (i.status === "fail" ? 0 : i.status === "warn" ? 1 : 3);
    const items = [...A.items].sort((a, b) => rank(a) - rank(b));
    const byCat = {};
    items.forEach((i) => (byCat[i.category] = byCat[i.category] || []).push(i));
    const tbl = (rows) => {
      if (!rows || !rows.length) return "";
      const cols = Object.keys(rows[0]);
      return `<details><summary>Details (${rows.length})</summary><div class="tscroll"><table class="t"><thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
        <tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td>${esc(r[c])}</td>`).join("")}</tr>`).join("")}</tbody></table></div></details>`;
    };
    const item = (i) => {
      const rv = i.review ? `<div class="muted">↳ ${esc(i.review.action)}${i.review.justification ? `: “${esc(i.review.justification)}”` : ""}</div>` : "";
      const acts = !isDS || i.status === "pass" ? "" : `<div class="row-actions">
        ${i.status === "warn" && !i.review ? `<button class="btn-sm btn-ok" data-rv="${i.id}" data-a="acknowledge">Acknowledge</button>` : ""}
        ${!i.review ? `<button class="btn-sm btn-warn" data-rv="${i.id}" data-a="waive">Waive with justification…</button>` : `<button class="btn-sm btn-ghost" data-rv="${i.id}" data-a="revoke">Revoke</button>`}</div>`;
      const res = i.resolved && i.status !== "pass" ? (i.review?.action === "waive" ? chip("waived") : `<span class="chip pass">acknowledged</span>`) : "";
      return `<tr><td>${chip(i.status)}${res ? `<div style="margin-top:.3rem">${res}</div>` : ""}</td>
        <td><b style="color:var(--text-primary)">${esc(i.title)}</b><div class="muted">${esc(i.detail)}</div>${rv}${tbl(i.table)}${acts}</td>
        <td class="num">${esc(i.value)}</td><td class="muted">${esc(i.threshold)}</td></tr>`;
    };
    const lf = Object.entries(A.lf_stats).map(([f, s]) => `<div class="card" style="background:var(--bg-tertiary)"><h3>${FAM[f]} · ${s.n.toLocaleString()} candidates · LF conflict ${pct(s.conflict_rate)}</h3>
      <div class="tscroll"><table class="t"><thead><tr><th>Labelling function</th><th class="num">Coverage</th><th class="num">Votes for</th><th class="num">Votes against</th></tr></thead>
      <tbody>${s.lfs.map((r) => `<tr><td>${esc(r.lf)}<div class="muted">${esc(r.description)}</div></td><td class="num">${pct(r.coverage)}</td><td class="num">${pct(r.pos)}</td><td class="num">${pct(r.neg)}</td></tr>`).join("")}</tbody></table></div></div>`).join("");
    v.innerHTML = `<h1>Automatic audit &amp; data scientist review</h1>
      <p class="lede">${chip(A.report.overall)} ${A.report.counts.pass} pass · ${A.report.counts.warn} warnings · ${A.report.counts.fail} failures · ${A.report.n_patients.toLocaleString()} patients · report <span class="mono">${esc(A.report.id)}</span>
      ${A.locked ? " · <b>review decided (locked)</b>" : ""}</p>
      ${Object.entries(byCat).map(([c, items]) => `<div class="card"><h2>${esc(c)}</h2><div class="tscroll"><table class="t"><thead><tr><th style="width:110px">Status</th><th>Check</th><th class="num">Value</th><th>Threshold</th></tr></thead><tbody>${items.map(item).join("")}</tbody></table></div></div>`).join("")}
      <h1 style="margin-top:2rem">Label model</h1><p class="lede">How often each labelling function fired. A function that never fires usually means missing evidence or wrong codes.</p>${lf}`;
    v.querySelectorAll("[data-rv]").forEach((b) => (b.onclick = () => {
      const a = b.dataset.a;
      let j = "";
      if (a === "waive") { j = prompt("Justification (≥20 characters, recorded in the audit trail):"); if (!j) return; }
      if (a === "acknowledge") { j = prompt("Optional note:", "Reviewed") || ""; }
      act(() => post("audit/review", { check_id: b.dataset.rv, action: a, justification: j }), "Recorded");
    }));
  }

  // ------------------------------------------------------------ queues
  function caseList(list, empty) {
    if (!list.length) return `<div class="card empty">${empty}</div>`;
    return `<div class="card"><div class="tscroll"><table class="t"><thead><tr><th>Case</th><th>Type</th><th>Outcome family</th><th></th></tr></thead><tbody>
      ${list.map((c) => `<tr><td class="mono">${esc(c.case_id)}</td><td>${c.type === "annotation" ? "Report value check" : "Event adjudication"}</td><td>${FAM[c.family] || c.family}</td>
      <td><a href="#/case/${esc(c.case_id)}"><button class="btn-sm btn-primary">Open</button></a></td></tr>`).join("")}</tbody></table></div></div>`;
  }
  function viewQueue(v) {
    v.innerHTML = `<h1>My cases</h1><p class="lede">${QUEUE.open.length} open · ${QUEUE.completed} completed. Cases are blinded: you will not see the automatic label, why the case was sampled, or other readers' decisions.</p>
      ${caseList(QUEUE.open, "No open cases. Thank you!")}`;
  }
  function viewDisagreements(v) {
    v.innerHTML = `<h1>Disagreements</h1><p class="lede">Cases where the two independent readers disagreed. You see both reads (anonymised) and make the final decision. Cases you read yourself are not shown.</p>
      ${caseList(QUEUE.disagreements, "No disagreements awaiting adjudication.")}`;
  }

  // ------------------------------------------------------------ case view
  async function viewCase(v, id) {
    const C = await api("cases/" + encodeURIComponent(id));
    if (C.case_type === "annotation") return annotationCase(v, C);
    const P = C.packet, done = !!C.my_read;
    const seg = (segs) => segs.map((s) => (s.h ? `<mark>${esc(s.t)}</mark>` : esc(s.t))).join("");
    const hist = Object.entries(P.patient.history).filter(([, x]) => x).map(([k]) => k.replace(/_/g, " ")).join(", ") || "none recorded";
    const meds = Object.entries(P.patient.baseline_meds).filter(([, x]) => x).map(([k]) => k.replace(/_/g, " ")).join(", ") || "none";
    const enc = P.encounters.length ? `<table class="t"><thead><tr><th class="num">Day</th><th>Date</th><th>Visit</th><th>Code</th><th>Position</th></tr></thead><tbody>
      ${P.encounters.map((e) => `<tr><td class="num">${e.day >= 0 ? "+" : ""}${e.day}</td><td>${esc(e.date)}</td><td>${esc(e.visit)}</td><td class="mono">${esc(e.code)}</td><td>${esc(e.position)}</td></tr>`).join("")}</tbody></table>`
      : `<p class="muted">No outcome diagnosis code in this window (case found from a procedure).</p>`;
    const labs = P.labs.length ? `<table class="t"><thead><tr><th class="num">Day</th><th>Time</th><th>Test</th><th class="num">Value</th><th>Unit</th><th class="num">Ref high</th><th></th></tr></thead><tbody>
      ${P.labs.map((l) => `<tr><td class="num">${l.day >= 0 ? "+" : ""}${l.day}</td><td class="mono">${esc(l.time.slice(5).replace("T", " "))}</td><td>${esc(l.analyte.replace(/_/g, " "))}</td><td class="num">${esc(l.value)}</td><td>${esc(l.unit)}</td><td class="num">${esc(l.ref_high ?? "—")}</td><td class="flag-${esc(l.flag)}">${esc(l.flag || "")}</td></tr>`).join("")}</tbody></table>`
      : `<p class="muted">No troponin / natriuretic peptide results in this window.</p>`;
    const procs = P.procedures.length ? `<table class="t"><thead><tr><th class="num">Day</th><th>Date</th><th>Procedure</th><th>Code</th></tr></thead><tbody>
      ${P.procedures.map((p) => `<tr><td class="num">${p.day >= 0 ? "+" : ""}${p.day}</td><td>${esc(p.date)}</td><td>${esc(p.category.replace(/_/g, " "))}</td><td class="mono">${esc(p.code)}</td></tr>`).join("")}</tbody></table>`
      : `<p class="muted">No relevant procedures in this window.</p>`;
    const notes = P.notes.length ? P.notes.map((n, i) => `<div class="note-card"><div class="note-head"><span><b style="color:var(--text-primary)">${esc(n.title)}</b></span><span>${esc(n.date)} · day ${n.day >= 0 ? "+" : ""}${n.day}</span></div>
      ${n.excerpts.length ? n.excerpts.map((e) => `<div class="excerpt">${seg(e)}</div>`).join("") : `<div class="excerpt">${esc(n.text.slice(0, 400))}</div>`}
      <details><summary>Full note</summary><div class="fulltext" id="full-${i}">${esc(n.text)}</div></details></div>`).join("") : `<p class="muted">No notes in this window.</p>`;

    const reads = C.independent_reads ? `<h3>Independent reads</h3><div class="reads">${C.independent_reads.map((r) => `<div class="read-box"><b>${esc(r.reader)}</b><br>
      ${esc(r.outcome.replace("_", " "))}${r.subtype ? ` · ${esc(P.subtypes[r.subtype] || r.subtype)}` : ""}${r.event_date ? ` · ${esc(r.event_date)}` : ""}<br>
      <span class="muted">${esc(r.confidence)} confidence</span>${r.criteria.length ? `<ul style="margin:.3rem 0 0 1rem">${r.criteria.map((c) => `<li>${esc(P.criteria[c] || c)}</li>`).join("")}</ul>` : ""}${r.comment ? `<div style="margin-top:.3rem">“${esc(r.comment)}”</div>` : ""}</div>`).join("")}</div>` : "";
    const R = C.my_read || {};
    const form = done ? `<div class="card"><h2>Submitted ${chip("pass")}</h2><p class="muted">${esc(R.outcome)}${R.subtype ? " · " + esc(R.subtype) : ""}${R.event_date ? " · " + esc(R.event_date) : ""}</p>
        <div class="row-actions"><a href="#/${C.mode === "lead" ? "disagreements" : "queue"}"><button class="btn-sm btn-primary">Back to list</button></a></div></div>` : `
      <div class="card decision"><h2>${C.mode === "lead" ? "Lead adjudication" : "Your decision"}</h2>
        ${C.mode === "lead" ? reads : `<div class="blind">Blinded read: the automatic label, sampling reason and other readers' decisions are hidden.</div>`}
        <div class="muted" style="margin-bottom:.5rem">${esc(P.definition)}</div>
        ${[["event", "Confirmed event"], ["not_event", "Not an event"], ["indeterminate", "Cannot determine from the record"]].map(([k, l]) => `<label class="opt"><input type="radio" name="outcome" value="${k}"> ${l}</label>`).join("")}
        <div id="ev-fields" class="hidden"><h3>Subtype</h3>${Object.entries(P.subtypes).map(([k, l]) => `<label class="opt"><input type="radio" name="subtype" value="${k}"> ${esc(l)}</label>`).join("")}
          <h3>Event date</h3><input type="date" id="ev-date" value="${esc(P.candidate_date)}">
          <h3>Criteria met</h3>${Object.entries(P.criteria).map(([k, l]) => `<label class="opt"><input type="checkbox" name="crit" value="${k}"> ${esc(l)}</label>`).join("")}</div>
        <h3>Confidence</h3><select id="conf"><option value="high">High</option><option value="medium" selected>Medium</option><option value="low">Low</option></select>
        <h3>Comment</h3><textarea id="cmt" placeholder="Required if the outcome cannot be determined"></textarea>
        <div class="row-actions"><button class="btn-primary" id="submit" style="width:100%">Submit decision</button></div></div>`;

    v.innerHTML = `<p class="muted"><a href="#/${C.mode === "lead" ? "disagreements" : "queue"}" style="color:var(--text-muted)">← back</a></p>
      <h1>${esc(P.family_name)} <span class="mono" style="font-size:1rem;color:var(--text-muted)">${esc(C.case_id)}</span></h1>
      <p class="lede">Candidate date <b>${esc(P.candidate_date)}</b> (day 0) · window ${P.window_days[0]} to +${P.window_days[1]} days ·
        ${P.patient.age ? esc(P.patient.age) + " y" : ""} ${esc(P.patient.sex || "")} · history: ${esc(hist)} · baseline meds: ${esc(meds)}
        ${P.patient.death_day != null ? ` · <b style="color:var(--danger)">died day ${P.patient.death_day >= 0 ? "+" : ""}${P.patient.death_day}</b>` : ""}</p>
      <div class="case-grid"><div>
        <div class="card"><h2>Coded encounters</h2><div class="tscroll">${enc}</div></div>
        <div class="card"><h2>Biomarkers</h2><div class="tscroll">${labs}</div></div>
        <div class="card"><h2>Procedures</h2><div class="tscroll">${procs}</div></div>
        <div class="card"><h2>Notes &amp; reports</h2><p class="muted" style="margin-bottom:.6rem">Highlighted terms are keyword matches only. Read the context yourself (negation, history, rule-out).</p>${notes}</div>
      </div><div>${form}</div></div>`;
    if (done) return;
    v.querySelectorAll(".opt input").forEach((i) => i.addEventListener("change", () => {
      v.querySelectorAll(".opt").forEach((o) => o.classList.toggle("sel", $("input", o).checked));
      $("#ev-fields").classList.toggle("hidden", (v.querySelector("input[name=outcome]:checked") || {}).value !== "event");
    }));
    $("#submit").onclick = async () => {
      const outcome = (v.querySelector("input[name=outcome]:checked") || {}).value;
      const d = { outcome, subtype: (v.querySelector("input[name=subtype]:checked") || {}).value, event_date: $("#ev-date").value,
        criteria: [...v.querySelectorAll("input[name=crit]:checked")].map((x) => x.value), confidence: $("#conf").value, comment: $("#cmt").value };
      try {
        const r = await post(`cases/${encodeURIComponent(C.case_id)}/read`, d);
        toast(r.status === "disagreement" ? "Submitted. Readers disagreed, so the case goes to a lead cardiologist." : "Submitted");
        await refresh();
        const next = (C.mode === "lead" ? QUEUE.disagreements : QUEUE.open)[0];
        location.hash = next ? `#/case/${next.case_id}` : `#/${C.mode === "lead" ? "disagreements" : "queue"}`;
      } catch (ex) { toast(ex.message, true); }
    };
  }

  function annotationCase(v, C) {
    const P = C.packet, done = !!C.my_read;
    const hl = esc(P.text).replace(/(\d{1,4}(?:\.\d+)?\s*(?:%|percent|agatston)?)/gi, "<mark>$1</mark>");
    v.innerHTML = `<p class="muted"><a href="#/queue" style="color:var(--text-muted)">← back</a></p>
      <h1>Report value check <span class="mono" style="font-size:1rem;color:var(--text-muted)">${esc(C.case_id)}</span></h1>
      <p class="lede">Check the values the NLP extracted from this imaging report (dated ${esc(P.date)}). Correct them if wrong; leave a field blank if the report does not state it.</p>
      <div class="case-grid"><div class="card"><h2>Report text</h2><div class="fulltext" style="max-height:none;font-size:.95rem">${hl}</div></div>
      <div class="card decision">${done ? `<h2>Submitted ${chip("pass")}</h2><a href="#/queue"><button class="btn-sm btn-primary">Back</button></a>` : `
        <h3>LV ejection fraction (%)</h3><input type="number" id="a-lvef" step="0.5" value="${P.extracted.lvef ?? ""}" placeholder="not reported">
        <p class="muted">Extracted: ${P.extracted.lvef ?? "nothing"} · for a range enter the midpoint</p>
        <h3>Coronary calcium score (Agatston)</h3><input type="number" id="a-cac" step="1" value="${P.extracted.cac_score ?? ""}" placeholder="not reported">
        <p class="muted">Extracted: ${P.extracted.cac_score ?? "nothing"}</p>
        <h3>Comment</h3><textarea id="a-cmt"></textarea>
        <div class="row-actions"><button class="btn-primary" id="a-submit" style="width:100%">Submit</button></div>`}</div></div>`;
    if (done) return;
    $("#a-submit").onclick = async () => {
      try {
        await post(`cases/${encodeURIComponent(C.case_id)}/read`, { lvef: { value: $("#a-lvef").value }, cac_score: { value: $("#a-cac").value }, comment: $("#a-cmt").value });
        toast("Submitted"); await refresh();
        location.hash = QUEUE.open[0] ? `#/case/${QUEUE.open[0].case_id}` : "#/queue";
      } catch (ex) { toast(ex.message, true); }
    };
  }

  // ------------------------------------------------------------ metrics
  function viewMetrics(v) {
    const M = STATUS.metrics, G = STATUS.governance.gates;
    const ci = (c) => (c ? ` <span class="muted">(${pct(c[0], 0)}–${pct(c[1], 0)})</span>` : "");
    const rows = Object.entries(M.families).filter(([, x]) => x.cases).map(([f, x]) => `<tr><td>${FAM[f]}</td><td class="num">${x.final}/${x.cases}</td><td class="num">${x.double_reads}</td>
      <td class="num">${num(x.kappa)}</td><td class="num">${pct(x.raw_agreement)}</td><td class="num">${pct(x.ppv_auto_confirmed)}${ci(x.ppv_ci)}</td>
      <td class="num">${pct(x.npv_auto_rejected)}${ci(x.npv_ci)}</td><td class="num">${x.lead_resolved}</td><td class="num">${pct(x.indeterminate_fraction)}</td><td class="num">${pct(x.auto_subtype_accuracy)}</td></tr>`).join("");
    const strata = Object.entries(M.families).filter(([, x]) => x.cases).map(([f, x]) => `<tr><td>${FAM[f]}</td>${["uncertain", "auto_confirmed", "auto_rejected"].map((s) => {
      const z = x.strata[s]; return `<td class="num">${z.cases ? `${z.event} / ${z.not_event} / ${z.indeterminate}` : "—"}</td>`; }).join("")}</tr>`).join("");
    const A = M.annotation;
    v.innerHTML = `<h1>Agreement &amp; label accuracy</h1>
      <p class="lede">Gate thresholds: κ ≥ ${G.min_kappa}, PPV ≥ ${pct(G.min_ppv_auto_confirmed, 0)}, NPV ≥ ${pct(G.min_npv_auto_rejected, 0)}, annotation accuracy ≥ ${pct(G.min_annotation_accuracy, 0)}, indeterminate ≤ ${pct(G.max_indeterminate_fraction, 0)}.</p>
      <div class="card"><h2>By outcome family</h2><div class="tscroll"><table class="t"><thead><tr><th>Outcome</th><th class="num">Final</th><th class="num">Double reads</th><th class="num">Cohen's κ</th><th class="num">Raw agreement</th>
        <th class="num">PPV auto-confirmed</th><th class="num">NPV auto-rejected</th><th class="num">Lead-resolved</th><th class="num">Indeterminate</th><th class="num">Auto-subtype accuracy</th></tr></thead><tbody>${rows || `<tr><td colspan="10" class="muted">No adjudication yet</td></tr>`}</tbody></table></div>
        <p class="muted" style="margin-top:.75rem">PPV/NPV are measured on simple random samples of auto-confirmed / auto-rejected candidates (95% Wilson CI). Uncertain candidates are all adjudicated and become labels directly.</p></div>
      <div class="card"><h2>Final decisions by sampling stratum <span class="muted">(event / not event / indeterminate)</span></h2><div class="tscroll"><table class="t"><thead><tr><th>Outcome</th><th class="num">Uncertain</th><th class="num">Auto-confirmed</th><th class="num">Auto-rejected</th></tr></thead><tbody>${strata}</tbody></table></div></div>
      <div class="card"><h2>Imaging report extraction</h2><div class="tscroll"><table class="t"><thead><tr><th>Field</th><th class="num">Reports evaluated</th><th class="num">Accuracy (weighted)</th></tr></thead><tbody>
        ${["lvef", "cac_score"].map((k) => `<tr><td>${k === "lvef" ? "LVEF" : "Coronary calcium"}</td><td class="num">${A[k]?.evaluated ?? 0}</td><td class="num">${pct(A[k]?.accuracy)}${ci(A[k]?.ci)}</td></tr>`).join("")}</tbody></table></div>
        <p class="muted" style="margin-top:.75rem">Reports with no extracted value are over-sampled to find misses; accuracy is re-weighted by sampling probability.</p></div>`;
  }

  // ------------------------------------------------------------ audit trail / users
  async function viewLog(v) {
    const L = await api("log?limit=300");
    v.innerHTML = `<h1>Audit trail</h1><p class="lede">${L.verify.ok ? chip("pass") + ` Hash chain intact (${L.verify.entries.toLocaleString()} entries)` : chip("fail") + ` Hash chain BROKEN at entry ${L.verify.first_bad_id}`}. Each entry's hash covers the previous one, so any edit or deletion is detectable.</p>
      <div class="card"><div class="tscroll"><table class="t"><thead><tr><th>#</th><th>Time (UTC)</th><th>User</th><th>Action</th><th>Entity</th><th>Details</th><th>Hash</th></tr></thead><tbody>
      ${L.entries.map((e) => `<tr><td class="num">${e.id}</td><td class="mono">${esc(e.ts.slice(0, 19).replace("T", " "))}</td><td>${esc(e.user)}</td><td>${esc(e.action)}</td><td class="mono">${esc(e.entity)}</td>
        <td class="muted">${esc(JSON.stringify(e.details).slice(0, 160))}</td><td class="mono">${esc(e.hash)}</td></tr>`).join("")}</tbody></table></div></div>`;
  }
  async function viewUsers(v) {
    const U = await api("users");
    v.innerHTML = `<h1>Users</h1><div class="card"><div class="tscroll"><table class="t"><thead><tr><th>Username</th><th>Name</th><th>Role</th><th>Active</th></tr></thead><tbody>
      ${U.map((u) => `<tr><td>${esc(u.username)}</td><td>${esc(u.full_name)}</td><td>${ROLE[u.role]}</td><td>${u.active ? "yes" : "no"}</td></tr>`).join("")}</tbody></table></div></div>
      <div class="card"><h2>Add user</h2><div class="grid2">
        <div><label>Username</label><input id="nu-u"></div><div><label>Full name</label><input id="nu-n"></div>
        <div><label>Role</label><select id="nu-r">${Object.entries(ROLE).map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select></div>
        <div><label>Initial password (12+ characters)</label><input id="nu-p" type="password"></div></div>
        <div class="row-actions" style="margin-top:1rem"><button class="btn-sm btn-primary" id="nu-go">Create user</button></div></div>`;
    $("#nu-go").onclick = () => act(() => post("users", { username: $("#nu-u").value, full_name: $("#nu-n").value, role: $("#nu-r").value, password: $("#nu-p").value }), "User created");
  }

  // ------------------------------------------------------------ boot
  $("#login-form").addEventListener("submit", login);
  $("#logout").addEventListener("click", logout);
  if (sessionStorage.getItem(TOKEN)) boot().catch(logout);
})();
