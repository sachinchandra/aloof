import threading
from paas import site
def test_cur_is_thread_local():
    site.CUR.update(path="/pricing"); seen = []
    t = threading.Thread(target=lambda: seen.append(site.CUR["path"])); t.start(); t.join()
    assert seen == ["/"] and site.CUR["path"] == "/pricing"
    site.CUR.update(path="/")
