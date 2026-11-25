"""Adapter for the "Moving Story" platform (Lido Hawthorn, Classic Elsternwick).

This is a classic server-rendered PHP site, and it publishes everything we
need as plain HTML — no browser required.  Each film page renders one
``<ul class="Sessions">`` per day, in the same order as the day tabs
("Today", "Tomorrow", "Friday", …), with the hidden days carrying the
``Sessions Hidden`` class.

So the mapping is: *block index → today + index days*.  Times are wall-clock
("6:20 pm") and are read as Melbourne local time.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from ..http import Http
from ..models import Film, MELBOURNE_TZ, film_key, make_screening
from .base import Adapter, AdapterOutput

_FILM_LINK = re.compile(r'href="(/movies/([a-z0-9][a-z0-9\-]*))"')
_TAB = re.compile(r'class="(?:Selected )?btn-primary outline">\s*([^<]+?)\s*</a>')
_UL = re.compile(r'<ul class="Sessions([^"]*)">')
_LI = re.compile(r"<li[^>]*>(.*?)</li>", re.S)
_TIME = re.compile(r'<span class="Time">\s*([^<]+?)\s*</span>')
_ATTR = re.compile(r'<span class="Attribute[^"]*">\s*([^<]+?)\s*</span>')
_DATANAME = re.compile(r'data-name="([^"]*)"')
_TICKET = re.compile(r'href="(/tickets\?c=[^"&]+&(?:amp;)?s=\d+)"')
_DATAID = re.compile(r'data-id="(\d+)"')
_GENRE = re.compile(r'data-genre="([^"]*)"')
_TITLE = re.compile(r"<title>(.*?)</title>", re.S)


def _parse_clock(text: str) -> time | None:
    m = re.match(r"\s*(\d{1,2}):(\d{2})\s*([ap])\.?m\.?", text.strip(), re.I)
    if not m:
        return None
    hour = int(m.group(1)) % 12
    if m.group(3).lower() == "p":
        hour += 12
    return time(hour, int(m.group(2)))


def _first_sunday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7)


class MovingStoryAdapter(Adapter):
    name = "movingstory"

    def fetch(self, cinema: dict, http: Http) -> AdapterOutput:
        site = (cinema.get("website") or "").rstrip("/")
        if not site:
            return AdapterOutput(note="no website on record")
        cinema_id = self.venue_id(cinema)

        home = http.get(site + "/")
        slugs = sorted({m.group(2) for m in _FILM_LINK.finditer(home.text)})
        if not slugs:
            return AdapterOutput(note="no film links on home page")

        out = AdapterOutput()
        today = datetime.now(MELBOURNE_TZ).date()
        seen_films: set[str] = set()

        for slug in slugs:
            try:
                page = http.get(f"{site}/movies/{slug}")
            except Exception:                                   # noqa: BLE001
                continue
            html = page.text

            tabs = [t.strip() for t in _TAB.findall(html)]
            blocks = _UL.findall(html)
            sessions_found_for_film = 0

            for idx, closing in enumerate(_UL.finditer(html)):
                # locate this block's content
                start = closing.end()
                end = html.find("</ul>", start)
                if end < 0:
                    continue
                block = html[start:end]

                # Day index -> calendar date.  Prefer an explicit label if the
                # tab carries one ("Mon 7 Dec"), else assume consecutive days.
                day = self._resolve_date(tabs[idx] if idx < len(tabs) else "", today, idx)

                for li in _LI.findall(block):
                    tmatch = _TIME.search(li)
                    if not tmatch:
                        continue
                    clock = _parse_clock(tmatch.group(1))
                    if not clock:
                        continue
                    name_match = _DATANAME.search(li)
                    title = (name_match.group(1) if name_match else "").strip()
                    if not title:
                        continue
                    if title not in seen_films:
                        gmatch = _GENRE.search(li)
                        genres = [
                            g.strip()
                            for g in (gmatch.group(1) if gmatch else "").split(",")
                            if g.strip()
                        ]
                        out.films.append(Film(key=film_key(title), title=title, genres=genres))
                        seen_films.add(title)

                    attrs = [a.strip() for a in _ATTR.findall(li) if a.strip()]
                    ticket = ""
                    tlink = _TICKET.search(li)
                    if tlink:
                        ticket = site + tlink.group(1).replace("&amp;", "&")
                    idm = _DATAID.search(li)
                    session_id = idm.group(1) if idm else ""

                    out.screenings.append(
                        make_screening(
                            cinema_id=cinema_id,
                            film_title=title,
                            local_naive=datetime.combine(day, clock),
                            source=self.name,
                            screen="",
                            fmt=self._format_from(attrs),
                            attributes=attrs,
                            ticket_url=ticket,
                        )
                    )
                    sessions_found_for_film += 1

            if sessions_found_for_film == 0 and tabs:
                out.note = out.note or f"{slug}: no sessions parsed"

        return out

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _resolve_date(label: str, today: date, idx: int) -> date:
        lab = label.strip().lower()
        if lab.startswith("today"):
            return today
        if lab.startswith("tomorrow"):
            return today + timedelta(days=1)
        # "Mon 7 Dec" / "Fri 4 Dec"
        m = re.match(r"\w+\s+(\d{1,2})\s+(\w{3})", label.strip(), re.I)
        if m:
            day_num = int(m.group(1))
            months = ["jan", "feb", "mar", "apr", "may", "jun",
                      "jul", "aug", "sep", "oct", "nov", "dec"]
            try:
                month = months.index(m.group(2).lower()[:3]) + 1
            except ValueError:
                return today + timedelta(days=idx)
            year = today.year
            cand = date(year, month, day_num)
            # the date switcher can straddle a year boundary
            if (today - cand).days > 180:
                cand = date(year + 1, month, day_num)
            elif (cand - today).days > 300:
                cand = date(year - 1, month, day_num)
            return cand
        return today + timedelta(days=idx)

    @staticmethod
    def _format_from(attrs: list[str]) -> str:
        joined = " ".join(attrs).lower()
        for token in ("70mm", "35mm", "4k", "imax", "dolby"):
            if token in joined:
                return token.upper() if "mm" in token else token
        return ""
