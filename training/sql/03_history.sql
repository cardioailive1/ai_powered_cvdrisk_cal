-- Diagnosis history on or before index (conditions + observation-domain Z codes)
WITH codeset AS (SELECT * FROM UNNEST([
    STRUCT('prior_mi' AS name, 'ICD10CM' AS vocab, 'I21|I22|I25\\.2' AS pattern),
    STRUCT('prior_mi', 'ICD9CM', '410|412'),
    STRUCT('prior_stroke', 'ICD10CM', 'I6[0-3]|G45|Z86\\.73'),
    STRUCT('prior_stroke', 'ICD9CM', '43[0-6]|V12\\.54'),
    STRUCT('angina', 'ICD10CM', 'I20'),
    STRUCT('angina', 'ICD9CM', '413'),
    STRUCT('heart_failure', 'ICD10CM', 'I50|I11\\.0|I13\\.0|I13\\.2'),
    STRUCT('heart_failure', 'ICD9CM', '428'),
    STRUCT('afib', 'ICD10CM', 'I48'),
    STRUCT('afib', 'ICD9CM', '427\\.3'),
    STRUCT('family_hx_cad', 'ICD10CM', 'Z82\\.49'),
    STRUCT('family_hx_cad', 'ICD9CM', 'V17\\.3'),
    STRUCT('hypertension', 'ICD10CM', 'I1[0-6]'),
    STRUCT('hypertension', 'ICD9CM', '40[1-5]'),
    STRUCT('pad', 'ICD10CM', 'I70\\.2|I73\\.9'),
    STRUCT('pad', 'ICD9CM', '440\\.2|443\\.9'),
    STRUCT('smoking_current', 'ICD10CM', 'F17\\.2|Z72\\.0'),
    STRUCT('smoking_current', 'ICD9CM', '305\\.1'),
    STRUCT('smoking_former', 'ICD10CM', 'Z87\\.891'),
    STRUCT('smoking_former', 'ICD9CM', 'V15\\.82'),
    STRUCT('diabetes_t1', 'ICD10CM', 'E10'),
    STRUCT('diabetes_t1', 'ICD9CM', '250\\.\\d[13]'),
    STRUCT('diabetes_t2', 'ICD10CM', 'E11'),
    STRUCT('diabetes_t2', 'ICD9CM', '250\\.\\d[02]'),
    STRUCT('prediabetes', 'ICD10CM', 'R73\\.0'),
    STRUCT('prediabetes', 'ICD9CM', '790\\.2')])),
events AS (
  SELECT co.person_id, x.condition_source_concept_id AS src, x.condition_start_date AS dt
  FROM `PROJECT.SCRATCH_DATASET.cvd_cohort` co
  JOIN `PROJECT.OMOP_DATASET.condition_occurrence` x ON x.person_id = co.person_id AND x.condition_start_date <= co.index_date
  UNION ALL
  SELECT co.person_id, o.observation_source_concept_id, o.observation_date
  FROM `PROJECT.SCRATCH_DATASET.cvd_cohort` co
  JOIN `PROJECT.OMOP_DATASET.observation` o ON o.person_id = co.person_id AND o.observation_date <= co.index_date
)
SELECT e.person_id, s.name AS flag, MAX(e.dt) AS last_date
FROM events e
JOIN `PROJECT.OMOP_DATASET.concept` c ON c.concept_id = e.src
JOIN codeset s ON s.vocab = c.vocabulary_id AND REGEXP_CONTAINS(c.concept_code, CONCAT('^(', s.pattern, ')'))
GROUP BY 1, 2
