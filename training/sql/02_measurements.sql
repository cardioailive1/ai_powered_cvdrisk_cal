-- Latest baseline lab / vital per feature in the look-back window (units harmonised in Python)
WITH loinc_map AS (SELECT * FROM UNNEST([
    STRUCT('8302-2' AS code, 'height_cm' AS feature),
    STRUCT('3137-7', 'height_cm'),
    STRUCT('29463-7', 'weight_kg'),
    STRUCT('3141-9', 'weight_kg'),
    STRUCT('39156-5', 'bmi'),
    STRUCT('8480-6', 'sbp'),
    STRUCT('8459-0', 'sbp'),
    STRUCT('8462-4', 'dbp'),
    STRUCT('8453-3', 'dbp'),
    STRUCT('2093-3', 'total_chol'),
    STRUCT('2085-8', 'hdl'),
    STRUCT('13457-7', 'ldl'),
    STRUCT('18262-6', 'ldl'),
    STRUCT('2089-1', 'ldl'),
    STRUCT('2571-8', 'trig'),
    STRUCT('1558-6', 'glucose'),
    STRUCT('2345-7', 'glucose'),
    STRUCT('4548-4', 'hba1c'),
    STRUCT('17856-6', 'hba1c'),
    STRUCT('30522-7', 'hscrp'),
    STRUCT('1988-5', 'hscrp'),
    STRUCT('33914-3', 'egfr'),
    STRUCT('62238-1', 'egfr'),
    STRUCT('98979-8', 'egfr'),
    STRUCT('48642-3', 'egfr'),
    STRUCT('48643-1', 'egfr')])),
ranked AS (
  SELECT co.person_id, lm.feature, m.value_as_number AS value,
         COALESCE(u.concept_code, m.unit_source_value) AS unit,
         ROW_NUMBER() OVER (PARTITION BY co.person_id, lm.feature ORDER BY m.measurement_date DESC, m.measurement_id DESC) AS rn
  FROM `PROJECT.SCRATCH_DATASET.cvd_cohort` co
  JOIN `PROJECT.OMOP_DATASET.measurement` m
    ON m.person_id = co.person_id
   AND m.measurement_date BETWEEN DATE_SUB(co.index_date, INTERVAL 730 DAY) AND co.index_date
  JOIN `PROJECT.OMOP_DATASET.concept` c ON c.concept_id = m.measurement_concept_id AND c.vocabulary_id = 'LOINC'
  JOIN loinc_map lm ON lm.code = c.concept_code
  LEFT JOIN `PROJECT.OMOP_DATASET.concept` u ON u.concept_id = m.unit_concept_id
  WHERE m.value_as_number IS NOT NULL
)
SELECT person_id, feature, value, unit FROM ranked WHERE rn = 1
