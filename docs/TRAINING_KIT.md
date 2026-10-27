# Training kit: trainer's guide

This guide runs the **CardioAI Label & Annotation Training Program**: six modules, 8 sessions over two weeks, ending in certification. Trainees learn on the real curation workbench, inside the Mayo Clinic Platform workspace, using a **separate training folder that holds only synthetic data**.

The kit is `curation/training_kit.py`. Every command refuses to run unless the folder carries both the `SYNTHETIC` and the `TRAINING` marker, so it cannot touch live data.

| Folder | Contents | Who can read it |
|---|---|---|
| `train_env/` | Synthetic training data + its curation workbench store | Trainer, trainees (through the workbench) |
| `faults_env/` | A copy with three planted extraction faults (Modules 3–4) | Trainer, DS and lead trainees |
| `trainer_only/` | Answer keys, case maps, clean data for `repair`, scores, certification reports | **Trainer only** (`chmod 700`) |

Keep all three folders inside the workspace, out of git: they are synthetic, but the habit matters.

## Before session 1 (trainer, ~30 min)

```bash
mkdir -m 700 trainer_only
python -m curation.training_kit setup        --data-dir train_env --trainer-dir trainer_only   # synthetic cohort, label run, audit
python -m curation.training_kit add-trainees --data-dir train_env --roster roster.csv          # prints one-time passwords
python -m curation.server --data-dir train_env --host 127.0.0.1 --port 8091                    # training workbench (port ≠ live)
```

`roster.csv` columns: `username,full_name,email,role`. The role is `data_scientist`, `cardiologist` or `lead_cardiologist`; readers need `@cardioailive.com` addresses. Hand out passwords in person.

## Session by session

| Session | Module | Trainer runs | Trainees do |
|---|---|---|---|
| 1 | 1 · Definitions | Present the deck. | Definitions exercise (deck slide). |
| 2 | 2 · Auto-labelling | `training_kit explain --data-dir train_env --near-threshold 3` | For each near-threshold candidate: name the votes and the one piece of evidence that would move it. `explain --candidate <id>` for any other. |
| 3 | 3 · Audit | `training_kit plant-faults --data-dir train_env --out faults_env --trainer-dir trainer_only` | DS + lead: `python -m curation.pipeline --data-dir faults_env label` (labelling, then the required automatic audit). Name each failure from `AUDIT_REPORT.md`. |
| 4 | 4 · Post-audit review | — | DS: for each failure, fix it with `training_kit repair --data-dir faults_env --check <id> --trainer-dir trainer_only` (this stands in for re-extracting), then run `label` again, which audits the new run. The workbench stays locked until the audit has run. Acknowledge warnings in the workbench, then approve the dataset. Trainer: `training_kit grade-faults --data-dir faults_env --trainer-dir trainer_only`. |
| 5 | 5 · Adjudication | `training_kit make-set --data-dir train_env --set practice --trainer-dir trainer_only` | Readers + lead: 10 practice event cases + 2 report checks in **My cases**. Compare answers as a group. |
| 6 | Calibration | `make-set --set calibration` and `make-set --set disagreements`. Lead: `training_kit key --set calibration`, then confirm the key (below). | Readers + lead: 40 event cases + 5 report checks, alone. Lead: 10 seeded disagreements in **Disagreements**, with a written rationale for each. |
| 7 | 6 · Gates | `training_kit score --set calibration` and `score --set disagreements` | Review `discordant_calibration_<user>.md` with the lead. |
| 8 | Certification | Record results in the program's sign-off tracker. | Retakes are scheduled within 2 weeks. |

**Planted faults** (Modules 3–4): the key is in `trainer_only/faults_key.json`.
- A batch of cholesterol values arrives in mmol/L. This triggers both `units` and `plausibility`.
- The notes extract is empty (`evidence_tables`).
- Death dates are shifted before outcome events (`events_after_death`).

`repair` refuses any other check. It tells the trainee that the warning reflects the real data and must be acknowledged with a justification.

A DS trainee passes `grade-faults` when all of these are true:
- all three planted faults are fixed, and none is waived;
- every remaining warning is acknowledged with a justification of at least 20 characters;
- the dataset is approved.

## The answer key

`make-set` writes a **draft** key from the synthetic generator's ground truth: `trainer_only/<set>_key.csv`.

- **Lead confirms:** `training_kit key --set calibration` writes `<set>_key_review.md`, which shows each case's evidence next to the key. The lead reads it and fills `confirmed_by` (or corrects the key and explains in `note`).
- **Scoring status:** `score` exits with status 2, and the report is marked **Draft**, until every row is confirmed.
- **Lead's own certification:** the lead's calibration is scored against the same key. A second senior cardiologist, or the CardioAI medical director, confirms the key used to certify the lead.

## Pass marks

These are the program standards, set in `STANDARDS` in the kit:

| Trainee | Set | Pass |
|---|---|---|
| Reader | Calibration (40 event cases) | Overall agreement ≥ 85% · every family ≥ 75% · κ vs key ≥ 0.70 · event date within 7 days on ≥ 90% of agreed events |
| Lead | Calibration + 10 disagreements | Reader standard, plus ≥ 85% agreement on disagreements, each with a rationale (≥ 20 characters) |
| Data scientist | Planted faults | `grade-faults` → `pass: true`, and the lead accepts the justifications |

Agreement means all of these:
- the same outcome;
- for events, the same subtype ("unknown" or "unspecified" matches anything);
- event dates within 7 days.

These are the same rules the live workbench uses between two readers. Report-value (LVEF / calcium) accuracy is reported but does not decide certification.

## After the program

- **Live accounts:** certified trainees get accounts in the **live** data folder with `curation.pipeline user-add`. Training accounts are never copied over.
- **Archive:** keep `trainer_only/CERTIFICATION_*.md` and `scores_*.json` with the program's sign-off tracker.
- **Retakes:** run a new environment (`setup` into a new folder) so the retake set is fresh.
