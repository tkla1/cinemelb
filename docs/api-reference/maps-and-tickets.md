# Google Maps, Apple Maps, and Australian cinema ticket links

Reference notes for the SPA's venue cards. Verified against live pages on
2026-11-26 unless a row says otherwise.

## 1. Map links without an API key

Use Google Maps `search/?api=1&query=…`. It is the only documented, keyless,
cross-platform form, and Google states the same URL opens the Maps app when
installed and the web otherwise.

```
Pin (coordinates)   https://www.google.com/maps/search/?api=1&query=<lat>%2C<lon>
Pin (address)       https://www.google.com/maps/search/?api=1&query=<urlencoded "Name, street, suburb VIC 3000">
Pin + place id      https://www.google.com/maps/search/?api=1&query=<lat>%2C<lon>&query_place_id=<PLACE_ID>
Directions          https://www.google.com/maps/dir/?api=1&destination=<lat>%2C<lon>&travelmode=transit
```

- `api=1` is mandatory; without it every other parameter is ignored.
- Coordinates alone give a pin but **no place panel** (no hours, no reviews).
  An address or name query gives a richer card. `query_place_id` is the only
  way to guarantee the right place, but we do not hold place IDs yet.
- Encode `,` as `%2C` and spaces as `%20`; URLs max out at 2,048 characters.
- `place/?q=place_id:…` is not in the documented reference — treat it as
  desktop-only folklore and do not use it.
- No deprecation notice exists for the documented form. The legacy
  `google.com/maps/dir//lat,lon` still works and is what Hoyts and Village
  themselves emit, but it carries no compatibility promise.

Apple Maps is worth offering only behind iOS detection, inside a details panel
rather than as a second primary button:

```
Place   https://maps.apple.com/place?coordinate=<lat>,<lon>&name=<venue>
Search  https://maps.apple.com/search?query=<venue>&center=<lat>,<lon>
Route   https://maps.apple.com/directions?destination=<lat>,<lon>&mode=transit
Legacy  https://maps.apple.com/?q=<venue>&ll=<lat>,<lon>
```

The Google URL already hands off to the Google Maps iOS app when installed, so
Apple Maps only helps people who removed it. Skip it for now.

## 2. Venue metadata worth showing

Priority: name → suburb → address → sessions → phone (`tel:`) → website →
directions. Accessibility and parking are genuinely useful but are chain-level
(Hoyts: "open captioning at all our cinema locations") or free text we do not
hold, so they belong behind a disclosure, not in the always-visible row.

We have, for all 63 venues: address, suburb, postcode, state, lat/lon, phone
(sometimes), website (31/63), booking_url (sometimes).

Inline: name, suburb, address, distance, next sessions, Directions.
Expandable: phone, website, booking page, accessibility link.

## 3. Ticket URL patterns (AU chains)

| Chain | Venue page | Session / buy link | Confidence |
|---|---|---|---|
| Hoyts | `hoyts.com.au/cinemas/<slug>` | `hoyts.com.au/orders/tickets?cinemaId=<CODE>&sessionId=<id>` | verified live |
| Village | `villagecinemas.com.au/cinemas/<slug>` | **no session deep link** — the old `#/sessions/…` hash SPA is gone; cart is client-side | verified negative |
| Palace (+ Astor) | `palacecinemas.com.au/cinemas/<slug>` | `buy.palacecinemas.com.au/?cinema=<id>&session=<id>` (Astor = 180) | verified live |
| Reading | `readingcinemas.com.au/cinemas/<id>` | `readingcinemas.com.au/cinemas/<id>/sessions/<sessionId>/<movieId>` | high (router constants) |
| Cinema Nova | `cinemanova.com.au` | `ticketing.cinemanova.com.au/Ticketing/visSelectTickets.aspx?cinemacode=1&txtSessionId=<id>` | verified live |
| Classic / Lido / Cameo | `classiccinemas.com.au`, `lidocinemas.com.au`, `cameocinemas.com.au` | `<host>/tickets?c=<code>&s=<sessionId>` (`0000000002` / `0000000003` / `0000000001`) | verified live |
| Astor | `astortheatre.net.au/sessions/<YYYY-MM-DD-HHMM>` | Palace buy link, `cinema=180` | verified live |
| Sun | `suntheatre.com.au/tickets/` | `/tickets/` widget | partial |
| Dendy | `dendy.com.au/<alias>/` | `/<alias>/sessions/<sessionId>/<movieId>` | medium (string pattern) |
| IMAX Melbourne | `imaxmelbourne.com.au/movie/<slug>` | own queue system | low — Cloudflare 403 |
| Event | `eventcinemas.com.au/cinema/<slug>` | `/Sessions` → seat select | partial |
| Moving Story, Ritz, Thornbury Picture House, ACMI | — | — | unverified |

Gotchas:

- `imax.com.au` is **not** IMAX Melbourne; it redirects to Event. IMAX
  Melbourne is `imaxmelbourne.com.au`.
- Palace cinema IDs are global (Astor = 180). We already hold 18,154 session
  URLs, so the cheapest correct approach is to **use the stored URL** and only
  synthesise one when it is missing.
- Hoyts and Nova both run Vista, hence the `cinemaId`/`sessionId` and
  `visSelectTickets` shapes; a Vista-shaped parser covers both.
- Village has no session deep link: link the venue page and let the user pick.

## 4. Sessions with no ticket URL (903 of 19,057)

Never render a dead control. Tiered policy:

1. Session URL present → "Buy tickets" (external).
2. No session URL, venue has a booking page → "Tickets at <chain>" to the
   venue page, or a quieter "Check availability".
3. Known sold out → static "Sold out" label, no link, venue link still there.
4. Neither → omit the CTA; keep the session time.

Do not hide the session: walk-ins still use the time.

## 5. External-link affordance

```html
<a href="…" target="_blank" rel="noopener noreferrer"
   aria-label="Buy tickets for <Film> at <Venue>, <time> (opens in a new tab)">
  Buy tickets
  <svg aria-hidden="true" focusable="false">…</svg>
</a>
```

`target="_blank"` already implies `noopener` in current browsers; keep it
explicit for older engines. Put "(opens in a new tab)" in the accessible name,
not the visible copy. Link text must stand alone in a link list, so the
accessible name carries film + venue + time while the visible label stays short.

## 6. Not verified

- Reading's `cinemaId` semantics; the SPA returns a 200 shell for any path, so
  status codes prove nothing.
- Dendy's session URL pattern (read from JS, never executed).
- IMAX Melbourne (Cloudflare 403 on both hosts).
- Village session deep link is a **negative** claim; worth one manual check.
- Place IDs for the 63 venues — a separate batch job via Places API Text Search.

## Sources

- https://developers.google.com/maps/documentation/urls/get-started
- https://developer.apple.com/library/archive/featuredarticles/iPhoneURLScheme_Reference/MapLinks/MapLinks.html
- https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/a
- https://www.wikidata.org/wiki/Wikidata:Property_proposal/Google_Maps_Place_ID
- Live: hoyts.com.au, villagecinemas.com.au, palacecinemas.com.au,
  buy.palacecinemas.com.au, astortheatre.net.au, cinemanova.com.au,
  ticketing.cinemanova.com.au, classic/lido/cameocinemas.com.au,
  suntheatre.com.au, dendy.com.au, readingcinemas.com.au
