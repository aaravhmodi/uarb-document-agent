import urllib.error
import urllib.request

from uarb_agent import links


def test_serves_exact_links_only(tmp_path):
    token = "a" * 24
    (tmp_path / token).mkdir()
    (tmp_path / token / "M12205.zip").write_bytes(b"zipdata")
    server = links.serve(tmp_path, 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert urllib.request.urlopen(f"{base}/{token}/M12205.zip").read() == b"zipdata"
        for bad in ["/", f"/{token}/", f"/{token}/../{token}/M12205.zip", "/short/M12205.zip", f"/{token}/x.zip"]:
            try:
                urllib.request.urlopen(base + bad)
                raise AssertionError(bad)
            except urllib.error.HTTPError as e:
                assert e.code == 404
    finally:
        server.shutdown()


def test_cleanup_removes_old(tmp_path):
    (tmp_path / "old").mkdir()
    links.cleanup(tmp_path, days=-1)  # everything counts as old
    assert not (tmp_path / "old").exists()
