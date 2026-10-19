-- Post-index procedures used as adjudication evidence  [-> temp view cvd_evidence_procedures]
WITH proc_concepts AS (SELECT /*+ BROADCAST(s) */ c.concept_id, s.category
  FROM omop.concept c
  JOIN (SELECT * FROM VALUES
    ('pci', 'CPT4', '929(2\\d|3\\d|4[0-4])|9298[0-4]'),
    ('pci', 'ICD10PCS', '027[0-3]'),
    ('coronary_angiography', 'CPT4', '934(5[4-9]|6[01])|9350[18]'),
    ('coronary_angiography', 'ICD10PCS', 'B21[01]'),
    ('head_ct', 'CPT4', '70450|70460|70470|70496'),
    ('head_ct', 'ICD10PCS', 'B020'),
    ('head_mri', 'CPT4', '7055[1-3]|7054[4-6]'),
    ('head_mri', 'ICD10PCS', 'B030'),
    ('echo', 'CPT4', '9330[3-8]|9331[2-8]|9335[01]'),
    ('echo', 'ICD10PCS', 'B24'),
    ('thrombolysis', 'CPT4', '37195'),
    ('thrombolysis', 'ICD10PCS', '3E03317'),
    ('thrombectomy', 'CPT4', '61645'),
    ('thrombectomy', 'ICD10PCS', '03C[G-V]3'),
    ('cpr', 'CPT4', '92950'),
    ('cpr', 'ICD10PCS', '5A12012'),
    ('rhythm_restoration', 'ICD10PCS', '5A2204Z'),
    ('cardioversion', 'CPT4', '9296[01]'),
    ('icd_implant', 'CPT4', '3324[09]|3323[01]|33270'),
    ('icd_implant', 'ICD10PCS', '0JH[68]0[89]Z'),
    ('cardiac_surgery', 'CPT4', '335(1\\d|2\\d|3[0-6])|334(0[5-9]|1\\d|2\\d|30)'),
    ('cardiac_surgery', 'ICD10PCS', '021[0-3]0|02R[FGHJ]0')
  AS t(category, vocab, pattern)) s
    ON s.vocab = c.vocabulary_id AND c.concept_code RLIKE concat('^(', s.pattern, ')'))
SELECT DISTINCT co.person_id, pc.category, p.procedure_date, c.concept_code AS code
FROM cvd_cohort co
JOIN omop.procedure_occurrence p ON p.person_id = co.person_id AND p.procedure_date >= co.index_date
JOIN proc_concepts pc ON pc.concept_id = IF(p.procedure_source_concept_id > 0, p.procedure_source_concept_id, p.procedure_concept_id)
JOIN omop.concept c ON c.concept_id = pc.concept_id
