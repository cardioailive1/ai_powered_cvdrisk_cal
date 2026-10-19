/* CardioAI account, user administration and audit-log pages. Every action is also enforced server-side;
   hiding a control here is convenience, not security. */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  let C = null, ROLES = null, auditBefore = null;
  const when = (t) => (t ? new Date(t).toLocaleString() : "—");

  function flash(el, msg, ok = true) { el.textContent = msg; el.className = "inline-msg " + (ok ? "ok" : "bad"); }
  function secret(title, note, value) {
    $("#secret-title").textContent = title; $("#secret-note").textContent = note; $("#secret-value").textContent = value;
    $("#secret-modal").classList.remove("hidden");
  }

  // ---------------------------------------------------------------- my account
  async function account() {
    const me = C.me(), esc = C.esc;
    $("#acct-profile").innerHTML = `<div><span>Name</span><b>${esc(me.full_name || "—")}</b></div>
      <div><span>Email</span><b>${esc(me.email)}</b></div><div><span>Organization</span><b>${esc(me.organization || "—")}</b></div>
      <div><span>Role</span><b>${esc(me.role_label)}</b></div><div><span>Permissions</span><b>${me.permissions.map(esc).join(", ")}</b></div>
      <div><span>Signed in at</span><b>${when(me.last_login_at)}</b></div>`;
    $("#acct-mfa").innerHTML = me.mfa_enabled
      ? `<p class="ok-text">✓ Two-factor authentication is on (authenticator app + recovery codes).</p>
         <p class="field-hint">Lost your device? An administrator can reset it; you will set it up again at your next sign-in.</p>`
      : `<p class="bad-text">Two-factor authentication is not set up.</p>`;
    const rows = await C.api("/api/me/sessions");
    $("#acct-sessions").innerHTML = `<table class="data-table"><thead><tr><th>Device</th><th>IP</th><th>Signed in</th><th>Last active</th><th></th></tr></thead><tbody>${
      rows.map((s) => `<tr><td>${esc(s.user_agent.slice(0, 80) || "—")}</td><td>${esc(s.ip)}</td><td>${when(s.created_at)}</td><td>${when(s.last_seen_at)}</td>
        <td>${s.current ? "<span class='pill ok'>this device</span>" : `<button class="btn-secondary small" data-revoke="${s.id}">Sign out</button>`}</td></tr>`).join("")}</tbody></table>`;
    $("#acct-sessions").querySelectorAll("[data-revoke]").forEach((b) => b.addEventListener("click", async () => {
      await C.api("/api/me/sessions/" + b.dataset.revoke, { method: "DELETE" }); account();
    }));
  }

  async function changePassword(e) {
    e.preventDefault();
    const msg = $("#ap-msg");
    if ($("#ap-new").value !== $("#ap-new2").value) return flash(msg, "The two passwords do not match.", false);
    try {
      await C.api("/api/auth/password", { method: "POST", body: JSON.stringify({ current_password: $("#ap-current").value, new_password: $("#ap-new").value }) });
      e.target.reset(); flash(msg, "Password updated. Your other sessions were signed out.");
    } catch (ex) { flash(msg, ex.errors ? ex.errors.join(" ") : ex.message, false); }
  }

  // ---------------------------------------------------------------- users
  function roleOptions(sel) {
    return Object.entries(ROLES.roles).map(([k, r]) => `<option value="${k}" ${k === sel ? "selected" : ""}>${C.esc(r.label)}</option>`).join("");
  }

  async function users() {
    const me = C.me(), esc = C.esc, manage = me.permissions.includes("users:manage");
    if (!ROLES) ROLES = await C.api("/api/admin/roles");
    $("#cu-role").innerHTML = roleOptions("clinician");
    const all = await C.api("/api/admin/users");
    const pending = all.filter((u) => u.status === "pending");
    const badge = $("#pending-badge");
    badge.textContent = pending.length; badge.classList.toggle("hidden", !pending.length);

    $("#pending-list").innerHTML = pending.length ? `<table class="data-table"><thead><tr><th>Name</th><th>Email</th><th>Organization</th><th>Requested</th>${manage ? "<th>Role</th><th></th>" : ""}</tr></thead><tbody>${
      pending.map((u) => `<tr data-id="${u.id}"><td>${esc(u.full_name)}</td><td>${esc(u.email)}</td><td>${esc(u.organization || "—")}</td><td>${when(u.created_at)}</td>
        ${manage ? `<td><select class="role-select" data-role>${roleOptions("clinician")}</select></td>
        <td class="actions"><button class="btn-primary small" data-act="approve">Approve</button><button class="btn-secondary small" data-act="reject">Reject</button></td>` : ""}</tr>`).join("")
    }</tbody></table><p class="field-hint">Verify each requester's identity and need for access (e.g. against your staff directory) before approving.</p>`
      : `<p class="field-hint">No pending requests.</p>`;

    const others = all.filter((u) => u.status !== "pending");
    $("#user-table").innerHTML = `<table class="data-table"><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th>MFA</th><th>Last sign-in</th><th>Sessions</th>${manage ? "<th>Actions</th>" : ""}</tr></thead><tbody>${
      others.map((u) => {
        const self = u.id === me.id;
        const role = manage && !self && u.status !== "rejected" ? `<select class="role-select" data-change-role>${roleOptions(u.role)}</select>` : esc(ROLES.roles[u.role]?.label || u.role);
        const st = `<span class="pill ${u.status === "active" ? "ok" : "bad"}">${esc(u.status)}</span>${u.locked ? " <span class='pill bad'>locked</span>" : ""}${u.must_change_password ? " <span class='pill'>temp password</span>" : ""}`;
        const acts = !manage ? "" : `<td class="actions">${self ? "<span class='field-hint'>you</span>" : `
          ${u.status === "active" ? `<button class="btn-secondary small" data-act="disable">Disable</button>` : u.status === "disabled" ? `<button class="btn-secondary small" data-act="enable">Enable</button>` : ""}
          ${u.locked ? `<button class="btn-secondary small" data-act="unlock">Unlock</button>` : ""}
          <button class="btn-secondary small" data-act="reset-password">Reset password</button>
          ${u.mfa_enabled ? `<button class="btn-secondary small" data-act="reset-mfa">Reset MFA</button>` : ""}
          ${u.active_sessions ? `<button class="btn-secondary small" data-act="revoke-sessions">Sign out</button>` : ""}`}</td>`;
        return `<tr data-id="${u.id}" data-email="${esc(u.email)}"><td>${esc(u.full_name || "—")}</td><td>${esc(u.email)}</td><td>${role}</td><td>${st}</td>
          <td>${u.mfa_enabled ? "✓" : "—"}</td><td>${when(u.last_login_at)}</td><td>${u.active_sessions}</td>${acts}</tr>`;
      }).join("")}</tbody></table>`;

    $("#role-matrix").innerHTML = `<table class="data-table"><thead><tr><th>Permission</th>${Object.values(ROLES.roles).map((r) => `<th>${esc(r.label)}</th>`).join("")}</tr></thead><tbody>${
      Object.entries(ROLES.permissions).map(([p, d]) => `<tr><td title="${esc(d)}">${esc(d)}</td>${Object.values(ROLES.roles).map((r) => `<td>${r.permissions.includes(p) ? "✓" : ""}</td>`).join("")}</tr>`).join("")}</tbody></table>`;

    document.querySelectorAll("#page-users [data-act]").forEach((b) => b.addEventListener("click", () => act(b)));
    document.querySelectorAll("#page-users [data-change-role]").forEach((s) => s.addEventListener("change", async () => {
      const tr = s.closest("tr");
      if (!confirm(`Change ${tr.dataset.email}'s role to ${s.options[s.selectedIndex].text}? They will be signed out.`)) return users();
      try { await C.api("/api/admin/users/" + tr.dataset.id, { method: "PATCH", body: JSON.stringify({ role: s.value }) }); } catch (ex) { alert(ex.message); }
      users();
    }));
  }

  async function act(b) {
    const tr = b.closest("tr"), id = tr.dataset.id, a = b.dataset.act, email = tr.dataset.email || "";
    const ask = { disable: `Disable ${email}? They are signed out immediately.`, "reset-password": `Issue a one-time password for ${email}? Their sessions end.`,
      "reset-mfa": `Reset two-factor authentication for ${email}? Only do this after verifying their identity.`, reject: "Reject this account request?" };
    if (ask[a] && !confirm(ask[a])) return;
    try {
      if (a === "approve") await C.api(`/api/admin/users/${id}/approve`, { method: "POST", body: JSON.stringify({ role: tr.querySelector("[data-role]").value }) });
      else if (a === "disable" || a === "enable") await C.api(`/api/admin/users/${id}`, { method: "PATCH", body: JSON.stringify({ status: a === "disable" ? "disabled" : "active" }) });
      else {
        const r = await C.api(`/api/admin/users/${id}/${a}`, { method: "POST" });
        if (r.temporary_password) secret("One-time password", r.note, r.temporary_password);
      }
    } catch (ex) { alert(ex.message); }
    users();
  }

  async function createUser(e) {
    e.preventDefault();
    const msg = $("#cu-result");
    try {
      const r = await C.api("/api/admin/users", { method: "POST", body: JSON.stringify({ full_name: $("#cu-name").value.trim(),
        email: $("#cu-email").value.trim(), organization: $("#cu-org").value.trim(), role: $("#cu-role").value }) });
      e.target.reset(); flash(msg, "Account created for " + r.user.email + ".");
      secret("One-time password for " + r.user.email, r.note, r.temporary_password);
      users();
    } catch (ex) { flash(msg, ex.errors ? ex.errors.join(" ") : ex.message, false); }
  }

  // ---------------------------------------------------------------- audit
  function auditQuery() {
    const q = new URLSearchParams();
    if ($("#af-action").value.trim()) q.set("action", $("#af-action").value.trim());
    if ($("#af-actor").value.trim()) q.set("actor", $("#af-actor").value.trim());
    if ($("#af-outcome").value) q.set("outcome", $("#af-outcome").value);
    return q;
  }

  async function auditLog(more) {
    const esc = C.esc, q = auditQuery();
    $("#audit-export").href = "/api/audit/export.csv?" + q.toString();
    q.set("limit", "100");
    if (more && auditBefore) q.set("before_id", auditBefore);
    const rows = await C.api("/api/audit?" + q.toString());
    const html = rows.map((e) => `<tr class="${e.outcome !== "success" ? "row-bad" : ""}"><td>${e.id}</td><td>${when(e.created_at)}</td><td>${esc(e.actor || "—")}</td>
      <td><code>${esc(e.action)}</code></td><td><span class="pill ${e.outcome === "success" ? "ok" : "bad"}">${esc(e.outcome)}</span></td>
      <td>${esc(e.target_type ? e.target_type + ":" + e.target_id : "")}</td><td>${esc(e.ip)}</td>
      <td class="details">${esc(JSON.stringify(e.details))}</td></tr>`).join("");
    if (!more) $("#audit-table").innerHTML = `<table class="data-table"><thead><tr><th>#</th><th>Time</th><th>User</th><th>Action</th><th>Outcome</th><th>Target</th><th>IP</th><th>Details</th></tr></thead><tbody id="audit-body"></tbody></table>`;
    $("#audit-body").insertAdjacentHTML("beforeend", html);
    auditBefore = rows.length ? rows[rows.length - 1].id : auditBefore;
    $("#audit-more").classList.toggle("hidden", rows.length < 100);
  }

  async function verify() {
    const el = $("#audit-status");
    const r = await C.api("/api/audit/verify");
    flash(el, r.ok ? `✓ Integrity verified: ${r.events_checked} events, hash chain intact.` : `✗ Chain broken at event #${r.broken_at_id} — escalate as a security incident.`, r.ok);
  }

  // ---------------------------------------------------------------- wiring
  const loaders = { "page-account": account, "page-users": users, "page-audit": () => auditLog(false) };
  window.CardioAdmin = {
    init(ctx) {
      C = ctx;
      if (C.me().permissions.includes("users:read")) users().catch(() => {});  // pending-request badge
    },
    open(id) { (loaders[id] || (() => {}))().catch((ex) => console.warn(ex.message)); },
    clear() { ["#acct-profile", "#acct-sessions", "#pending-list", "#user-table", "#audit-table"].forEach((s) => { $(s).innerHTML = ""; }); ROLES = null; },
  };
  $("#acct-pw-form").addEventListener("submit", changePassword);
  $("#create-user-form").addEventListener("submit", createUser);
  $("#audit-filter").addEventListener("submit", (e) => { e.preventDefault(); auditBefore = null; auditLog(false); });
  $("#audit-more").addEventListener("click", () => auditLog(true));
  $("#audit-verify").addEventListener("click", verify);
  $("#secret-close").addEventListener("click", () => { $("#secret-value").textContent = ""; $("#secret-modal").classList.add("hidden"); });
})();
