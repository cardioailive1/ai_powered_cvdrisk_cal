-- Death date (Mayo record + third-party death data) and last live interaction from FACT_MORTALITY_STATUS
SELECT co.person_id, MIN(to_date(x./* CONFIRM:date of death column */ CONFIRM_COLUMN)) AS death_date_ext,
       MAX(to_date(x.EHR_LAST_LIVE_INTERACTION_DTM)) AS last_live_date
FROM cvd_cohort co
JOIN omop.person p ON p.person_id = co.person_id
JOIN NATIVE_SCHEMA.FACT_MORTALITY_STATUS x ON cast(x./* CONFIRM:patient key column in FACT_MORTALITY_STATUS (PATIENT_DK or PATIENT_CLINIC_NUMBER) */ CONFIRM_COLUMN AS STRING) = cast(p./* CONFIRM:OMOP person column holding PATIENT_CLINIC_NUMBER (e.g. person_source_value) */ CONFIRM_COLUMN AS STRING)
GROUP BY co.person_id
