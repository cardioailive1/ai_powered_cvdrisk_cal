-- Post-index troponin / natriuretic peptide results (with reference range when available)
WITH loinc_map AS (SELECT * FROM UNNEST([
    STRUCT('10839-9' AS code, 'troponin_i' AS analyte),
    STRUCT('42757-5', 'troponin_i'),
    STRUCT('49563-0', 'troponin_i'),
    STRUCT('6598-7', 'troponin_t'),
    STRUCT('89579-7', 'hs_troponin_i'),
    STRUCT('67151-1', 'hs_troponin_t'),
    STRUCT('30934-4', 'bnp'),
    STRUCT('42637-9', 'bnp'),
    STRUCT('33762-6', 'nt_probnp'),
    STRUCT('83107-3', 'nt_probnp')]))
SELECT co.person_id, lm.analyte,
       COALESCE(m.measurement_datetime, CAST(m.measurement_date AS DATETIME)) AS measured_at,
       m.value_as_number AS value, COALESCE(u.concept_code, m.unit_source_value) AS unit, m.range_high
FROM `PROJECT.SCRATCH_DATASET.cvd_cohort` co
JOIN `PROJECT.OMOP_DATASET.measurement` m ON m.person_id = co.person_id AND m.measurement_date > co.index_date
JOIN `PROJECT.OMOP_DATASET.concept` c ON c.concept_id = m.measurement_concept_id AND c.vocabulary_id = 'LOINC'
JOIN loinc_map lm ON lm.code = c.concept_code
LEFT JOIN `PROJECT.OMOP_DATASET.concept` u ON u.concept_id = m.unit_concept_id
WHERE m.value_as_number IS NOT NULL
