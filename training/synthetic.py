"""Synthetic dataset in the exact training-contract format produced by extract.py.

PURPOSE: lets you run and test the whole pipeline (train -> bundle -> Render app)
without patient data. Models trained on it are labelled provenance
'synthetic-demo' and the app shows a NOT-FOR-CLINICAL-USE banner. Effect sizes
are loosely based on published epidemiology but are NOT real estimates.

    python -m training.synthetic --n 80000 --out data/synthetic.parquet
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from cardioai_core.schema import AGE_MAX, AGE_MIN


def generate(n: int = 80000, seed: int = 7) -> pd.DataFrame:
    r = np.random.default_rng(seed)
    age = np.clip(r.normal(58, 13, n), AGE_MIN, AGE_MAX + 0.9)   # adults only, like the real cohort
    male = r.random(n) < 0.49
    race = r.choice(["white", "black", "hispanic", "asian", "other"], n, p=[0.74, 0.09, 0.08, 0.05, 0.04])
    height = np.where(male, r.normal(177, 7, n), r.normal(163, 6.5, n))
    bmi = np.clip(r.normal(28.5, 5.5, n), 16, 60)
    weight = bmi * (height / 100) ** 2
    a = (age - 55) / 10

    sbp = np.clip(r.normal(127 + 4 * a + 0.4 * (bmi - 28) + 4 * (race == "black"), 16, n), 85, 230)
    dbp = np.clip(r.normal(78 + 0.3 * (bmi - 28) - 1.0 * a, 10, n), 45, 130)
    smoking = r.choice(["never", "former", "current"], n, p=[0.6, 0.26, 0.14])
    p_t2 = 1 / (1 + np.exp(-(-2.6 + 0.45 * a + 0.09 * (bmi - 28))))
    u = r.random(n)
    diabetes = np.select([u < 0.005, u < 0.005 + p_t2, u < 0.005 + p_t2 + 0.15], ["type1", "type2", "prediabetes"], "none")
    dm = np.isin(diabetes, ["type1", "type2"])

    tc = np.clip(r.normal(195, 38, n), 100, 380)
    hdl = np.clip(np.where(male, r.normal(47, 12, n), r.normal(58, 15, n)) - 0.4 * (bmi - 28), 18, 115)
    trig = np.clip(np.exp(r.normal(np.log(130) + 0.02 * (bmi - 28) + 0.25 * dm, 0.5, n)), 30, 1400)
    ldl = np.clip(tc - hdl - trig / 5, 25, 290)
    glucose = np.where(dm, r.normal(150, 40, n), np.where(diabetes == "prediabetes", r.normal(108, 8, n), r.normal(92, 8, n)))
    hba1c = np.where(dm, r.normal(7.6, 1.3, n), np.where(diabetes == "prediabetes", r.normal(6.0, 0.2, n), r.normal(5.3, 0.3, n)))
    hscrp = np.clip(np.exp(r.normal(np.log(1.8) + 0.04 * (bmi - 28) + 0.3 * (smoking == "current"), 0.9, n)), 0.1, 40)
    egfr = np.clip(r.normal(118 - 0.85 * age - 8 * dm, 13, n), 6, 140)

    def b(p):
        return (r.random(n) < np.clip(p, 0, 1)).astype(int)

    prior_mi = b(0.015 + 0.03 * np.clip(a + 1, 0, None) * (1 + male))
    prior_stroke = b(0.01 + 0.02 * np.clip(a + 1, 0, None))
    angina = b(0.02 + 0.5 * prior_mi + 0.02 * np.clip(a, 0, None))
    heart_failure = b(0.01 + 0.15 * prior_mi + 0.015 * np.clip(a, 0, None))
    afib = b(0.01 + 0.03 * np.clip(a, 0, None) + 0.15 * heart_failure)
    family_hx_cad = b(0.15)
    hypertension = np.maximum(b(0.1 + 0.08 * np.clip(a + 1, 0, None)), (sbp >= 140).astype(int) * b(0.8))
    pad = b(0.01 + 0.02 * np.clip(a, 0, None) + 0.03 * (smoking == "current"))
    statin = b(0.1 + 0.6 * np.maximum(prior_mi, prior_stroke) + 0.25 * dm + 0.15 * (ldl > 160))
    aspirin = b(0.08 + 0.6 * prior_mi + 0.3 * prior_stroke)
    acei_arb = b(0.05 + 0.5 * hypertension + 0.4 * heart_failure + 0.2 * dm)
    beta_blocker = b(0.04 + 0.5 * prior_mi + 0.5 * heart_failure + 0.4 * afib)
    anticoagulant = b(0.01 + 0.65 * afib)
    antihypertensive = np.maximum.reduce([acei_arb, beta_blocker, b(0.1 * hypertension)])
    # treated BP is lower
    sbp = sbp - 6 * antihypertensive

    cac_measured = (r.random(n) < 0.3) & (age > 40)
    cac = np.where(cac_measured,
                   np.where(r.random(n) < 1 / (1 + np.exp(-(0.9 * a + 0.8 * male - 0.2))),
                            np.exp(r.normal(3.5 + 0.5 * a + 0.4 * male + 0.4 * dm, 1.3, n)), 0.0), np.nan)
    lvef_measured = r.random(n) < 0.25 + 0.5 * np.maximum(heart_failure, prior_mi)
    lvef = np.where(lvef_measured, np.clip(r.normal(60 - 18 * heart_failure - 7 * prior_mi, 7, n), 10, 80), np.nan)

    # ---- hazards (per year) -------------------------------------------------
    lp_ath = (0.65 * a + 0.45 * male + 0.012 * (sbp - 125) + 0.006 * (ldl - 120) - 0.015 * (hdl - 50)
              + 0.55 * (smoking == "current") + 0.15 * (smoking == "former") + 0.5 * dm + 0.25 * dm * (a > 0)
              + 0.18 * np.log(hscrp) - 0.008 * (egfr - 85) + 0.9 * prior_mi + 0.5 * angina + 0.25 * family_hx_cad
              + 0.35 * pad - 0.2 * statin - 0.1 * aspirin
              + np.where(np.isnan(cac), 0, 0.22 * np.log1p(np.nan_to_num(cac)) - 0.5))
    lp_stroke = (0.75 * a + 0.1 * male + 0.02 * (sbp - 125) + 0.45 * (smoking == "current") + 0.4 * dm
                 + 1.0 * prior_stroke + 0.7 * afib * (1 - 0.6 * anticoagulant) + 0.3 * (race == "black")
                 + 0.1 * np.log(hscrp) + 0.2 * hypertension)
    lp_hem = 0.6 * a + 0.03 * (sbp - 125) + 0.6 * anticoagulant + 0.4 * (race == "asian") + 0.5 * prior_stroke
    lp_hf = (0.75 * a + 0.3 * male + 0.012 * (sbp - 125) + 0.05 * (bmi - 28) + 0.6 * dm + 0.9 * prior_mi
             + 1.3 * heart_failure + 0.7 * afib - 0.015 * (egfr - 85) + 0.4 * (smoking == "current")
             + np.where(np.isnan(lvef), 0, -0.045 * (np.nan_to_num(lvef) - 58)))
    lp_scd = (0.5 * a + 0.6 * male + 1.0 * prior_mi + 1.0 * heart_failure + 0.5 * (smoking == "current")
              + 0.35 * dm - 0.01 * (egfr - 85)
              + np.where(np.isnan(lvef), 0, -0.07 * (np.nan_to_num(lvef) - 58)) - 0.25 * beta_blocker)
    lp_af = (0.9 * a + 0.4 * male + 0.4 * (race == "white") + 0.008 * (sbp - 125) + 0.04 * (bmi - 28)
             + 0.8 * heart_failure + 0.35 * prior_mi + 0.25 * hypertension + 0.2 * (smoking == "current")
             + np.where(np.isnan(lvef), 0, -0.02 * (np.nan_to_num(lvef) - 58)))

    def times(base_rate, lp):
        rate = base_rate * np.exp(lp)
        return (-np.log(r.random(n)) / rate * 365.25).round()

    df = pd.DataFrame(dict(
        person_id=np.arange(1, n + 1), age=age.round(1), sex=np.where(male, "male", "female"), race=race,
        height_cm=height.round(1), weight_kg=weight.round(1), sbp=sbp.round(), dbp=dbp.round(),
        smoking=smoking, diabetes=diabetes, total_chol=tc.round(), hdl=hdl.round(), ldl=ldl.round(),
        trig=trig.round(), glucose=glucose.round(), hba1c=hba1c.round(1), hscrp=hscrp.round(2), egfr=egfr.round(),
        prior_mi=prior_mi, prior_stroke=prior_stroke, angina=angina, heart_failure=heart_failure, afib=afib,
        family_hx_cad=family_hx_cad, hypertension=hypertension, pad=pad, statin=statin, aspirin=aspirin,
        acei_arb=acei_arb, beta_blocker=beta_blocker, anticoagulant=anticoagulant, antihypertensive=antihypertensive,
        cac_score=np.round(cac), lvef=np.round(lvef),
    ))
    # realistic missingness
    for col, p in {"hscrp": 0.6, "hba1c": 0.35, "glucose": 0.2, "ldl": 0.05, "trig": 0.05, "egfr": 0.1,
                   "height_cm": 0.05, "weight_kg": 0.04, "dbp": 0.02}.items():
        df.loc[r.random(n) < p, col] = np.nan

    df["days_ami"] = times(0.004, lp_ath)
    df["days_ischemic_stroke"] = times(0.0028, lp_stroke)
    df["days_hemorrhagic_stroke"] = times(0.0004, lp_hem)
    df["days_tia"] = times(0.0012, lp_stroke * 0.8)
    df["days_hf"] = times(0.003, lp_hf)
    hfref_share = 1 / (1 + np.exp(-(0.8 * prior_mi + 0.6 * male - 0.3 + np.where(np.isnan(lvef), 0, -0.08 * (np.nan_to_num(lvef) - 55)))))
    is_ref = r.random(n) < hfref_share
    df["days_hfref"] = np.where(is_ref, df["days_hf"], np.nan)
    df["days_hfpef"] = np.where(~is_ref, df["days_hf"], np.nan)
    df["days_scd"] = times(0.0007, lp_scd)
    df["days_af"] = times(0.005, lp_af)
    df["days_any_stroke"] = df[["days_ischemic_stroke", "days_hemorrhagic_stroke", "days_tia"]].min(axis=1)

    death = times(0.006, 0.9 * a + 0.3 * male + 0.6 * heart_failure + 0.3 * dm + 0.5 * (smoking == "current"))
    obs = r.uniform(180, 3650, n).round()
    df["death_days"] = np.where(death <= obs, death, np.nan)
    df["followup_days"] = np.minimum(obs, death)
    for c in [c for c in df.columns if c.startswith("days_")]:
        df.loc[df[c] > df["followup_days"], c] = np.nan
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=80000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="data/synthetic.parquet")
    a = ap.parse_args()
    df = generate(a.n, a.seed)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(a.out, index=False)
    print(f"Wrote {len(df)} synthetic rows to {a.out}")


if __name__ == "__main__":
    main()
