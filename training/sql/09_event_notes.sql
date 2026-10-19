-- Notes around candidate events (codes or trigger procedures): -3 to +30 days.
WITH triggers AS (
  SELECT DISTINCT person_id, event_date AS d FROM cvd_candidate_codes
  UNION
  SELECT DISTINCT person_id, procedure_date AS d FROM cvd_evidence_procedures WHERE category IN ('pci', 'thrombolysis', 'thrombectomy', 'cpr', 'cardioversion')
)
SELECT DISTINCT n.person_id, n.note_id, n.note_date, n.note_title, n.note_text
FROM triggers t
JOIN omop.note n
  ON n.person_id = t.person_id
 AND n.note_date BETWEEN date_sub(t.d, 3) AND date_add(t.d, 30)
WHERE lower(n.note_text) RLIKE 'infarct|stemi|troponin|acute coronary|stroke|cva|hemorrhage|transient ischemic|\\btia\\b|heart failure|\\bchf\\b|ejection fraction|lvef|\\bbnp\\b|diuresis|cardiac arrest|rosc|ventricular (fibrillation|tachycardia)|\\bvf\\b|\\bvt\\b|nsvt|atrial fib|atrial flutter|afib'
