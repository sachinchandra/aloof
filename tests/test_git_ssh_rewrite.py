"""doc-bug 39: deploy key + https URL -> fetch over SSH; failure hints mention the git@ URL."""
import pytest
from paas import creds

def test_ssh_url():
    assert creds.ssh_url("https://github.com/o/r") == "git@github.com:o/r.git"
    assert creds.ssh_url("https://gitlab.com/g/sub/r.git") == "git@gitlab.com:g/sub/r.git"
    assert creds.ssh_url("https://git.example.com/o/r") is None
    assert creds.ssh_url("https://github.com/o/r?x=1") is None

@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("PAAS_ALLOW_PRIVATE_GIT", "1"); monkeypatch.setattr(creds, "_check_url", lambda u: "ssh" if u.startswith("git@") else "https")
    st = {"creds": {}, "fetched": [], "fail": False}
    monkeypatch.setattr(creds, "_git_cred", lambda a, app, host, kind: st["creds"].get(kind))
    monkeypatch.setattr(creds.crypto, "dec", lambda x: "KEY")
    def run(args, env, cwd=None, timeout=600):
        if "fetch" in args:
            st["fetched"].append(args[-2])
            if st["fail"]: raise RuntimeError("Repository not found")
        return "abc123"
    monkeypatch.setattr(creds, "_run", run); return st

def test_rewrites_when_only_deploy_key(env, tmp_path):
    env["creds"] = {"ssh": {"secret_enc": "x"}}; logs = []
    creds.clone("a1", {"git": "https://github.com/o/r"}, str(tmp_path / "d"), logs.append, account_id=1)
    assert env["fetched"] == ["git@github.com:o/r.git"] and any("deploy key" in l for l in logs)

def test_no_rewrite_with_token(env, tmp_path):
    env["creds"] = {"ssh": {"secret_enc": "x"}, "token": {"secret_enc": "x", "username": "u"}}
    creds.clone("a1", {"git": "https://github.com/o/r"}, str(tmp_path / "d"), lambda m: None, account_id=1)
    assert env["fetched"] == ["https://github.com/o/r"]

def test_hint_for_unknown_host_with_key(env, tmp_path):
    env["creds"] = {"ssh": {"secret_enc": "x"}}; env["fail"] = True
    with pytest.raises(RuntimeError) as e: creds.clone("a1", {"git": "https://git.example.com/o/r"}, str(tmp_path / "d"), lambda m: None, account_id=1)
    assert "only work over SSH" in str(e.value) and "git@git.example.com:" in str(e.value)

def test_hint_public(env, tmp_path):
    env["fail"] = True
    with pytest.raises(RuntimeError) as e: creds.clone("a1", {"git": "https://github.com/o/r"}, str(tmp_path / "d"), lambda m: None, account_id=1)
    assert "aloof git deploy-key github.com -a a1" in str(e.value) and "git@github.com:" in str(e.value)
