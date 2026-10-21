# CardioAI — AI-trained CVD Risk Calculators

Five calculators: **AMI, Stroke, Heart Failure, Sudden Cardiac Death, Atrial Fibrillation**. Each gives a 30-day, 1-year and 5-year risk.

- The AI models are trained on de-identified **Mayo Clinic Platform** records inside your Platform_Discover workspace.
- The app is a FastAPI backend plus a dynamic frontend, deployed on **Render.com**.
- Published equations run alongside the AI as a reference: Pooled Cohort Equations, CHARGE-AF, CHA₂DS₂-VASc and the ACC/AHA/HRS ICD criteria.
- **Adults only, aged 18–89.** Pediatric patients are excluded from training, validation and use.
  - The upper bound is 89 because de-identified data top-codes ages 90 and over.
  - The criteria are in `cardioai_core/schema.py` → `ELIGIBILITY` and on the models sheet's Eligibility tab.
  - The cohort SQL, the automatic audit and the app all use them; the app refuses an age under 18 or over 89.

```
cardioai/
├── app/                    Render web service (FastAPI)
│   ├── main.py             API: /api/meta, /api/predict, /api/models, /api/assessments; security headers
│   ├── routes_auth.py      Account requests, sign-in (password + TOTP MFA), password change, sessions
│   ├── routes_admin.py     Approvals, roles, disable/unlock/reset, audit log view / verify / export
│   ├── auth.py             Server-side sessions (HttpOnly cookie), CSRF, idle timeout, lockout, password policy
│   ├── rbac.py             Roles → permissions (viewer, clinician, auditor, admin)
│   ├── mfa.py              RFC 6238 TOTP, recovery codes, encrypted MFA secrets
│   ├── audit.py            Append-only hash-chained audit trail
│   ├── db.py               Users, sessions, audit events, assessments (Render Postgres; SQLite locally)
│   ├── recommendations.py  Guideline-anchored clinical considerations
│   └── static/             Frontend (form is generated from /api/meta)
├── cardioai_core/          Shared by app AND training. Same features at train and predict time
│   ├── schema.py           Calculators, endpoints, horizons, input fields, feature builder
│   ├── equations.py        Published equations (unit-tested against published examples)
│   └── bundle.py           Model bundle loader, prediction, per-patient explanations
├── training/               Runs INSIDE Mayo Clinic Platform_Discover
│   ├── codesets.py         History ICD, CPT, ICD-10-PCS, LOINC, ATC code lists (clinical review point)
│   ├── codelists.py        Candidate outcome code rules → code_lists/candidate_codes.csv (every code, described)
│   ├── extract.py          OMOP CDM → raw curation dataset (candidates + evidence; Spark SQL)
│   ├── sql/                Rendered SQL for review
│   ├── nlp.py              Clinical mention detection with negation / history / hypothetical context
│   ├── labeling.py         Episodes, labelling functions, label model, subtypes (auto-labelling)
│   ├── audit.py            Automatic data + label audit
│   ├── imaging_nlp.py      Coronary calcium + LVEF from radiology/echo report text
│   ├── train.py            Trains every endpoint × horizon from an approved release → bundle + VALIDATION_REPORT.md
│   ├── synthetic.py / synthetic_raw.py   Synthetic data (pipeline testing only)
├── curation/               Curation workbench — WORKSPACE ONLY, never deployed to Render
│   ├── pipeline.py         CLI: init, user-add, label, audit, status, review, approve, waive, release, verify-log, refit
│   ├── server.py + static/ Web workbench: pipeline, audit review, blinded case review, lead adjudication, metrics, audit trail
│   ├── adjudication.py     Sampling, blinded double reads, consensus, κ / PPV / NPV / annotation accuracy
│   ├── gates.py · release.py · store.py (hash-chained audit log) · governance.py · simulate.py (synthetic only)
├── models/synthetic-demo/  Demo bundle (trained from a synthetic, SIMULATED-adjudication release). NOT for clinical use
├── tests/                  pytest: equations, NLP, API, all 5 calculators
└── render.yaml             Render Blueprint (web service + Postgres)
```

---

## 1. Label, review and adjudicate, then train (inside your Mayo Clinic Platform workspace)

Patient-level data never leaves Mayo's environment. Everything in this section runs **inside the Discover workspace**, including the curation workbench web app. **Never deploy `curation/` to Render.** The only thing exported is the trained **model bundle**: weights plus aggregate metrics, with small cells (<11) suppressed.

```
 OMOP CDM ──extract──▶ raw/ ──1 auto-label──▶ label run ──2 audit──▶ 3 data-scientist review
                                                                          │ approve = creates sample
             model bundle ◀──train── locked release ◀──5 gates + 2 sign-offs── 4 cardiologist adjudication
```

| Stage | What happens | Who |
|---|---|---|
| **1. Auto-label & annotate** | Post-index diagnosis codes become candidate events (codes within 7 days form one episode). Procedures with no nearby code (PCI, CPR, thrombolysis, cardioversion) add *evidence-only* candidates to catch missed codes. About 25 labelling functions score the evidence: troponin rise/fall vs the 99th-percentile URL, BNP/NT-proBNP, head imaging, reperfusion, resuscitation, visit type, diagnosis position, and note text with negation / history / rule-out context. A transparent log-odds model gives *confirmed / uncertain / rejected* plus a subtype (STEMI/NSTEMI/type 2, ischemic/hemorrhagic/TIA, HFrEF/HFmrEF/HFpEF, VF/VT, post-operative AF). LVEF and calcium are extracted from imaging reports. | automatic |
| **2. Automatic audit** | 22 checks: schema, duplicates, plausibility, unconverted units, core and differential missingness by sex/race, subgroup sizes, follow-up adequacy, leakage (events on/before index), events after death, auto-label uncertainty, labelling-function conflict and coverage, 5-year incidence vs expected range, ICD-9→10 drift, NLP extraction yield. Each check is pass / warn / fail. | automatic |
| **3. Data scientist review** | Every warning acknowledged, every failure waived with a written justification (≥20 characters) or fixed. Approval locks the review and creates the adjudication sample in the same transaction. | data scientist |
| **4. Cardiologist adjudication** | Per outcome family: **all** uncertain label-relevant candidates (cap 300), plus 50 random auto-confirmed (measures PPV) and 50 auto-rejected (measures NPV), plus 100 imaging reports for LVEF/CAC checks. Event cases get **two independent blinded reads**: no auto-label, no sampling reason, no other reader. Readers apply the standard definitions (4th UDMI, AHA/ASA stroke, Universal HF, AHA/ACC/HRS SCD, ECG-documented AF ≥30 s), tick the criteria met, and set subtype and date. Disagreements go to a **lead cardiologist who was not a reader** and sees both reads. | cardiologists, lead cardiologist |
| **5. Gates + release** | See the gates list below. The release then writes `dataset.parquet` + `release.json` (SHA-256, approvals, gates, governance, metrics). | data scientist + lead cardiologist |

**Release gates**
- Audit fully resolved.
- Dataset approved by the data scientist.
- All adjudication cases final.
- For each outcome family:
  - Cohen's κ ≥ 0.6;
  - PPV of auto-confirmed labels ≥ 85%;
  - NPV of auto-rejected labels ≥ 85%;
  - indeterminate cases ≤ 15%.
- LVEF/CAC extraction accuracy ≥ 90%, weighted by sampling probability.
- Audit trail hash chain intact.
- Sign-offs from a data scientist **and** a lead cardiologist (two different people).

Gate failures can only be waived by the accountable role, with a justification. Thresholds are in `data/governance.json`; its hash is recorded in every label run and release.

**Final labels**
- An adjudicated decision overrides the auto-label.
- Unadjudicated candidates keep their auto-label.
- Uncertain candidates that weren't adjudicated, and *cannot determine* decisions, are **unresolved**. Training excludes a patient from a horizon when an unresolved candidate falls inside it before any confirmed event, rather than guessing.
- By default, type 2 MI does not count as an AMI event, and post-operative AF does not count as new-onset AF (`definitions` in governance).

```bash
# in the Discover workspace
pip install -r requirements-training.txt
python -m training.extract --render-only                     # review SQL (training/sql/) — no data access
python -m training.extract --cdm <catalog>.<omop_schema> --native-dataset <catalog>.<native_schema> --out data/raw

python -m curation.pipeline init --data-dir data --provenance mayo-clinic-platform
python -m curation.pipeline user-add dsmith  --role data_scientist    --full-name "D Smith"
python -m curation.pipeline user-add drjones --role cardiologist      --full-name "Dr A Jones"
python -m curation.pipeline user-add drlee   --role cardiologist      --full-name "Dr B Lee"
python -m curation.pipeline user-add drchen  --role lead_cardiologist --full-name "Dr C Chen"
python -m curation.pipeline label --data-dir data            # 1
python -m curation.pipeline audit --data-dir data            # 2
python -m curation.server --data-dir data --port 8090        # 3–5 in the browser (workspace-only)
python -m curation.pipeline status  --data-dir data
python -m curation.pipeline release --data-dir data --as dsmith   # or the button in the workbench
python -m training.train --release data/releases/<REL-id> --out bundles/mcp-$(date +%Y-%m)
tar -czf mcp-bundle.tar.gz -C bundles mcp-$(date +%Y-%m)     # submit for Mayo export review
```

**Workbench access.** Open the workbench through the workspace's own browser or port forwarding, for example a JupyterLab server proxy at `…/proxy/8090/`. All URLs are relative, so this works behind a proxy. Bind it to `127.0.0.1` and don't expose it outside the workspace. Roles: `admin`, `data_scientist`, `cardiologist`, `lead_cardiologist`. You need at least two cardiologists for double reads, plus one lead cardiologist to resolve disagreements.

**Audit trail.** Every login, label run, audit, acknowledgement, waiver, case view, read, approval and release is appended to a hash-chained log. Editing or deleting any entry breaks the chain. The workbench shows this, and it is a release gate.

**Improving the label model.** After adjudication, run `python -m curation.pipeline refit-label-model --out lm.json`. It proposes weights learned from the cardiologists' decisions and reports cross-validated AUC against the current weights. To use them, run `label --label-model lm.json`. That creates a **new** label run, which needs its own audit, review and adjudication sample, so the measured PPV/NPV always describe the labels actually used.

**Training.** `train.py` refuses Mayo-provenance data without a release, refuses a release whose dataset hash doesn't match, and records the label governance in the model bundle. The Render app then shows *"Labels: cardiologist-adjudicated (n cases, κ range)"*.

**Step-by-step execution in the workspace:** see [`docs/RUNBOOK_MCP.md`](docs/RUNBOOK_MCP.md).

**Before going live, review with your team:**
- the expanded candidate outcome codes in `training/code_lists/candidate_codes.csv` (334 codes with descriptions; rules in `training/codelists.py`) and the other code lists in `training/codesets.py` (ICD, CPT, ICD-10-PCS, LOINC; the `PRIMARY_STATUS` concept IDs and the default troponin URLs);
- the labelling-function weights in `training/labeling.py`;
- the governance thresholds and definitions.

**Known limitations**
- Patients with **no** candidate code and no trigger procedure can't be found, so sensitivity of the code + procedure net is not measured.
- Out-of-hospital SCD without an encounter is under-captured.
- Rule-based NLP needs extending to your report templates. The annotation review measures how well it works.

### Mayo Clinic Platform environment (confirmed by MCP, A. Resnick, 14 Oct 2026)

| Topic | Answer from Mayo Clinic Platform | What this build does |
|---|---|---|
| Lab definitions | Mayo Clinic Laboratories catalogue ("Clinical & Interpretive", "Fees & Codes") | Troponin T Gen 5 (TRPS, LOINC 67151-1): sex-specific URL ≤15 ng/L men, ≤10 ng/L women, plus Mayo's rise rule (≥10 ng/L in 2 h or ≥12 ng/L in 6 h). NT-proBNP (PBNP1, LOINC 83107-3): age/sex reference limits shown to adjudicators. |
| Browser apps in the workspace | Yes, on localhost via JupyterLab | Curation workbench: `python -m curation.server --host 127.0.0.1 --port 8090`. URLs are relative, so it works behind the Jupyter proxy. |
| pip installs | Allowed | `pip install -r requirements-training.txt` |
| Docker | Allowed, but `docker pull` doesn't work; images must be self-contained | Not needed: the pipeline is pure Python. |
| Desktop (VNC), 3D Slicer, ITK-SNAP, MONAI Label | Installed / pip-installable | Not needed for these calculators. They're only relevant if image-level outlining is added later. |
| Readers | 5 extra annotator seats, each needing a **cardioailive.com** e-mail. **Mayo staff cannot annotate.** | Enforced: cardiologist accounts need `@cardioailive.com`, at most 5 active readers (`reader_policy` in governance). Plan staffing with `python -m curation.pipeline plan-workload`. |
| Compute | Shared A100 40 GB reservations | Not needed: the models are gradient-boosted trees on CPU (minutes). |
| Report text | Radiology in `FACT_RADIOLOGY` (also OMOP `note`); echo in `FACT_ECHO_TEST.ECHO_NOTE` | Radiology comes via OMOP `note`. Echo comes from `FACT_ECHO_TEST` with `--native-dataset`; three columns are still to confirm (below). Duplicates across sources are removed. |
| Existing annotations | None. Labels come from radiology reports and echo notes. | Matches the design: report/echo-derived labels, verified by cardiologists. |
| Public imaging datasets | Keep outside the environment | Not used. |

**Still to confirm with Mayo Clinic Platform** (training session):
1. In `FACT_ECHO_TEST`: the patient key column, the echo date/time column, and how that key maps to OMOP `person_id`. Set these in `training/native_sources.json`. The extract refuses to run until they are set.
2. The Spark catalog/schema names for the OMOP tables and the native `FACT_*` tables, and whether `note` holds all radiology reports.
3. Which `condition_status` / `condition_type` concepts mark the primary diagnosis (`PRIMARY_STATUS`).
4. The date Mayo switched from conventional troponin T to Gen 5, and whether `measurement.range_high` is populated.
5. How complete death data is (deaths outside Mayo), which matters for sudden cardiac death.
6. Model export review: the cell-suppression threshold (this build suppresses counts <11) and the approval steps.
7. Whether the 5 annotator seats are separate from the data scientists' seats.

### Try the whole pipeline on synthetic data (no patient data)

```bash
python -m training.synthetic_raw --n 60000 --out demo/raw          # raw extract format + hidden truth
python -m curation.pipeline init --data-dir demo --provenance synthetic-demo
# add users as above, then: label, audit, review/approve in the workbench (or CLI `review` / `approve --as`)
python -m curation.pipeline simulate-adjudication --data-dir demo   # SYNTHETIC ONLY: simulated readers from hidden truth
```
**Training the team.** `python -m curation.training_kit` runs the Label & Annotation Training Program on a separate synthetic training folder:
- the practice and calibration case sets, read in the real workbench;
- seeded disagreements for the lead cardiologist;
- the planted-fault audit exercise for data scientists;
- certification scoring.

See [docs/TRAINING_KIT.md](docs/TRAINING_KIT.md).

The simulator refuses to run unless the data folder carries the `SYNTHETIC` marker. Any label run or release it touches is permanently flagged `simulated_adjudication`, and the workbench, release report, model bundle and calculator app all say so.

## 2. Deploy on Render

1. Push this folder to a **private** GitHub repo.
2. In Render: **New + → Blueprint** → select the repo. `render.yaml` creates the web service and Postgres.
3. In the dashboard, set `ADMIN_USERNAME` (the first administrator's email) and `ADMIN_PASSWORD` (12+ characters). The account is created on first boot.
4. Open the site and sign in as that administrator. You'll be asked to set up two-factor authentication (any authenticator app) and shown 8 recovery codes.
5. Users go to **Request an account** on the sign-in page. Their request waits in **Users → Account requests** until an administrator approves it and picks the role. Administrators can also create accounts directly, which gives a one-time password.

**Accounts and access control** (details and the HIPAA / SOC 2 control map: [docs/SECURITY_COMPLIANCE.md](docs/SECURITY_COMPLIANCE.md)):

| Role | Can |
|---|---|
| Viewer | See calculators, model cards and performance. Cannot enter patient data. |
| Clinician | Run calculations; see their own saved assessments. |
| Compliance auditor | Read the audit log (view, verify, export) and the user list. No clinical data. |
| Administrator | Everything, including approving requests, roles, disable/unlock, password and MFA resets. |

Every account needs MFA. Sessions end after 15 minutes of inactivity. Accounts lock for 15 minutes after 5 failed sign-ins. Every sign-in, access denial, account change and calculation is written to the audit log.

> **HIPAA.** Render signs a BAA only for HIPAA-enabled workspaces, which need the **Scale or Enterprise** plan. Pro is not enough. Until the BAA is signed, use the app only with synthetic or non-PHI inputs.

**Switching from demo to Mayo-trained models.** Either option works:
- Commit the bundle under `models/mcp-YYYY-MM/` and set `MODEL_DIR` to that path, **or**
- Host `mcp-bundle.tar.gz` in private storage (e.g. a signed GCS/S3 URL) and set `MODEL_BUNDLE_URL` (plus `MODEL_BUNDLE_TOKEN` if needed).

Then set `ALLOW_DEMO_MODELS=false`. The demo banner disappears automatically once the bundle's provenance is `mayo-clinic-platform`. Confirm your Mayo agreement allows hosting the exported model this way.

### Database schema (Prisma)

Prisma owns the Render Postgres schema. The FastAPI app reads and writes through SQLAlchemy and never creates tables in production (`DB_SCHEMA_MANAGER=prisma`).

| File | Purpose |
|---|---|
| `prisma/schema.prisma` | Tables `users`, `auth_sessions`, `audit_events`, `auth_throttle` and `assessments`. Keep in step with `app/db.py`; `tests/test_postgres.py` checks it. |
| `prisma/migrations/` | Versioned SQL migrations, applied by `prisma migrate deploy`. |
| `prisma.config.mjs` | Prisma 7 config; reads `DATABASE_URL`. |
| `scripts/db_migrate.py` | Render `preDeployCommand`. It migrates a fresh database, or adopts tables created by the earlier app version (rows kept) and then migrates. |

**Changing the schema:**
1. Edit `prisma/schema.prisma` and `app/db.py` together.
2. Run `npm run db:new -- --name <change>` against a dev database. This writes a new migration folder.
3. Commit both files.

Render applies the migration before the next deploy. CI fails if the migrations and the schema drift apart.

## 3. Run locally

```bash
pip install -r requirements.txt
ADMIN_USERNAME=admin@example.org ADMIN_PASSWORD='a-long-local-passphrase' uvicorn app.main:app --reload
# open http://localhost:8000 — sign in, enrol MFA; API docs at /docs (disabled when APP_ENV=production)
pip install -r requirements-dev.txt
pytest -q                                  # 81 tests (+4 Postgres tests when TEST_DATABASE_URL is set)

# retrain the demo bundle
pip install pyarrow
python -m training.synthetic --out data/synthetic.parquet
python -m training.train --data data/synthetic.parquet --provenance synthetic-demo --min-events 25 --out models/synthetic-demo
```

## What changed from the previous version

- **Risk numbers.** Before, they came from fixed multipliers in the browser. The C-statistic was `Math.random()` and the CIs were constants. Now every number comes from a trained model or a published equation, and the C-statistic shown is the measured hold-out value.
- **Logins.** Before, they were hard-coded in page source. Now:
  - accounts are requested and then approved, and every account has MFA;
  - roles control access;
  - sessions are server-side and time out;
  - all activity is audited.

  **Retire the old passwords.**
- **New features:**
  - per-patient "what drives this risk" explanations;
  - separate endpoints (ischemic/hemorrhagic/TIA, HFrEF/HFpEF);
  - optional imaging inputs (CAC, LVEF);
  - assessment history;
  - input validation.

## Regulatory note

Software that produces patient-specific risk to guide treatment can be a medical device. Before clinical use, get regulatory review (FDA CDS guidance / SaMD) and Mayo Clinic Platform_Validate.
