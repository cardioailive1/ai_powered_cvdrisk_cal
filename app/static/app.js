/* CardioAI frontend — form is generated from /api/meta, results from /api/predict. */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const TOKEN_KEY = "cardioai.token";
  let META = null;
  let activeCalc = "ami";

  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pct = (x, d = 1) => (x == null ? "—" : (x * 100 < 0.1 && x > 0 ? "<0.1%" : (x * 100).toFixed(d) + "%"));
  const HLABEL = { "30d": "30-Day", "1y": "1-Year", "5y": "5-Year", "10y": "10-Year" };

  // ---------------------------------------------------------------- API
  async function api(path, opts = {}) {
    const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    const t = sessionStorage.getItem(TOKEN_KEY);
    if (t) headers.Authorization = "Bearer " + t;
    const res = await fetch(path, { ...opts, headers });
    let body = null;
    try { body = await res.json(); } catch (_) { /* empty */ }
    if (res.status === 401 && path !== "/api/auth/login") { logout(); throw new Error("Session expired — please sign in again."); }
    if (!res.ok) {
      const err = new Error((body && (body.detail || (body.errors || []).join(" "))) || "Request failed (" + res.status + ")");
      err.errors = body && body.errors;
      throw err;
    }
    return body;
  }

  // ---------------------------------------------------------------- auth
  async function login(e) {
    e.preventDefault();
    const btn = $("#login-btn"), err = $("#error-message");
    btn.disabled = true; btn.textContent = "Signing in…";
    try {
      const r = await api("/api/auth/login", { method: "POST", body: JSON.stringify({ username: $("#username").value.trim(), password: $("#password").value }) });
      sessionStorage.setItem(TOKEN_KEY, r.token);
      await start();
    } catch (ex) {
      err.textContent = ex.message; err.classList.add("show");
      setTimeout(() => err.classList.remove("show"), 4000);
      $("#password").value = "";
    } finally { btn.disabled = false; btn.textContent = "Access Calculator"; }
  }

  function logout() {
    sessionStorage.removeItem(TOKEN_KEY);
    $("#login-screen").classList.remove("hidden");
    ["#main-content", "#logout-btn", "#user-chip"].forEach((s) => $(s).classList.add("hidden"));
    $("#username").value = ""; $("#password").value = "";
  }

  async function start() {
    const me = await api("/api/me");
    META = await api("/api/meta");
    $("#login-screen").classList.add("hidden");
    ["#main-content", "#logout-btn", "#user-chip"].forEach((s) => $(s).classList.remove("hidden"));
    $("#user-chip").textContent = me.username + (me.role === "admin" ? " · admin" : "");
    renderModelInfo(); renderCalcs(); renderForm(); loadHistory();
  }

  // ---------------------------------------------------------------- render: header / calculators
  function renderModelInfo() {
    const m = META.model;
    const strip = $("#model-strip"), banner = $("#demo-banner");
    if (!m) {
      strip.innerHTML = "<span><b>AI models:</b> not loaded — published equations only</span>";
      banner.innerHTML = "<strong>No AI model bundle loaded.</strong> Only published equations are available.";
      banner.classList.remove("hidden");
      return;
    }
    const src = m.provenance === "mayo-clinic-platform" ? "Mayo Clinic Platform" : m.provenance;
    strip.innerHTML = `<span><b>Model bundle:</b> ${esc(m.bundle_id)}</span><span><b>Training data:</b> ${esc(src)}</span>` +
      (m.n_patients ? `<span><b>Patients:</b> ${Number(m.n_patients).toLocaleString()}</span>` : "") +
      (m.created_utc ? `<span><b>Trained:</b> ${esc(m.created_utc.slice(0, 10))}</span>` : "") + labelInfo(m.label_governance);
    if (m.is_demo) {
      banner.innerHTML = `<strong>DEMO MODELS — NOT FOR CLINICAL USE.</strong> ${esc(m.data_description || "")} ` +
        "Replace with the bundle trained in your Mayo Clinic Platform workspace before any clinical evaluation.";
      banner.classList.remove("hidden");
    } else banner.classList.add("hidden");
  }

  function labelInfo(g) {
    if (!g || g.status === "ungoverned") return `<span><b>Labels:</b> diagnosis codes (not adjudicated)</span>`;
    const ks = Object.values(g.families || {}).map((f) => f.kappa).filter((k) => k != null);
    const kr = ks.length ? ` · κ ${Math.min(...ks).toFixed(2)}–${Math.max(...ks).toFixed(2)}` : "";
    const who = g.status === "simulated" ? "SIMULATED adjudication" : "cardiologist-adjudicated";
    return `<span><b>Labels:</b> ${who} (${Number(g.adjudicated_cases || 0).toLocaleString()} cases${kr}) · release ${esc(g.release_id)}</span>`;
  }

  function renderCalcs() {
    const box = $("#calc-list");
    box.innerHTML = Object.entries(META.calculators).map(([id, c]) => {
      const p = c.performance_5y;
      const perf = p ? `<p class="calculator-perf">Measured 5-yr C-stat: ${p.auc.toFixed(3)} (95% CI ${p.auc_ci[0].toFixed(3)}–${p.auc_ci[1].toFixed(3)})</p>`
                     : `<p class="calculator-perf">No trained model loaded for this calculator</p>`;
      const cls = p ? (p.meets_target ? "ok" : "miss") : "";
      return `<div class="calculator-type ${id === activeCalc ? "active" : ""}" data-type="${id}" tabindex="0" role="button" aria-pressed="${id === activeCalc}">
        <div class="calculator-header"><span class="calculator-name">${esc(c.name)}</span>
          <span class="calculator-badge ${cls}" title="Target C-statistic">Target C-stat ≥${c.target_cstat.toFixed(2)}</span></div>
        <p class="calculator-description">${esc(c.description)}</p>
        <p class="calculator-target">Target: ${esc(c.population)}</p>${perf}</div>`;
    }).join("");
    box.querySelectorAll(".calculator-type").forEach((el) => {
      const pick = () => { activeCalc = el.dataset.type; renderCalcs(); applyCalcVisibility(); };
      el.addEventListener("click", pick);
      el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
    });
  }

  // ---------------------------------------------------------------- render: form
  function fieldHTML(f) {
    const id = "f-" + f.key;
    const unit = f.unit ? ` (${esc(f.unit)})` : "";
    if (f.type === "bool") return `<div class="checkbox-item" data-calcs="${(f.calcs || []).join(",")}"><input type="checkbox" id="${id}" name="${f.key}"><label for="${id}">${esc(f.label)}</label></div>`;
    let input;
    if (f.type === "select") {
      const nice = { white: "White", black: "Black/African American", hispanic: "Hispanic/Latino", asian: "Asian", other: "Other", type1: "Type 1", type2: "Type 2" };
      input = `<select id="${id}" name="${f.key}">${f.options.map((o) => `<option value="${o}" ${o === f.default ? "selected" : ""}>${esc(nice[o] || o[0].toUpperCase() + o.slice(1))}</option>`).join("")}</select>`;
    } else {
      const step = ["hba1c", "hscrp"].includes(f.key) ? "0.1" : "any";
      input = `<input type="number" id="${id}" name="${f.key}" min="${f.min}" max="${f.max}" step="${step}" ${f.default != null ? `value="${f.default}"` : ""} ${f.required ? "required" : `placeholder="optional"`}>`;
    }
    const hint = f.section === "imaging" ? `<div class="field-hint">Leave blank if not available</div>` : "";
    return `<div class="form-group" data-calcs="${(f.calcs || []).join(",")}"><label for="${id}">${esc(f.label)}${unit}</label>${input}${hint}</div>`;
  }

  function renderForm() {
    const bySection = {};
    META.fields.forEach((f) => (bySection[f.section] = bySection[f.section] || []).push(f));
    let n = 2, html = "";
    for (const [sec, title] of Object.entries(META.sections)) {
      const fs = bySection[sec] || [];
      const isChecks = fs.every((f) => f.type === "bool");
      let inner = isChecks ? `<div class="checkbox-group">${fs.map(fieldHTML).join("")}</div>` : `<div class="form-grid">${fs.map(fieldHTML).join("")}`;
      if (sec === "demographics") inner += `<div class="form-group"><label>BMI: <span id="bmi-value" class="range-value">—</span></label><input type="text" id="bmi" disabled style="background:var(--bg-primary)"></div>`;
      if (!isChecks) inner += "</div>";
      html += `<div class="form-section" data-section="${sec}"><h2 class="section-title"><span class="section-icon">${n++}</span>${esc(title)}</h2>${inner}</div>`;
    }
    $("#form-sections").innerHTML = html;
    ["#f-height_cm", "#f-weight_kg"].forEach((s) => $(s).addEventListener("input", bmi));
    bmi(); applyCalcVisibility();
  }

  function applyCalcVisibility() {
    document.querySelectorAll("[data-calcs]").forEach((el) => {
      const c = el.dataset.calcs;
      el.classList.toggle("hidden", !!c && !c.split(",").includes(activeCalc));
    });
    document.querySelectorAll(".form-section[data-section]").forEach((sec) => {
      const visible = [...sec.querySelectorAll("[data-calcs]")].some((el) => !el.classList.contains("hidden"));
      sec.classList.toggle("hidden", !visible);
    });
  }

  function bmi() {
    const h = parseFloat($("#f-height_cm").value) / 100, w = parseFloat($("#f-weight_kg").value);
    const v = h > 0 && w > 0 ? (w / (h * h)).toFixed(1) : "—";
    $("#bmi-value").textContent = v; $("#bmi").value = v;
  }

  function collect() {
    const p = {};
    META.fields.forEach((f) => {
      const el = document.getElementById("f-" + f.key);
      const hiddenForCalc = f.calcs && !f.calcs.includes(activeCalc);
      if (f.type === "bool") p[f.key] = !hiddenForCalc && el.checked;
      else if (f.type === "number") p[f.key] = hiddenForCalc || el.value === "" ? null : Number(el.value);
      else p[f.key] = el.value;
    });
    return p;
  }

  function resetForm() {
    renderForm();
    $("#form-errors").classList.add("hidden");
    $("#results-content").innerHTML = `<div style="text-align:center;padding:3rem 0;color:var(--text-muted)"><p>Complete the form and click "Calculate Risk" to see the cardiovascular event probability assessment.</p></div>`;
  }

  // ---------------------------------------------------------------- predict
  async function calculate(e) {
    e.preventDefault();
    const errBox = $("#form-errors");
    errBox.classList.add("hidden");
    document.querySelectorAll(".field-error").forEach((x) => x.classList.remove("field-error"));
    $("#loading").classList.add("active"); $("#results-content").style.display = "none";
    try {
      const r = await api("/api/predict", { method: "POST", body: JSON.stringify({ calculator: activeCalc, patient: collect() }) });
      renderResults(r); loadHistory();
      if (window.innerWidth < 1200) $("#results-panel").scrollIntoView({ behavior: "smooth" });
    } catch (ex) {
      const list = ex.errors || [ex.message];
      errBox.innerHTML = `<strong>Please check:</strong><ul>${list.map((m) => `<li>${esc(m)}</li>`).join("")}</ul>`;
      errBox.classList.remove("hidden");
    } finally {
      $("#loading").classList.remove("active"); $("#results-content").style.display = "block";
    }
  }

  function renderResults(r) {
    let h = "";
    if (r.warnings && r.warnings.length) h += `<div class="warn-list">${r.warnings.map((w) => `<div>⚠️ ${esc(w)}</div>`).join("")}</div>`;

    if (!r.applicable) {
      h += `<div class="na-box">${esc(r.not_applicable_reason)}</div>`;
    } else if (r.primary) {
      const p = r.primary;
      h += `<div class="risk-display"><div class="risk-percentage">${pct(p.risk)}</div>
        <div class="risk-label">${HLABEL[p.horizon]} ${esc(p.label)} risk · AI model</div>
        <div class="risk-category ${p.category_class}">${esc(p.category)} Risk</div></div>`;
      h += `<div class="risk-visualization"><div class="metric-label">Position on 0–30% scale</div>
        <div class="risk-bar-container"><div class="risk-bar" style="width:${Math.max(4, Math.min(100, (p.risk / 0.3) * 100))}%">${pct(p.risk)}</div></div></div>`;
    }

    if (r.endpoints && r.endpoints.length) {
      const hs = META.horizons;
      h += `<div class="block-title">Predicted risk by horizon</div><table class="horizon-table"><thead><tr><th>Outcome</th>${hs.map((x) => `<th>${HLABEL[x]}</th>`).join("")}</tr></thead><tbody>`;
      r.endpoints.forEach((ep) => {
        h += `<tr><td class="ep">${esc(ep.label)}</td>${hs.map((x) => ep.horizons[x] ? `<td title="Hold-out C-stat ${ep.horizons[x].auc?.toFixed(3) ?? "—"}">${pct(ep.horizons[x].risk)}</td>` : `<td class="na" title="Not enough events to train this horizon">n/a</td>`).join("")}</tr>`;
      });
      h += "</tbody></table>";
      const prim = r.endpoints.find((e) => e.id === r.primary?.endpoint);
      const ph = prim && prim.horizons[r.primary.horizon];
      if (ph && ph.auc != null) {
        h += `<div class="metrics-grid"><div class="metric-card"><div class="metric-label">Model performance (hold-out)</div>
          <div class="metric-value">C-stat ${ph.auc.toFixed(3)}</div>
          <div class="confidence-interval">95% CI ${ph.auc_ci[0].toFixed(3)}–${ph.auc_ci[1].toFixed(3)} · target ≥${META.calculators[r.calculator].target_cstat.toFixed(2)} · ${ph.meets_target ? "target met" : "target not met"}${ph.calibration_slope != null ? " · calibration slope " + ph.calibration_slope.toFixed(2) : ""}</div></div></div>`;
      }
    }

    if (r.contributions && r.contributions.length) {
      const maxAbs = Math.max(...r.contributions.map((c) => Math.abs(c.delta)), 1e-6);
      h += `<div class="block-title">What drives this patient's risk</div>`;
      h += r.contributions.map((c) => {
        const w = (Math.abs(c.delta) / maxAbs) * 50;
        return `<div class="contrib" title="Change in predicted risk vs. a typical patient (${esc(c.reference ?? "—")})"><span class="name">${esc(c.label)}</span>
          <span class="track"><span class="bar ${c.delta > 0 ? "up" : "down"}" style="width:${w}%"></span></span>
          <span class="val">${c.delta > 0 ? "+" : "−"}${(Math.abs(c.delta) * 100).toFixed(1)}</span></div>`;
      }).join("") + `<div class="field-hint">Percentage-point change in ${HLABEL[r.primary.horizon]} risk compared with the training cohort's typical value for each factor.</div>`;
    }

    if (r.equations && r.equations.length) {
      h += `<div class="block-title">Published equations (reference)</div>`;
      h += r.equations.map((e) => `<div class="eq-card"><div class="eq-name">${esc(e.name)}</div><div class="eq-out">${esc(e.outcome)}</div>
        ${e.applicable ? (e.risk != null ? `<div class="eq-val">${pct(e.risk)} <span style="font-size:.85rem;color:var(--text-muted)">${esc(e.category)}</span></div>` : `<div class="eq-val" style="font-size:1.05rem">${esc(e.category)}</div>`) : `<div class="eq-note">Not applicable: ${esc(e.reason)}</div>`}
        ${e.applicable && e.note ? `<div class="eq-note">${esc(e.note)}</div>` : ""}<div class="eq-ref">${esc(e.reference)}</div></div>`).join("");
    }

    h += `<div class="recommendations" style="margin-top:1.5rem"><div class="recommendations-title">Clinical considerations</div>` +
      r.recommendations.map((t, i) => `<div class="recommendation-item"><div class="recommendation-icon">${i + 1}</div><div class="recommendation-text">${esc(t)}</div></div>`).join("") + "</div>";
    h += `<div class="export-buttons"><button class="btn-export" type="button" onclick="window.print()">📄 Print / Save as PDF</button></div>`;
    $("#results-content").innerHTML = h;
  }

  async function loadHistory() {
    try {
      const rows = await api("/api/assessments?limit=5");
      if (!rows.length) return $("#history").classList.add("hidden");
      $("#history-list").innerHTML = rows.map((a) => `<div class="history-item"><span>${esc(META.calculators[a.calculator]?.name || a.calculator)}</span>
        <span>${a.primary ? pct(a.primary.risk) + " · " + HLABEL[a.primary.horizon] : "—"} · ${new Date(a.created_at).toLocaleString()}</span></div>`).join("");
      $("#history").classList.remove("hidden");
    } catch (_) { /* non-critical */ }
  }

  // ---------------------------------------------------------------- boot
  $("#login-form").addEventListener("submit", login);
  $("#logout-btn").addEventListener("click", logout);
  $("#patient-form").addEventListener("submit", calculate);
  $("#reset-btn").addEventListener("click", resetForm);
  if (sessionStorage.getItem(TOKEN_KEY)) start().catch(logout);
})();
