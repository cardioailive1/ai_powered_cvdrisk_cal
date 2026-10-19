-- Drug exposure (ATC classes -> descendant RxNorm concepts) in the 365 days before index
WITH atc_prefix AS (SELECT * FROM VALUES
    ('statin', 'C10AA'),
    ('statin', 'C10BA'),
    ('statin', 'C10BX'),
    ('aspirin', 'B01AC06'),
    ('acei_arb', 'C09'),
    ('beta_blocker', 'C07'),
    ('anticoagulant', 'B01AA'),
    ('anticoagulant', 'B01AE'),
    ('anticoagulant', 'B01AF'),
    ('antihypertensive', 'C02'),
    ('antihypertensive', 'C03'),
    ('antihypertensive', 'C07'),
    ('antihypertensive', 'C08'),
    ('antihypertensive', 'C09')
  AS t(name, prefix)),
atc_drugs AS (
  SELECT DISTINCT p.name, ca.descendant_concept_id AS concept_id
  FROM atc_prefix p
  JOIN omop.concept a ON a.vocabulary_id = 'ATC' AND a.concept_code LIKE concat(p.prefix, '%')
  JOIN omop.concept_ancestor ca ON ca.ancestor_concept_id = a.concept_id
)
SELECT DISTINCT co.person_id, d.name AS flag
FROM cvd_cohort co
JOIN omop.drug_exposure de
  ON de.person_id = co.person_id
 AND de.drug_exposure_start_date BETWEEN date_sub(co.index_date, 365) AND co.index_date
JOIN atc_drugs d ON d.concept_id = de.drug_concept_id
