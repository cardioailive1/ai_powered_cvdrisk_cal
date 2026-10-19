-- Pre-index imaging / echo reports for CAC + LVEF annotation (radiology reports are in OMOP note;
-- Mayo Clinic Platform Radiology guide: FACT_RADIOLOGY.RADIOLOGY_REPORT == note.note_text). Text stays in the workspace.
SELECT co.person_id, n.note_id, n.note_date, n.note_text
FROM cvd_cohort co
JOIN omop.note n
  ON n.person_id = co.person_id
 AND n.note_date BETWEEN date_sub(co.index_date, 730) AND co.index_date
WHERE lower(n.note_text) RLIKE 'agatston|calcium score|ejection fraction|lvef'
