# Security & compliance: CardioAI CVD Risk web app (Render)

This document covers the **Render web app** only:
- the sign-in gate, accounts, roles, sessions, audit trail and data handling.
- not the training and curation work inside the Mayo Clinic Platform workspace. That stays behind MCP's "data behind glass" controls (docs/RUNBOOK_MCP.md).

> **No software is "HIPAA compliant" or "SOC 2 certified" on its own.**
> - **HIPAA compliance** is an organizational status: Business Associate Agreements (BAAs), a risk analysis, policies, training, and operating the controls.
> - **SOC 2** is an independent auditor's report on controls operated over a period (Type II: usually 3–12 months).
>
> The app provides the *technical* controls both frameworks expect, and records the evidence an auditor asks for. The organizational items are listed in [section 5](#5-go-live-checklist-organizational-items).

## 1. Shared responsibility

| Layer | Who | What they cover |
|---|---|---|
| Hosting (compute, Postgres, network, TLS, disk encryption, backups) | **Render** | Render's security page: <br>• AES encryption at rest for databases, backups and secrets<br>• TLS 1.2+ in transit<br>• private networking<br>• point-in-time recovery<br><br>Render reports SOC 2 Type II and ISO 27001. Request the reports for vendor review. |
| HIPAA BAA with Render | **CardioAI (you)** | Render signs a BAA **only for HIPAA-enabled workspaces**, which need the **Scale or Enterprise** plan. **Pro is not enough.** <br>• Enabling it adds a 20 % usage fee and cannot be undone.<br>• Not available in the Singapore region.<br>• PHI must never appear in logs, build output, resource or table names, or render.yaml.<br><br>See <https://render.com/docs/hipaa-compliance>. |
| Application controls (this repo) | **CardioAI engineering** | Authentication, MFA, RBAC, sessions, audit, input handling and security headers. Listed below. |
| People and process | **CardioAI compliance / clinical lead** | <br>• Approving who gets access<br>• reviewing the audit log<br>• policies and training<br>• incident response<br>• vendor management |

**Until the Render BAA is signed, use the app only with synthetic or non-PHI inputs.**

## 2. What the app enforces

### Accounts and sign-in
- **Self-service account requests.** "Request an account" on the sign-in page:
  - The request records name, work email, organization and acceptance of the acceptable-use / confidentiality terms.
  - New accounts are **pending** and cannot sign in until an administrator **approves** the request and assigns a role. This is access authorization, HIPAA §164.308(a)(4).
  - `SIGNUP_ALLOWED_DOMAINS` can restrict requests to listed email domains. `SIGNUP_MODE=closed` turns requests off, so only administrators create accounts.
  - Duplicate requests get the same response, so the form cannot be used to find out who has an account.
- **Unique user IDs.** Each account is one person's email. There are no shared logins.
- **Passwords.** Policy follows NIST SP 800-63B:
  - at least 12 characters;
  - checked against common and breached-style passwords and against the user's own name or email;
  - no forced composition rules or periodic expiry.

  Passwords are stored as PBKDF2-HMAC-SHA256 with 600,000 iterations and a per-user salt. Older hashes are upgraded at the user's next sign-in.
- **Multi-factor authentication, required for every account** (`REQUIRE_MFA=true`):
  - TOTP (RFC 6238), so any authenticator app works.
  - Enrolment is forced at first sign-in.
  - Eight single-use recovery codes are issued; only their HMAC is stored.
  - TOTP secrets are encrypted at rest (Fernet, `MFA_ENCRYPTION_KEY`).
  - Codes cannot be replayed.
- **Brute-force protection:**
  - 5 failed attempts lock the account for 15 minutes (`LOCKOUT_*`);
  - per-IP limits apply to sign-in (100 per 15 min, since hospital users often share one outbound IP) and account requests (5 per hour).

  These limits are stored in Postgres, so they hold across all autoscaled instances.
- **No account enumeration.** "Unknown email" and "wrong password" give the same message, and the response time is equalized with a dummy hash.

### Sessions
- **Server-side sessions:**
  - The browser holds a random 256-bit token in an `HttpOnly; Secure; SameSite=Strict` `__Host-` cookie. Page scripts cannot read it.
  - The database stores only its SHA-256.
  - No tokens are kept in localStorage or sessionStorage.
- **Automatic logoff** (§164.312(a)(2)(iii)). A session ends after:
  - **15 minutes of inactivity** (`SESSION_IDLE_MINUTES`), enforced by the server; the page warns 60 seconds before;
  - **12 hours** at most (`SESSION_MAX_HOURS`), whatever the activity.
- **Revocation.** Sessions are revoked by:
  - sign-out;
  - a password change (other sessions);
  - an MFA change;
  - a role change or disable (all of the user's sessions);
  - an administrator's "Sign out everywhere".

  Users see their active sessions under **My account** and can end them.
- **Restricted setup sessions.** A user who must change a temporary password or enrol MFA gets a session that can do *only* that. A new full session is issued afterwards, which prevents session fixation.
- **CSRF protection:**
  - every state-changing request must carry the `X-CSRF-Token` bound to the session;
  - SameSite=Strict cookies.

### Role-based access control (least privilege / minimum necessary)

| Permission | Viewer | Clinician | Compliance auditor | Administrator |
|---|:-:|:-:|:-:|:-:|
| View calculators, model cards, performance | ✓ | ✓ | ✓ | ✓ |
| Run calculations (handles clinical inputs) | | ✓ | | ✓ |
| View *own* saved assessments | | ✓ | | ✓ |
| View user accounts and requests | | | ✓ | ✓ |
| Approve, create, change role, disable, unlock, reset password/MFA | | | | ✓ |
| View, verify, export the audit log | | | ✓ | ✓ |

- Every API route declares its permission (`app/rbac.py`, `require(...)`), and the server checks it on each request. Hiding controls in the UI is only a convenience.
- Nobody can see another user's assessments. There is no "all assessments" endpoint.
- Administrators cannot change their own role or status.
- The last active administrator cannot be demoted or disabled.

### Audit controls (§164.312(b); SOC 2 CC7.2)
- **What is logged.** Each event records who, what, when, from where (IP, user agent), the outcome and the target record. Logged events:
  - every sign-in, failure, lockout, MFA event, password change, sign-out and timeout;
  - every access denial and CSRF rejection;
  - every account request, approval, rejection, role or status change and reset;
  - every calculation and every view of saved assessments;
  - every read, verification or export of the audit log itself;
  - retention purges.
- **No clinical values** are ever written to the audit log or the application logs.
- **Tamper-evident.** Each event's SHA-256 covers its content and the previous event's hash. **Audit log → Verify integrity** re-checks the whole chain.
- **Append-only in Postgres.** A database trigger rejects `UPDATE`, `DELETE` and `TRUNCATE` on `audit_events` (Prisma migration `20261020000000_auth_rbac_audit`).
- **Export.** Auditors and administrators can export CSV for SIEM or archive. Formula characters are neutralized.

### Integrity, transmission and data handling
- **Transport.** TLS terminates at Render. The app adds:
  - HSTS (2 years);
  - a strict Content-Security-Policy (`script-src 'self'`, `frame-ancestors 'none'`);
  - X-Frame-Options DENY, nosniff, no-referrer, Permissions-Policy and COOP/CORP;
  - `Cache-Control: no-store` on the page and all API responses.
- **Database.** Postgres accepts connections only on Render's private network (`ipAllowList: []`). The schema is versioned by reviewed Prisma migrations.
- **Data minimization.** The calculator collects **no direct identifiers**: no name, MRN, date of birth or address. The sign-up terms tell users not to enter them. Stored assessments are still treated as PHI, because they hold age, sex and clinical values.
- **Retention.** Stored assessments are deleted after `ASSESSMENT_RETENTION_DAYS` (365 by default), checked at each start. Set `STORE_ASSESSMENTS=false` to store none. Audit events are kept; HIPAA documentation retention is 6 years, so archive exports.
- **Errors.** Validation and server errors never echo submitted values back. Interactive API docs are disabled in production.

## 3. Control mapping

### HIPAA Security Rule: technical safeguards (45 CFR §164.312)

| Standard | Implementation |
|---|---|
| (a)(1) Access control | Approval workflow, RBAC, per-route permission checks |
| (a)(2)(i) Unique user identification | One account per email; no shared accounts |
| (a)(2)(ii) Emergency access procedure | **Organizational.** Keep ≥ 2 administrators, and keep the recovery codes of a break-glass admin sealed. `ADMIN_USERNAME`/`ADMIN_PASSWORD` can create a new admin only if that email has no account yet. |
| (a)(2)(iii) Automatic logoff | 15-minute idle timeout (server-enforced), 12-hour maximum |
| (a)(2)(iv) Encryption and decryption | Render disk/backup encryption; MFA secrets encrypted in the app; TLS |
| (b) Audit controls | Hash-chained, append-only audit trail with viewer, verification and export |
| (c)(1) Integrity | Audit hash chain; DB triggers; versioned migrations; assessments stored as written |
| (d) Person or entity authentication | Password + mandatory TOTP MFA; lockout |
| (e)(1) Transmission security | TLS 1.2+, HSTS, private database network |

Supporting administrative safeguards (§164.308):
- **Information system activity review (a)(1)(ii)(D):** the audit-log page.
- **Access authorization and establishment (a)(4):** the approval workflow and role changes.
- **Log-in monitoring (a)(5)(ii)(C):** failed-sign-in and lockout events.
- **Password management (a)(5)(ii)(D):** the policy, user change and admin reset.

### SOC 2 Trust Services Criteria (Security, Confidentiality)

| Criterion | Implementation / evidence |
|---|---|
| CC6.1 Logical access security | Authentication, MFA, RBAC, sessions; this document; `tests/test_auth.py` |
| CC6.2 Provisioning based on authorization | Account requests need admin approval; `users.approved` events |
| CC6.3 Modify / remove access | Role change and disable revoke sessions immediately; `users.updated` events; quarterly access review (§5) |
| CC6.6 Boundary protection | CSP and security headers; private-network Postgres; CORS off by default |
| CC6.7 Data in transmission | TLS, HSTS, Secure cookies |
| CC7.2 Monitoring for anomalies | Audit trail of failures, lockouts and denials; CSV export for SIEM |
| CC8.1 Change management | Pull requests; CI runs pytest plus Prisma validation, migration and drift checks; Render deploys only after checks pass (`autoDeployTrigger: checksPass`) |
| CC9.2 Vendor management | Render SOC 2 / ISO 27001 reports; Render BAA |
| A1.2 Backups and recovery | Render Postgres backups and PITR. **Test a restore** (§5). |
| C1.1 / C1.2 Confidential information, disposal | Data minimization; retention purge; no clinical data in logs |

## 4. Configuration reference

All settings are environment variables, set in the Render dashboard or in `render.yaml`. Defaults are in `app/config.py`.

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | generated | Signs MFA challenges; derives CSRF tokens. Same value on all instances. |
| `MFA_ENCRYPTION_KEY` | generated | Encrypts TOTP secrets. **Do not lose it**: losing it forces every user to re-enrol MFA. |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | — | First administrator. Created once. |
| `SIGNUP_MODE` | `approval` | `approval` or `closed` |
| `SIGNUP_ALLOWED_DOMAINS` | *(any)* | Comma-separated domains allowed to request accounts |
| `REQUIRE_MFA` | `true` | Keep it on |
| `SESSION_IDLE_MINUTES` / `SESSION_MAX_HOURS` | `15` / `12` | Automatic logoff |
| `LOCKOUT_THRESHOLD` / `LOCKOUT_MINUTES` | `5` / `15` | Account lockout |
| `IP_LOGIN_LIMIT` / `IP_SIGNUP_LIMIT` | `100`/15 min, `5`/hour | Per-IP throttles |
| `PASSWORD_MIN_LENGTH` | `12` | |
| `STORE_ASSESSMENTS` / `ASSESSMENT_RETENTION_DAYS` | `true` / `365` | |
| `APP_ENV` | `production` on Render | Turns on Secure cookies and HSTS, and turns off `/docs` |

## 5. Go-live checklist (organizational items)

**Before any real patient information is entered:**
1. **Render BAA.** Upgrade the workspace to **Scale or Enterprise**, sign the BAA (Workspace Settings → Compliance), and enable HIPAA. Keep the service out of the Singapore region.
2. **Other BAAs.** Get BAAs with any other service that could see PHI: email, support and monitoring tools. Don't stream Render logs to a vendor without a BAA.
3. **Risk analysis** (§164.308(a)(1)). Document threats, likelihood and mitigations for this system, and review it each year.
4. **Policies.** Write and approve policies for:
   - access control and approval criteria;
   - acceptable use (matching the sign-up terms);
   - incident response and breach notification (HIPAA: without unreasonable delay, ≤ 60 days);
   - sanctions;
   - contingency / backup;
   - change management;
   - vendor management;
   - data retention.
5. **Workforce training.** Train staff on HIPAA and security at onboarding and every year, and keep the records.
6. **At least two administrators**, both with MFA. Record a break-glass procedure.
7. **Restrict sign-ups** with `SIGNUP_ALLOWED_DOMAINS` if only your organizations should request access. Verify each requester's identity and role before approving.
8. **Models.** Set `ALLOW_DEMO_MODELS=false` once the Mayo-trained bundle is live.

**While operating (this is the evidence for SOC 2 Type II):**

| Cadence | Task | Evidence |
|---|---|---|
| Weekly | Review the audit log for failures, lockouts, denials and unusual hours. Run **Verify integrity**. | Exported CSV plus a reviewer note |
| Monthly | Archive an audit CSV export to immutable storage (6-year retention) | Archive listing |
| Quarterly | Access review: confirm every active account and role is still needed; disable leavers | Signed review |
| Quarterly | Restore a Postgres backup to a scratch database and run the app's tests against it | Restore log |
| Each change | PR review, green CI, deploy after checks pass | GitHub PR history |
| Yearly | Penetration test; risk-analysis update; policy review; training | Reports |

For a SOC 2 report, engage an independent CPA firm. Start with a readiness assessment, then a Type I report (design at a point in time), then a Type II report (operation over 3–12 months).

## 6. Known limitations and next steps

- **No email-based self-service password reset.** There is no email service. Administrators issue one-time passwords. Adding one would need a provider covered by a BAA.
- **No SSO (SAML / OIDC) yet.** For hospital deployments, federating with the institution's identity provider (Entra ID, Okta) moves MFA and leaver handling there. Recommended next step.
- **No WebAuthn / passkeys yet.** TOTP is the second factor.
- **IP limits trust Render's `X-Forwarded-For`** (`--proxy-headers`). Don't expose the service except through Render's load balancer.
- **Regulatory.** Patient-specific risk output may be Software as a Medical Device (see the README's regulatory note). That is separate from HIPAA and SOC 2.
