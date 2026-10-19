-- Drug exposure (ATC classes -> descendant RxNorm concepts) in the 365 days before index
WITH atc_prefix AS (SELECT * FROM UNNEST([
    STRUCT('statin' AS name, 'C10AA' AS prefix),
    STRUCT('statin', 'C10BA'),
    STRUCT('statin', 'C10BX'),
    STRUCT('aspirin', 'B01AC06'),
    STRUCT('acei_arb', 'C09'),
    STRUCT('beta_blocker', 'C07'),
    STRUCT('anticoagulant', 'B01AA'),
    STRUCT('anticoagulant', 'B01AE'),
    STRUCT('anticoagulant', 'B01AF'),
    STRUCT('antihypertensive', 'C02'),
    STRUCT('antihypertensive', 'C03'),
    STRUCT('antihypertensive', 'C07'),
    STRUCT('antihypertensive', 'C08'),
    STRUCT('antihypertensive', 'C09')])),
atc_drugs AS (
  SELECT DISTINCT p.name, ca.descendant_concept_id AS concept_id
  FROM atc_prefix p
  JOIN `PROJECT.OMOP_DATASET.concept` a ON a.vocabulary_id = 'ATC' AND STARTS_WITH(a.concept_code, p.prefix)
  JOIN `PROJECT.OMOP_DATASET.concept_ancestor` ca ON ca.ancestor_concept_id = a.concept_id
)
SELECT DISTINCT co.person_id, d.name AS flag
FROM `PROJECT.SCRATCH_DATASET.cvd_cohort` co
JOIN `PROJECT.OMOP_DATASET.drug_exposure` de
  ON de.person_id = co.person_id
 AND de.drug_exposure_start_date BETWEEN DATE_SUB(co.index_date, INTERVAL 365 DAY) AND co.index_date
JOIN atc_drugs d ON d.concept_id = de.drug_concept_id
