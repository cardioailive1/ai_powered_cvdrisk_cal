-- Landmark cohort: one index outpatient visit per patient  [-> temp view cvd_cohort]
WITH candidate_visits AS (
  SELECT DISTINCT v.person_id, v.visit_start_date AS index_date
  FROM omop.visit_occurrence v
  JOIN omop.observation_period op
    ON op.person_id = v.person_id
   AND v.visit_start_date BETWEEN date_add(op.observation_period_start_date, 365) AND op.observation_period_end_date
  JOIN omop.measurement m
    ON m.person_id = v.person_id
   AND m.measurement_date BETWEEN date_sub(v.visit_start_date, 365) AND v.visit_start_date
  JOIN omop.concept c
    ON c.concept_id = m.measurement_concept_id AND c.vocabulary_id = 'LOINC' AND c.concept_code IN ('2093-3', '2085-8')
  WHERE v.visit_concept_id IN (9202)
    AND v.visit_start_date BETWEEN DATE '2012-01-01' AND DATE '2019-12-31'
),
ranked AS (
  SELECT person_id, index_date,
         ROW_NUMBER() OVER (PARTITION BY person_id
                            ORDER BY xxhash64(concat(cast(person_id AS STRING), cast(index_date AS STRING)))) AS rn
  FROM candidate_visits
),
idx AS (
  SELECT r.person_id, r.index_date, cast(datediff(r.index_date, date_add(make_date(cast(p.year_of_birth AS INT), cast(coalesce(p.month_of_birth, 7) AS INT), 1), cast(coalesce(p.day_of_birth, 1) - 1 AS INT))) AS DOUBLE) / 365.25 AS age,
         p.gender_concept_id, p.race_concept_id, p.ethnicity_concept_id
  FROM ranked r
  JOIN omop.person p ON p.person_id = r.person_id
  WHERE r.rn = 1
),
obs AS (
  SELECT i.person_id, MAX(op.observation_period_end_date) AS obs_end
  FROM idx i
  JOIN omop.observation_period op
    ON op.person_id = i.person_id AND i.index_date BETWEEN op.observation_period_start_date AND op.observation_period_end_date
  GROUP BY i.person_id
),
dth AS (SELECT person_id, MIN(death_date) AS death_date FROM omop.death GROUP BY person_id)
SELECT i.person_id, i.index_date, i.age, i.gender_concept_id, i.race_concept_id, i.ethnicity_concept_id,
       o.obs_end, d.death_date
FROM idx i
LEFT JOIN obs o ON o.person_id = i.person_id
LEFT JOIN dth d ON d.person_id = i.person_id
WHERE i.age BETWEEN 18 AND 90
