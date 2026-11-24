"""Notes read a patient bucket at a time (13.5 million notes on MCP): same labels as reading everything at once,
the per-patient store matches the source exactly, and it is rebuilt when the notes change."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from curation import governance, notes_store, runs  # noqa: E402
from training import labeling  # noqa: E402
from training.synthetic_raw import generate_raw, write_raw  # noqa: E402


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    d = tmp_path_factory.mktemp("notes")
    write_raw(d / "raw", generate_raw(n=1500, seed=5))
    return d


def _norm(df, cols):
    return df[cols].astype(str).sort_values(cols).reset_index(drop=True)


def test_store_matches_source(data, monkeypatch):
    monkeypatch.setattr(notes_store, "NOTE_BUCKETS", 7)
    monkeypatch.setattr(notes_store, "ROW_GROUP_ROWS", 50)
    raw = data / "raw"
    src = pd.read_parquet(raw / "event_notes.parquet")
    notes_store.ensure(raw)
    cols = list(src.columns)
    allb = pd.concat([notes_store.read_bucket(raw, k) for k in range(7)], ignore_index=True)
    assert len(allb) == len(src) and _norm(allb, cols).equals(_norm(src, cols))
    some = src["person_id"].drop_duplicates().sample(25, random_state=1).tolist()
    got = notes_store.notes_for(raw, some)
    assert _norm(got, cols).equals(_norm(src[src["person_id"].isin(some)], cols))
    assert notes_store.note_count(raw) == len(src)


def test_store_rebuilt_when_notes_change(data, monkeypatch):
    monkeypatch.setattr(notes_store, "NOTE_BUCKETS", 5)
    raw = data / "raw"
    notes_store.ensure(raw)
    src = pd.read_parquet(raw / "event_notes.parquet")
    src.iloc[:-10].to_parquet(raw / "event_notes.parquet", index=False)
    assert not notes_store.is_current(raw)
    notes_store.ensure(raw)
    assert sum(len(notes_store.read_bucket(raw, k)) for k in range(5)) == len(src) - 10
    src.to_parquet(raw / "event_notes.parquet", index=False)


def test_labels_same_as_all_at_once(data, monkeypatch):
    """Episodes, evidence and votes are built bucket by bucket: identical to building them for everyone at once."""
    monkeypatch.setattr(notes_store, "NOTE_BUCKETS", 6)
    gov = governance.load(data)
    model = labeling.DEFAULT_LABEL_MODEL | {"thresholds": gov["label_thresholds"]}
    raw = runs.load_raw(data, include_notes=True)
    assert "event_notes" not in runs.load_raw(data)          # the notes are never loaded whole by default
    cands = labeling.build_episodes(raw["candidate_codes"], raw["evidence_procedures"], raw["features"])
    ev = labeling.Evidence(raw["evidence_labs"], raw["evidence_procedures"], raw["event_notes"],
                           dict(zip(raw["features"]["person_id"], raw["features"]["sex"])))
    whole = labeling.mark_relevance(labeling.label_candidates(cands, ev, raw["features"], model), gov["definitions"])

    from curation.store import User, open_store
    S = open_store(data)
    with S() as db:
        db.add(User(username="t", role="data_scientist", password_hash="x"))
        db.commit()
        run = runs.create_label_run(db, data, "t")
    parts = pd.read_parquet(Path(run.path) / "labels.parquet")
    assert sorted(parts["candidate_id"]) == sorted(whole["candidate_id"]) and parts["candidate_id"].is_unique
    cols = list(whole.columns)
    assert _norm(parts, cols).equals(_norm(whole, cols))


def test_baseline_annotation_streamed(data):
    from training.audit import run_audit
    raw = runs.load_raw(data, include_notes=True)
    ann = runs.baseline_annotation(data)
    assert len(ann) == len(raw["baseline_imaging_notes"])
    labels = pd.read_parquet(next((data / "label_runs").glob("*/labels.parquet")))     # from the test above
    cfg = governance.load(data)
    a = {c["id"]: c for c in run_audit(raw["features"], labels, {}, raw["baseline_imaging_notes"], cfg)["checks"]}
    b = {c["id"]: c for c in run_audit(raw["features"], labels, {}, ann, cfg)["checks"]}
    assert a["annotation_yield"]["value"] == b["annotation_yield"]["value"]
    assert runs.baseline_annotation(data).equals(ann)          # cached result reused
