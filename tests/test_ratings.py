"""Offline tests for build-time rating enrichment.

Every fixture is local: no test opens a socket.  The only network-shaped code
under test is the parsers and the fake session plumbing, which is exactly the
part that must not depend on a live provider being reachable.
"""

from __future__ import annotations

import gzip
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cinemelb import ratings


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status: int = 200, text: str = "", payload=None) -> None:
        self.status_code = status
        self.text = text
        self.content = text.encode("utf-8")
        self._payload = payload

    def json(self):
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)


class FakeSession:
    """A tiny requests.Session stand-in that records every call."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.handler(url, kwargs)
        if isinstance(result, FakeResponse) or isinstance(result, dict):
            return result
        return FakeResponse()


def _claims(**values):
    """Build the Wikidata claim shape used by the fixtures."""
    out = {}
    for prop, value in values.items():
        out[prop] = [{
            "rank": "normal",
            "mainsnak": {"snaktype": "value", "datavalue": {"value": value}},
        }]
    return out


def _write_gzip(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _tiny_imdb_dir(root: Path) -> tuple[Path, Path, Path]:
    """Write the three tiny IMDb TSVs used by the local-join tests."""
    basics = root / "title.basics.tsv.gz"
    akas = root / "title.akas.tsv.gz"
    ratings_file = root / "title.ratings.tsv.gz"

    _write_gzip(
        basics,
        "tconst\ttitleType\tprimaryTitle\toriginalTitle\tisAdult\tstartYear\t"
        "endYear\truntimeMinutes\tgenres\n"
        "tt0133093\tmovie\tThe Matrix\tThe Matrix\t0\t1999\t\\N\t136\tAction\n"
        "tt0080339\tmovie\tAkira\tAkira\t0\t1988\t\\N\t124\tAnimation\n"
        "tt2016001\tmovie\tAkira\tAkira\t0\t2016\t\\N\t90\tDrama\n"
        "tt0111161\tmovie\tThe Shawshank Redemption\tThe Shawshank Redemption\t0\t"
        "1994\t\\N\t142\tDrama\n"
        "tt1392190\tmovie\tMad Max: Fury Road\tMad Max: Fury Road\t0\t2015\t\\N\t"
        "120\tAction\n"
        "tt0000001\ttvSeries\tAkira\tAkira\t0\t2019\t\\N\t24\tAnimation\n"
        "tt0103064\tmovie\tTerminator 2: Judgment Day\tTerminator 2: Judgment Day\t"
        "0\t1991\t\\N\t137\tAction\n"
        "tt0114369\tmovie\tSe7en\tSe7en\t0\t1995\t\\N\t127\tCrime\n",
    )
    _write_gzip(
        akas,
        "titleId\tordering\ttitle\tregion\tlanguage\ttypes\tattributes\t"
        "isOriginalTitle\n"
        "tt1392190\t1\tMad Max 4: Fury Road\tAU\ten\t\\N\t\\N\t0\n"
        "tt1392190\t2\tMad Max: Fury Road\tGB\ten\t\\N\t\\N\t1\n"
        "tt0133093\t1\tMatrix, The\tAU\ten\t\\N\t\\N\t0\n",
    )
    _write_gzip(
        ratings_file,
        "tconst\taverageRating\tnumVotes\n"
        "tt0133093\t8.7\t2100000\n"
        "tt0080339\t8.2\t200000\n"
        "tt2016001\t4.0\t900\n"
        "tt0111161\t9.3\t3000000\n"
        "tt1392190\t8.1\t1200000\n"
        "tt0103064\t8.6\t1100000\n"
        "tt0114369\t8.6\t1800000\n"
        "tt0000001\t7.0\t20\n",
    )
    return basics, akas, ratings_file


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

class TestNormalisation(unittest.TestCase):
    def test_presentation_suffixes(self):
        cases = {
            "DUNE: PART THREE - IMAX 70MM": "dune part three",
            "Dune: Part Three": "dune part three",
            "Dune: Part 3": "dune part three",
            "Dune - 70MM": "dune",
            "Dune - 70MM IMAX": "dune",
            "Dune - IMAX 3D": "dune",
            "Dune - 35mm": "dune",
            "Dune - 4K Restoration": "dune",
            "Dune - 3D": "dune",
            "Dune - Vistavision": "dune",
            "Dune - Dolby Atmos": "dune",
            "Dune - Re-issue": "dune",
            "Dune - Subbed": "dune",
            "Dune Subtitled": "dune",
            "Dune - Dubbed": "dune",
            "Dune - Open Captions": "dune",
            "Dune - Eng Subs": "dune",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(ratings.normalise_title(raw), expected)

    def test_event_suffixes(self):
        cases = {
            "Hamlet - Live": "hamlet",
            "NT Live: Hamlet - Live": "nt live hamlet",
            "Hamlet Encore": "hamlet",
            "Hamlet Q&A": "hamlet",
            "The Room Q&A with Tommy Wiseau": "room",
            "Hamlet - 35th Anniversary": "hamlet",
            "Pans Labyrinth - 20th Anniversary": "pans labyrinth",
            "Rocky (1976) - 50th Anniversary": "rocky",
            "Hamlet - Tickets on sale now": "hamlet",
            "Andrew Lloyd Webber's Starlight Express - On Stage": "starlight express",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(ratings.normalise_title(raw), expected)

    def test_festival_and_venue_prefixes(self):
        cases = {
            "JFF26 Look Back": "look back",
            "BFF26 Savage House": "savage house",
            "MIFF26: The Seed of the Sacred Fig": "seed of the sacred fig",
            "Astor: The Thing": "thing",
            "Astor 2001: A Space Odyssey": "2001 a space odyssey",
            "The Astor Theatre: The Thing": "thing",
            "Cult Classix: Halloween": "halloween",
            "Reading Rewind: Jaws": "jaws",
            "Met Opera: La Boh\u00e8me": "la boheme",
            "La Scala: Turandot": "turandot",
            "Op\u00e9ra de Paris: Carmen": "carmen",
            "Teatro di San Carlo: Aida": "aida",
            "EXHIBITION ON SCREEN: Klimt": "klimt",
            "JIFF: Look Back": "look back",
            "CASFFA: X": "x",
            "Jodofest: Y": "y",
            "Jodofest 2026: Y": "y",
            "Jodofest2026: Y": "y",
            "MIFF 2026: The Seed of the Sacred Fig": "seed of the sacred fig",
            "Double Feature: Alien + Aliens": "alien aliens",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(ratings.normalise_title(raw), expected)

    def test_titles_with_numbers_are_not_mistaken_for_festival_codes(self):
        self.assertEqual(ratings.normalise_title("Apollo 13"), "apollo 13")
        self.assertEqual(ratings.normalise_title("Ocean's 11"), "oceans 11")
        self.assertEqual(
            ratings.normalise_title("The 40-Year-Old Virgin"),
            "40 year old virgin",
        )
        self.assertEqual(
            ratings.normalise_title("Dune 2026"), "dune 2026"
        )

    def test_articles_and_apostrophes(self):
        self.assertEqual(ratings.normalise_title("Fox, The"), "fox")
        self.assertEqual(ratings.normalise_title("The Fox"), "fox")
        self.assertEqual(ratings.normalise_title("Beautiful Day, A"), "beautiful day")
        self.assertEqual(ratings.normalise_title("Pan's Labyrinth"), "pans labyrinth")
        self.assertEqual(ratings.normalise_title("Pans Labyrinth"), "pans labyrinth")
        self.assertEqual(
            ratings.normalise_title("Pan\u2019s Labyrinth \u2013 20th Anniversary"),
            "pans labyrinth",
        )
        self.assertEqual(
            ratings.normalise_title("Disney\u2019s Hexed"),
            "hexed",
        )

    def test_ampersand_and_numerals(self):
        self.assertEqual(ratings.normalise_title("Fast & Furious"), "fast and furious")
        self.assertEqual(
            ratings.normalise_title("Fast and Furious"), "fast and furious"
        )
        self.assertEqual(
            ratings.normalise_title("Terminator 2: Judgement Day"),
            "terminator two judgement day",
        )
        self.assertEqual(
            ratings.normalise_title("Terminator 2: Judgment Day"),
            "terminator two judgment day",
        )
        # The resolver, not the normaliser, bridges the spelling difference.
        self.assertIn(
            "terminator two judgment day",
            ratings._strict_variants("terminator two judgement day"),
        )
        self.assertEqual(
            ratings.normalise_title("10 Cloverfield Lane"),
            "ten cloverfield lane",
        )
        # Years and larger numbers stay numeric.
        self.assertEqual(
            ratings.normalise_title("2001: A Space Odyssey"),
            "2001 a space odyssey",
        )

    def test_year_hint(self):
        self.assertEqual(ratings.title_year_hint("Seven (1995)"), 1995)
        self.assertEqual(ratings.normalise_title("Seven (1995)"), "seven")
        self.assertEqual(
            ratings.title_year_hint("Rocky (1976) - 50th Anniversary"), 1976
        )
        self.assertEqual(
            ratings.normalise_title("Rocky (1976) - 50th Anniversary"), "rocky"
        )
        self.assertIsNone(ratings.title_year_hint("2001: A Space Odyssey"))
        info = ratings.parse_title("Rocky (1976) - 50th Anniversary")
        self.assertEqual(info.strict, "rocky")
        self.assertEqual(info.year_hint, 1976)

    def test_mojibake(self):
        self.assertEqual(ratings.normalise_title("Am\u00c3\u00a9lie"), "amelie")
        self.assertEqual(ratings.normalise_title("Pok\ufffdmon"), "pokmon")
        # A right single quote encoded as UTF-8 then read as cp1252.
        self.assertEqual(ratings.normalise_title("Dune\u00e2\u20ac\u2122s"), "dunes")

    def test_middle_dot_matches_hyphen(self):
        self.assertEqual(
            ratings.normalise_title("WALL\u00b7E"),
            ratings.normalise_title("WALL-E"),
        )

    def test_noise_is_stripped_without_flattening(self):
        # "Part Three" is meaningful: Dune must not collapse into Dune: Part Two.
        self.assertNotEqual(
            ratings.normalise_title("Dune"),
            ratings.normalise_title("Dune: Part Three"),
        )
        self.assertEqual(
            ratings.normalise_title("Aakasamlo Oka Tara (Telugu, Eng Sub)"),
            "aakasamlo oka tara",
        )
        self.assertEqual(
            ratings.normalise_title("Terminator 3 (2003)"), "terminator three"
        )
        self.assertEqual(ratings.normalise_title("  Dune  "), "dune")
        self.assertEqual(
            ratings.normalise_title("  JFF26 Look Back  "), "look back"
        )

    def test_loose_key(self):
        self.assertEqual(
            ratings.loose_key(
                "The Lord of the Rings: The Fellowship of the Ring"
            ),
            "fellowship lord ring rings",
        )
        self.assertEqual(
            ratings.loose_key("Matrix Reloaded, The"), "matrix reloaded"
        )

    def test_cache_key_uses_year_hint(self):
        self.assertEqual(
            ratings.cache_key("Rocky (1976) - 50th Anniversary", None),
            "rocky (1976)",
        )
        self.assertEqual(
            ratings.cache_key("Dune: Part 3", None),
            ratings.cache_key("Dune: Part Three", None),
        )
        self.assertNotEqual(
            ratings.cache_key("Akira", 1988),
            ratings.cache_key("Akira", 2016),
        )
        self.assertEqual(
            ratings.cache_key("Dune: Part Three (2026)", None),
            ratings.cache_key("Dune: Part Three", 2026),
        )


# ---------------------------------------------------------------------------
# Cache and TTL
# ---------------------------------------------------------------------------

class TestCache(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ratings.json"
            cache = {"dune": {"imdb": 8.1, "source": ["imdb-bulk"]}}
            ratings.save_cache(path, cache)
            self.assertEqual(ratings.load_cache(path), cache)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["dune"]["imdb"], 8.1
            )

    def test_corrupt_cache_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ratings.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(ratings.load_cache(path), {})

    def _entry(self, days: int) -> dict:
        stamp = datetime.now(timezone.utc) - timedelta(days=days)
        return {
            "imdb_id": "tt0133093",
            "imdb": 8.7,
            "imdb_votes": 2100000,
            "rt": None,
            "rt_url": "",
            "mc": None,
            "mc_url": "",
            "confidence": "high",
            "source": ["imdb-bulk"],
            "fetched_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def test_ttl_fresh_entry_is_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ratings.json"
            key = ratings.cache_key("The Matrix", 1999)
            ratings.save_cache(path, {key: self._entry(1)})
            session = FakeSession(lambda url, kw: {})
            result = ratings.enrich(
                [{"key": "matrix", "title": "The Matrix", "year": 1999}],
                cache_path=path,
                session=session,
                pace=0,
            )
            self.assertEqual(result["stats"]["cached"], 1)
            self.assertEqual(result["stats"]["imdb"], 1)
            self.assertEqual(session.calls, [])

    def test_ttl_expired_entry_is_a_new_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ratings.json"
            key = ratings.cache_key("The Matrix", 1999)
            ratings.save_cache(path, {key: self._entry(20)})
            result = ratings.enrich(
                [{"key": "matrix", "title": "The Matrix", "year": 1999}],
                cache_path=path,
                session=FakeSession(lambda url, kw: {}),
                pace=0,
                limit=0,
            )
            self.assertEqual(result["stats"]["cached"], 0)
            self.assertEqual(result["stats"]["skipped"], 1)

    def test_limit_counts_only_new_lookups(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ratings.json"
            key = ratings.cache_key("The Matrix", 1999)
            ratings.save_cache(path, {key: self._entry(1)})
            films = [
                {"key": "matrix", "title": "The Matrix", "year": 1999},
                {"key": "x", "title": "Some Other Film", "year": 2024},
                {"key": "y", "title": "Yet Another Film", "year": 2025},
            ]
            result = ratings.enrich(
                films,
                cache_path=path,
                session=FakeSession(lambda url, kw: {}),
                pace=0,
                limit=0,
            )
            self.assertEqual(result["stats"]["cached"], 1)
            self.assertEqual(result["stats"]["skipped"], 2)


# ---------------------------------------------------------------------------
# IMDb bulk files and the local join
# ---------------------------------------------------------------------------

class TestImdbRatings(unittest.TestCase):
    def test_parses_tiny_gzipped_tsv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "title.ratings.tsv.gz"
            _write_gzip(
                path,
                "tconst\taverageRating\tnumVotes\n"
                "tt1\t8.4\t12345\n"
                "tt2\t\\N\t1\n"
                "tt3\t7.1\t\\N\n",
            )
            mapping = ratings.imdb_ratings(path, max_age_days=2)
            self.assertEqual(mapping["tt1"], (8.4, 12345))
            self.assertNotIn("tt2", mapping)
            self.assertNotIn("tt3", mapping)

    def test_bad_header_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "title.ratings.tsv.gz"
            _write_gzip(path, "wrong\theader\n")
            with self.assertRaises(ratings.IMDbDatasetError):
                ratings.imdb_ratings(path, max_age_days=2)

    def test_missing_file_and_failed_download_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "title.ratings.tsv.gz"
            session = FakeSession(lambda url, kw: FakeResponse(status=404, text=""))
            with self.assertRaises(ratings.IMDbDatasetError):
                ratings.imdb_ratings(path, session=session, max_age_days=2)


class TestImdbIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        basics, akas, ratings_file = _tiny_imdb_dir(self.root)
        self.index = ratings.load_imdb_index(
            basics, akas, ratings_file,
            max_age_days=7,
            cache_path=self.root / "index.pickle",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_basics_match(self):
        match = self.index.resolve("The Matrix", year=1999, runtime=136)
        self.assertIsNotNone(match)
        self.assertEqual(match.imdb_id, "tt0133093")
        self.assertEqual(match.method, "exact")
        self.assertEqual(match.score, 1.0)
        # Unknown venue type is not assumed to be a multiplex.
        self.assertFalse(match.needs_review)

    def test_pre_2000_at_known_multiplex_needs_review(self):
        match = self.index.resolve(
            "The Matrix", year=1999, runtime=136, arthouse=False
        )
        self.assertIsNotNone(match)
        self.assertTrue(match.needs_review)

    def test_aka_match_prefers_australian_alias(self):
        match = self.index.resolve("The Matrix", year=1999, arthouse=True)
        self.assertIsNotNone(match)
        self.assertEqual(match.imdb_id, "tt0133093")
        self.assertFalse(match.needs_review)

        alias = self.index.resolve(
            "Mad Max 4: Fury Road", year=2015, runtime=120, arthouse=True
        )
        self.assertIsNotNone(alias)
        self.assertEqual(alias.imdb_id, "tt1392190")
        self.assertEqual(alias.method, "aka")

    def test_year_hint_and_stylised_title(self):
        match = self.index.resolve(
            "Seven (1995)", year=1995, runtime=127, arthouse=True
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.imdb_id, "tt0114369")
        self.assertEqual(match.method, "exact")

    def test_spelling_variant_still_hits_exact_basics(self):
        match = self.index.resolve(
            "Terminator 2: Judgement Day - 35th Anniversary",
            year=1991,
            runtime=137,
            arthouse=True,
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.imdb_id, "tt0103064")
        self.assertEqual(match.method, "exact")

    def test_fuzzy_match(self):
        match = self.index.resolve(
            "Shawshank Redemtion", year=1994, runtime=142, arthouse=True
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.imdb_id, "tt0111161")
        self.assertEqual(match.method, "fuzzy")

    def test_year_mismatch_is_rejected(self):
        self.assertIsNone(self.index.resolve("The Matrix", year=2016))

    def test_akira_uses_votes_not_recency(self):
        match = self.index.resolve("AKIRA", arthouse=True)
        self.assertIsNotNone(match)
        self.assertEqual(match.imdb_id, "tt0080339")
        self.assertEqual(match.candidate_year, 1988)
        self.assertFalse(match.needs_review)

    def test_akira_pre_2000_is_flagged_at_multiplex(self):
        match = self.index.resolve("AKIRA", arthouse=False)
        self.assertIsNotNone(match)
        self.assertEqual(match.imdb_id, "tt0080339")
        self.assertTrue(match.needs_review)

    def test_exactly_one_candidate_within_the_year(self):
        match = self.index.resolve("Akira", year=1988, arthouse=True)
        self.assertIsNotNone(match)
        self.assertEqual(match.imdb_id, "tt0080339")
        self.assertFalse(match.needs_review)

    def test_no_candidate_returns_none(self):
        self.assertIsNone(self.index.resolve("Dune", year=2021, arthouse=True))

    def test_tv_series_is_not_a_movie_candidate(self):
        # The series shares the Akira title but must not enter the movie pool.
        match = self.index.resolve("Akira", year=2019, arthouse=True)
        self.assertIsNone(match)


# ---------------------------------------------------------------------------
# OMDb and RT/MC parsers
# ---------------------------------------------------------------------------

class TestOmdb(unittest.TestCase):
    def test_ratings_array(self):
        payload = {
            "Response": "True",
            "imdbRating": "8.4",
            "imdbVotes": "809,060",
            "Ratings": [
                {"Source": "Internet Movie Database", "Value": "8.4/10"},
                {"Source": "Rotten Tomatoes", "Value": "92%"},
                {"Source": "Metacritic", "Value": "79/100"},
            ],
        }
        session = FakeSession(lambda url, kw: payload)
        out = ratings.omdb_lookup("tt15239678", "key", session)
        self.assertEqual(out, {"imdb": 8.4, "rt": 92, "mc": 79})
        url, kwargs = session.calls[0]
        self.assertEqual(kwargs["params"]["i"], "tt15239678")
        self.assertEqual(kwargs["params"]["apikey"], "key")

    def test_miss_is_not_an_error(self):
        session = FakeSession(
            lambda url, kw: {"Response": "False", "Error": "Movie not found!"}
        )
        self.assertEqual(
            ratings.omdb_lookup("tt0", "key", session),
            {"imdb": None, "rt": None, "mc": None},
        )

    def test_metascore_fallback(self):
        session = FakeSession(
            lambda url, kw: {"Response": "True", "Metascore": "79"}
        )
        self.assertEqual(ratings.omdb_lookup("tt1", "key", session)["mc"], 79)


class TestRottenTomatoes(unittest.TestCase):
    SCORECARD = """<html><script
        id="media-scorecard-json"
        type="application/json">
        {"criticsScore": {"scorePercent": "95%"},
         "audienceScore": {"scorePercent": "84%"}}
        </script></html>"""

    def test_scorecard_attributes_span_lines(self):
        session = FakeSession(lambda url, kw: FakeResponse(text=self.SCORECARD))
        out = ratings.fetch_rt("m/dune_part_two", session)
        self.assertEqual(out, {"critics": 95, "audience": 84})
        self.assertIn("/m/dune_part_two", session.calls[0][0])

    def test_json_ld_tomatometer_fallback(self):
        page = (
            '<script type="application/ld+json">'
            '{"@type":"Movie","name":"Dune","aggregateRating":'
            '{"ratingValue":"91"}}</script>'
        )
        session = FakeSession(lambda url, kw: FakeResponse(text=page))
        self.assertEqual(
            ratings.fetch_rt("dune", session),
            {"critics": 91, "audience": None},
        )

    def test_slug_candidates(self):
        slugs = ratings.rt_slug_candidates("The Room", 2003)
        self.assertIn("room", slugs)
        self.assertIn("the_room", slugs)
        self.assertIn("room_2003", slugs)
        self.assertIn("the_room_2003", slugs)

    def test_date_created_check_for_generated_slugs(self):
        page = (
            '<script type="application/ld+json">'
            '{"@type":"Movie","name":"Dune","dateCreated":"2024-02-27",'
            '"aggregateRating":{"ratingValue":"95"}}</script>'
            '<script id="media-scorecard-json" type="application/json">'
            '{"criticsScore":{"scorePercent":"95%"}}</script>'
        )
        session = FakeSession(lambda url, kw: FakeResponse(text=page))
        fetcher = ratings._Fetcher(session, pace=0)
        wrong, _ = ratings._rt_scores("dune", fetcher, 25, expected_year=1999)
        self.assertIsNone(wrong["critics"])
        right, _ = ratings._rt_scores("dune", fetcher, 25, expected_year=2024)
        self.assertEqual(right["critics"], 95)


class TestMetacritic(unittest.TestCase):
    def test_backend_critic_and_user_scores(self):
        def handler(url, kwargs):
            if "/critic/" in url:
                return {"data": {"item": {"score": 62}}}
            if "/user/" in url:
                return {"data": {"item": {"score": 7.7}}}
            return {}

        out = ratings.fetch_mc("movie/dune-part-two", FakeSession(handler))
        self.assertEqual(out, {"critics": 62, "users": 7.7})

    def test_null_score_is_none(self):
        def handler(url, kwargs):
            return {"data": {"item": {"score": None}}}

        out = ratings.fetch_mc("dune-part-two", FakeSession(handler))
        self.assertEqual(out, {"critics": None, "users": None})

    def test_page_json_ld_fallback(self):
        page = (
            '<script type="application/ld+json">'
            '{"@type":"Movie","aggregateRating":{"ratingValue":"79"}}'
            "</script>"
        )

        def handler(url, kwargs):
            if "backend.metacritic.com" in url:
                return FakeResponse(status=404, text="")
            return FakeResponse(text=page)

        out = ratings.fetch_mc("movie/dune-part-two", FakeSession(handler))
        self.assertEqual(out["critics"], 79)
        self.assertIsNone(out["users"])

    def test_finder_year_gate(self):
        payload = {
            "data": {
                "items": [
                    {
                        "title": "Dune: Part Two",
                        "slug": "dune-part-two",
                        "premiereYear": 2024,
                        "criticScoreSummary": {"score": 79},
                    }
                ]
            }
        }
        session = FakeSession(lambda url, kw: payload)
        hit = ratings.mc_finder("Dune: Part Two", session, year=2024)
        self.assertEqual(hit["slug"], "dune-part-two")
        self.assertIsNone(ratings.mc_finder("Dune: Part Two", session, year=1999))

    def test_finder_ambiguity_is_a_miss(self):
        payload = {
            "data": {
                "items": [
                    {"title": "Dune", "slug": "dune-1984", "premiereYear": 1984},
                    {"title": "Dune", "slug": "dune-2021", "premiereYear": 2021},
                ]
            }
        }
        session = FakeSession(lambda url, kw: payload)
        self.assertIsNone(ratings.mc_finder("Dune", session, year=None))

    def test_finder_404_is_a_miss(self):
        session = FakeSession(lambda url, kw: FakeResponse(status=404, text=""))
        self.assertIsNone(ratings.mc_finder("No Such Film", session, year=2024))


# ---------------------------------------------------------------------------
# Wikidata fallback
# ---------------------------------------------------------------------------

class TestWikidata(unittest.TestCase):
    def _session(self):
        def handler(url, kwargs):
            params = kwargs.get("params") or {}
            if params.get("action") == "wbsearchentities":
                return {
                    "search": [
                        {"id": "Q1", "label": "Dune: Part Three"},
                        {"id": "Q2", "label": "Dune: Part Two"},
                    ]
                }
            if params.get("action") == "wbgetentities":
                return {
                    "entities": {
                        "Q1": {
                            "labels": {"en": {"value": "Dune: Part Three"}},
                            "claims": _claims(
                                P345="tt31378509",
                                P1258="m/dune_part_three",
                                P1712="movie/dune-part-three",
                                P577={"time": "+2026-12-18T00:00:00Z"},
                            ),
                        },
                        "Q2": {
                            "labels": {"en": {"value": "Dune: Part Two"}},
                            "claims": _claims(P345="tt15239678"),
                        },
                    }
                }
            return {}

        return FakeSession(handler)

    def test_resolves_ids_and_strips_rt_prefix(self):
        out = ratings.wikidata_ids("DUNE: PART THREE - IMAX 70MM", self._session())
        self.assertEqual(out["imdb_id"], "tt31378509")
        self.assertEqual(out["rt_slug"], "dune_part_three")
        self.assertEqual(out["mc_slug"], "movie/dune-part-three")
        self.assertEqual(out["label"], "Dune: Part Three")
        self.assertEqual(out["year"], 2026)

    def test_ambiguous_search_is_a_miss(self):
        def handler(url, kwargs):
            params = kwargs.get("params") or {}
            if params.get("action") == "wbsearchentities":
                return {
                    "search": [
                        {"id": "Q1", "label": "Akira"},
                        {"id": "Q2", "label": "Akira"},
                    ]
                }
            return {
                "entities": {
                    "Q1": {
                        "labels": {"en": {"value": "Akira"}},
                        "claims": _claims(P345="tt0080339"),
                    },
                    "Q2": {
                        "labels": {"en": {"value": "Akira"}},
                        "claims": _claims(P345="tt2016001"),
                    },
                }
            }

        self.assertEqual(ratings.wikidata_ids("Akira", FakeSession(handler)), {})

    def test_candidate_year_mismatch_is_rejected(self):
        candidates = [
            {"label": "Akira", "year": 1988, "imdb_id": "tt0080339"},
        ]
        self.assertIsNone(ratings._select_candidate(candidates, 2016))
        self.assertEqual(
            ratings._select_candidate(candidates, 1988)["imdb_id"], "tt0080339"
        )


# ---------------------------------------------------------------------------
# End-to-end enrich, offline
# ---------------------------------------------------------------------------

class TestEnrich(unittest.TestCase):
    def test_local_join_and_imdb_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            basics, akas, ratings_file = _tiny_imdb_dir(root)
            cache_path = root / "ratings.json"
            session = FakeSession(lambda url, kw: {})
            seen: list[tuple[str, dict]] = []

            result = ratings.enrich(
                [{
                    "key": "matrix",
                    "title": "Matrix, The",
                    "year": 1999,
                    "runtime_minutes": 136,
                    "arthouse": True,
                }],
                cache_path=cache_path,
                session=session,
                pace=0,
                progress=lambda key, status: seen.append((key, status)),
            )

            stats = result["stats"]
            self.assertEqual(stats["films"], 1)
            self.assertEqual(stats["resolved"], 1)
            self.assertEqual(stats["imdb"], 1)
            self.assertEqual(stats["rt"], 0)
            self.assertEqual(stats["mc"], 0)
            self.assertEqual(stats["failed"], 0)

            entry = result["cache"][ratings.cache_key("Matrix, The", 1999)]
            self.assertEqual(entry["imdb_id"], "tt0133093")
            self.assertEqual(entry["imdb"], 8.7)
            self.assertEqual(entry["match_method"], "exact")
            self.assertFalse(entry["needs_review"])
            self.assertIn("imdb-basics", entry["source"])
            self.assertIn("imdb-bulk", entry["source"])
            self.assertEqual(seen[0][0], "matrix")
            self.assertEqual(seen[0][1]["status"], "resolved")
            # The local index was built from the fixture, not downloaded.
            self.assertTrue((root / "title.index.pickle").exists())
            self.assertTrue(basics.exists() and akas.exists() and ratings_file.exists())

    def test_datasets_in_cache_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            _tiny_imdb_dir(cache_dir)
            cache_path = root / "ratings.json"
            result = ratings.enrich(
                [{
                    "key": "matrix",
                    "title": "The Matrix",
                    "year": 1999,
                    "runtime_minutes": 136,
                    "arthouse": True,
                }],
                cache_path=cache_path,
                session=FakeSession(lambda url, kw: {}),
                pace=0,
            )
            entry = result["cache"][ratings.cache_key("The Matrix", 1999)]
            self.assertEqual(entry["imdb_id"], "tt0133093")
            self.assertTrue((cache_dir / "title.index.pickle").exists())

    def test_direct_imdb_id_uses_manual_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tiny_imdb_dir(root)
            cache_path = root / "ratings.json"
            result = ratings.enrich(
                [{
                    "key": "manual",
                    "title": "Some Localised Title",
                    "year": 1999,
                    "imdb_id": "tt0133093",
                    "arthouse": True,
                }],
                cache_path=cache_path,
                session=FakeSession(lambda url, kw: {}),
                pace=0,
            )
            entry = result["cache"][ratings.cache_key("Some Localised Title", 1999)]
            self.assertEqual(entry["imdb_id"], "tt0133093")
            self.assertEqual(entry["match_method"], "manual")
            self.assertEqual(entry["imdb"], 8.7)
            self.assertIn("catalogue", entry["source"])

    def test_review_row_carries_no_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tiny_imdb_dir(root)
            cache_path = root / "ratings.json"
            result = ratings.enrich(
                [{
                    "key": "akira",
                    "title": "AKIRA",
                    "arthouse": False,
                }],
                cache_path=cache_path,
                session=FakeSession(lambda url, kw: {}),
                pace=0,
            )
            entry = result["cache"][ratings.cache_key("AKIRA", None)]
            self.assertEqual(entry["imdb_id"], "tt0080339")
            self.assertTrue(entry["needs_review"])
            self.assertIsNone(entry["imdb"])
            self.assertIsNone(entry["rt"])
            self.assertIsNone(entry["mc"])
            self.assertEqual(entry["confidence"], "low")

    def test_empty_title_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ratings.json"
            result = ratings.enrich(
                [{"key": "x", "title": "", "year": None}],
                cache_path=path,
                session=FakeSession(lambda url, kw: {}),
                pace=0,
            )
            self.assertEqual(result["stats"]["skipped"], 1)
            self.assertEqual(result["stats"]["films"], 1)


if __name__ == "__main__":
    unittest.main()
