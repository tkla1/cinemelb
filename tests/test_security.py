"""Security regression tests: one per finding in docs/security-review-2026-11-26.md.

These are the checks the review asked for. They are offline and fast; the
Playwright proofs in scratch/review/ remain the executable witnesses.

Run with:  uv run python -m unittest tests.test_security -v
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, time, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cinemelb.export import SITE_MARKER, write_site
from cinemelb.models import Cinema, Film, Screening
from cinemelb.sanitize import clean_text, json_for_script, safe_href
from cinemelb.serve import make_server
from cinemelb.store import Store

EVIL = 'Evil</script><script>window.__PWNED=1</script>'


def _hit(name: str = "Venue", title: str = "Film", url: str = "") -> object:
    from cinemelb.query import Hit

    return Hit(
        cinema={
            "id": "c1", "name": name, "suburb": "Brunswick", "chain": "",
            "tags": ["x"], "latitude": -37.76, "longitude": 144.96,
        },
        screening={
            "film_title": title, "local_date": "2026-11-27",
            "local_time": "19:00", "ticket_url": url, "format": "70mm",
            "sold_out": 0,
        },
    )


class TestSanitize(unittest.TestCase):
    def test_clean_text_strips_controls(self):
        self.assertEqual(clean_text("A\x1b[2JB\x07"), "A[2JB")
        self.assertEqual(clean_text(None), "")

    def test_safe_href_scheme_allowlist(self):
        self.assertEqual(safe_href("https://example.com/x"), "https://example.com/x")
        self.assertEqual(safe_href(" http://example.com "), "http://example.com")
        for bad in ("javascript:alert(1)", "data:text/html,x", "file:///etc/passwd",
                    "//evil.example/x", "", None):
            self.assertEqual(safe_href(bad), "", bad)

    def test_json_for_script_cannot_close_the_tag(self):
        out = json_for_script({"t": EVIL})
        self.assertNotIn("</script>", out)
        self.assertIn("\\u003c/script", out)
        # Still valid JSON, and parses back to the same string.
        self.assertEqual(json.loads(out), {"t": EVIL})


class TestMapExport(unittest.TestCase):
    def test_render_escapes_the_inline_payload(self):
        from cinemelb.mapview import render

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            out = Path(tmp) / "map.html"
            render([_hit(name=EVIL, title=EVIL)], title=EVIL, out_path=out)
            text = out.read_text(encoding="utf-8")
        self.assertNotIn("</script><script>window.__PWNED", text)
        self.assertIn("\\u003c/script", text)

    def test_render_drops_javascript_ticket_urls(self):
        from cinemelb.mapview import render

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            out = Path(tmp) / "map.html"
            render([_hit(url="javascript:window.__PWNED=1")], title="t", out_path=out)
            text = out.read_text(encoding="utf-8")
        self.assertNotIn("javascript:window.__PWNED", text)

    def test_cdn_assets_have_integrity(self):
        from cinemelb.mapview import _TEMPLATE

        external = re.findall(r'(?:src|href)="(https?://[^"]+)"[^>]*>', _TEMPLATE)
        self.assertTrue(external)
        for tag in re.findall(r"<(?:script|link)\b[^>]*https?://[^>]*>", _TEMPLATE):
            self.assertIn("integrity=", tag, tag)


class TestModelIngestion(unittest.TestCase):
    def test_scraped_control_characters_never_enter_the_model(self):
        c = Cinema(id="v", name="V\x1b]52;c;A\x07", suburb="B\x1b[2J", tags=["t\x07"])
        f = Film(key="k", title="T\x1b[2J", genres=["g\x07"], aka=["o\x1b"])
        s = Screening(
            id="s", cinema_id="v", film_key="k", film_title="F\x1b[H",
            starts_at=datetime.now(timezone.utc), local_date=date.today(),
            local_time=time(12), screen="1\x1b", format="70\x1bmm",
            ticket_url="https://x\x1b", attributes=["CC\x1b"],
        )
        for value in (c.name, c.suburb, f.title, f.genres[0], f.aka[0],
                      s.film_title, s.screen, s.format, s.ticket_url):
            self.assertNotIn("\x1b", value)
        self.assertEqual(c.tags, ["t"])


class TestExportGuard(unittest.TestCase):
    def test_refuses_to_delete_an_unmarked_directory(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            out = Path(tmp) / "site"
            out.mkdir()
            (out / "user-file.txt").write_text("keep me", encoding="utf-8")
            with self.assertRaises(SystemExit):
                write_site({"version": 2}, out)
            self.assertTrue((out / "user-file.txt").exists())
            self.assertFalse((out / "index.html").exists())

    def test_replaces_a_marked_site(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            out = Path(tmp) / "site"
            write_site({"version": 2}, out)
            self.assertTrue((out / SITE_MARKER).exists())
            write_site({"version": 2}, out)          # second run is fine
            self.assertTrue((out / "index.html").exists())
            self.assertFalse((out.with_name("site.building")).exists())

    def test_force_replaces_an_unmarked_directory(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            out = Path(tmp) / "site"
            out.mkdir()
            (out / "old.txt").write_text("old", encoding="utf-8")
            write_site({"version": 2}, out, force=True)
            self.assertFalse((out / "old.txt").exists())
            self.assertTrue((out / SITE_MARKER).exists())

    def test_refuses_a_filesystem_root(self):
        with self.assertRaises(SystemExit):
            write_site({"version": 2}, Path(Path.cwd().anchor))


class TestServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = make_server("127.0.0.1", 0,
                                  db=str(Path(self.tmp.name) / "t.sqlite3"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.app.store.close()
        self.tmp.cleanup()

    def _get(self, path: str, host: str | None = None) -> tuple[int, str]:
        req = urllib.request.Request(self.base + path)
        if host is not None:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def test_foreign_host_is_rejected(self):
        status, _ = self._get("/api/venues", host="attacker.example")
        self.assertEqual(status, 421)
        status, _ = self._get("/", host="attacker.example")
        self.assertEqual(status, 421)

    def test_loopback_hosts_are_served(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            status, _ = self._get("/api/venues", host=host)
            self.assertEqual(status, 200, host)

    def test_500_does_not_echo_the_exception(self):
        def boom(_path):
            raise RuntimeError("sqlite3 /home/user/secret/cinemelb.sqlite3")

        self.server.app.handle = boom          # type: ignore[method-assign]
        status, body = self._get("/api/venues")
        self.assertEqual(status, 500)
        self.assertNotIn("secret", body)
        self.assertNotIn("sqlite3", body)
        self.assertIn("reference", body)


class TestCliOutput(unittest.TestCase):
    def test_scraped_escapes_do_not_reach_the_terminal(self):
        from typer.testing import CliRunner

        from cinemelb.cli import app

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "cli.sqlite3"
            now = datetime.now(timezone.utc)
            with Store(db) as store:
                store.upsert_cinemas([Cinema(id="v", name="V", latitude=-37.7,
                                             longitude=144.9)])
                store.upsert_films([Film(key="f", title="F")])
                store.replace_screenings("v", [Screening(
                    id="s", cinema_id="v", film_key="f", film_title="F",
                    starts_at=now, local_date=now.date(), local_time=now.time())])
                # Simulate rows written before the ingestion strip existed.
                store.conn.execute(
                    "UPDATE films SET title=? WHERE key='f'",
                    ("Proof \x1b[2J\x1b[H Film",),
                )
                store.conn.execute(
                    "UPDATE cinemas SET name=? WHERE id='v'",
                    ("V \x1b]52;c;SGVsbG8=\x07",),
                )
                store.conn.commit()
                out = CliRunner().invoke(
                    app, ["--db", str(db), "whats-on"]).stdout
        self.assertNotIn("\x1b", out)
        self.assertIn("Proof", out)


if __name__ == "__main__":
    unittest.main()
