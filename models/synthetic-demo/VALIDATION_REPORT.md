# Validation report — synthetic-demo

- Provenance: **synthetic-demo**
- Created: 2026-10-19T14:37:33+00:00
- Patients: 60,000
- scikit-learn: 1.9.1
- Labels: first confirmed event within horizon; death before horizon without event = negative; censored or unresolved candidate within horizon = excluded

> **Not for clinical use.** SYNTHETIC data — not derived from patients. For pipeline testing only.

- Label governance: **simulated** — release REL-20261019-143535-df2c, 1616 adjudicated cases

> Adjudication was **simulated** (synthetic readers), not performed by cardiologists.

| Model | Hold-out C-statistic (95% CI) | Target | Meets target* | LR baseline | Cal. slope | E/O | Test n / events |
|---|---|---|---|---|---|---|---|
| ami__30d | 0.737 (0.643-0.839) | ≥0.82 | No | 0.793 | 0.98 | 1.00 | 12,000 / 26 |
| ami__1y | 0.820 (0.789-0.847) | ≥0.82 | No | 0.831 | 0.97 | 1.04 | 11,351 / 254 |
| ami__5y | 0.812 (0.794-0.828) | ≥0.82 | No | 0.819 | 1.04 | 1.06 | 6,928 / 800 |
| any_stroke__1y | 0.751 (0.710-0.796) | ≥0.75 | No | 0.750 | 0.89 | 1.03 | 11,342 / 144 |
| any_stroke__5y | 0.747 (0.727-0.767) | ≥0.75 | No | 0.754 | 0.88 | 1.01 | 6,815 / 531 |
| ischemic_stroke__1y | 0.735 (0.697-0.777) | ≥0.75 | No | 0.747 | 0.79 | 1.02 | 11,341 / 111 |
| ischemic_stroke__5y | 0.740 (0.720-0.768) | ≥0.75 | No | 0.755 | 0.86 | 0.98 | 6,775 / 405 |
| hemorrhagic_stroke__5y | 0.696 (0.593-0.779) | ≥0.75 | No | 0.722 | 0.99 | 1.09 | 6,650 / 28 |
| tia__1y | 0.735 (0.629-0.858) | ≥0.75 | No | 0.715 | 1.00 | 1.16 | 11,337 / 24 |
| tia__5y | 0.689 (0.637-0.729) | ≥0.75 | No | 0.708 | 1.27 | 1.10 | 6,676 / 98 |
| hf__30d | 0.919 (0.878-0.954) | ≥0.80 | Yes | 0.917 | 1.18 | 0.80 | 12,000 / 34 |
| hf__1y | 0.876 (0.857-0.894) | ≥0.80 | Yes | 0.880 | 1.02 | 0.96 | 11,347 / 313 |
| hf__5y | 0.845 (0.828-0.859) | ≥0.80 | Yes | 0.850 | 0.98 | 0.99 | 6,903 / 843 |
| hfref__30d | 0.894 (0.821-0.959) | ≥0.80 | Yes | 0.911 | 1.13 | 0.72 | 11,998 / 23 |
| hfref__1y | 0.903 (0.879-0.924) | ≥0.80 | Yes | 0.911 | 1.10 | 0.97 | 11,327 / 177 |
| hfref__5y | 0.863 (0.844-0.882) | ≥0.80 | Yes | 0.871 | 1.00 | 0.98 | 6,760 / 455 |
| hfpef__1y | 0.793 (0.744-0.834) | ≥0.80 | No | 0.799 | 1.09 | 0.97 | 11,325 / 91 |
| hfpef__5y | 0.777 (0.751-0.805) | ≥0.80 | No | 0.786 | 0.97 | 0.98 | 6,703 / 289 |
| scd__1y | 0.841 (0.774-0.889) | ≥0.78 | No | 0.862 | 1.15 | 0.99 | 11,345 / 46 |
| scd__5y | 0.782 (0.745-0.824) | ≥0.78 | No | 0.789 | 0.96 | 1.02 | 6,708 / 150 |
| af__30d | 0.757 (0.628-0.877) | ≥0.80 | No | 0.791 | 1.03 | 1.34 | 11,567 / 20 |
| af__1y | 0.815 (0.789-0.837) | ≥0.80 | No | 0.823 | 1.07 | 0.99 | 10,925 / 297 |
| af__5y | 0.801 (0.790-0.817) | ≥0.80 | No | 0.804 | 1.02 | 0.98 | 6,677 / 959 |

*Target met only if the lower bound of the 95% CI is at or above the target.

## AI vs published equation (same hold-out patients)

| Model | Equation | n | Equation C-stat | AI C-stat |
|---|---|---|---|---|
| ami__5y | Pooled Cohort Equations (2013 ACC/AHA) | 5,217 | 0.750 | 0.759 |
| af__5y | CHARGE-AF (simple model) | 4,902 | 0.764 | 0.771 |

## Subgroup C-statistics (5-year models)

**ami__5y**: sex=female 0.809; sex=male 0.797; race=asian 0.808; race=black 0.809; race=hispanic 0.847; race=other 0.794; race=white 0.810

**any_stroke__5y**: sex=female 0.755; sex=male 0.738; race=asian 0.746; race=black 0.758; race=hispanic 0.756; race=other 0.758; race=white 0.740

**ischemic_stroke__5y**: sex=female 0.736; sex=male 0.745; race=asian 0.727; race=black 0.729; race=hispanic 0.764; race=other 0.776; race=white 0.734

**hemorrhagic_stroke__5y**: sex=female 0.641; sex=male 0.741; race=asian n/a; race=black n/a; race=hispanic n/a; race=other n/a; race=white 0.676

**tia__5y**: sex=female 0.720; sex=male 0.663; race=asian n/a; race=black 0.644; race=hispanic n/a; race=other n/a; race=white 0.695

**hf__5y**: sex=female 0.844; sex=male 0.844; race=asian 0.860; race=black 0.884; race=hispanic 0.884; race=other 0.859; race=white 0.833

**hfref__5y**: sex=female 0.845; sex=male 0.869; race=asian 0.876; race=black 0.884; race=hispanic 0.914; race=other 0.861; race=white 0.853

**hfpef__5y**: sex=female 0.816; sex=male 0.733; race=asian 0.777; race=black 0.823; race=hispanic 0.824; race=other 0.786; race=white 0.767

**scd__5y**: sex=female 0.757; sex=male 0.775; race=asian n/a; race=black 0.809; race=hispanic 0.817; race=other n/a; race=white 0.775

**af__5y**: sex=female 0.792; sex=male 0.804; race=asian 0.833; race=black 0.782; race=hispanic 0.819; race=other 0.718; race=white 0.802

## Not trained

- any_stroke__30d: insufficient events (train 52, test <11)
- ischemic_stroke__30d: insufficient events (train 41, test <11)
- hemorrhagic_stroke__30d: insufficient events (train <11, test <11)
- hemorrhagic_stroke__1y: insufficient events (train 28, test <11)
- tia__30d: insufficient events (train <11, test <11)
- hfpef__30d: insufficient events (train 27, test <11)
- scd__30d: insufficient events (train 14, test <11)
