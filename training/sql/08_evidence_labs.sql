-- Post-index troponin / natriuretic peptide results (with reference range when available)
WITH loinc_map AS (SELECT * FROM VALUES
    ('10839-9', 'troponin_i'),
    ('42757-5', 'troponin_i'),
    ('49563-0', 'troponin_i'),
    ('6598-7', 'troponin_t'),
    ('89579-7', 'hs_troponin_i'),
    ('67151-1', 'hs_troponin_t'),
    ('30934-4', 'bnp'),
    ('42637-9', 'bnp'),
    ('33762-6', 'nt_probnp'),
    ('83107-3', 'nt_probnp')
  AS t(code, analyte))
SELECT co.person_id, lm.analyte,
       coalesce(m.measurement_datetime, cast(m.measurement_date AS TIMESTAMP)) AS measured_at,
       m.value_as_number AS value, coalesce(u.concept_code, m.unit_source_value) AS unit, m.range_high
FROM cvd_cohort co
JOIN omop.measurement m ON m.person_id = co.person_id AND m.measurement_date > co.index_date
JOIN omop.concept c ON c.concept_id = m.measurement_concept_id AND c.vocabulary_id = 'LOINC'
JOIN loinc_map lm ON lm.code = c.concept_code
LEFT JOIN omop.concept u ON u.concept_id = m.unit_concept_id
WHERE m.value_as_number IS NOT NULL
