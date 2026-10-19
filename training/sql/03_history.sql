-- Diagnosis history on or before index (conditions + observation-domain Z codes)
WITH hist_concepts AS (SELECT /*+ BROADCAST(s) */ c.concept_id, s.name
  FROM omop.concept c
  JOIN (SELECT * FROM VALUES
    ('prior_mi', 'ICD10CM', 'I21|I22|I25\\.2'),
    ('prior_mi', 'ICD9CM', '410|412'),
    ('prior_stroke', 'ICD10CM', 'I6[0-3]|G45|Z86\\.73'),
    ('prior_stroke', 'ICD9CM', '43[0-6]|V12\\.54'),
    ('angina', 'ICD10CM', 'I20'),
    ('angina', 'ICD9CM', '413'),
    ('heart_failure', 'ICD10CM', 'I50|I11\\.0|I13\\.0|I13\\.2'),
    ('heart_failure', 'ICD9CM', '428'),
    ('afib', 'ICD10CM', 'I48'),
    ('afib', 'ICD9CM', '427\\.3'),
    ('family_hx_cad', 'ICD10CM', 'Z82\\.49'),
    ('family_hx_cad', 'ICD9CM', 'V17\\.3'),
    ('hypertension', 'ICD10CM', 'I1[0-6]'),
    ('hypertension', 'ICD9CM', '40[1-5]'),
    ('pad', 'ICD10CM', 'I70\\.2|I73\\.9'),
    ('pad', 'ICD9CM', '440\\.2|443\\.9'),
    ('smoking_current', 'ICD10CM', 'F17\\.2|Z72\\.0'),
    ('smoking_current', 'ICD9CM', '305\\.1'),
    ('smoking_former', 'ICD10CM', 'Z87\\.891'),
    ('smoking_former', 'ICD9CM', 'V15\\.82'),
    ('diabetes_t1', 'ICD10CM', 'E10'),
    ('diabetes_t1', 'ICD9CM', '250\\.\\d[13]'),
    ('diabetes_t2', 'ICD10CM', 'E11'),
    ('diabetes_t2', 'ICD9CM', '250\\.\\d[02]'),
    ('prediabetes', 'ICD10CM', 'R73\\.0'),
    ('prediabetes', 'ICD9CM', '790\\.2')
  AS t(name, vocab, pattern)) s
    ON s.vocab = c.vocabulary_id AND c.concept_code RLIKE concat('^(', s.pattern, ')')),
events AS (
  SELECT co.person_id, x.condition_source_concept_id AS src, x.condition_start_date AS dt
  FROM cvd_cohort co
  JOIN omop.condition_occurrence x ON x.person_id = co.person_id AND x.condition_start_date <= co.index_date
  UNION ALL
  SELECT co.person_id, o.observation_source_concept_id, o.observation_date
  FROM cvd_cohort co
  JOIN omop.observation o ON o.person_id = co.person_id AND o.observation_date <= co.index_date
)
SELECT e.person_id, h.name AS flag, MAX(e.dt) AS last_date
FROM events e
JOIN hist_concepts h ON h.concept_id = e.src
GROUP BY e.person_id, h.name
