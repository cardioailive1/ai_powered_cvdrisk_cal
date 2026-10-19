-- Post-index procedures used as adjudication evidence
CREATE OR REPLACE TABLE `PROJECT.SCRATCH_DATASET.cvd_evidence_procedures` AS
WITH codeset AS (SELECT * FROM UNNEST([
    STRUCT('pci' AS category, 'CPT4' AS vocab, '929(2\\d|3\\d|4[0-4])|9298[0-4]' AS pattern),
    STRUCT('pci', 'ICD10PCS', '027[0-3]'),
    STRUCT('coronary_angiography', 'CPT4', '934(5[4-9]|6[01])|9350[18]'),
    STRUCT('coronary_angiography', 'ICD10PCS', 'B21[01]'),
    STRUCT('head_ct', 'CPT4', '70450|70460|70470|70496'),
    STRUCT('head_ct', 'ICD10PCS', 'B020'),
    STRUCT('head_mri', 'CPT4', '7055[1-3]|7054[4-6]'),
    STRUCT('head_mri', 'ICD10PCS', 'B030'),
    STRUCT('echo', 'CPT4', '9330[3-8]|9331[2-8]|9335[01]'),
    STRUCT('echo', 'ICD10PCS', 'B24'),
    STRUCT('thrombolysis', 'CPT4', '37195'),
    STRUCT('thrombolysis', 'ICD10PCS', '3E03317'),
    STRUCT('thrombectomy', 'CPT4', '61645'),
    STRUCT('thrombectomy', 'ICD10PCS', '03C[G-V]3'),
    STRUCT('cpr', 'CPT4', '92950'),
    STRUCT('cpr', 'ICD10PCS', '5A12012'),
    STRUCT('rhythm_restoration', 'ICD10PCS', '5A2204Z'),
    STRUCT('cardioversion', 'CPT4', '9296[01]'),
    STRUCT('icd_implant', 'CPT4', '3324[09]|3323[01]|33270'),
    STRUCT('icd_implant', 'ICD10PCS', '0JH[68]0[89]Z'),
    STRUCT('cardiac_surgery', 'CPT4', '335(1\\d|2\\d|3[0-6])|334(0[5-9]|1\\d|2\\d|30)'),
    STRUCT('cardiac_surgery', 'ICD10PCS', '021[0-3]0|02R[FGHJ]0')]))
SELECT DISTINCT co.person_id, s.category, p.procedure_date, c.concept_code AS code
FROM `PROJECT.SCRATCH_DATASET.cvd_cohort` co
JOIN `PROJECT.OMOP_DATASET.procedure_occurrence` p ON p.person_id = co.person_id AND p.procedure_date >= co.index_date
JOIN `PROJECT.OMOP_DATASET.concept` c ON c.concept_id = IF(p.procedure_source_concept_id > 0, p.procedure_source_concept_id, p.procedure_concept_id)
JOIN codeset s ON s.vocab = c.vocabulary_id AND REGEXP_CONTAINS(c.concept_code, CONCAT('^(', s.pattern, ')'))
