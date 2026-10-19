-- Every post-index outcome diagnosis code (row level). Codes only create CANDIDATES.
CREATE OR REPLACE TABLE `PROJECT.SCRATCH_DATASET.cvd_candidate_codes` AS
WITH codeset AS (SELECT * FROM UNNEST([
    STRUCT('ami' AS family, 'ICD10CM' AS vocab, 'I21|I22' AS pattern),
    STRUCT('ami', 'ICD9CM', '410'),
    STRUCT('stroke', 'ICD10CM', 'I6[0-3]|G45\\.[0-389]'),
    STRUCT('stroke', 'ICD9CM', '43[0-2]|433\\.\\d1|434\\.\\d1|435|436'),
    STRUCT('hf', 'ICD10CM', 'I50|I11\\.0|I13\\.0|I13\\.2'),
    STRUCT('hf', 'ICD9CM', '428|402\\.\\d1|404\\.\\d[13]'),
    STRUCT('scd', 'ICD10CM', 'I46|I49\\.0|I47\\.2'),
    STRUCT('scd', 'ICD9CM', '427\\.5|427\\.4|427\\.1'),
    STRUCT('af', 'ICD10CM', 'I48'),
    STRUCT('af', 'ICD9CM', '427\\.3')]))
SELECT DISTINCT co.person_id, s.family, x.condition_start_date AS event_date, c.concept_code AS code,
       v.visit_concept_id, x.condition_status_concept_id
FROM `PROJECT.SCRATCH_DATASET.cvd_cohort` co
JOIN `PROJECT.OMOP_DATASET.condition_occurrence` x ON x.person_id = co.person_id AND x.condition_start_date > co.index_date
JOIN `PROJECT.OMOP_DATASET.concept` c ON c.concept_id = x.condition_source_concept_id
JOIN codeset s ON s.vocab = c.vocabulary_id AND REGEXP_CONTAINS(c.concept_code, CONCAT('^(', s.pattern, ')'))
LEFT JOIN `PROJECT.OMOP_DATASET.visit_occurrence` v ON v.visit_occurrence_id = x.visit_occurrence_id
