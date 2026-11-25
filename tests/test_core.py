"""Offline unit tests: no network, no live scraper required.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cinemelb.config import MELBOURNE_BOUNDS, REGISTRY_PATH
from cinemelb.geo import format_distance, haversine_km, parse_latlon, travel_minutes
from cinemelb.models import (
    MELBOURNE_TZ,
    film_key,
    make_screening,
    normalise_title,
    slugify,
    to_utc,
)
from cinemelb.store import Store


class TestGeo(unittest.TestCase):
    def test_haversine_known_distance(self):
        # Flinders Street Station -> Southern Cross Station ~1.5 km
        d = haversine_km(-37.8183, 144.9671, -37.8183, 144.9526)
        self.assertAlmostEqual(d, 1.28, delta=0.15)

    def test_haversine_zero(self):
        self.assertEqual(haversine_km(-37.8, 144.9, -37.8, 144.9), 0.0)

    def test_parse_latlon_variants(self):
        self.assertEqual(parse_latlon("-37.81,144.96"), (-37.81, 144.96))
        self.assertEqual(parse_latlon(" -37.81 ; 144.96 "), (-37.81, 144.96))
        self.assertIsNone(parse_latlon("Brunswick"))
        self.assertIsNone(parse_latlon("999,999"))

    def test_format_distance(self):
        self.assertEqual(format_distance(0.4), "400 m")
        self.assertEqual(format_distance(2.15), "2.1 km")
        self.assertEqual(format_distance(20.4), "20 km")

    def test_travel_minutes_positive(self):
        self.assertGreater(travel_minutes(5.0), 0)
        self.assertGreaterEqual(travel_minutes(0.1), 1)


class TestModels(unittest.TestCase):
    def test_normalise_title_handles_articles_and_punctuation(self):
        a = normalise_title("The Hunger Games: Sunrise on the Reaping")
        b = normalise_title("Hunger Games - Sunrise on the Reaping")
        self.assertEqual(a, b)

    def test_normalise_ampersand(self):
        self.assertEqual(normalise_title("Fast & Furious"), normalise_title("Fast and Furious"))

    def test_normalise_trailing_article(self):
        """Cinema Nova writes "Fox, The"; everyone else writes "The Fox"."""
        self.assertEqual(normalise_title("Fox, The"), normalise_title("The Fox"))
        self.assertEqual(normalise_title("Matrix, The"), normalise_title("The Matrix"))
        self.assertEqual(normalise_title("Beautiful Day, A"), normalise_title("A Beautiful Day"))

    def test_film_key_trailing_article_collapses(self):
        self.assertEqual(film_key("Fox, The"), film_key("The Fox"))

    def test_slugify(self):
        self.assertEqual(slugify("Hoyts Melbourne Central"), "hoyts-melbourne-central")
        self.assertEqual(slugify("Cinema Nova (Carlton)"), "cinema-nova-carlton")

    def test_film_key_stable(self):
        self.assertEqual(film_key("Dune: Part Three"), film_key("dune part three"))

    def test_curly_apostrophe_same_key_as_straight(self):
        """Regression: the ASCII encode used to run *before* the quote fold.

        U+2019 is not decomposed by NFKD, so ``encode("ascii", "ignore")``
        dropped it while a straight apostrophe became a space.  "Disney’s
        Hexed" therefore keyed as ``disneys-hexed`` and "Disney's Hexed" as
        ``disney-s-hexed`` — the same film split into two catalogue entries.
        """
        self.assertEqual(film_key("Disney\u2019s Hexed"), film_key("Disney's Hexed"))
        self.assertEqual(
            normalise_title("Who\u2019s Afraid of Virginia Woolf?"),
            normalise_title("Who's Afraid of Virginia Woolf?"),
        )
        # Curly double quotes and the en dash must fold too.
        self.assertEqual(film_key("GIANT \u2013 The Play"), film_key("GIANT - The Play"))

    def test_make_screening_uses_melbourne_local_time(self):
        """A 7pm session must be 7pm in Melbourne, not 7pm UTC."""
        s = make_screening(
            cinema_id="x",
            film_title="Dune: Part Three",
            local_naive=datetime(2026, 11, 26, 19, 0),
            source="test",
        )
        self.assertEqual(s.local_date, date(2026, 11, 26))
        self.assertEqual(s.local_time, time(19, 0))
        # November is AEDT (UTC+11) -> 19:00 local == 08:00 UTC
        self.assertEqual(s.starts_at.astimezone(timezone.utc).hour, 8)

    def test_make_screening_winter_offset(self):
        """July is AEST (UTC+10)."""
        s = make_screening(
            cinema_id="x",
            film_title="Y",
            local_naive=datetime(2026, 7, 1, 19, 0),
            source="test",
        )
        self.assertEqual(s.starts_at.astimezone(timezone.utc).hour, 9)

    def test_to_utc_aware_input(self):
        dt = datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(to_utc(dt), dt)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed(self):
        from cinemelb.models import Cinema, Film

        self.store.upsert_cinemas([
            Cinema(id="nova", name="Cinema Nova", suburb="Carlton",
                   latitude=-37.7981, longitude=144.9679, metro=True),
            Cinema(id="astor", name="The Astor", suburb="St Kilda",
                   latitude=-37.858, longitude=144.9921, metro=True),
        ])
        self.store.upsert_films([Film(key="dune-part-three", title="Dune: Part Three")])
        a = make_screening(cinema_id="nova", film_title="Dune: Part Three",
                           local_naive=datetime(2026, 11, 26, 19, 30), source="t")
        b = make_screening(cinema_id="astor", film_title="Dune: Part Three",
                           local_naive=datetime(2026, 11, 26, 20, 0), source="t")
        self.store.replace_screenings("nova", [a])
        self.store.replace_screenings("astor", [b])

    def test_roundtrip_and_filters(self):
        self._seed()
        self.assertEqual(len(self.store.cinemas()), 2)
        hits = self.store.screenings(day=date(2026, 11, 26))
        self.assertEqual(len(hits), 2)
        late = self.store.screenings(day=date(2026, 11, 26), after="19:45")
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0]["cinema_id"], "astor")

    def test_replace_is_authoritative(self):
        self._seed()
        self.store.replace_screenings("nova", [])
        self.assertEqual(len(self.store.screenings(cinema_ids=["nova"])), 0)
        self.assertEqual(len(self.store.screenings(cinema_ids=["astor"])), 1)

    def test_metro_flag_survives(self):
        from cinemelb.models import Cinema

        self.store.upsert_cinemas([Cinema(id="far", name="Far Away", metro=False)])
        self.assertFalse(self.store.cinema("far")["metro"])


class TestRegistry(unittest.TestCase):
    """The shipped registry must be complete and geographically sane."""

    @classmethod
    def setUpClass(cls):
        if not REGISTRY_PATH.exists():
            raise unittest.SkipTest("registry not built yet")
        cls.rows = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

    def test_has_many_venues(self):
        self.assertGreaterEqual(len(self.rows), 50,
                                "Melbourne has 50+ venues; the registry looks truncated")

    def test_required_fields(self):
        for r in self.rows:
            with self.subTest(venue=r["id"]):
                self.assertTrue(r["name"])
                self.assertTrue(r["suburb"])
                self.assertRegex(r["postcode"], r"^\d{4}$")
                self.assertIsNotNone(r["latitude"])
                self.assertIsNotNone(r["longitude"])
                self.assertTrue(r["source"], "every venue needs an adapter")

    def test_coordinates_inside_greater_melbourne(self):
        b = MELBOURNE_BOUNDS
        for r in self.rows:
            with self.subTest(venue=r["id"]):
                self.assertTrue(
                    b["min_lat"] <= r["latitude"] <= b["max_lat"]
                    and b["min_lon"] <= r["longitude"] <= b["max_lon"],
                    f"{r['name']} at {r['latitude']},{r['longitude']} is outside Melbourne",
                )

    def test_includes_independent_venues(self):
        names = " ".join(r["name"].lower() for r in self.rows)
        for expected in ("nova", "astor", "lido", "classic", "sun", "cameo", "imax", "acmi"):
            with self.subTest(venue=expected):
                self.assertIn(expected, names,
                              f"expected the independent venue {expected!r} in the registry")

    def test_no_duplicate_ids(self):
        ids = [r["id"] for r in self.rows]
        self.assertEqual(len(ids), len(set(ids)))


class TestQueries(unittest.TestCase):
    """Query engine behaviour against a small in-memory dataset."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        from cinemelb.models import Cinema, Film

        self.store.upsert_cinemas([
            Cinema(id="nova", name="Cinema Nova", suburb="Carlton", chain="Independent",
                   latitude=-37.7981, longitude=144.9679, tags=["arthouse"]),
            Cinema(id="hoyts", name="Hoyts Melbourne Central", suburb="Melbourne",
                   chain="Hoyts", latitude=-37.8090, longitude=144.9636, tags=[]),
            Cinema(id="far", name="Far Cinema", suburb="Frankston", chain="",
                   latitude=-38.14, longitude=145.12, tags=[]),
        ])
        self.store.upsert_films([
            Film(key="dune-part-three", title="Dune: Part Three"),
            Film(key="the-fox", title="The Fox"),
        ])
        self.store.replace_screenings("nova", [
            make_screening(cinema_id="nova", film_title="Dune: Part Three",
                           local_naive=datetime(2026, 11, 26, 19, 0), source="t")])
        self.store.replace_screenings("hoyts", [
            make_screening(cinema_id="hoyts", film_title="Dune: Part Three",
                           local_naive=datetime(2026, 11, 26, 21, 30),
                           screen="Cinema 5", source="t"),
            make_screening(cinema_id="hoyts", film_title="The Fox",
                           local_naive=datetime(2026, 11, 26, 20, 0), source="t")])
        self.store.replace_screenings("far", [
            make_screening(cinema_id="far", film_title="Dune: Part Three",
                           local_naive=datetime(2026, 11, 26, 18, 0), source="t")])

    def tearDown(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_fuzzy_film_lookup(self):
        from cinemelb.query import find_film

        matches, hits = find_film(self.store, "dune")
        self.assertTrue(matches)
        self.assertEqual(len(hits), 3)

    def test_film_time_filter(self):
        from cinemelb.query import Filters, find_film

        _, hits = find_film(self.store, "dune", filters=Filters(after=time(20, 0)))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].screening["local_time"], "21:30")

    def test_nearest_ranks_by_distance(self):
        from cinemelb.query import nearest

        # near Carlton: nova is ~1.2km, hoyts ~1.3km, frankston is far
        hits = nearest(self.store, (-37.7981, 144.9679), radius_km=5)
        ids = [h.cinema["id"] for h in hits]
        self.assertIn("nova", ids)
        self.assertNotIn("far", ids)
        self.assertLessEqual(hits[0].distance_km, 0.2)

    def test_tag_filter(self):
        from cinemelb.query import Filters, find_film

        _, hits = find_film(self.store, "dune", filters=Filters(tags=["arthouse"]))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].cinema["id"], "nova")

    def test_unknown_film_returns_nothing(self):
        from cinemelb.query import find_film

        matches, hits = find_film(self.store, "zzzzz not a film")
        self.assertEqual(matches, [])
        self.assertEqual(hits, [])

    def test_sold_out_hidden_by_default(self):
        """A full session is real information, but not a booking option."""
        from cinemelb.models import make_screening
        from cinemelb.query import Filters, find_film

        sold = make_screening(cinema_id="hoyts", film_title="Dune: Part Three",
                              local_naive=datetime(2026, 11, 27, 21, 0), source="t")
        sold.sold_out = True
        self.store.replace_screenings("hoyts", [sold])

        _, default_hits = find_film(self.store, "dune")
        self.assertEqual(len(default_hits), 2)          # nova + far remain

        _, with_sold = find_film(self.store, "dune", filters=Filters(include_sold_out=True))
        self.assertEqual(len(with_sold), 3)

    def test_only_best_match_queried_by_default(self):
        """`dune` must not silently merge "Dune" and "Dune: Part Two"."""
        from cinemelb.models import Film
        from cinemelb.query import find_film

        self.store.upsert_films([Film(key="dune-part-two", title="Dune: Part Two")])
        from cinemelb.models import make_screening

        self.store.replace_screenings("nova", [
            make_screening(cinema_id="nova", film_title="Dune: Part Three",
                           local_naive=datetime(2026, 11, 26, 19, 0), source="t"),
            make_screening(cinema_id="nova", film_title="Dune: Part Two",
                           local_naive=datetime(2026, 11, 26, 21, 0), source="t"),
        ])
        matches, hits = find_film(self.store, "dune")
        self.assertEqual(len(matches), 1)
        self.assertEqual({h.screening["film_title"] for h in hits}, {"Dune: Part Three"})

    def test_limit_keeps_every_venue(self):
        """A venue with one far-future session must survive the default limit.

        Regression: results are time-sorted, so a global truncation let the
        venues with the most sessions fill the list and dropped a venue whose
        only session was weeks out — exactly how IMAX's 70mm run vanished from
        "where is Dune: Part Three showing?".
        """
        from cinemelb.models import Cinema
        from cinemelb.query import Filters, find_film

        # 40 venues with many early sessions, plus one venue with a single
        # session three weeks out.
        venues = [
            Cinema(id=f"v{i}", name=f"Venue {i}", suburb="Melbourne",
                   latitude=-37.8, longitude=144.96)
            for i in range(40)
        ]
        venues.append(Cinema(id="imax", name="IMAX Melbourne", suburb="Carlton",
                             latitude=-37.80, longitude=144.96))
        self.store.upsert_cinemas(venues)

        for i in range(40):
            rows = [
                make_screening(cinema_id=f"v{i}", film_title="Dune: Part Three",
                               local_naive=datetime(2026, 11, 26, 10 + h, 0), source="t")
                for h in range(8)
            ]
            self.store.replace_screenings(f"v{i}", rows)
        self.store.replace_screenings("imax", [
            make_screening(cinema_id="imax", film_title="DUNE: PART THREE - IMAX 70MM",
                           local_naive=datetime(2026, 12, 17, 19, 0), source="t",
                           film_key_override="dune-part-three")])

        _, hits = find_film(self.store, "dune", filters=Filters(limit=40))
        self.assertIn("imax", {h.cinema["id"] for h in hits},
                      "a venue whose only session is weeks out was truncated away")
        # Every venue's first session is reserved, so 41 venues fit in 40 slots
        # only if the cap is respected — it is a hard cap, so one venue may be
        # left out, but the *breadth* must be near-total rather than 5 venues.
        self.assertGreaterEqual(len({h.cinema["id"] for h in hits}), 20)


class TestImaxAdapter(unittest.TestCase):
    """Pure-function checks on the IMAX title handling (no network)."""

    def test_presentation_suffix_stripped(self):
        from cinemelb.adapters.imax import _base_title

        cases = {
            "DUNE: PART THREE - IMAX 70MM": "DUNE: PART THREE",
            "DUNE: PART THREE - 4K PRESENTATION": "DUNE: PART THREE",
            "THE ODYSSEY - IMAX 70MM PRESENTATION": "THE ODYSSEY",
            "Dune: Part Three": "Dune: Part Three",
            "Wild Asia 3D": "Wild Asia 3D",
        }
        for raw, expected in cases.items():
            with self.subTest(title=raw):
                self.assertEqual(_base_title(raw), expected)

    def test_suffixed_title_keys_to_the_same_film(self):
        """The whole point: IMAX's wording must not hide the venue."""
        from cinemelb.adapters.imax import _base_title
        from cinemelb.models import film_key

        self.assertEqual(
            film_key(_base_title("DUNE: PART THREE - IMAX 70MM")),
            film_key("Dune: Part Three"),
        )

    def test_format_detection(self):
        from cinemelb.adapters.imax import _format_of

        self.assertEqual(_format_of(["IMAX70MM"], False), "IMAX 70mm")
        self.assertEqual(_format_of(["IMAX70MM"], True), "IMAX 70mm")
        self.assertEqual(_format_of(["3D"], False), "IMAX 3D")
        self.assertEqual(_format_of([], False), "IMAX")
        self.assertEqual(_format_of(["IMAX 70MM PRESENTATION"], False), "IMAX 70mm")


class TestMovingStoryAdapter(unittest.TestCase):
    """Sold-out sessions render without an href; they must still be stored."""

    #: Verbatim shape of a sold-out session anchor on a Moving Story date page.
    SOLD_OUT = (
        '<li class="Stack">'
        '<div class="Markup Movie" data-genres="">'
        '<div class="Wrapper">'
        '<a href="/movies/classic-live-comedy-featuring-bron-lewis" class="Image">'
        '<img src="https://example.test/x.jpg" alt="" /></a>'
        '<span class="Byline">Exempt</span>'
        '<span class="Title"><a href="/movies/classic-live-comedy-featuring-bron-lewis">'
        "Classic Live Comedy - Featuring Bron Lewis</a></span>"
        "</div>"
        '<div class="Tickets"><ul class="Sessions"><li>'
        '<a class="Link sessions-link" data-id="134617" '
        'data-name="Classic Live Comedy - Featuring Bron Lewis" '
        'data-genre="Live Performance,Comedy">'
        '<span class="Time">8:00 pm</span>'
        '<span class="Attribute">LiveComedy</span>'
        '<span class="Attribute SoldOut">Sold Out</span>'
        "</a></li></ul></div></div></li>"
    )

    def _parse(self, markup: str):
        from cinemelb.adapters.movingstory import MovingStoryAdapter
        from cinemelb.adapters.base import AdapterOutput

        adapter = MovingStoryAdapter()
        out = AdapterOutput()
        cinema = {"id": "classic-elsternwick", "website": "https://example.test"}
        adapter._parse_day(markup, "2026-11-27", cinema, "https://example.test", out, set())
        return out

    def test_sold_out_session_without_href_is_stored(self):
        out = self._parse(self.SOLD_OUT)
        self.assertEqual(len(out.screenings), 1, "sold-out session was dropped")
        s = out.screenings[0]
        self.assertEqual(s.film_title, "Classic Live Comedy - Featuring Bron Lewis")
        self.assertEqual(s.local_time, time(20, 0))
        self.assertTrue(s.sold_out, "sold_out flag not set from the Sold Out attribute")
        self.assertEqual(s.ticket_url, "", "no href means no ticket URL")

    def test_normal_session_still_carries_ticket_url(self):
        markup = self.SOLD_OUT.replace(
            '<a class="Link sessions-link" data-id="134617"',
            '<a class="Link sessions-link" href="/tickets?c=0000000002&amp;s=125521" '
            'data-id="125521"',
        ).replace(
            '<span class="Attribute SoldOut">Sold Out</span>', ""
        )
        out = self._parse(markup)
        self.assertEqual(len(out.screenings), 1)
        s = out.screenings[0]
        self.assertFalse(s.sold_out)
        self.assertEqual(
            s.ticket_url, "https://example.test/tickets?c=0000000002&s=125521"
        )


if __name__ == "__main__":
    unittest.main()
