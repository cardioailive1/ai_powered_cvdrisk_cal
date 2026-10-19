-- Landmark cohort: one index outpatient visit per patient
CREATE OR REPLACE TABLE `PROJECT.SCRATCH_DATASET.cvd_cohort` AS
WITH candidate_visits AS (
  SELECT DISTINCT v.person_id, v.visit_start_date AS index_date
  FROM `PROJECT.OMOP_DATASET.visit_occurrence` v
  JOIN `PROJECT.OMOP_DATASET.observation_period` op
    ON op.person_id = v.person_id
   AND v.visit_start_date BETWEEN DATE_ADD(op.observation_period_start_date, INTERVAL 365 DAY)
                              AND op.observation_period_end_date
  JOIN `PROJECT.OMOP_DATASET.measurement` m
    ON m.person_id = v.person_id
   AND m.measurement_date BETWEEN DATE_SUB(v.visit_start_date, INTERVAL 365 DAY) AND v.visit_start_date
  JOIN `PROJECT.OMOP_DATASET.concept` c
    ON c.concept_id = m.measurement_concept_id AND c.vocabulary_id = 'LOINC' AND c.concept_code IN ('2093-3', '2085-8')
  WHERE v.visit_concept_id IN (9202)
    AND v.visit_start_date BETWEEN DATE '2012-01-01' AND DATE '2019-12-31'
),
ranked AS (
  SELECT person_id, index_date,
         ROW_NUMBER() OVER (PARTITION BY person_id
                            ORDER BY FARM_FINGERPRINT(CONCAT(CAST(person_id AS STRING), CAST(index_date AS STRING)))) AS rn
  FROM candidate_visits
)
SELECT r.person_id,
       r.index_date,
       DATE_DIFF(r.index_date, DATE(p.year_of_birth, COALESCE(p.month_of_birth, 7), COALESCE(p.day_of_birth, 1)), DAY) / 365.25 AS age,
       p.gender_concept_id, p.race_concept_id, p.ethnicity_concept_id,
       (SELECT MAX(op.observation_period_end_date) FROM `PROJECT.OMOP_DATASET.observation_period` op
         WHERE op.person_id = r.person_id AND r.index_date BETWEEN op.observation_period_start_date AND op.observation_period_end_date) AS obs_end,
       (SELECT MIN(d.death_date) FROM `PROJECT.OMOP_DATASET.death` d WHERE d.person_id = r.person_id) AS death_date
FROM ranked r
JOIN `PROJECT.OMOP_DATASET.person` p ON p.person_id = r.person_id
WHERE r.rn = 1
  AND DATE_DIFF(r.index_date, DATE(p.year_of_birth, COALESCE(p.month_of_birth, 7), COALESCE(p.day_of_birth, 1)), DAY) / 365.25 BETWEEN 18 AND 90
