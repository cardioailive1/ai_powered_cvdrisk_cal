-- Echo reports around candidate events (-7 to +30 days: LVEF for HF subtype)
WITH triggers AS (SELECT DISTINCT person_id, event_date AS d FROM cvd_candidate_codes)
SELECT DISTINCT t.person_id, concat('ECHO-', cast(xxhash64(concat(cast(x./* CONFIRM:patient key column in FACT_ECHO_TEST (PATIENT_DK or PATIENT_CLINIC_NUMBER) */ CONFIRM_COLUMN AS STRING), cast(x./* CONFIRM:date/time the echo was performed */ CONFIRM_COLUMN AS STRING), x.ECHO_NOTE)) AS STRING)) AS note_id, to_date(x./* CONFIRM:date/time the echo was performed */ CONFIRM_COLUMN) AS note_date,
       'Echocardiogram report' AS note_title, x.ECHO_NOTE AS note_text
FROM triggers t
JOIN omop.person p ON p.person_id = t.person_id
JOIN NATIVE_SCHEMA.FACT_ECHO_TEST x ON cast(x./* CONFIRM:patient key column in FACT_ECHO_TEST (PATIENT_DK or PATIENT_CLINIC_NUMBER) */ CONFIRM_COLUMN AS STRING) = cast(p./* CONFIRM:OMOP person column holding PATIENT_CLINIC_NUMBER (e.g. person_source_value) */ CONFIRM_COLUMN AS STRING)
WHERE x.ECHO_NOTE IS NOT NULL
  AND to_date(x./* CONFIRM:date/time the echo was performed */ CONFIRM_COLUMN) BETWEEN date_sub(t.d, 7) AND date_add(t.d, 30)
