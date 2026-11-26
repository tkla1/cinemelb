"""Offline checks for cover resolution — no network, no cache writes."""

from __future__ import annotations

import unittest

from cinemelb import posters


def candidate(imdb_id: str, title: str, year: int | None,
              kind: str = "movie", url: str = "x") -> dict:
    return {"id": imdb_id, "title": title, "year": year, "kind": kind,
            "url": f"https://m.media-amazon.com/images/M/{url}@._V1_.jpg"}


class TestSearchTitle(unittest.TestCase):
    def test_strips_festival_prefix_even_without_a_colon(self) -> None:
        self.assertEqual(posters.search_title("BFF26 Hill"), "Hill")
        self.assertEqual(posters.search_title("JFF26: Look Back"), "Look Back")

    def test_strips_presentation_noise(self) -> None:
        self.assertEqual(posters.search_title("Dune: Part Three - IMAX 70MM"),
                         "Dune: Part Three")
        self.assertEqual(posters.search_title("Australia 3D"), "Australia")
        self.assertEqual(
            posters.search_title("Godzilla Minus Zero  (Japanese, Eng Sub)"),
            "Godzilla Minus Zero",
        )

    def test_strips_venue_prefix(self) -> None:
        self.assertEqual(posters.search_title("Astor: The Last Waltz"),
                         "The Last Waltz")
        self.assertEqual(
            posters.search_title("Cult Classix The Rage in Placid Lake"),
            "The Rage in Placid Lake",
        )

    def test_strips_reissue_noise(self) -> None:
        self.assertEqual(posters.search_title("Dune: Part Two - Re-issue"),
                         "Dune: Part Two")
        self.assertEqual(
            posters.search_title("CASFFA: Princess Stella (Princezna stokrát jinak)"),
            "Princess Stella",
        )


class TestChoose(unittest.TestCase):
    def test_exact_title_prefers_the_matching_year(self) -> None:
        candidates = [
            candidate("tt1", "Akira", 1988),
            candidate("tt2", "Akira", 2016),
        ]
        pick = posters.choose(candidates, "AKIRA", 2016)
        self.assertEqual(pick["id"], "tt2")
        self.assertEqual(pick["method"], "exact")

    def test_ratings_id_wins_over_a_year_mismatch(self) -> None:
        candidates = [
            candidate("tt0094625", "Akira", 1988),
            candidate("tt9999999", "Akira", 2026),
        ]
        pick = posters.choose(candidates, "Akira", 2026, imdb_id="tt0094625")
        self.assertEqual(pick["id"], "tt0094625")
        self.assertEqual(pick["method"], "ratings-id")

    def test_short_title_does_not_borrow_a_longer_title(self) -> None:
        # The real failure that motivated the strict gate: "BFF26 Hill" was
        # being given Notting Hill's poster.
        candidates = [candidate("tt0125439", "Notting Hill", 1999)]
        self.assertIsNone(posters.choose(candidates, "BFF26 Hill", 2026))

    def test_fuzzy_match_needs_a_year_within_one(self) -> None:
        candidates = [candidate("tt1", "A Time to Love and a Time to Die", 1958)]
        self.assertIsNone(posters.choose(candidates, "A Time to Live, a Time to Die", None))
        self.assertIsNotNone(
            posters.choose(
                [candidate("tt1", "A Time to Love and a Time to Die", 1958)],
                "A Time to Live and a Time to Die", 1958,
            )
        )

    def test_ignores_television_and_music(self) -> None:
        candidates = [
            candidate("tt1", "Akira", 1988, kind="tvSeries"),
            candidate("tt2", "Akira", 1988, kind="musicVideo"),
        ]
        self.assertIsNone(posters.choose(candidates, "Akira", 1988))

    def test_ignores_candidates_without_an_image(self) -> None:
        candidates = [dict(candidate("tt1", "Akira", 1988), url="")]
        self.assertIsNone(posters.choose(candidates, "Akira", 1988))


class TestPosterUrl(unittest.TestCase):
    def test_resizes_an_imdb_transform_url(self) -> None:
        url = "https://m.media-amazon.com/images/M/ABC@._V1_.jpg"
        out = posters.poster_url(url)
        self.assertTrue(out.endswith("._V1_UX300_CR0,0,300,444_AL_.jpg"))
        self.assertNotIn("_V1_.jpg", out)

    def test_leaves_non_imdb_urls_alone(self) -> None:
        url = "https://example.com/poster.png"
        self.assertEqual(posters.poster_url(url), url)

    def test_empty(self) -> None:
        self.assertEqual(posters.poster_url(""), "")


class TestPlaceholder(unittest.TestCase):
    def test_palace_endpoint_is_a_brand_image(self) -> None:
        self.assertTrue(posters.is_placeholder(
            "https://www.palacecinemas.com.au/poster/The%20Fox%20345x500-2.jpg"))
        self.assertTrue(posters.is_placeholder(
            "https://www.palacecinemas.com.au/poster/BFF26%20Parky%20345x500.gif"))

    def test_real_posters_are_kept(self) -> None:
        self.assertFalse(posters.is_placeholder(
            "https://www.cinemanova.com.au/media/Webtiles/ChopinTILE.jpg"))
        self.assertFalse(posters.is_placeholder(
            "https://m.media-amazon.com/images/M/ABC@._V1_.jpg"))

    def test_missing_is_a_placeholder(self) -> None:
        self.assertTrue(posters.is_placeholder(""))


class TestPosterFor(unittest.TestCase):
    def test_cache_hit_wins(self) -> None:
        film = {"key": "the-fox", "poster_url": "https://example.com/x.jpg"}
        cache = {"the-fox": {"url": "https://img/cover.jpg"}}
        self.assertEqual(posters.poster_for(film, cache),
                         ("https://img/cover.jpg", True))

    def test_falls_back_to_a_real_venue_poster(self) -> None:
        film = {"key": "nova-film",
                "poster_url": "https://www.cinemanova.com.au/media/x.jpg"}
        self.assertEqual(posters.poster_for(film, {}),
                         ("https://www.cinemanova.com.au/media/x.jpg", False))

    def test_drops_a_venue_placeholder(self) -> None:
        film = {"key": "palace-film",
                "poster_url": "https://www.palacecinemas.com.au/poster/x.jpg"}
        self.assertEqual(posters.poster_for(film, {}), ("", False))


if __name__ == "__main__":
    unittest.main()
