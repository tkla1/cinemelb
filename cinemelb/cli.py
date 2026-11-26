"""Command line interface.

Designed around the questions the tool exists to answer:

    cinemelb film "The Hunger Games"          where is it showing?
    cinemelb film dune --near brunswick       …and what's closest to me?
    cinemelb at 20:00 --date fri              what starts around 8pm Friday?
    cinemelb now --near "3000" --hours 3      what can I see in the next 3 hours?
    cinemelb near fed-square --radius 2       everything within 2km
    cinemelb whats-on --date tomorrow         the whole day, grouped by film
    cinemelb trending                         films with the widest coverage
    cinemelb cinemas --tag arthouse           the venue registry
    cinemelb refresh                          re-scrape
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import query as Q
from .geo import format_distance, geocode
from .models import MELBOURNE_TZ, normalise_title
from .store import Store

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Melbourne cinema showtimes — query every venue, indie included.",
)
console = Console()


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #

def _store(ctx: typer.Context) -> Store:
    store = ctx.obj.get("store") if ctx.obj else None
    if store is None:
        store = Store(ctx.obj["db"] if ctx.obj else None)
        ctx.obj["store"] = store
    return store


def _parse_day(value: str | None) -> date | None:
    """Accept YYYY-MM-DD, 'today', 'tomorrow', 'fri', 'friday', '+2'."""
    if not value:
        return None
    v = value.strip().lower()
    today = datetime.now(MELBOURNE_TZ).date()
    if v in ("today", "now"):
        return today
    if v in ("tomorrow", "tmr"):
        return today + timedelta(days=1)
    if v.startswith("+"):
        try:
            return today + timedelta(days=int(v[1:]))
        except ValueError:
            return None
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, name in enumerate(weekdays):
        if name.startswith(v) and len(v) >= 3:
            ahead = (i - today.weekday()) % 7
            return today + timedelta(days=ahead)
    try:
        return date.fromisoformat(value)
    except ValueError:
        # Silently ignoring an unparseable date would answer a different
        # question than the one asked ("what's on Friday" returning all week).
        console.print(
            f"[red]Could not read date {value!r}[/red]. "
            f"Use today, tomorrow, a weekday (fri), or YYYY-MM-DD."
        )
        raise typer.Exit(2) from None


def _parse_time(value: str | None) -> time | None:
    """Accept 20, 20:30, 8pm, 8:30pm, 2030."""
    if not value:
        return None
    v = value.strip().lower().replace(".", "")
    pm = v.endswith("pm")
    am = v.endswith("am")
    if pm or am:
        v = v[:-2].strip()
    try:
        if ":" in v:
            hh, mm = v.split(":", 1)
            h, m = int(hh), int(mm)
        elif len(v) == 4 and v.isdigit():
            h, m = int(v[:2]), int(v[2:])
        else:
            h, m = int(v), 0
    except ValueError:
        return None
    if pm and h < 12:
        h += 12
    if am and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return time(h, m)


def _origin(location: str | None) -> tuple[float, float] | None:
    if not location:
        return None
    coords = geocode(location)
    if not coords:
        console.print(f"[yellow]Could not resolve location {location!r} — "
                      f"ignoring it.[/yellow]")
        return None
    return coords


def _filters(
    *,
    day: str | None = None,
    after: str | None = None,
    before: str | None = None,
    tags: Optional[list[str]] = None,
    chain: Optional[list[str]] = None,
    fmt: Optional[list[str]] = None,
    accessible: bool = False,
    exclude_tag: Optional[list[str]] = None,
    limit: int = 200,
    sold_out: bool = False,
) -> Q.Filters:
    return Q.Filters(
        day=_parse_day(day),
        after=_parse_time(after),
        before=_parse_time(before),
        tags=list(tags or []),
        chains=list(chain or []),
        formats=list(fmt or []),
        accessible=accessible,
        include_sold_out=sold_out,
        exclude_tags=list(exclude_tag or []),
        limit=limit,
    )


def _emit(payload, as_json: bool) -> None:
    if as_json:
        console.print_json(json.dumps(payload, default=str))


def _empty(message: str) -> None:
    console.print(f"[dim]{message}[/dim]")


def _screening_count(store: Store, film_key: str) -> int:
    return len(store.screenings(film_key=film_key))


def _best_alternative(store: Store, title: str,
                      *, skip: str) -> tuple[str, str, int] | None:
    """The most-screened similarly-titled film other than ``skip``.

    Used only to hint, never to change the answer: "dune" resolves exactly to
    the 1984/2021 "Dune" while "Dune: Part Three" is the film playing
    everywhere, and quietly answering the smaller question is the confusing
    failure this guards against.
    """
    best: tuple[str, str, int] | None = None
    for key, name in Q.resolve_film(store, title):
        if key == skip:
            continue
        count = _screening_count(store, key)
        if best is None or count > best[2]:
            best = (key, name, count)
    return best


def _freshness(store: Store) -> None:
    """Warn when results are built on stale scrape data.

    A showtimes tool that quietly serves week-old data is worse than one that
    says so, because the failure mode is turning up at a cinema.
    """
    last = store.last_refresh()
    if last is None:
        console.print("[yellow]No successful refresh yet — run "
                      "`cinemelb refresh`.[/yellow]")
        return
    now = datetime.now(timezone.utc)
    age = now - last
    hours = age.total_seconds() / 3600
    if hours < 1:
        when = f"{max(1, int(age.total_seconds() // 60))} min ago"
    elif hours < 48:
        when = f"{hours:.0f} h ago"
    else:
        when = f"{hours / 24:.0f} days ago"
    style = "yellow" if hours > 24 else "dim"
    console.print(f"[{style}]data from {when} — `cinemelb refresh` to update[/{style}]")


def _show_hits(hits: list[Q.Hit], *, as_json: bool = False,
               show_distance: bool = False, limit: int | None = None) -> None:
    if as_json:
        _emit([h.to_dict() for h in hits], True)
        return
    if not hits:
        _empty("No screenings matched.")
        return

    table = Table(box=None, pad_edge=False)
    table.add_column("When", style="cyan", no_wrap=True)
    table.add_column("Film", style="white")
    table.add_column("Cinema", style="green")
    table.add_column("Suburb", style="dim")
    if show_distance:
        table.add_column("Dist", justify="right", style="magenta", no_wrap=True)

    rows = hits[: limit or len(hits)]
    for h in rows:
        cells = [h.when, h.film, h.cinema["name"],
                 h.cinema.get("suburb", "") or ""]
        if show_distance:
            cells.append(format_distance(h.distance_km) if h.distance_km is not None else "")
        table.add_row(*cells)
    console.print(table)
    if limit and len(hits) > limit:
        console.print(f"[dim]… {len(hits) - limit} more (use --limit)[/dim]")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

@app.callback()
def main_callback(
    ctx: typer.Context,
    db: Optional[str] = typer.Option(None, "--db", help="Path to the SQLite database"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable JSON output"),
) -> None:
    ctx.obj = {"db": db, "json": json_out}


@app.command()
def refresh(
    ctx: typer.Context,
    only: Optional[list[str]] = typer.Option(None, "--only", "-o",
                                             help="Suburb / venue id / name (repeatable)"),
    source: Optional[list[str]] = typer.Option(None, "--source", "-s",
                                               help="Adapter name (repeatable)"),
    fresh: bool = typer.Option(False, "--fresh", help="Bypass the HTTP cache"),
) -> None:
    """Re-scrape session times into the local database."""
    from .scrape import refresh as do_refresh

    console.print("[bold]Refreshing…[/bold]")
    report = do_refresh(
        only=only, sources=source, fresh=fresh,
        db_path=ctx.obj.get("db"),
        progress=lambda v: console.print(
            f"  {'[green]ok[/green]' if v.ok else '[red]!![/red]'} "
            f"{v.cinema_id:<32} {v.screenings:>5} screenings"
            + (f"  [yellow]{v.note}[/yellow]" if v.note else "")
        ),
    )
    console.print(f"\n[bold]{report.summary()}[/bold]")
    failed = report.failed()
    if failed:
        console.print(f"[yellow]{len(failed)} venue(s) had no data[/yellow]")


@app.command()
def normalise(ctx: typer.Context) -> None:
    """Fold programme prefixes ("BFF26 …", "Astor: …") into the real film titles.

    Idempotent. A full ``refresh`` runs this automatically; the command exists
    so an existing database can be corrected without re-scraping.
    """
    store = _store(ctx)
    stats = store.normalise_films()
    films = len(store.films())
    console.print(
        f"[bold]Films[/bold] {films} · retitled {stats['retitled']} · "
        f"renamed {stats['renamed']} · merged {stats['merged']}"
    )


@app.command()
def film(
    ctx: typer.Context,
    title: str = typer.Argument(..., help="Film title (fuzzy)"),
    day: Optional[str] = typer.Option(None, "--date", "-d", help="today|tomorrow|fri|YYYY-MM-DD"),
    after: Optional[str] = typer.Option(None, "--after", "-a", help="e.g. 19:00 or 7pm"),
    before: Optional[str] = typer.Option(None, "--before", "-b"),
    near: Optional[str] = typer.Option(None, "--near", "-n", help="Suburb, postcode or address"),
    radius: float = typer.Option(25.0, "--radius", "-r", help="km, used with --near"),
    tag: Optional[list[str]] = typer.Option(None, "--tag", help="Venue tag, e.g. arthouse"),
    chain: Optional[list[str]] = typer.Option(None, "--chain", help="e.g. palace, hoyts"),
    fmt: Optional[list[str]] = typer.Option(None, "--format", help="e.g. 70mm, imax"),
    accessible: bool = typer.Option(False, "--accessible", help="Only CC/AD sessions"),
    limit: int = typer.Option(40, "--limit", "-l",
                              help="Max sessions; each venue is represented first"),
    by_cinema: bool = typer.Option(True, "--group/--list", help="Group results by cinema"),
    similar: bool = typer.Option(False, "--similar",
                                 help="Also include similarly-titled films"),
    sold_out: bool = typer.Option(False, "--sold-out",
                                  help="Include sold-out sessions"),
) -> None:
    """Where is this film showing?"""
    store = _store(ctx)
    f = _filters(day=day, after=after, before=before, tags=tag, chain=chain,
                 fmt=fmt, accessible=accessible, limit=limit, sold_out=sold_out)
    origin = _origin(near)
    if origin:
        f.max_distance_km = radius

    matches, hits = Q.find_film(store, title, filters=f, origin=origin,
                                include_similar=similar)
    if not matches:
        _empty(f"No film matching {title!r} in the database. Try `cinemelb trending`.")
        return

    if ctx.obj.get("json"):
        _emit({"matched_films": [{"key": k, "title": t} for k, t in matches],
               "screenings": [h.to_dict() for h in hits]}, True)
        return

    best = matches[0][1]
    others = [t for _, t in Q.resolve_film(store, title)[1:4]]
    if normalise_title(best) != normalise_title(title):
        hint = f" [dim](also: {', '.join(others)} — add --similar)[/dim]" if others else ""
        console.print(f"[dim]Matched:[/dim] [white]{best}[/white]{hint}")
    elif others:
        # An exact title match is not always the film the user means: "dune"
        # matches the 1984/2021 "Dune" exactly, while "Dune: Part Three" is
        # playing at 44 venues.  Say so instead of quietly answering the
        # smaller question — the user can still pass --similar to widen it.
        chosen = _screening_count(store, matches[0][0])
        rival = _best_alternative(store, title, skip=matches[0][0])
        if rival and rival[2] >= max(chosen * 3, chosen + 20):
            console.print(
                f"[dim]Also showing:[/dim] [white]{rival[1]}[/white] "
                f"[dim]({rival[2]} screenings — `cinemelb film {rival[1]!r}`)[/dim]"
            )

    if not hits:
        _empty("No screenings matched those filters.")
        return

    if not by_cinema:
        _show_hits(hits, show_distance=bool(origin))
        return

    # Group by film_key, not by the upstream title string: IMAX calls the same
    # film "DUNE: PART THREE - IMAX 70MM" while everyone else calls it "Dune:
    # Part Three", and splitting the answer into two headings hides the fact
    # that they are one film.  The presentation difference is already carried
    # by each screening's format, shown in the time list.
    grouped: dict[tuple[str, str], list[Q.Hit]] = {}
    for h in hits:
        grouped.setdefault((h.screening["film_key"], h.cinema["id"]), []).append(h)

    current_film = None
    current_cinema = None
    for (film_key, _), group in sorted(
        grouped.items(),
        key=lambda kv: (kv[0][0], kv[1][0].distance_km if kv[1][0].distance_km is not None else 999,
                        kv[1][0].cinema["name"]),
    ):
        head = group[0]
        meta = store.film(film_key) or {}
        if film_key != current_film:
            bits = []
            if meta.get("rating"):
                bits.append(meta["rating"])
            if meta.get("runtime_minutes"):
                bits.append(f"{meta['runtime_minutes']} min")
            if meta.get("genres"):
                bits.append(" / ".join(meta["genres"][:3]))
            suffix = f"  [dim]{' · '.join(bits)}[/dim]" if bits else ""
            display_title = meta.get("title") or head.film
            console.print(f"\n[bold white]{display_title}[/bold white]{suffix}")
            current_film = film_key
            current_cinema = None
        if head.cinema["id"] != current_cinema:
            dist = f"  [magenta]{format_distance(head.distance_km)}[/magenta]" \
                if head.distance_km is not None else ""
            chain = f" [dim]({head.cinema['chain']})[/dim]" if head.cinema.get("chain") else ""
            console.print(f"  [green]{head.cinema['name']}[/green]"
                          f" [dim]{head.cinema.get('suburb', '')}[/dim]{chain}{dist}")
            current_cinema = head.cinema["id"]
        times = []
        for h in group:
            label = f"{date.fromisoformat(h.screening['local_date']):%a} {h.screening['local_time']}"
            if h.screening.get("format"):
                label += f" ({h.screening['format']})"
            times.append(label)
        console.print("      " + "  ".join(times))

    _freshness(store)


@app.command()
def at(
    ctx: typer.Context,
    when: str = typer.Argument(..., help="Start time, e.g. 20:00 / 8pm"),
    window: int = typer.Option(30, "--window", "-w", help="+/- minutes around the time"),
    day: Optional[str] = typer.Option(None, "--date", "-d", help="today|tomorrow|fri|YYYY-MM-DD"),
    near: Optional[str] = typer.Option(None, "--near", "-n"),
    radius: float = typer.Option(25.0, "--radius", "-r"),
    tag: Optional[list[str]] = typer.Option(None, "--tag"),
    chain: Optional[list[str]] = typer.Option(None, "--chain"),
    fmt: Optional[list[str]] = typer.Option(None, "--format"),
    limit: int = typer.Option(60, "--limit", "-l"),
    sold_out: bool = typer.Option(False, "--sold-out"),
) -> None:
    """What starts around a given time?"""
    store = _store(ctx)
    start = _parse_time(when)
    if start is None:
        console.print(f"[red]Could not read time {when!r}[/red]")
        raise typer.Exit(2)
    target_day = _parse_day(day) or datetime.now(MELBOURNE_TZ).date()

    f = _filters(day=day, tags=tag, chain=chain, fmt=fmt, limit=limit,
                 sold_out=sold_out)
    origin = _origin(near)
    if origin:
        f.max_distance_km = radius

    total = datetime.combine(target_day, start)
    lo = (total - timedelta(minutes=window)).time()
    hi = (total + timedelta(minutes=window)).time()
    hits = Q.films_at(store, target_day, start=lo, end=hi, filters=f, origin=origin)

    if ctx.obj.get("json"):
        _emit([h.to_dict() for h in hits], True)
        return
    console.print(f"[bold]{target_day:%A %d %B}[/bold] · "
                  f"{lo.strftime('%H:%M')}–{hi.strftime('%H:%M')} · {len(hits)} screenings")
    _show_hits(hits, show_distance=bool(origin))
    _freshness(store)


@app.command()
def now(
    ctx: typer.Context,
    hours: float = typer.Option(3.0, "--hours", "-h", help="Look-ahead window"),
    near: Optional[str] = typer.Option(None, "--near", "-n"),
    radius: float = typer.Option(10.0, "--radius", "-r"),
    tag: Optional[list[str]] = typer.Option(None, "--tag"),
    limit: int = typer.Option(40, "--limit", "-l"),
) -> None:
    """What can I see in the next few hours?"""
    store = _store(ctx)
    origin = _origin(near)
    f = Q.Filters(tags=list(tag or []), limit=limit)
    if origin:
        f.max_distance_km = radius
    hits = Q.upcoming_hits(store, within_hours=hours, filters=f, origin=origin)
    if ctx.obj.get("json"):
        _emit([h.to_dict() for h in hits], True)
        return
    console.print(f"[bold]Next {hours:g} hours[/bold] · {len(hits)} screenings")
    _show_hits(hits, show_distance=bool(origin))
    _freshness(store)


@app.command()
def near(
    ctx: typer.Context,
    location: Optional[str] = typer.Argument(
        None, help="Suburb, postcode, address or lat,lon. Negative coordinates "
                   "need a leading `--`, e.g. `near -- -37.81,144.96`"
    ),
    at: Optional[str] = typer.Option(
        None, "--at", help="Coordinates as lat,lon (use this to avoid the "
                           "leading-minus problem entirely)"
    ),
    radius: float = typer.Option(2.0, "--radius", "-r", help="km"),
    day: Optional[str] = typer.Option(None, "--date", "-d"),
    after: Optional[str] = typer.Option(None, "--after", "-a"),
    tag: Optional[list[str]] = typer.Option(None, "--tag"),
    films_only: bool = typer.Option(False, "--films", help="One row per film, not per session"),
    limit: int = typer.Option(50, "--limit", "-l"),
    sold_out: bool = typer.Option(False, "--sold-out"),
) -> None:
    """Everything showing within a radius of a location."""
    store = _store(ctx)
    target = at or location
    if not target:
        console.print("[red]Give a location, or --at lat,lon.[/red]")
        raise typer.Exit(2)
    origin = _origin(target)
    if not origin:
        raise typer.Exit(2)
    f = _filters(day=day, after=after, tags=tag, limit=limit, sold_out=sold_out)
    hits = Q.nearest(store, origin, radius_km=radius, filters=f)

    if ctx.obj.get("json"):
        _emit([h.to_dict() for h in hits], True)
        return
    label = target if location else f"{origin[0]:.4f},{origin[1]:.4f}"
    console.print(f"[bold]{label}[/bold] · within {radius:g} km · {len(hits)} screenings")
    if films_only:
        seen: dict[str, Q.Hit] = {}
        for h in hits:
            seen.setdefault(h.screening["film_key"], h)
        table = Table(box=None)
        table.add_column("Film")
        table.add_column("Cinema", style="green")
        table.add_column("Next", style="cyan")
        table.add_column("Dist", justify="right", style="magenta")
        for h in list(seen.values())[:limit]:
            table.add_row(h.film, h.cinema["name"], h.when,
                          format_distance(h.distance_km) if h.distance_km is not None else "")
        console.print(table)
    else:
        _show_hits(hits, show_distance=True)
    _freshness(store)


@app.command("whats-on")
def whats_on(
    ctx: typer.Context,
    day: Optional[str] = typer.Option(None, "--date", "-d"),
    near: Optional[str] = typer.Option(None, "--near", "-n"),
    radius: float = typer.Option(10.0, "--radius", "-r"),
    tag: Optional[list[str]] = typer.Option(None, "--tag"),
    limit: int = typer.Option(30, "--limit", "-l"),
) -> None:
    """The whole day, grouped by film, with venue counts."""
    store = _store(ctx)
    target = _parse_day(day) or datetime.now(MELBOURNE_TZ).date()
    origin = _origin(near)

    f = Q.Filters(day=target, tags=list(tag or []), limit=10000)
    if origin:
        f.max_distance_km = radius
        hits = Q.nearest(store, origin, radius_km=radius, filters=f)
    else:
        hits = Q.films_at(store, target, filters=f)

    grouped: dict[str, list[Q.Hit]] = {}
    for h in hits:
        grouped.setdefault(h.screening["film_key"], []).append(h)

    if ctx.obj.get("json"):
        _emit({k: [h.to_dict() for h in v] for k, v in grouped.items()}, True)
        return

    console.print(f"[bold]{target:%A %d %B}[/bold] · "
                  f"{len(grouped)} films · {len(hits)} screenings")
    table = Table(box=None)
    table.add_column("Film", style="white")
    table.add_column("Venues", justify="right", style="green")
    table.add_column("Sessions", justify="right")
    table.add_column("Venue names", style="dim")
    rows = sorted(grouped.items(), key=lambda kv: -len({h.cinema["id"] for h in kv[1]}))
    for _, group in rows[:limit]:
        venues = {h.cinema["id"]: h.cinema["name"] for h in group}
        names = ", ".join(list(venues.values())[:4])
        if len(venues) > 4:
            names += f" +{len(venues) - 4}"
        table.add_row(group[0].film, str(len(venues)),
                      str(len(group)), names)
    console.print(table)
    _freshness(store)


@app.command()
def trending(
    ctx: typer.Context,
    day: Optional[str] = typer.Option(None, "--date", "-d"),
    limit: int = typer.Option(25, "--limit", "-l"),
) -> None:
    """Films showing at the most venues."""
    store = _store(ctx)
    target = _parse_day(day)
    rows = Q.film_leaderboard(store, day=target, limit=limit)
    if ctx.obj.get("json"):
        _emit(rows, True)
        return
    if not rows:
        _empty("No screenings in the database — run `cinemelb refresh`.")
        return
    table = Table(box=None)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Film", style="white")
    table.add_column("Venues", justify="right", style="green")
    table.add_column("Sessions", justify="right")
    for i, r in enumerate(rows, 1):
        table.add_row(str(i), r["title"], str(r["cinema_count"]), str(r["screenings"]))
    console.print(table)


@app.command("cinemas")
def cinemas_cmd(
    ctx: typer.Context,
    suburb: Optional[str] = typer.Option(None, "--suburb", "-s"),
    tag: Optional[list[str]] = typer.Option(None, "--tag"),
    detail: bool = typer.Option(False, "--detail", help="Show address, coords, adapter"),
) -> None:
    """List venues in the registry."""
    store = _store(ctx)
    rows = Q.cinema_catalogue(store)
    if suburb:
        rows = [r for r in rows if suburb.lower() in (r.get("suburb") or "").lower()]
    if tag:
        rows = [r for r in rows
                if all(t.lower() in {x.lower() for x in r.get("tags") or []} for t in tag)]
    if ctx.obj.get("json"):
        _emit(rows, True)
        return

    table = Table(box=None)
    table.add_column("Cinema", style="green")
    table.add_column("Suburb", style="dim")
    table.add_column("Chain")
    table.add_column("Tags", style="cyan")
    table.add_column("Sessions", justify="right")
    if detail:
        table.add_column("Address", style="dim")
    for r in rows:
        cells = [r["name"], r.get("suburb", ""), r.get("chain", ""),
                 ",".join(r.get("tags") or []), str(r.get("screening_count", 0))]
        if detail:
            cells.append(r.get("address", ""))
        table.add_row(*cells)
    console.print(table)
    console.print(f"[dim]{len(rows)} venues[/dim]")


@app.command()
def search(ctx: typer.Context, text: str = typer.Argument(...)) -> None:
    """Fuzzy-search film titles in the catalogue."""
    store = _store(ctx)
    matches = Q.resolve_film(store, text)
    if not matches:
        _empty("No match.")
        return
    for key, title in matches:
        console.print(f"  [white]{title}[/white] [dim]({key})[/dim]")


@app.command()
def stats(ctx: typer.Context) -> None:
    """Database and scraper health."""
    store = _store(ctx)
    s = store.stats()
    if ctx.obj.get("json"):
        _emit(s, True)
        return
    console.print(f"[bold]{s['cinemas']}[/bold] venues · "
                  f"[bold]{s['films']}[/bold] films · "
                  f"[bold]{s['screenings']}[/bold] screenings")
    if s["days"]:
        console.print(f"dates covered: {s['days'][0]} → {s['days'][-1]} ({len(s['days'])} days)")
    if s["sources"]:
        console.print("by adapter: " + ", ".join(f"{k}={v}" for k, v in s["sources"].items()))
    rows = store.source_summary()
    if rows:
        table = Table(box=None, title="adapter health (latest run)")
        table.add_column("adapter")
        table.add_column("venues", justify="right")
        table.add_column("working", justify="right")
        table.add_column("screenings", justify="right")
        table.add_column("last error", style="dim")
        for r in rows:
            # Only surface an error while the adapter is still failing; a stale
            # message from a run before the adapter existed is just noise.
            err = (r["last_error"] or "")[:52] if r["ok"] < r["venues"] else ""
            table.add_row(
                r["source"] or "", str(r["venues"]),
                f"{r['ok']}/{r['venues']}",
                str(r["screenings"] or 0),
                err,
            )
        console.print(table)


@app.command("next")
def next_cmd(
    ctx: typer.Context,
    title: str = typer.Argument(..., help="Film title (fuzzy)"),
    near: Optional[str] = typer.Option(None, "--near", "-n",
                                       help="Rank by distance from here"),
    radius: float = typer.Option(25.0, "--radius", "-r"),
    count: int = typer.Option(5, "--count", "-c", help="How many to show"),
    day: Optional[str] = typer.Option(None, "--date", "-d"),
) -> None:
    """When and where is the next showing of a film near me?"""
    store = _store(ctx)
    origin = _origin(near)
    f = Q.Filters(day=_parse_day(day), limit=5000)
    matches, hits = Q.find_film(store, title, filters=f, origin=origin)
    if not matches:
        _empty(f"No film matching {title!r}.")
        raise typer.Exit(2)

    now = datetime.now(MELBOURNE_TZ).replace(tzinfo=None)
    upcoming = []
    for h in hits:
        local = datetime.fromisoformat(f"{h.screening['local_date']}T{h.screening['local_time']}")
        if local >= now:
            upcoming.append(h)
    upcoming.sort(key=lambda h: (h.screening["local_date"], h.screening["local_time"]))

    if ctx.obj.get("json"):
        _emit([h.to_dict() for h in upcoming[:count]], True)
        return

    console.print(f"[bold white]{matches[0][1]}[/bold white]")
    if not upcoming:
        _empty("No upcoming screenings found.")
        return
    for h in upcoming[:count]:
        dist = f"  [magenta]{format_distance(h.distance_km)}[/magenta]" \
            if h.distance_km is not None else ""
        when = h.when
        in_days = (date.fromisoformat(h.screening["local_date"])
                   - datetime.now(MELBOURNE_TZ).date()).days
        rel = "today" if in_days == 0 else ("tomorrow" if in_days == 1 else f"in {in_days}d")
        console.print(f"  [cyan]{when}[/cyan] [dim]({rel})[/dim]  "
                      f"[green]{h.cinema['name']}[/green] "
                      f"[dim]{h.cinema.get('suburb', '')}[/dim]{dist}")


@app.command("week")
def week_cmd(
    ctx: typer.Context,
    title: str = typer.Argument(..., help="Film title (fuzzy)"),
    days: int = typer.Option(7, "--days", "-n", help="How many days ahead"),
    near: Optional[str] = typer.Option(None, "--near", "-n"),
    radius: float = typer.Option(25.0, "--radius", "-r"),
    limit: int = typer.Option(40, "--limit", "-l"),
) -> None:
    """A film's next week, day by day."""
    store = _store(ctx)
    origin = _origin(near)
    f = Q.Filters(limit=10000)
    if origin:
        f.max_distance_km = radius

    today = datetime.now(MELBOURNE_TZ).date()
    by_day: dict[str, list[Q.Hit]] = {}
    matches: list[tuple[str, str]] = []
    for offset in range(days):
        target = today + timedelta(days=offset)
        matches, hits = Q.find_film(store, title, filters=Q.Filters(day=target, limit=10000),
                                    origin=origin)
        if origin:
            hits = [h for h in hits if h.distance_km is not None and h.distance_km <= radius]
        if hits:
            by_day[target.isoformat()] = hits

    if not matches:
        _empty(f"No film matching {title!r}.")
        return

    if ctx.obj.get("json"):
        _emit({k: [h.to_dict() for h in v] for k, v in by_day.items()}, True)
        return

    console.print(f"[bold white]{matches[0][1]}[/bold white] · next {days} days")
    if not by_day:
        _empty("No screenings in that window.")
        return

    table = Table(box=None)
    table.add_column("Day", style="cyan")
    table.add_column("Venues", justify="right", style="green")
    table.add_column("Sessions", justify="right")
    table.add_column("Closest / first venues", style="dim")
    for iso, hits in list(by_day.items())[:limit]:
        d = date.fromisoformat(iso)
        venues = {h.cinema["id"]: h.cinema["name"] for h in hits}
        names = ", ".join(list(venues.values())[:3])
        if len(venues) > 3:
            names += f" +{len(venues) - 3}"
        table.add_row(f"{d:%a %d %b}", str(len(venues)), str(len(hits)), names)
    console.print(table)


@app.command("map")
def map_cmd(
    ctx: typer.Context,
    title: Optional[str] = typer.Argument(None, help="Film title (omit to map all venues)"),
    day: Optional[str] = typer.Option(None, "--date", "-d"),
    after: Optional[str] = typer.Option(None, "--after", "-a"),
    near: Optional[str] = typer.Option(None, "--near", "-n"),
    radius: float = typer.Option(10.0, "--radius", "-r"),
    tag: Optional[list[str]] = typer.Option(None, "--tag"),
    out: str = typer.Option("cinemelb-map.html", "--out", "-o"),
    limit: int = typer.Option(400, "--limit", "-l"),
    open_browser: bool = typer.Option(False, "--open", help="Open the map when written"),
) -> None:
    """Open an interactive map of matching venues and session times."""
    from . import mapview

    store = _store(ctx)
    origin = _origin(near)
    target = _parse_day(day)

    if title:
        f = Q.Filters(day=target, tags=list(tag or []), limit=limit,
                      after=_parse_time(after))
        if origin:
            f.max_distance_km = radius
        matches, hits = Q.find_film(store, title, filters=f, origin=origin)
        if not matches:
            _empty(f"No film matching {title!r}.")
            raise typer.Exit(2)
        heading = matches[0][1]
        if len(matches) > 1:
            heading += f" (+{len(matches) - 1} similar)"
    else:
        f = Q.Filters(day=target, tags=list(tag or []), limit=limit,
                      after=_parse_time(after))
        if origin:
            hits = Q.nearest(store, origin, radius_km=radius, filters=f)
        else:
            hits = Q.films_at(store, target or datetime.now(MELBOURNE_TZ).date(), filters=f)
        heading = "Melbourne cinemas"

    if not hits:
        _empty("Nothing matched — nothing to map.")
        return

    when = f"{target:%a %d %b}" if target else "next up"
    path = mapview.render(
        hits,
        title=heading,
        subtitle=f"{len(hits)} sessions · {len({h.cinema['id'] for h in hits})} venues · {when}",
        origin=origin,
        radius_km=radius if origin else None,
        out_path=out,
    )
    console.print(f"[green]wrote[/green] {path} "
                  f"[dim]({len(hits)} sessions, "
                  f"{len({h.cinema['id'] for h in hits})} venues)[/dim]")
    if open_browser:
        import webbrowser

        webbrowser.open(path.resolve().as_uri())


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Report which adapters load and which venues they cover."""
    from .adapters import available, known
    from .scrape import load_registry

    rows = load_registry()
    by_source: dict[str, list[str]] = {}
    for r in rows:
        by_source.setdefault(r.get("source") or "?", []).append(r["id"])

    ready = set(available())
    table = Table(box=None)
    table.add_column("adapter", style="green")
    table.add_column("venues", justify="right")
    table.add_column("status")
    for source in known():
        n = len(by_source.get(source, []))
        if n == 0:
            continue
        status = "[green]ready[/green]" if source in ready else "[red]missing[/red]"
        table.add_row(source, str(n), status)
    console.print(table)
    missing = [s for s in by_source if s not in ready]
    if missing:
        console.print(f"[yellow]unimplemented: {', '.join(missing)}[/yellow]")


@app.command()
def export(
    ctx: typer.Context,
    out: str = typer.Option("dist", "--out", "-o", help="Directory to write the static site"),
    days: Optional[int] = typer.Option(None, "--days", help="Limit to the next N days"),
    synopsis: int = typer.Option(200, "--synopsis", help="Synopsis character cap (0 = full)"),
    no_urls: bool = typer.Option(False, "--no-urls", help="Drop ticket URLs (smaller bundle)"),
    ratings: bool = typer.Option(True, "--ratings/--no-ratings",
                                 help="Bake cached IMDb/RT/MC scores into the bundle"),
    posters: bool = typer.Option(True, "--posters/--no-posters",
                                 help="Bake resolved cover images into the bundle"),
) -> None:
    """Build the static SPA site into a directory, ready to deploy."""
    from .export import build_bundle, write_site

    store = _store(ctx)
    bundle = build_bundle(
        store,
        days=days,
        synopsis_cap=synopsis,
        include_urls=not no_urls,
        ratings=_ratings_for(store) if ratings else None,
        posters=_posters_for() if posters else None,
    )
    path = write_site(bundle, out)

    payload = (path / "data" / "bundle.json").stat().st_size
    counts = bundle["counts"]
    console.print(
        f"[bold]Exported[/bold] {counts['screenings']:,} sessions · "
        f"{counts['films']} films · {counts['cinemas']} cinemas · "
        f"{counts['dates']} dates"
    )
    console.print(f"  bundle  {payload / 1024:,.0f} KB  →  {path}")
    if ratings:
        console.print(f"  ratings {counts.get('rated', 0)} of {counts['films']} films scored")
    if posters:
        console.print(f"  covers  {counts.get('posters', 0)} of {counts['films']} films")


def _ratings_for(store) -> dict[str, dict]:
    """Film key -> cached score entry, if the ratings module and cache exist.

    The cache is keyed by normalised title because that is what the enrichment
    step matches on; the bundle needs it keyed by film key.
    """
    try:
        from .ratings import cache_key, load_cache
    except ImportError:
        return {}
    cache = load_cache(Path("data/ratings.json"))
    out: dict[str, dict] = {}
    for film in store.films():
        entry = cache.get(cache_key(film["title"], film.get("year")))
        if entry:
            out[film["key"]] = entry
    return out


def _posters_for() -> dict[str, dict]:
    """Film key -> cached cover entry. Missing cache is not an error."""
    try:
        from .posters import load_cache
    except ImportError:
        return {}
    return load_cache(Path("data/posters.json"))


@app.command()
def posters(
    ctx: typer.Context,
    limit: Optional[int] = typer.Option(
        None, "--limit", help="Only look up this many films that are not cached"),
    refresh: bool = typer.Option(
        False, "--refresh", help="Ignore cached covers and look them up again"),
    retry_misses: bool = typer.Option(
        False, "--retry-misses",
        help="Look up films that were cached as no-match, keeping resolved covers"),
    pace: float = typer.Option(0.35, "--pace", help="Seconds between IMDb requests"),
    ttl: float = typer.Option(30.0, "--ttl", help="Days a cached cover stays fresh"),
) -> None:
    """Resolve a real cover image for every film.

    Venue feeds are inconsistent and Palace's poster endpoint returns its own
    logo for every title, so covers are resolved from IMDb's public suggestion
    endpoint and cached in ``data/posters.json``. Matching is strict; a film
    with no confident match keeps its venue poster (unless that poster is a
    known placeholder) and otherwise gets a typographic fallback in the UI.
    """
    from .posters import enrich

    store = _store(ctx)
    films = store.films()
    if not films:
        console.print("[yellow]No films in the database. Run `refresh` first.[/yellow]")
        raise typer.Exit(1)

    imdb_ids: dict[str, str] = {}
    for key, entry in _ratings_for(store).items():
        imdb_id = str(entry.get("imdb_id") or "")
        if imdb_id:
            imdb_ids[key] = imdb_id

    seen = 0

    def progress(film_key: str, entry: dict) -> None:
        nonlocal seen
        seen += 1
        if seen % 25 == 0:
            console.print(
                f"  {seen:>4}  {film_key[:40]:<40} {entry.get('status', '-'):<5} "
                f"{entry.get('imdb_id', '')}",
                highlight=False,
            )

    result = enrich(
        films,
        cache_path=Path("data/posters.json"),
        imdb_ids=imdb_ids,
        limit=limit,
        ttl_days=0 if refresh else ttl,
        pace=pace,
        retry_misses=retry_misses,
        progress=progress,
    )
    stats = result["stats"]
    console.print(
        f"[bold]Covers[/bold] {stats['films']} films · cached {stats['cached']} · "
        f"looked up {stats['looked']} · resolved {stats['ok']} · missing "
        f"{stats['miss']} · failed {stats['failed']}"
    )
    console.print("  cache  data/posters.json — run `export` to bake it in")


@app.command()
def ratings(
    ctx: typer.Context,
    limit: Optional[int] = typer.Option(
        None, "--limit", help="Only look up this many films that are not cached"),
    refresh: bool = typer.Option(
        False, "--refresh", help="Ignore cached scores and look them up again"),
    scrape: bool = typer.Option(
        False, "--scrape-rt-mc",
        help="Parse Rotten Tomatoes/Metacritic pages directly; their terms "
             "forbid automated collection"),
    pace: float = typer.Option(2.5, "--pace", help="Seconds between requests to the same host"),
    ttl: float = typer.Option(14.0, "--ttl", help="Days a cached score stays fresh"),
) -> None:
    """Attach IMDb, Rotten Tomatoes and Metacritic scores to the catalogue.

    Build-time and cached: the browser never calls a ratings service. IMDb comes
    from the official bulk dataset, RT/MC from OMDb when ``OMDB_API_KEY`` is set,
    and Metacritic also has a keyless fallback. Direct page parsing needs
    ``--scrape-rt-mc`` and is against both sites' terms.
    """
    import os

    from .ratings import enrich

    store = _store(ctx)
    films = store.films()
    if not films:
        console.print("[yellow]No films in the database. Run `refresh` first.[/yellow]")
        raise typer.Exit(1)

    omdb_key = os.environ.get("OMDB_API_KEY") or None
    if omdb_key:
        console.print("[dim]OMDb key found: IMDb, Rotten Tomatoes and Metacritic.[/dim]")
    else:
        console.print(
            "[dim]No OMDB_API_KEY: IMDb from the bulk dataset, Metacritic via its "
            "finder; Rotten Tomatoes only with --scrape-rt-mc.[/dim]"
        )
    if scrape:
        console.print(
            "[yellow]--scrape-rt-mc parses RT/MC pages; their terms forbid automated "
            "collection.[/yellow]"
        )

    seen = 0

    def progress(film_key: str, status: dict) -> None:
        nonlocal seen
        seen += 1
        if seen % 20 == 0:
            console.print(
                f"  {seen:>4}  {film_key[:40]:<40} "
                f"imdb={status.get('imdb') if status.get('imdb') is not None else '-':<5} "
                f"rt={status.get('rt') if status.get('rt') is not None else '-':<5} "
                f"mc={status.get('mc') if status.get('mc') is not None else '-'}",
                highlight=False,
            )

    result = enrich(
        films,
        cache_path=Path("data/ratings.json"),
        omdb_key=omdb_key,
        scrape=scrape,
        limit=limit,
        ttl_days=0 if refresh else ttl,
        pace=pace,
        progress=progress,
    )
    stats = result["stats"]
    console.print(
        f"[bold]Ratings[/bold] {stats['films']} films · cached {stats['cached']} · "
        f"resolved {stats['resolved']} · IMDb {stats['imdb']} · RT {stats['rt']} · "
        f"MC {stats['mc']} · skipped {stats['skipped']} · failed {stats['failed']}"
    )
    console.print("  cache  data/ratings.json — run `export` to bake it in")


@app.command()
def serve(
    ctx: typer.Context,
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address"),
    port: int = typer.Option(8787, "--port", "-p", help="Port"),
) -> None:
    """Serve the results as a read-only local web page."""
    from .serve import serve as serve_ui

    serve_ui(host=host, port=port, db=ctx.obj.get("db"))


def run() -> None:
    app()


if __name__ == "__main__":
    run()
