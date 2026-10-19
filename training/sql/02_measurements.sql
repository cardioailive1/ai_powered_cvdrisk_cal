-- Latest baseline lab / vital per feature in the look-back window (units harmonised in Python)
WITH loinc_map AS (SELECT * FROM VALUES
    ('8302-2', 'height_cm'),
    ('3137-7', 'height_cm'),
    ('29463-7', 'weight_kg'),
    ('3141-9', 'weight_kg'),
    ('39156-5', 'bmi'),
    ('8480-6', 'sbp'),
    ('8459-0', 'sbp'),
    ('8462-4', 'dbp'),
    ('8453-3', 'dbp'),
    ('2093-3', 'total_chol'),
    ('2085-8', 'hdl'),
    ('13457-7', 'ldl'),
    ('18262-6', 'ldl'),
    ('2089-1', 'ldl'),
    ('2571-8', 'trig'),
    ('1558-6', 'glucose'),
    ('2345-7', 'glucose'),
    ('4548-4', 'hba1c'),
    ('17856-6', 'hba1c'),
    ('30522-7', 'hscrp'),
    ('1988-5', 'hscrp'),
    ('33914-3', 'egfr'),
    ('62238-1', 'egfr'),
    ('98979-8', 'egfr'),
    ('48642-3', 'egfr'),
    ('48643-1', 'egfr')
  AS t(code, feature)),
ranked AS (
  SELECT co.person_id, lm.feature, m.value_as_number AS value,
         coalesce(u.concept_code, m.unit_source_value) AS unit,
         ROW_NUMBER() OVER (PARTITION BY co.person_id, lm.feature ORDER BY m.measurement_date DESC, m.measurement_id DESC) AS rn
  FROM cvd_cohort co
  JOIN omop.measurement m
    ON m.person_id = co.person_id
   AND m.measurement_date BETWEEN date_sub(co.index_date, 730) AND co.index_date
  JOIN omop.concept c ON c.concept_id = m.measurement_concept_id AND c.vocabulary_id = 'LOINC'
  JOIN loinc_map lm ON lm.code = c.concept_code
  LEFT JOIN omop.concept u ON u.concept_id = m.unit_concept_id
  WHERE m.value_as_number IS NOT NULL
)
SELECT person_id, feature, value, unit FROM ranked WHERE rn = 1
