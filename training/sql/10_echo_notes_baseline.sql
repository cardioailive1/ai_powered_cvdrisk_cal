-- Pre-index echo reports (LVEF annotation) from the native FACT_ECHO_TEST table
SELECT co.person_id, concat('ECHO-', cast(xxhash64(concat(cast(x./* CONFIRM:patient key column in FACT_ECHO_TEST (PATIENT_DK or PATIENT_CLINIC_NUMBER) */ CONFIRM_COLUMN AS STRING), cast(x./* CONFIRM:date/time the echo was performed */ CONFIRM_COLUMN AS STRING), x.ECHO_NOTE)) AS STRING)) AS note_id, to_date(x./* CONFIRM:date/time the echo was performed */ CONFIRM_COLUMN) AS note_date,
       'Echocardiogram report' AS note_title, x.ECHO_NOTE AS note_text
FROM cvd_cohort co
JOIN omop.person p ON p.person_id = co.person_id
JOIN NATIVE_SCHEMA.FACT_ECHO_TEST x ON cast(x./* CONFIRM:patient key column in FACT_ECHO_TEST (PATIENT_DK or PATIENT_CLINIC_NUMBER) */ CONFIRM_COLUMN AS STRING) = cast(p./* CONFIRM:OMOP person column holding PATIENT_CLINIC_NUMBER (e.g. person_source_value) */ CONFIRM_COLUMN AS STRING)
WHERE x.ECHO_NOTE IS NOT NULL
  AND to_date(x./* CONFIRM:date/time the echo was performed */ CONFIRM_COLUMN) BETWEEN date_sub(co.index_date, 730) AND co.index_date
