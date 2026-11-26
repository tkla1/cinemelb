"""Bundle contract: version 3 and the published-alias map."""

import tempfile
import unittest
from datetime import date, datetime, time, timezone
from pathlib import Path

from cinemelb.export import _validate, build_bundle
from cinemelb.models import Cinema, Film, Screening
from cinemelb.store import Store


class TestBundleAliases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.store.upsert_cinemas([
            Cinema(id="nova", name="Cinema Nova", suburb="Carlton",
                   latitude=-37.7981, longitude=144.9679, metro=True),
        ])
        # The listing publishes "BFF26 Parky"; ingestion cleans it and keeps
        # the published string in aka.
        self.store.upsert_films([
            Film(key="parky", title="BFF26 Parky", aka=["BFF26 Parky"]),
            Film(key="dune", title="Dune: Part Three"),
        ])
        screening = Screening(
            id="s1", cinema_id="nova", film_key="parky",
            film_title="BFF26 Parky",
            starts_at=datetime(2026, 11, 26, 8, 30, tzinfo=timezone.utc),
            local_date=date(2026, 11, 26), local_time=time(19, 30), source="t",
        )
        self.store.replace_screenings("nova", [screening])

    def tearDown(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_bundle_v3_ships_aliases(self):
        bundle = build_bundle(self.store)
        self.assertEqual(bundle["version"], 3)
        self.assertEqual(bundle["aliases"], {"parky": ["BFF26 Parky"]})
        film = next(row for row in bundle["films"] if row[0] == "parky")
        self.assertEqual(film[1], "Parky")

    def test_alias_equal_to_title_is_dropped(self):
        self.store.upsert_films([Film(key="dune", title="Dune: Part Three",
                                      aka=["Dune: Part Three"])])
        bundle = build_bundle(self.store)
        self.assertNotIn("dune", bundle["aliases"])

    def test_dangling_alias_fails_validation(self):
        bundle = build_bundle(self.store)
        broken = dict(bundle)
        broken["aliases"] = {"not-a-film": ["BFF26 Nowhere"]}
        with self.assertRaises(SystemExit):
            _validate(broken)

    def test_empty_alias_value_fails_validation(self):
        bundle = build_bundle(self.store)
        broken = dict(bundle)
        broken["aliases"] = {"parky": [""]}
        with self.assertRaises(SystemExit):
            _validate(broken)


if __name__ == "__main__":
    unittest.main()
