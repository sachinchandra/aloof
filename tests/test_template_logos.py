import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from paas.templating import tpl_logo, registry, LOGO_DIR

def test_every_template_has_committed_logo():
    for tid in registry():
        assert os.path.isfile(os.path.join(LOGO_DIR, tid + ".svg")), tid
        out = tpl_logo(tid)
        assert "<svg" in out and "tlogo" in out and "<title>" not in out

def test_fallback_monogram():
    out = tpl_logo("no-such-app", "Cool Thing")
    assert "tlogo-mono" in out and ">CT<" in out and "<svg" not in out
    assert ">..<" not in tpl_logo("../etc", "x")
