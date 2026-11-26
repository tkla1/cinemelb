"""SQLite-backed store for cinemas, films and screenings.

Why SQLite rather than JSON files: the interesting queries are all relational
and time-shaped — "this film, at these venues, after 7pm, sorted by distance",
"what starts between 8 and 9pm at venues within 5km". Doing that in Python
over a JSON blob works but throws away an obvious win, and a cinema refresh
repeatedly replaces a few thousand screening rows, which SQLite handles with
one transaction.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .config import DB_PATH
from .models import Cinema, Film, Screening

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS cinemas (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    chain       TEXT,
    address     TEXT,
    suburb      TEXT,
    postcode    TEXT,
    state       TEXT,
    latitude    REAL,
    longitude   REAL,
    phone       TEXT,
    website     TEXT,
    booking_url TEXT,
    screens     INTEGER,
    source      TEXT,
    source_ids  TEXT,      -- json
    tags        TEXT,      -- json array
    notes       TEXT,
    active      INTEGER DEFAULT 1,
    metro       INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS films (
    key             TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    year            INTEGER,
    runtime_minutes INTEGER,
    rating          TEXT,
    genres          TEXT,  -- json array
    synopsis        TEXT,
    poster_url      TEXT,
    trailer_url     TEXT,
    directors       TEXT,  -- json array
    aka             TEXT   -- json array
);

CREATE TABLE IF NOT EXISTS screenings (
    id           TEXT PRIMARY KEY,
    cinema_id    TEXT NOT NULL REFERENCES cinemas(id) ON DELETE CASCADE,
    film_key     TEXT NOT NULL,
    film_title   TEXT NOT NULL,
    starts_at    TEXT NOT NULL,   -- ISO 8601 UTC
    local_date   TEXT NOT NULL,   -- YYYY-MM-DD, Melbourne
    local_time   TEXT NOT NULL,   -- HH:MM, Melbourne
    screen       TEXT,
    format       TEXT,
    attributes   TEXT,            -- json array
    ticket_url   TEXT,
    sold_out     INTEGER DEFAULT 0,
    source       TEXT
);

CREATE INDEX IF NOT EXISTS idx_screening_film   ON screenings(film_key);
CREATE INDEX IF NOT EXISTS idx_screening_cinema ON screenings(cinema_id, local_date);
CREATE INDEX IF NOT EXISTS idx_screening_start  ON screenings(starts_at);
CREATE INDEX IF NOT EXISTS idx_screening_date   ON screenings(local_date);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    source      TEXT,
    cinema_id   TEXT,
    ok          INTEGER,
    detail      TEXT,
    n_screenings INTEGER
);
"""


class Store:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -- writes ------------------------------------------------------------
    def upsert_cinemas(self, cinemas: Iterable[Cinema]) -> int:
        import json

        rows = [
            (
                c.id, c.name, c.chain, c.address, c.suburb, c.postcode, c.state,
                c.latitude, c.longitude, c.phone, c.website, c.booking_url,
                c.screens, c.source, json.dumps(c.source_ids), json.dumps(c.tags),
                c.notes, 1 if c.active else 0, 1 if c.metro else 0,
            )
            for c in cinemas
        ]
        with self.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO cinemas (id,name,chain,address,suburb,postcode,state,
                    latitude,longitude,phone,website,booking_url,screens,source,
                    source_ids,tags,notes,active,metro)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, chain=excluded.chain, address=excluded.address,
                    suburb=excluded.suburb, postcode=excluded.postcode, state=excluded.state,
                    latitude=excluded.latitude, longitude=excluded.longitude,
                    phone=excluded.phone, website=excluded.website,
                    booking_url=excluded.booking_url, screens=excluded.screens,
                    source=excluded.source, source_ids=excluded.source_ids,
                    tags=excluded.tags, notes=excluded.notes, active=excluded.active,
                    metro=excluded.metro
                """,
                rows,
            )
        return len(rows)

    def upsert_films(self, films: Iterable[Film]) -> int:
        import json

        from .models import film_key
        from .titles import display_title

        rows = []
        for f in films:
            # Strip the listing's programme prefix from both the key and the
            # title, but keep the published string in `aka`. "BFF26 Parky" and
            # "Parky" are then one catalogue film with one set of scores and
            # one cover. If an adapter supplied a hand-built key, leave it be.
            title = display_title(f.title)
            key = film_key(title) if f.key == film_key(f.title) else f.key
            aka = [str(item) for item in f.aka if item]
            if title != f.title and f.title not in aka:
                aka.insert(0, f.title)
            rows.append(
                (
                    key, title, f.year, f.runtime_minutes, f.rating,
                    json.dumps(f.genres), f.synopsis, f.poster_url, f.trailer_url,
                    json.dumps(f.directors), json.dumps(aka),
                )
            )
        with self.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO films (key,title,year,runtime_minutes,rating,genres,
                    synopsis,poster_url,trailer_url,directors,aka)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    -- IMAX publishes titles in upper case ("DUNE: PART THREE").
                    -- An all-caps title must not overwrite a properly cased one
                    -- from another chain, or the whole catalogue starts
                    -- shouting.  SQLite's UPPER() makes "is this all caps?" a
                    -- single comparison; a mixed-case title never equals its
                    -- own upper-case form.
                    title=CASE
                        WHEN excluded.title = UPPER(excluded.title)
                             AND films.title <> UPPER(films.title)
                        THEN films.title
                        ELSE excluded.title
                    END,
                    year=COALESCE(excluded.year, films.year),
                    runtime_minutes=COALESCE(excluded.runtime_minutes, films.runtime_minutes),
                    rating=CASE WHEN excluded.rating<>'' THEN excluded.rating ELSE films.rating END,
                    genres=CASE WHEN excluded.genres<>'[]' THEN excluded.genres ELSE films.genres END,
                    synopsis=CASE WHEN excluded.synopsis<>'' THEN excluded.synopsis ELSE films.synopsis END,
                    poster_url=CASE WHEN excluded.poster_url<>'' THEN excluded.poster_url ELSE films.poster_url END,
                    trailer_url=CASE WHEN excluded.trailer_url<>'' THEN excluded.trailer_url ELSE films.trailer_url END,
                    aka=CASE
                        WHEN excluded.aka<>'[]' AND films.aka='[]' THEN excluded.aka
                        WHEN excluded.aka<>'[]' AND films.aka<>'[]' THEN films.aka
                        ELSE films.aka
                    END
                """,
                rows,
            )
        return len(rows)

    def normalise_films(self) -> dict[str, int]:
        """Rewrite existing rows after a display-title rule change.

        The store is a cache of scrapes, but it is a *persistent* one: a rule
        that strips "BFF26" changes the key of rows that are already on disk.
        Re-scraping would fix them eventually; this makes the catalogue correct
        immediately, and is idempotent. Screenings follow their film, and two
        prefixed spellings that collapse to one key are merged.
        """
        import json

        from .models import film_key
        from .titles import display_title

        def _aka(row) -> list[str]:
            try:
                value = json.loads(row["aka"] or "[]")
            except (json.JSONDecodeError, TypeError):
                return []
            return [str(item) for item in value if item]

        stats = {"renamed": 0, "merged": 0, "retitled": 0}
        with self.transaction() as conn:
            # On Windows a multi-row UPDATE can spill a statement journal; the
            # default temp-file path is not always writable, which surfaced as
            # a misleading SQLITE_CANTOPEN. The catalogue is small, so keep
            # temp tables in memory for this migration.
            conn.execute("PRAGMA temp_store=MEMORY")
            rows = conn.execute("SELECT * FROM films").fetchall()
            for row in rows:
                old_key = row["key"]
                raw = row["title"]
                clean = display_title(raw)
                if not clean:
                    continue
                new_key = film_key(clean)
                aka = _aka(row)
                if raw != clean and raw not in aka:
                    aka.insert(0, raw)

                if new_key == old_key:
                    if clean != raw:
                        conn.execute(
                            "UPDATE films SET title=?, aka=? WHERE key=?",
                            (clean, json.dumps(aka), old_key),
                        )
                        stats["retitled"] += 1
                    continue

                target = conn.execute(
                    "SELECT * FROM films WHERE key=?", (new_key,)
                ).fetchone()
                if target is None:
                    conn.execute(
                        "UPDATE films SET key=?, title=?, aka=? WHERE key=?",
                        (new_key, clean, json.dumps(aka), old_key),
                    )
                    conn.execute(
                        "UPDATE screenings SET film_key=? WHERE film_key=?",
                        (new_key, old_key),
                    )
                    stats["renamed"] += 1
                    continue

                # Merge into the row that already has the clean key.
                target_aka = _aka(target)
                for item in [raw, *aka]:
                    if item and item not in target_aka:
                        target_aka.append(item)
                conn.execute(
                    """
                    UPDATE films SET
                        year=COALESCE(year, ?),
                        runtime_minutes=COALESCE(runtime_minutes, ?),
                        rating=CASE WHEN rating IS NULL OR rating='' THEN ? ELSE rating END,
                        genres=CASE WHEN genres IS NULL OR genres='[]' THEN ? ELSE genres END,
                        synopsis=CASE WHEN synopsis IS NULL OR synopsis='' THEN ? ELSE synopsis END,
                        poster_url=CASE WHEN poster_url IS NULL OR poster_url='' THEN ? ELSE poster_url END,
                        trailer_url=CASE WHEN trailer_url IS NULL OR trailer_url='' THEN ? ELSE trailer_url END,
                        directors=CASE WHEN directors IS NULL OR directors='[]' THEN ? ELSE directors END,
                        aka=?
                    WHERE key=?
                    """,
                    (
                        row["year"], row["runtime_minutes"], row["rating"],
                        row["genres"], row["synopsis"], row["poster_url"],
                        row["trailer_url"], row["directors"],
                        json.dumps(target_aka), new_key,
                    ),
                )
                conn.execute(
                    "UPDATE screenings SET film_key=? WHERE film_key=?",
                    (new_key, old_key),
                )
                conn.execute("DELETE FROM films WHERE key=?", (old_key,))
                stats["merged"] += 1
        return stats

    def replace_screenings(self, cinema_id: str, screenings: Sequence[Screening]) -> int:
        """Replace every future screening for one cinema.

        A refresh for a venue is authoritative: if a showing disappeared from
        the source it is gone, and leaving stale rows behind would make the
        tool lie about what is on.
        """
        import json

        with self.transaction() as conn:
            conn.execute("DELETE FROM screenings WHERE cinema_id=?", (cinema_id,))
            conn.executemany(
                """
                INSERT OR REPLACE INTO screenings
                    (id,cinema_id,film_key,film_title,starts_at,local_date,
                     local_time,screen,format,attributes,ticket_url,sold_out,source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        s.id, s.cinema_id, s.film_key, s.film_title,
                        s.starts_at.isoformat(), s.local_date.isoformat(),
                        s.local_time.strftime("%H:%M"), s.screen, s.format,
                        json.dumps(s.attributes), s.ticket_url,
                        1 if s.sold_out else 0, s.source,
                    )
                    for s in screenings
                ],
            )
        return len(screenings)

    def drop_foreign_screenings(self, cinema_id: str, source: str) -> int:
        """Delete a venue's screenings that came from a *different* adapter.

        When a venue's platform changes — a chain takes over an indie, or a
        registry mis-assignment is corrected — the rows the old adapter wrote
        are wrong by definition.  A failed refresh deliberately keeps the
        previous rows, so this is what removes the ones that no longer belong
        to the adapter that owns the venue.
        """
        with self.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM screenings WHERE cinema_id=? AND source<>?",
                (cinema_id, source),
            )
            return cur.rowcount

    def log_run(
        self, *, source: str, cinema_id: str, ok: bool, detail: str = "",
        n_screenings: int = 0, started_at: datetime | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO scrape_runs
                   (started_at,finished_at,source,cinema_id,ok,detail,n_screenings)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    (started_at or datetime.now(timezone.utc)).isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                    source, cinema_id, 1 if ok else 0, detail[:500], n_screenings,
                ),
            )

    def prune_orphan_films(self) -> int:
        """Drop catalogue rows that no screening references.

        Film keys are derived from titles, so a normalisation fix (or a title
        rename upstream) leaves the old key stranded with no screenings.  Left
        alone it would keep showing up in `cinemelb search` as a phantom film
        you can never book.
        """
        with self.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM films WHERE key NOT IN (SELECT DISTINCT film_key FROM screenings)"
            )
        return cur.rowcount

    def prune_before(self, day: date) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM screenings WHERE local_date < ?", (day.isoformat(),)
            )
        return cur.rowcount

    # -- reads -------------------------------------------------------------
    def cinemas(self, *, active_only: bool = True) -> list[dict]:
        import json

        sql = "SELECT * FROM cinemas"
        if active_only:
            sql += " WHERE active=1"
        rows = self.conn.execute(sql + " ORDER BY name").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["source_ids"] = json.loads(d["source_ids"] or "{}")
            d["tags"] = json.loads(d["tags"] or "[]")
            d["metro"] = bool(d.get("metro", 1))
            out.append(d)
        return out

    def cinema(self, cinema_id: str) -> dict | None:
        import json

        r = self.conn.execute("SELECT * FROM cinemas WHERE id=?", (cinema_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["source_ids"] = json.loads(d["source_ids"] or "{}")
        d["tags"] = json.loads(d["tags"] or "[]")
        d["metro"] = bool(d.get("metro", 1))
        return d

    def screenings(
        self,
        *,
        cinema_ids: Sequence[str] | None = None,
        film_key: str | None = None,
        day: date | None = None,
        day_from: date | None = None,
        day_to: date | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> list[dict]:
        import json

        clauses: list[str] = []
        params: list[object] = []
        if cinema_ids:
            clauses.append(f"cinema_id IN ({','.join('?' * len(cinema_ids))})")
            params.extend(cinema_ids)
        if film_key:
            clauses.append("film_key=?")
            params.append(film_key)
        if day:
            clauses.append("local_date=?")
            params.append(day.isoformat())
        if day_from:
            clauses.append("local_date>=?")
            params.append(day_from.isoformat())
        if day_to:
            clauses.append("local_date<=?")
            params.append(day_to.isoformat())
        if after:
            clauses.append("local_time>=?")
            params.append(after)
        if before:
            clauses.append("local_time<=?")
            params.append(before)

        sql = "SELECT * FROM screenings"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY local_date, local_time"
        rows = self.conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["attributes"] = json.loads(d["attributes"] or "[]")
            d["sold_out"] = bool(d["sold_out"])
            out.append(d)
        return out

    def films(self) -> list[dict]:
        import json

        rows = self.conn.execute("SELECT * FROM films ORDER BY title").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["genres"] = json.loads(d["genres"] or "[]")
            d["directors"] = json.loads(d["directors"] or "[]")
            d["aka"] = json.loads(d["aka"] or "[]")
            out.append(d)
        return out

    def film(self, key: str) -> dict | None:
        import json

        r = self.conn.execute("SELECT * FROM films WHERE key=?", (key,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["genres"] = json.loads(d["genres"] or "[]")
        d["directors"] = json.loads(d["directors"] or "[]")
        d["aka"] = json.loads(d["aka"] or "[]")
        return d

    def source_summary(self) -> list[dict]:
        """Per-adapter health from the most recent run of each venue.

        A flat "last 10 runs" list is dominated by whatever failed last; what
        an operator needs is "which platform is broken and how many venues did
        it cost me".
        """
        sql = """
            SELECT r.source,
                   COUNT(*)                                   AS venues,
                   SUM(CASE WHEN r.ok = 1 THEN 1 ELSE 0 END)  AS ok,
                   SUM(r.n_screenings)                        AS screenings,
                   MAX(r.finished_at)                         AS last_run,
                   (SELECT r2.detail FROM scrape_runs r2
                     WHERE r2.source = r.source AND r2.ok = 0
                     ORDER BY r2.id DESC LIMIT 1)             AS last_error
              FROM scrape_runs r
             WHERE r.id IN (SELECT MAX(id) FROM scrape_runs GROUP BY cinema_id)
             GROUP BY r.source
             ORDER BY r.source
        """
        return [dict(row) for row in self.conn.execute(sql).fetchall()]

    def last_refresh(self) -> datetime | None:
        """When did a scrape last succeed?  ``None`` if it never has."""
        row = self.conn.execute(
            "SELECT MAX(finished_at) FROM scrape_runs WHERE ok=1 AND n_screenings>0"
        ).fetchone()
        if not row or not row[0]:
            return None
        try:
            return datetime.fromisoformat(row[0])
        except ValueError:
            return None

    def stats(self) -> dict:
        q = self.conn.execute
        return {
            "cinemas": q("SELECT COUNT(*) FROM cinemas WHERE active=1").fetchone()[0],
            "films": q("SELECT COUNT(*) FROM films").fetchone()[0],
            "screenings": q("SELECT COUNT(*) FROM screenings").fetchone()[0],
            "days": [r[0] for r in q("SELECT DISTINCT local_date FROM screenings ORDER BY local_date")],
            "sources": {
                r[0]: r[1]
                for r in q("SELECT source, COUNT(*) FROM screenings GROUP BY source")
            },
            "last_runs": [
                dict(r) for r in q(
                    "SELECT source,cinema_id,ok,detail,n_screenings,finished_at "
                    "FROM scrape_runs ORDER BY id DESC LIMIT 10"
                )
            ],
        }
