-- Every post-index outcome diagnosis code (row level). Codes only create CANDIDATES.  [-> temp view cvd_candidate_codes]
WITH outcome_concepts AS (SELECT /*+ BROADCAST(s) */ c.concept_id, s.family
  FROM omop.concept c
  JOIN (SELECT * FROM VALUES
    ('ami', 'ICD10CM', 'I21|I22'),
    ('ami', 'ICD9CM', '410'),
    ('stroke', 'ICD10CM', 'I6[0-3]|G45\\.[0-389]'),
    ('stroke', 'ICD9CM', '43[0-2]|433\\.\\d1|434\\.\\d1|435|436'),
    ('hf', 'ICD10CM', 'I50|I11\\.0|I13\\.0|I13\\.2'),
    ('hf', 'ICD9CM', '428|402\\.\\d1|404\\.\\d[13]'),
    ('scd', 'ICD10CM', 'I46|I49\\.0|I47\\.2'),
    ('scd', 'ICD9CM', '427\\.5|427\\.4|427\\.1'),
    ('af', 'ICD10CM', 'I48'),
    ('af', 'ICD9CM', '427\\.3')
  AS t(family, vocab, pattern)) s
    ON s.vocab = c.vocabulary_id AND c.concept_code RLIKE concat('^(', s.pattern, ')'))
SELECT DISTINCT co.person_id, oc.family, x.condition_start_date AS event_date, c.concept_code AS code,
       v.visit_concept_id, x.condition_status_concept_id
FROM cvd_cohort co
JOIN omop.condition_occurrence x ON x.person_id = co.person_id AND x.condition_start_date > co.index_date
JOIN outcome_concepts oc ON oc.concept_id = x.condition_source_concept_id
JOIN omop.concept c ON c.concept_id = x.condition_source_concept_id
LEFT JOIN omop.visit_occurrence v ON v.visit_occurrence_id = x.visit_occurrence_id
