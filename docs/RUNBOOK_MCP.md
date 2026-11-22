# Runbook — training the 5 CVD calculators on Mayo Clinic Platform

Step-by-step execution inside the **Mayo Clinic Platform_Discover workspace**: from an empty workspace to a signed, cardiologist-adjudicated training release, trained models, and a model bundle approved for export to the Render app.

> **Data rule — read first.** Patient-level data (extracts, notes, labels, `data/`, `curation.db`, releases) **never leaves the workspace**. That includes no copy-paste, no screenshots outside the workspace, and no commits to GitHub. **The GitHub repository is public.** Only the exported **model bundle** (weights + aggregate metrics, cells < 11 suppressed) leaves, and only after Mayo Clinic Platform export review.

**Roles used below**

| Role | Who | Workspace seat |
|---|---|---|
| **DS**: data scientist | CardioAI data scientist (runs every command) | existing seat |
| **CL**: clinical lead / lead cardiologist | External cardiologist, `@cardioailive.com` | annotator seat |
| **R**: reader cardiologists (2–4) | External cardiologists, `@cardioailive.com` | annotator seats (max 5 incl. CL) |
| **MCP**: Mayo Clinic Platform | Technical Success Manager | — |

Each phase ends with a **✅ Done when** checkpoint and lists **⛔ Stop if** conditions. Record completion dates in the sign-off table at the end.

---

## Phase 0 — Accounts and prerequisites (≈1 week, before any data work)

| # | Step | Owner |
|---|---|---|
| 0.1 | Recruit the reader team: **1 lead cardiologist + 2–4 cardiologists**. Mayo Clinic staff **cannot** annotate in this environment. | CardioAI |
| 0.2 | Request workspace seats for each reader with a **`@cardioailive.com`** e-mail (up to 5 annotator seats). Ask MCP whether these are separate from data-scientist seats. | DS → MCP |
| 0.3 | Get answers to the open questions (see [Appendix A](#appendix-a--questions-for-mayo-clinic-platform)). Phase 2 answers most of them from the data. | DS → MCP |
| 0.4 | The clinical lead reads the **Clinician Lead Guide** (shared separately) and the **Models Sheet** ([`docs/CardioAI_Models_Sheet.xlsx`](CardioAI_Models_Sheet.xlsx)) and agrees the outcome definitions, subtype rules and gate thresholds. | CL |
| 0.5 | Book reader time. Phase 6 gives the estimate; plan for roughly **50–100 hours per reader** over 3–6 weeks. | CardioAI |

✅ **Done when** every reader can log in to the workspace and the CL has approved the definitions.

---

## Phase 1 — Set up the workspace environment (≈½ day) · DS

**1.1 Get the code into the workspace.** The workspace cannot reach github.com; the repository is mirrored in the workspace **Gitea**. Copy the HTTPS clone address from the repo page in Gitea, then in a JupyterLab **Terminal**:
```bash
git clone <Gitea HTTPS address of ai_powered_cvdrisk_cal>
cd ai_powered_cvdrisk_cal
git checkout <release-tag-or-commit>        # always run a pinned, reviewed commit
```
If the commit is missing, the Gitea mirror is behind GitHub: sync it (repo **Settings → Mirror settings → Synchronize now**) or ask the TSM. In a notebook, prefix shell commands with `!` and use `%cd` to change folder.

**1.2 Python environment.** pip installs are allowed. On MCP the OMOP data is in **BigQuery**, so the extraction uses `--engine bigquery` (no Spark needed).
```bash
pip install -r requirements-training.txt pytest
python -c "from google.cloud import bigquery; c = bigquery.Client(); print(c.project, [d.dataset_id for d in c.list_datasets()])"
```
Expect your project id (`mcp-acc-…`) and its writable dataset `temp_tables`.
**1.3 Self-test on synthetic data.** This touches no patient data.
```bash
pytest -q                                   # expect: all tests pass (Spark tests run if pyspark is present)
```
**1.4 Create the working folder** and keep it out of git:
```bash
mkdir -p data && echo "data/" >> .git/info/exclude
```

⛔ **Stop if** tests fail or BigQuery does not connect (or, on a Spark workspace, Spark does not start). Send the error to MCP; don't continue on a broken environment.
✅ **Done when** `pytest -q` passes in the workspace.

---

## Phase 2 — Locate the data and confirm settings (≈1 day) · DS (+ MCP)

**2.1 Find the dataset.** On MCP the OMOP CDM and the Mayo native tables are BigQuery views in **one** read-only dataset in the data project (Mayo's examples: `BQClient('mcp-ss-data-p-5o6i.vw_accelerate2605_core_v1')`, 193 tables including `person`, `note`, `DIM_PATIENT`, `FACT_ECG`, `FACT_ECHO_TEST`, `FACT_DIAGNOSIS`, `FACT_MORTALITY_STATUS`, `FACT_RADIOLOGY`). To list them (names only):
```python
from google.cloud import bigquery
c = bigquery.Client()
print(sorted(t.table_id for t in c.list_tables("mcp-ss-data-p-5o6i.vw_accelerate2605_core_v1")))
```
```bash
export OMOP=mcp-ss-data-p-5o6i.vw_accelerate2605_core_v1
export NATIVE=$OMOP          # native FACT_/DIM_ tables are in the same dataset
```
Intermediate tables (cohort, candidate codes, procedures) are written to your project's `temp_tables` dataset and expire after 7 days. Nothing leaves the workspace.

**2.2 Run the preflight** (read-only; prints aggregate counts only):
```bash
python -m training.preflight --engine bigquery --cdm $OMOP --native-dataset $NATIVE --out data/preflight.json
```
(On a Spark workspace: drop `--engine bigquery` and use Spark `catalog.schema` names.)
**2.3 Read `data/preflight.json` and set the configuration:**

| Preflight section | Decide / set | Where |
|---|---|---|
| `omop_tables` | All `true`. Otherwise fix `--cdm`. | — |
| `omop_person_column_candidates` | The OMOP `person` column with the highest match rate holds `PATIENT_CLINIC_NUMBER`, the person-level key across Mayo record systems (MCP, 21 Oct 2026). Expect ≈ 1.0. | Built in: `person_source_value` (100% match on MCP, 22 Nov 2026) |
| `dim_patient` | `dks_with_several_clinic_numbers` and `clinic_numbers_with_several_unmerged_rows` should both be **0**. Rows with `PATIENT_MERGED_FLAG = 'N'` give one row per person. | — |
| `native_tables` → columns of `FACT_ECHO_TEST`, `FACT_DIAGNOSIS`, `FACT_MORTALITY_STATUS`, `FACT_ECG` | Confirmed on MCP (22 Nov 2026) and built in: echo `PATIENT_DK` / `ECHO_PERFORMED_DTM` / `ECHO_TEST_FPK`; diagnosis `PATIENT_DK` / `DIAGNOSIS_DTM` / `PRIMARY_DIAGNOSIS_FLAG = 'Y'`; mortality `PATIENT_CLINIC_NUMBER` / `CONSENSUS_DEATH_DATE` where `CONSENSUS_STATUS = 'deceased'`; ECG as in the ECG deck. | Nothing to set |
| `diagnosis_code_lookup`, `diagnosis_code_systems` | `FACT_DIAGNOSIS` holds `DIAGNOSIS_CODE_DK`; the ICD code comes from `DIM_DIAGNOSIS_CODE.DIAGNOSIS_CODE` (built in). Check `with_code` ≈ `rows` and `i21_rows` > 0, and note the code systems (ICD-9 / ICD-10 / other). | Nothing to set |
| `primary_diagnosis_flag_values` | The value of `PRIMARY_DIAGNOSIS_FLAG` that means *primary* (e.g. `Y`). | `diagnosis.primary_value` |
| (after setting the above) re-run → `echo_person_bridge`, `diagnosis_person_link`, `mortality_person_link` | Each `match_rate` should be **≥ 0.95**. `clinic_numbers_with_several_person_ids` must be **0**, or the same patient could land in both the training and test sets: stop and ask MCP. | — |
| `mortality_by_year` | Deaths per year (Mayo record + third-party death data, Veritas). Cause of death is **not** populated. | — |
| `condition_status` | Fallback only (primary now comes from `FACT_DIAGNOSIS`): which OMOP concept IDs mean *primary diagnosis*, if any. | `PRIMARY_STATUS` in `training/codesets.py` |
| `troponin_by_year` | Year of the switch to Gen 5 hs-TnT (LOINC 67151-1) and whether `range_high` is filled. If not, the Mayo default upper limits are used (TRPS: men ≤15, women ≤10 ng/L). | note in the release log; adjust `DEFAULT_URL_NG_L` only if Mayo differs |
| `imaging_notes_by_year` | Confirms radiology/echo text coverage over 2010–2025. | — |

**2.4 Clinical review of code lists.** The CL and DS review the code lists together.
- **Candidate outcome codes:** every code is listed with its official description in `training/code_lists/candidate_codes.csv`, which is also the models sheet's *Candidate codes* tab. It includes the 20 codes deliberately excluded and why. To change the list, edit `RULES` in `training/codelists.py`, run `python -m training.codelists`, and commit both files. CI fails if they drift apart.
- **Other code lists:** history codes, CPT/ICD-10-PCS procedures, LOINC and ATC are in `training/codesets.py`.

Record the review in the commit message of any change.

**2.5 Set governance** (thresholds and definitions) in `data/governance.json` after `init` (Phase 4). Defaults: κ ≥ 0.6, PPV/NPV ≥ 85%, annotation accuracy ≥ 90%, indeterminate ≤ 15%, type 2 MI **excluded** from AMI, post-operative AF **excluded** from new-onset AF.

⛔ **Stop if** any native match rate is below 0.9, a clinic number maps to two OMOP person_ids, or the primary-diagnosis value can't be identified. Resolve with MCP first.
✅ **Done when** the preflight shows `native_unconfirmed: []`, the match rates are ≈ 1.0, death years are filled, and the code lists are signed off by the CL.

---

## Phase 3 — Extract the raw curation dataset (≈1–6 h) · DS

**3.1 Review the SQL** (no data access):
```bash
python -m training.extract --render-only --cdm $OMOP --native-dataset $NATIVE   # → training/sql/*.sql
```
**3.2 Run the extraction.** On MCP (BigQuery), the same queries are translated to BigQuery SQL (`training/bq_engine.py`); intermediate results go to `temp_tables`:
```bash
python -m training.extract --engine bigquery --cdm $OMOP --native-dataset $NATIVE \
       --index-start 2012-01-01 --index-end 2019-12-31 --out data/raw
```
The rendered SQL in 3.1 is Spark SQL; the BigQuery version is produced from it at run time (`training/bq_engine.py`). On a Spark workspace, drop `--engine bigquery`; give the Spark driver **≥ 32 GB** for `toPandas()` of the notes table. The final extract files need a similar amount of notebook memory on BigQuery. The extraction refuses to run while any `CONFIRM:` setting remains.

**3.3 Sanity-check row counts:**
```bash
python - <<'EOF'
import pandas as pd, glob
for f in sorted(glob.glob("data/raw/*.parquet")): print(f, len(pd.read_parquet(f)))
EOF
```
Expect hundreds of thousands of patients in `features`, and every evidence table non-empty.

**3.4 Check eligibility (adults only).** Open `data/raw/cohort_attrition.json`. It gives the number of patients each criterion removed: pediatric-only, age 90+, sex not recorded, died before index (OMOP, and separately the third-party death source in `FACT_MORTALITY_STATUS`), no follow-up. Counts under 11 show as `<11`. Follow-up ends at the later of the OMOP observation end and the last live interaction (completed appointment, immunization, lab collection or surgical case). A death counts only if it is within `DEATH_GRACE_DAYS` (30) of that point; later deaths are censored at last contact, because events in between are not observed.
- Confirm that `features.age` lies between 18 and 89.
- Record the attrition counts in the study protocol.
- The criteria are on the models sheet's **Eligibility** tab (I1–I5, E1–E6).

⛔ **Stop if** any table is empty, or the cohort is far smaller than expected. Re-check the schemas and the index window.
✅ **Done when** the 6 parquet files and `cohort_attrition.json` exist in `data/raw/`, and every patient is 18–89.

---

## Phase 4 — Initialise curation and accounts (≈1 h) · DS

```bash
export CURATION_DATA_DIR=$PWD/data
python -m curation.pipeline init --provenance mayo-clinic-platform
# edit data/governance.json now if the CL agreed different thresholds (Phase 2.5)
python -m curation.pipeline user-add dsmith   --role data_scientist                 --full-name "D Smith"
python -m curation.pipeline user-add drlead   --role lead_cardiologist --email lead@cardioailive.com --full-name "Dr Lead"
python -m curation.pipeline user-add drreader1 --role cardiologist     --email r1@cardioailive.com   --full-name "Dr Reader 1"
python -m curation.pipeline user-add drreader2 --role cardiologist     --email r2@cardioailive.com   --full-name "Dr Reader 2"
# up to 5 reader seats in total (lead + cardiologists); cardiologist accounts must be @cardioailive.com
```
Each user is prompted for a password (12+ characters). Hand passwords over in person or through a password manager. Never e-mail them.

✅ **Done when** `user-add` has succeeded for everyone.

---

## Phase 5 — Automatic labelling, annotation and audit (≈1–3 h) · DS

```bash
python -m curation.pipeline label        # candidate events → labelling functions → confirmed / uncertain / rejected + subtype,
                                         # then the required automatic audit (22 checks → pass / warn / fail)
python -m curation.pipeline plan-workload # reads and hours per cardiologist for 2, 3 or 4 readers
```
🔒 **The automatic audit is a required step.** It runs as part of `label` and cannot be skipped: the data scientist review (Phase 6), cardiologist adjudication (Phase 7) and the release sign-offs stay locked until the current label run has an audit. If the audit did not finish, run `python -m curation.pipeline audit`. If the extracted data changes after labelling, the audit is refused; run `label` again.

The reports are in `data/label_runs/<LR-id>/`: `AUDIT_REPORT.md`, `lf_stats.json` and `labels.parquet`.

⛔ **Stop if** the audit has failures you can't explain. Fix the extraction or code lists and **re-run `label`**, which creates a new label run and audits it. Common causes are listed in [Appendix B](#appendix-b--troubleshooting).
✅ **Done when** the audit has 0 unexplained failures.

---

## Phase 6 — Data scientist review (≈½–1 day) · DS

**6.1 Start the workbench** (workspace only, localhost):
```bash
python -m curation.server --host 127.0.0.1 --port 8090
```
Open it through JupyterLab's proxy: `https://<your-jupyter-host>/.../proxy/8090/`. Sign in as the data scientist.

**6.2 Audit review page.**
- **Acknowledge** every warning with a note.
- **Waive** any failure you accept, with a written justification (≥ 20 characters), or fix it and re-label.
- Check the label-model table: a labelling function that never fires usually means missing evidence.

**6.3 Approve the dataset** on the Pipeline page. This locks the review and **creates the adjudication sample**:
- every uncertain label-relevant case (cap 300 per outcome);
- 50 auto-confirmed and 50 auto-rejected cases per outcome;
- 100 imaging reports.

Each case is assigned to readers.

✅ **Done when** the Pipeline page shows stage 3 as approved and readers see cases in **My cases**.

---

## Phase 7 — Cardiologist adjudication (3–6 weeks) · R + CL

**7.1 Kick-off (1 h, CL leads).** Walk through the Clinician Lead Guide, the definitions and 5 practice cases together. Practice cases count, so do them on the live queue.

**7.2 Readers** work through **My cases**:
- each read is blind to the auto-label, the sampling reason and other readers;
- for each case, choose event / not an event / cannot determine, then subtype, date and the criteria met;
- report-value checks: confirm or correct the LVEF / calcium values.

**7.3 Lead cardiologist** works through **Disagreements**. These are cases where the two readers disagreed; the CL sees both reads and decides.

**7.4 DS monitors** progress daily:
```bash
python -m curation.pipeline status       # gate status, κ, PPV/NPV as cases complete
```
- If a reader is far behind, re-balance by adding a reader. New seats must follow the seat policy.
- If **κ is falling** for an outcome, the CL holds a calibration session on the disagreeing cases. Don't change definitions mid-run; if a definition must change, record it and start a new label run.

✅ **Done when** the Pipeline page shows `All adjudication cases final`.

---

## Phase 8 — Gates, sign-off and release (≈1 day) · DS + CL

**8.1** Open the Pipeline page and check every gate:
- **κ ≥ 0.6** per outcome;
- **PPV of auto-confirmed** and **NPV of auto-rejected** ≥ 85% per outcome;
- **indeterminate** ≤ 15%;
- **LVEF / calcium extraction accuracy** ≥ 90%;
- **audit trail intact**.

**8.2 A gate fails.** Decide with the CL and DS:

| Situation | Action |
|---|---|
| Low κ with high raw agreement and very skewed prevalence (the κ paradox, seen with HF in testing) | The CL may **waive** with that justification. |
| Low PPV or NPV | Do **not** waive. Run `python -m curation.pipeline refit-label-model --out lm.json`, then `label --label-model lm.json`. That starts a **new run**: audit → review → new sample → adjudication. |
| Low annotation accuracy | Fix the patterns in `training/imaging_nlp.py`, then start a new run. |

**8.3 Sign-offs** (two different people):
- DS clicks **Sign off as data scientist**;
- CL clicks **Sign off as lead cardiologist**.

**8.4 Build the release** (DS) from the Pipeline page or the CLI:
```bash
python -m curation.pipeline release --as dsmith       # → data/releases/REL-…/ (dataset.parquet, release.json, RELEASE_REPORT.md)
python -m curation.pipeline verify-log                 # audit-trail hash chain must be intact
```
✅ **Done when** `RELEASE_REPORT.md` lists both sign-offs and all gates PASS/WAIVED.

---

## Phase 9 — Train and validate (≈1–2 h) · DS

```bash
python -m training.train --release data/releases/REL-<id> --out bundles/mcp-$(date +%Y-%m)
```
Review `bundles/mcp-…/VALIDATION_REPORT.md` with the CL:
- **hold-out C-statistic and 95% CI** per model against its target (AMI ≥0.82, Stroke ≥0.75, HF ≥0.80, SCD ≥0.78, AF ≥0.80). "Meets target" requires the **lower CI bound** to be at or above the target;
- calibration slope (aim 0.8–1.2) and E/O (aim 0.8–1.2);
- subgroup C-statistics by sex and race. Flag any group > 0.05 below the overall value;
- AI vs published equation (PCE, CHARGE-AF) on the same patients;
- models skipped for too few events: the app shows these horizons as "n/a".

⛔ **Stop if** calibration or subgroup performance is unacceptable to the CL. Don't export; investigate features, cohort and labels first.
✅ **Done when** the CL signs the validation review (record it in the sign-off table).

---

## Phase 10 — Export review and deployment to Render (≈1–2 weeks incl. review) · DS + MCP

**10.1 Package** the bundle: weights + aggregate metrics only, cells < 11 already suppressed.
```bash
tar -czf mcp-bundle-$(date +%Y-%m).tar.gz -C bundles mcp-$(date +%Y-%m)
```
**10.2 Submit** the tarball, `VALIDATION_REPORT.md` and `RELEASE_REPORT.md` for **Mayo Clinic Platform export review**. Optionally also submit to **Platform_Validate** for an independent bias / sensitivity / specificity report.

**10.3 After approval:** upload the tarball to private object storage (signed URL). Render applies the Prisma database migrations automatically before each deploy (`scripts/db_migrate.py`). Then in the Render dashboard for `cardioai-cvd-risk`:
- `MODEL_BUNDLE_URL` = the signed URL (`MODEL_BUNDLE_TOKEN` if needed);
- `ALLOW_DEMO_MODELS` = `false`;
- redeploy.

**10.4 Verify** the live app:
- the header shows **Training data: Mayo Clinic Platform** and **Labels: cardiologist-adjudicated (n cases, κ …)**;
- the demo banner is gone;
- run 3 test patients and compare with the validation-report examples.

✅ **Done when** the live app serves the Mayo-trained bundle.

---

## Phase 11 — Ongoing operations

| Cadence | Task | Owner |
|---|---|---|
| Each run | Keep `data/curation.db` and `data/releases/` backed up **inside the workspace**; run `verify-log`. | DS |
| Quarterly | Re-run Phases 3 → 9 on new data. Compare C-statistic and calibration to the previous bundle before replacing it. | DS + CL |
| On any code-list or definition change | New label run → full audit → review → adjudication sample (no shortcuts). | DS + CL |
| Yearly | Re-validate subgroup performance; refresh the regulatory assessment (FDA CDS / SaMD). | CardioAI |

---

## Sign-off record

| Phase | Completed (date) | By | Notes |
|---|---|---|---|
| 0 Accounts & definitions approved | | CL | |
| 2 Settings confirmed, code lists signed off | | DS + CL | |
| 5 Audit run (label run id) | | DS | |
| 6 Dataset approved | | DS | |
| 7 Adjudication complete | | CL | |
| 8 Release built (release id) | | DS + CL | |
| 9 Validation review accepted | | CL | |
| 10 Export approved / deployed | | MCP / DS | |

---

## Appendix A — Questions for Mayo Clinic Platform

1. In `FACT_ECHO_TEST`: the patient key column, the echo date/time column, the echo id, and the mapping of the key to OMOP `person_id`. Phase 2 checks this with `match_rate`.
2. The catalog/schema names for OMOP and the native `FACT_*` tables, and whether OMOP `note` holds all `FACT_RADIOLOGY` reports.
3. Which `condition_status_concept_id` / `condition_type_concept_id` values mean the primary diagnosis.
4. When the switch from conventional troponin T to Gen 5 happened, and whether `range_high` is populated.
5. How complete death data is for deaths outside Mayo (state death index linkage).
6. Export review: the cell-suppression threshold (this build uses < 11) and the review steps.
7. Whether annotator seats are separate from data-scientist seats.

## Appendix B — Troubleshooting

| Symptom | Likely cause → fix |
|---|---|
| `Confirm these FACT_ECHO_TEST settings` | `training/native_sources.json` is missing or incomplete → Phase 2.3. |
| `AnalysisException: Table or view not found` | Wrong `--cdm` / `--native-dataset` → `SHOW TABLES IN …`. |
| Driver out of memory at `event_notes` | Raise driver memory, or narrow `--index-start/--index-end` and run in two batches. |
| Audit `lf_coverage` warning: troponin LFs never fire | Troponin LOINC codes differ → add them to `EVIDENCE_LOINC`, re-label. |
| Audit `icd_drift` warning | A code list misses ICD-9 or ICD-10 equivalents → fix `CANDIDATE_CODES`, re-label. |
| Audit `prevalence` warning | A family's rate is outside the expected range → check visit-type filters and code lists. |
| Reader sees "This case is not assigned to you" | Opened a case link that belongs to another reader; use **My cases**. |
| `All N annotator seats are in use` | Deactivate a reader (admin) or request more seats from MCP. |
| `Release blocked by gates: …` | Resolve or waive the listed gates (Phase 8.2), then sign off. |
| `Release dataset hash does not match` at training | The release was modified. Rebuild it; never edit release files by hand. |
