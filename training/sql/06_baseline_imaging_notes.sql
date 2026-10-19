-- Pre-index imaging / echo reports for CAC + LVEF annotation (radiology reports live in OMOP `note`;
-- Mayo Clinic Platform Radiology guide: FACT_RADIOLOGY.RADIOLOGY_REPORT == note.note_text). Text stays in the workspace.
SELECT co.person_id, n.note_id, n.note_date, n.note_text
FROM `PROJECT.SCRATCH_DATASET.cvd_cohort` co
JOIN `PROJECT.OMOP_DATASET.note` n
  ON n.person_id = co.person_id
 AND n.note_date BETWEEN DATE_SUB(co.index_date, INTERVAL 730 DAY) AND co.index_date
WHERE REGEXP_CONTAINS(LOWER(n.note_text), r'agatston|calcium score|ejection fraction|lvef')
