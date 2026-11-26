"""Programme-prefix handling: "BFF26 Parky" is the film "Parky"."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cinemelb import titles
from cinemelb.models import Film, film_key
from cinemelb.store import Store


class TestDisplayTitle(unittest.TestCase):
    def test_british_film_festival_code(self) -> None:
        self.assertEqual(titles.display_title("BFF26 Parky"), "Parky")
        self.assertEqual(titles.display_title("BFF 26 Parky"), "Parky")
        self.assertEqual(titles.display_title("BFF26: Parky"), "Parky")
        self.assertEqual(titles.display_title("BFF Hill"), "Hill")

    def test_other_festival_prefixes(self) -> None:
        self.assertEqual(titles.display_title("JIFF: Spaceballs (1987)"),
                         "Spaceballs (1987)")
        self.assertEqual(titles.display_title("MIFF: The Seed of the Sacred Fig"),
                         "The Seed of the Sacred Fig")
        self.assertEqual(titles.display_title("CASFFA: Princess Stella"),
                         "Princess Stella")
        self.assertEqual(titles.display_title("Jodofest: El Topo"), "El Topo")

    def test_venue_and_programme_prefixes(self) -> None:
        self.assertEqual(titles.display_title("Astor: The Last Waltz"),
                         "The Last Waltz")
        self.assertEqual(titles.display_title("Cult Classix: Suspiria (4k)"),
                         "Suspiria (4k)")
        self.assertEqual(titles.display_title("Palace Cinemas: The Fox"),
                         "The Fox")

    def test_leaves_real_titles_alone(self) -> None:
        for raw in (
            "The Fox",
            "Staff Only",
            "Palace of the Damned",
            "Astor",
            "BFF",
            "A Beautiful Planet 3D",
        ):
            self.assertEqual(titles.display_title(raw), raw)

    def test_stacked_prefixes(self) -> None:
        self.assertEqual(titles.display_title("JIFF: Astor: Paper Tiger"),
                         "Paper Tiger")

    def test_original_is_recoverable(self) -> None:
        self.assertEqual(titles.programme_of("BFF26 Parky"), "BFF26")
        self.assertEqual(titles.programme_of("Cult Classix: Suspiria"), "Cult Classix")
        self.assertEqual(titles.programme_of("Parky"), "")


class TestFilmKey(unittest.TestCase):
    def test_prefixed_and_plain_spellings_share_a_key(self) -> None:
        self.assertEqual(film_key("BFF26 Parky"), film_key("Parky"))
        self.assertEqual(film_key("JIFF: Spaceballs (1987)"),
                         film_key("Spaceballs (1987)"))
        self.assertEqual(film_key("Cult Classix: Suspiria (4k)"),
                         film_key("Suspiria (4k)"))

    def test_different_films_stay_apart(self) -> None:
        self.assertNotEqual(film_key("BFF26 Hill"), film_key("Notting Hill"))


class TestStoreNormalise(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self) -> None:
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _insert_film(self, key: str, title: str) -> None:
        self.store.conn.execute(
            "insert into films (key,title,year,runtime_minutes,rating,genres,"
            "synopsis,poster_url,trailer_url,directors,aka) "
            "values (?,?,?,?,?,?,?,?,?,?,?)",
            (key, title, None, 90, "M", "[]", "", "", "", "[]", "[]"),
        )

    def _insert_screening(self, screening_id: str, key: str, title: str) -> None:
        self.store.conn.execute(
            "insert into screenings (id,cinema_id,film_key,film_title,starts_at,"
            "local_date,local_time,screen,format,attributes,ticket_url,sold_out,source) "
            "values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (screening_id, "c1", key, title, "2026-11-26T09:00:00+00:00",
             "2026-11-26", "20:00", "", "", "[]", "", 0, "test"),
        )
        self.store.conn.commit()

    def test_rename_keeps_screenings_and_records_the_original(self) -> None:
        self._insert_film("bff26-parky", "BFF26 Parky")
        self._insert_screening("s1", "bff26-parky", "BFF26 Parky")

        stats = self.store.normalise_films()

        self.assertEqual(stats["renamed"], 1)
        row = self.store.conn.execute(
            "select key,title,aka from films").fetchone()
        self.assertEqual(row["key"], "parky")
        self.assertEqual(row["title"], "Parky")
        self.assertIn("BFF26 Parky", json.loads(row["aka"]))
        moved = self.store.conn.execute(
            "select film_key from screenings where id='s1'").fetchone()
        self.assertEqual(moved["film_key"], "parky")

    def test_collision_merges_into_the_plain_row(self) -> None:
        self._insert_film("parky", "Parky")
        self._insert_film("bff26-parky", "BFF26 Parky")
        self._insert_screening("s1", "bff26-parky", "BFF26 Parky")
        self._insert_screening("s2", "parky", "Parky")

        stats = self.store.normalise_films()

        self.assertEqual(stats["merged"], 1)
        rows = self.store.conn.execute(
            "select key,title from films order by key").fetchall()
        self.assertEqual([r["key"] for r in rows], ["parky"])
        keys = [r["film_key"] for r in self.store.conn.execute(
            "select film_key from screenings order by id").fetchall()]
        self.assertEqual(keys, ["parky", "parky"])
        aka = json.loads(self.store.conn.execute(
            "select aka from films where key='parky'").fetchone()["aka"])
        self.assertIn("BFF26 Parky", aka)

    def test_idempotent(self) -> None:
        self._insert_film("bff26-parky", "BFF26 Parky")
        self.store.normalise_films()
        stats = self.store.normalise_films()
        self.assertEqual(stats, {"renamed": 0, "merged": 0, "retitled": 0})

    def test_new_upsert_is_already_clean(self) -> None:
        self.store.upsert_films([
            Film(key=film_key("BFF26 Parky"), title="BFF26 Parky"),
        ])
        row = self.store.conn.execute("select key,title,aka from films").fetchone()
        self.assertEqual(row["key"], "parky")
        self.assertEqual(row["title"], "Parky")
        self.assertIn("BFF26 Parky", json.loads(row["aka"]))


if __name__ == "__main__":
    unittest.main()
