-- Candidate codes recorded as the PRIMARY diagnosis in FACT_DIAGNOSIS (PRIMARY_DIAGNOSIS_FLAG; MCP 21 Oct 2026).
-- Same person, same ICD code (dot ignored), diagnosis date within 1 day of the OMOP condition date.
SELECT DISTINCT cc.person_id, cc.event_date, cc.code
FROM cvd_candidate_codes cc
JOIN omop.person p ON p.person_id = cc.person_id
JOIN NATIVE_SCHEMA.FACT_DIAGNOSIS x ON cast(x./* CONFIRM:patient key column in FACT_DIAGNOSIS (PATIENT_DK or PATIENT_CLINIC_NUMBER) */ CONFIRM_COLUMN AS STRING) = cast(p./* CONFIRM:OMOP person column holding PATIENT_CLINIC_NUMBER (e.g. person_source_value) */ CONFIRM_COLUMN AS STRING)
WHERE upper(cast(x.PRIMARY_DIAGNOSIS_FLAG AS STRING)) = upper(/* CONFIRM:value of PRIMARY_DIAGNOSIS_FLAG meaning primary (e.g. Y) */ 'CONFIRM_VALUE')
  AND to_date(x./* CONFIRM:diagnosis date/time column */ CONFIRM_COLUMN) BETWEEN date_sub(cc.event_date, 1) AND date_add(cc.event_date, 1)
  AND regexp_replace(upper(cast(x./* CONFIRM:ICD diagnosis code column (with or without the dot) */ CONFIRM_COLUMN AS STRING)), '[.]', '') = regexp_replace(upper(cc.code), '[.]', '')
