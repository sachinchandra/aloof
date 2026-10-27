"""Authorize page: the access summary matches the selected level and switches live when the radio changes."""
import re, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp_server"))
import oauth

def _visible(h): return re.findall(r'<span class="acc" data-level="(\d)">', h)
def _hidden(h): return re.findall(r'<span class="acc" data-level="(\d)" hidden>', h)

def test_one_summary_per_level_only_selected_visible():
    for sel in (1, 2, 3):
        h = oauth.access_html(3, sel, "Bot", "paas:admin")
        assert _visible(h) == [str(sel)] and sorted(_hidden(h)) == sorted({"1", "2", "3"} - {str(sel)})
        assert 'addEventListener("change"' in h and "s.hidden=s.dataset.level!=e.target.value" in h

def test_write_summary_is_accurate_and_not_full_access():
    h = oauth.access_html(3, 2, "Bot", "paas:admin")
    vis = re.search(r'<span class="acc" data-level="2">(.*?)</span>', h).group(1)
    assert "paas:write" in vis and "full access" not in vis and "delete apps" not in vis
    assert "can't destroy apps" in vis and "restore backups" in vis and "billing or API keys" in vis

def test_read_only_request_offers_no_switch():
    h = oauth.access_html(1, 1, "Bot", "paas:read")
    assert _visible(h) == ["1"] and not _hidden(h) and "<script" not in h

def test_lower_levels_never_offered_above_request():
    h = oauth.access_html(2, 2, "Bot", "paas:write")
    assert 'data-level="3"' not in h and "full access" not in h

def test_client_name_escaped():
    h = oauth.access_html(3, 3, "<x>", "paas")
    assert "<x>" not in h and "&lt;x&gt;" in h and "<code>paas</code>" in h
