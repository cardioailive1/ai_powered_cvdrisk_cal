"""CLI identity: the workspace login is the person, so acting as your own curation user needs no password; acting
as someone else does."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from cardioai_core.security import hash_password  # noqa: E402
from curation import pipeline  # noqa: E402
from curation.store import User  # noqa: E402


def test_workspace_account_matches():
    u = User(username="tonywell", email="", role="data_scientist", password_hash="x")
    assert pipeline._workspace_account_matches(u, "tonywell_cardioailive_com", "cardioailive.com")
    assert pipeline._workspace_account_matches(u, "tonywell", "cardioailive.com")
    assert not pipeline._workspace_account_matches(u, "rnidhi_cardioailive_com", "cardioailive.com")
    c = User(username="drsmith", email="j.smith@cardioailive.com", role="cardiologist", password_hash="x")
    assert pipeline._workspace_account_matches(c, "j_smith_cardioailive_com", "cardioailive.com")


def test_login_without_password_only_for_own_account(tmp_path, monkeypatch):
    """With governance auth.passwords true."""
    from curation import governance
    cfg = governance.load(tmp_path)
    cfg["auth"]["passwords"] = True
    governance.save(tmp_path, cfg)
    db = pipeline._db(tmp_path)
    db.add(User(username="tonywell", role="data_scientist", password_hash=hash_password("p" * 12)))
    db.add(User(username="card1", role="cardiologist", password_hash=hash_password("q" * 12)))
    db.commit()
    monkeypatch.setattr(pipeline, "_sysuser", lambda: "cli:tonywell_cardioailive_com")
    monkeypatch.delenv("CURATION_PASSWORD", raising=False)
    monkeypatch.setattr(pipeline.getpass, "getpass", lambda *a: pytest.fail("asked for a password"))
    assert pipeline._login(db, "tonywell").username == "tonywell"
    monkeypatch.setattr(pipeline.getpass, "getpass", lambda *a: "wrong-password!")
    with pytest.raises(SystemExit):
        pipeline._login(db, "card1")                     # someone else's account: password still required
    monkeypatch.setattr(pipeline.getpass, "getpass", lambda *a: "q" * 12)
    assert pipeline._login(db, "card1").username == "card1"


def test_initials_only_no_passwords(tmp_path, monkeypatch, capsys):
    """Default (auth.passwords false): users are added by initials with no password and anyone signs in by initials."""
    import argparse
    db = pipeline._db(tmp_path)
    monkeypatch.setattr(pipeline.getpass, "getpass", lambda *a: pytest.fail("asked for a password"))
    for ini, role in (("jks", "cardiologist"), ("AB", "cardiologist"), ("cd", "lead_cardiologist")):
        pipeline.cmd_user_add(argparse.Namespace(data_dir=tmp_path, initials=ini, role=role, email=None, password=None))
    assert "Created lead_cardiologist CD" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        pipeline.cmd_user_add(argparse.Namespace(data_dir=tmp_path, initials="Dr Smith", role="cardiologist", email=None, password=None))
    with pytest.raises(SystemExit):        # same initials: needs a digit
        pipeline.cmd_user_add(argparse.Namespace(data_dir=tmp_path, initials="JKS", role="cardiologist", email=None, password=None))
    monkeypatch.setattr(pipeline, "_sysuser", lambda: "cli:someone_else")
    assert pipeline._login(db, "jks").username == "JKS"
