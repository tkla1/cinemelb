"""Interactive map export.

Given a set of :class:`cinemelb.query.Hit` results, render a single
self-contained HTML page: a Leaflet map with one pin per venue (sized by how
many matching sessions it has) and a sidebar listing every session.  This is
the answer to "where are the closest showings?" in visual form.

The page needs a network connection only for the Leaflet CDN assets; all the
data is embedded, so it opens instantly and never calls back to this tool.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from .geo import format_distance
from .models import MELBOURNE_TZ
from .sanitize import json_for_script, safe_href

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet"
      href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
      crossorigin="anonymous">
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:#12141a; color:#e8eaf0; display:flex; height:100vh; }}
  #map {{ flex:1 1 62%; min-width:0; }}
  aside {{ flex:0 0 38%; max-width:520px; overflow-y:auto; border-left:1px solid #262a35;
           background:#171a21; padding:18px 20px 40px; }}
  h1 {{ font-size:17px; margin:0 0 4px; font-weight:650; }}
  .sub {{ color:#8b93a5; font-size:12px; margin-bottom:16px; }}
  .venue {{ border:1px solid #262a35; border-radius:10px; padding:11px 13px; margin-bottom:10px;
            background:#1c2029; }}
  .venue h2 {{ font-size:14px; margin:0 0 2px; font-weight:600; }}
  .meta {{ color:#8b93a5; font-size:11.5px; margin-bottom:7px; }}
  .dist {{ color:#b48cf0; font-weight:600; }}
  .times {{ display:flex; flex-wrap:wrap; gap:5px; }}
  .t {{ background:#242a36; border-radius:5px; padding:2px 7px; font-size:12px;
        text-decoration:none; color:#cfe3ff; font-variant-numeric:tabular-nums; }}
  .t:hover {{ background:#31394a; }}
  .t.fmt {{ color:#ffd479; }}
  .tag {{ display:inline-block; background:#2a2033; color:#c9a6ff; border-radius:4px;
          padding:0 5px; font-size:10.5px; margin-right:4px; }}
  .leaflet-popup-content {{ color:#111; }}
  .leaflet-popup-content a {{ color:#0553b5; }}
</style>
</head>
<body>
<div id="map"></div>
<aside>
  <h1>{title}</h1>
  <div class="sub">{subtitle}</div>
  <div id="venues"></div>
</aside>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
        crossorigin="anonymous"></script>
<script>
const DATA = {data_json};

const map = L.map('map', {{ zoomControl: true }});
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  maxZoom: 19,
  attribution: '&copy; OpenStreetMap contributors &copy; CARTO'
}}).addTo(map);

const markers = [];
for (const v of DATA.venues) {{
  if (v.lat === null || v.lon === null) continue;
  const radius = Math.min(20, 6 + Math.sqrt(v.count) * 2.2);
  const m = L.circleMarker([v.lat, v.lon], {{
    radius, color: '#7c5cff', weight: 2, fillColor: '#9d82ff', fillOpacity: .55
  }}).addTo(map);
  const popup = `<b>${{esc(v.name)}}</b><br>${{esc(v.suburb)}}`
    + (v.distance ? `<br><span class="dist">${{esc(v.distance)}}</span>` : '')
    + `<br>${{v.count}} session${{v.count === 1 ? '' : 's'}}`;
  m.bindPopup(popup);
  markers.push(m);
}}

if (DATA.origin) {{
  L.marker([DATA.origin.lat, DATA.origin.lon]).addTo(map).bindPopup('<b>You</b>');
  if (DATA.origin.radius_km) {{
    L.circle([DATA.origin.lat, DATA.origin.lon], {{
      radius: DATA.origin.radius_km * 1000, color:'#4ade80', weight:1,
      fillColor:'#4ade80', fillOpacity:.05
    }}).addTo(map);
  }}
}}

const bounds = L.latLngBounds(markers.map(m => m.getLatLng()));
if (bounds.isValid()) map.fitBounds(bounds.pad(0.18));
else map.setView([-37.8136, 144.9631], 12);

function esc(s) {{
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}

const box = document.getElementById('venues');
box.innerHTML = DATA.venues.map(v => {{
  const times = v.sessions.map(s =>
    `<a class="t ${{s.format ? 'fmt' : ''}}" href="${{esc(s.ticket_url || '#')}}" target="_blank"
        title="${{esc(s.format || 'standard')}}">${{esc(s.time)}}${{s.date !== v.sessions[0].date ? ' ' + esc(s.date_short) : ''}}</a>`
  ).join('');
  const tags = (v.tags || []).map(t => `<span class="tag">${{esc(t)}}</span>`).join('');
  return `<div class="venue"><h2>${{esc(v.name)}}</h2>`
    + `<div class="meta">${{esc(v.suburb)}}`
    + (v.chain ? ` · ${{esc(v.chain)}}` : '')
    + (v.distance ? ` · <span class="dist">${{esc(v.distance)}}</span>` : '')
    + (v.travel ? ` · ~${{v.travel}} min` : '')
    + `</div><div class="meta">${{tags}}</div>`
    + `<div class="times">${{times}}</div></div>`;
}}).join('') || '<div class="meta">No sessions matched.</div>';
</script>
</body>
</html>
"""


def _venue_payload(hits, origin, radius_km) -> dict:
    grouped: dict[str, dict] = {}
    for h in hits:
        cid = h.cinema["id"]
        entry = grouped.get(cid)
        if entry is None:
            lat = h.cinema.get("latitude")
            lon = h.cinema.get("longitude")
            entry = {
                "id": cid,
                "name": h.cinema["name"],
                "suburb": h.cinema.get("suburb", ""),
                "chain": h.cinema.get("chain", ""),
                "tags": h.cinema.get("tags") or [],
                "lat": float(lat) if lat is not None else None,
                "lon": float(lon) if lon is not None else None,
                "distance": (format_distance(h.distance_km)
                             if h.distance_km is not None else ""),
                "travel": h.travel_min,
                "count": 0,
                "sessions": [],
            }
            grouped[cid] = entry
        entry["count"] += 1
        from datetime import date as _date

        d = _date.fromisoformat(h.screening["local_date"])
        entry["sessions"].append({
            "time": h.screening["local_time"],
            "date": h.screening["local_date"],
            "date_short": f"{d:%a}",
            "film": h.film,
            "format": h.screening.get("format", ""),
            "ticket_url": safe_href(h.screening.get("ticket_url", "")),
        })

    for entry in grouped.values():
        if entry["distance"]:
            entry["sessions"].sort(key=lambda s: (s["date"], s["time"]))
        else:
            entry["sessions"].sort(key=lambda s: (s["date"], s["time"]))

    venues = list(grouped.values())
    venues.sort(key=lambda v: (v["distance"] == "", _dist_key(v), v["name"]))
    payload = {"venues": venues}
    if origin:
        payload["origin"] = {"lat": origin[0], "lon": origin[1],
                             "radius_km": radius_km}
    return payload


def _dist_key(entry: dict) -> float:
    # "1.2 km" -> 1.2, "400 m" -> 0.4 ; blank -> large
    d = entry.get("distance") or ""
    try:
        if d.endswith(" km"):
            return float(d[:-3])
        if d.endswith(" m"):
            return float(d[:-2]) / 1000.0
    except ValueError:
        pass
    return 1e9


def render(hits, *, title: str, subtitle: str = "", origin=None,
           radius_km: float | None = None, out_path: str | Path) -> Path:
    payload = _venue_payload(hits, origin, radius_km)
    stamp = datetime.now(MELBOURNE_TZ).strftime("%a %d %b %Y, %H:%M")
    subtitle = subtitle or f"generated {stamp}"
    html_text = _TEMPLATE.format(
        title=html.escape(title),
        subtitle=html.escape(subtitle),
        data_json=json_for_script(payload),
    )
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_text, encoding="utf-8")
    return path
