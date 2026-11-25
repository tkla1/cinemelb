/* cinemelb — static single-page app.
 *
 * The whole dataset is one JSON bundle fetched once. Every query the CLI
 * answers is answered here in memory: "what's on today", "what's still
 * catchable", "all times for this film".
 *
 * The bundle is index-table shaped: a screening row is nine integers into
 * shared tables. Routing is hash-based, so the app never asks the server for a
 * path — a Pages SPA fallback answers unknown paths with the shell and a 200,
 * which would turn a mistyped data URL into a silently parsed HTML document.
 */
'use strict';

(function () {
  // ------------------------------------------------------------------ config
  var LEAD_MINUTES = 20;      // a session starting sooner than this is not catchable
  var RAIL_DAYS = 14;         // days shown in the day rail
  var TZ = 'Australia/Melbourne';
  // Planning speed for drive estimates, matching cinemelb.geo.SPEED_KMH.drive,
  // so the page and the CLI quote the same figure for the same venue.
  var DRIVE_KMH = 22;

  // Sessions starting before this many minutes past midnight belong to the
  // previous listing day (a 00:31 session is Friday's late night). Cinemas run
  // nothing else that early, so 06:00 is a safe cutoff.
  var LATE_CUTOFF = 6 * 60;

  // Session filters. "after" is a minute past midnight, so one preset works on
  // every day; "within" is a drive-time ceiling in minutes and only appears
  // once a place is set. Both are session-level, so they behave identically in
  // the day list and on a film page.
  var AFTER_PRESETS = [
    [0, 'Any time'],
    [12 * 60, 'After 12pm'],
    [15 * 60, 'After 3pm'],
    [17 * 60, 'After 5pm'],
    [18 * 60, 'After 6pm'],
    [19 * 60, 'After 7pm'],
    [20 * 60, 'After 8pm'],
    [21 * 60, 'After 9pm']
  ];
  var WITHIN_PRESETS = [
    [0, 'Any distance'],
    [10, 'Within 10 min drive'],
    [20, 'Within 20 min drive'],
    [30, 'Within 30 min drive'],
    [45, 'Within 45 min drive'],
    [60, 'Within 60 min drive']
  ];
  // Time-of-day buckets for the "by time" grouping. A day holds ~1,900
  // sessions, so a flat timeline is unreadable; buckets keep the pivot useful.
  // Late night runs past midnight: shifted rows carry minute + 1440.
  var TIME_BUCKETS = [
    { from: 0, to: 12 * 60, name: 'Morning' },
    { from: 12 * 60, to: 17 * 60, name: 'Afternoon' },
    { from: 17 * 60, to: 21 * 60, name: 'Evening' },
    { from: 21 * 60, to: 30 * 60, name: 'Late night' }
  ];

  // ------------------------------------------------------------------- state
  var S = {
    bundle: null,
    cinemas: [],
    films: [],
    rowFilm: null, rowCinema: null, rowMinute: null,
    rowDate: null, rowFormat: null, rowAttr: null,
    rowUrl: null, rowScreen: null, rowSold: null,
    byDate: null,
    byFilm: null,
    today: 0,          // index into bundle.dates
    nowMinute: 0,
    filters: { accessible: false, chain: '', later: false, after: 0, within: 0 },
    group: 'film',       // day view: 'film' | 'cinema' | 'time'
    filmGroup: 'day',    // film view: 'day' | 'cinema' | 'time'
    place: null,       // { name, lat, lon, source } once a location is set
    route: { name: 'day', date: null, film: null },
    chains: []
  };

  var D = {};

  // ------------------------------------------------------------ time helpers
  var dtf = null;
  try {
    dtf = new Intl.DateTimeFormat('en-AU', {
      timeZone: TZ, hour12: false,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit'
    });
  } catch (e) { dtf = null; }

  function pad(n) { return (n < 10 ? '0' : '') + n; }

  function melbourneNow() {
    var now = new Date();
    if (!dtf) {
      return { date: now.getFullYear() + '-' + pad(now.getMonth() + 1) + '-' + pad(now.getDate()),
               minute: now.getHours() * 60 + now.getMinutes() };
    }
    var p = {};
    var parts = dtf.formatToParts(now);
    for (var i = 0; i < parts.length; i++) p[parts[i].type] = parts[i].value;
    return { date: p.year + '-' + p.month + '-' + p.day,
             minute: (parseInt(p.hour, 10) % 24) * 60 + parseInt(p.minute, 10) };
  }

  var DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  var DOW_LONG = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  var MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var MON_LONG = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                  'August', 'September', 'October', 'November', 'December'];

  /* Parse an ISO date as a *local* midnight. `new Date("2026-11-25")` is parsed
     as UTC, which lands on the previous day in Melbourne. */
  function parseDay(iso) {
    var p = iso.split('-');
    return new Date(+p[0], +p[1] - 1, +p[2]);
  }

  function dayParts(iso) {
    var d = parseDay(iso);
    return { dow: DOW[d.getDay()], dowLong: DOW_LONG[d.getDay()],
             day: d.getDate(), mon: MON[d.getMonth()], monLong: MON_LONG[d.getMonth()] };
  }

  function shortDay(iso) {
    var p = dayParts(iso);
    return p.dow + ' ' + p.day + ' ' + p.mon;
  }

  /* Long-range coverage is quoted with its year; today's dates are not. */
  function coverDay(iso) {
    var p = dayParts(iso);
    var thisYear = parseDay(melbourneNow().date).getFullYear();
    var year = parseDay(iso).getFullYear();
    return p.dow + ' ' + p.day + ' ' + p.mon + (year === thisYear ? '' : ' ' + year);
  }

  function longDay(iso) {
    var p = dayParts(iso);
    return p.dowLong + ' ' + p.day + ' ' + p.monLong;
  }

  /* Melbourne cinemas write "7pm", "7:30pm", "12pm" — never "07:00". Minutes
     past 24:00 are late-night sessions shifted onto the previous listing day. */
  function clock(minute) {
    var m = ((minute % 1440) + 1440) % 1440;
    var h = Math.floor(m / 60), mm = m % 60;
    var suffix = h >= 12 ? 'pm' : 'am';
    var h12 = h % 12;
    if (h12 === 0) h12 = 12;
    return h12 + (mm ? ':' + pad(mm) : '') + suffix;
  }

  function relative(iso) {
    var i = S.bundle.dates.indexOf(iso);
    if (i < 0) return '';
    var delta = i - S.today;
    if (delta === 0) return 'Today';
    if (delta === 1) return 'Tomorrow';
    if (delta === -1) return 'Yesterday';
    return '';
  }

  // -------------------------------------------------------------- location
  /* A location is one of: a bundle gazetteer hit, a Nominatim lookup, the
     browser's own coordinates, or a literal "lat,lon". Distances are
     great-circle, and drive time divides by DRIVE_KMH — the same estimate the
     CLI prints, so "near Brunswick" reads the same in both places. */

  function haversineKm(lat1, lon1, lat2, lon2) {
    var R = 6371.0088;
    var p1 = lat1 * Math.PI / 180, p2 = lat2 * Math.PI / 180;
    var dphi = (lat2 - lat1) * Math.PI / 180;
    var dlam = (lon2 - lon1) * Math.PI / 180;
    var a = Math.sin(dphi / 2) * Math.sin(dphi / 2) +
            Math.cos(p1) * Math.cos(p2) * Math.sin(dlam / 2) * Math.sin(dlam / 2);
    return 2 * R * Math.asin(Math.sqrt(a));
  }

  function kmTo(cinemaIdx) {
    if (!S.place) return null;
    var c = S.cinemas[cinemaIdx];
    if (!c || c[6] == null || c[7] == null) return null;
    return haversineKm(S.place.lat, S.place.lon, c[6], c[7]);
  }

  function nearestKmOf(rows) {
    var best = null;
    for (var i = 0; i < rows.length; i++) {
      var km = kmTo(S.rowCinema[rows[i]]);
      if (km != null && (best === null || km < best)) best = km;
    }
    return best;
  }

  function fmtKm(km) {
    if (km == null) return '';
    if (km < 1) return Math.max(10, Math.round(km * 100) * 10) + ' m';
    if (km < 10) return km.toFixed(1) + ' km';
    return Math.round(km) + ' km';
  }

  function driveMinutes(km) {
    return Math.max(1, Math.round(km / DRIVE_KMH * 60));
  }

  function fmtDrive(km) {
    return km == null ? '' : '~' + driveMinutes(km) + ' min drive';
  }

  function titleCase(text) {
    var acronyms = { cbd: 'CBD', qv: 'QV', au: 'AU', vic: 'VIC', unsw: 'UNSW' };
    return text.split(' ').map(function (word) {
      if (!word) return word;
      var lower = word.toLowerCase();
      if (acronyms[lower]) return acronyms[lower];
      return lower.charAt(0).toUpperCase() + lower.slice(1);
    }).join(' ');
  }

  function bundlePlace(q) {
    var places = S.bundle.places || [];
    var key = q.toLowerCase().replace(/[.,]/g, ' ').replace(/\s+/g, ' ').trim();
    if (key.length < 3) return null;
    var best = null, bestScore = -1;
    for (var i = 0; i < places.length; i++) {
      var name = places[i][0];
      var score = -1;
      if (name === key) score = 100;
      else if (name.indexOf(key) === 0) score = 80;
      else if (name.indexOf(' ' + key) > 0) score = 70;
      else if (key.length >= 4 && name.indexOf(key) > 0) score = 50;
      if (score > bestScore) { bestScore = score; best = places[i]; }
    }
    if (!best) return null;
    return { name: titleCase(best[0]), lat: best[1], lon: best[2], source: 'gazetteer' };
  }

  function parseLatLon(q) {
    var m = /^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$/.exec(q);
    if (!m) return null;
    var lat = parseFloat(m[1]), lon = parseFloat(m[2]);
    if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return null;
    return { name: lat.toFixed(4) + ', ' + lon.toFixed(4), lat: lat, lon: lon, source: 'coords' };
  }

  function shortLabel(displayName) {
    // Nominatim returns "91, Swan Street, Cremorne, Yarra, Victoria, 3121,
    // Australia" or "Fitzroy North, Yarra, Victoria, Australia". Keep the
    // street (folding a leading house number into it) and the suburb, drop the
    // administrative tail.
    var parts = String(displayName || '').split(',').map(function (s) { return s.trim(); });
    var skip = /^(australia|victoria|new south wales|vic|nsw|\d{4})$/i;
    var keep = [];
    for (var i = 0; i < parts.length && keep.length < 2; i++) {
      var part = parts[i];
      if (!part || skip.test(part)) continue;
      if (/^\d+[a-z]?$/i.test(part) && parts[i + 1]) {
        keep.push(part + ' ' + parts[++i]);
        continue;
      }
      keep.push(part);
    }
    return keep.join(', ') || 'that address';
  }

  function geocodeRemote(q) {
    var cache;
    try { cache = JSON.parse(localStorage.getItem('cinemelb-geocache') || '{}'); }
    catch (e) { cache = {}; }
    if (cache[q]) return Promise.resolve(cache[q]);

    var url = 'https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1'
            + '&countrycodes=au&q=' + encodeURIComponent(q);
    return fetch(url, { headers: { Accept: 'application/json' } })
      .then(function (resp) {
        if (!resp.ok) throw new Error('lookup returned HTTP ' + resp.status);
        return resp.json();
      })
      .then(function (list) {
        if (!list || !list.length) throw new Error('no match for that address');
        var hit = {
          name: shortLabel(list[0].display_name),
          lat: parseFloat(list[0].lat),
          lon: parseFloat(list[0].lon),
          source: 'nominatim'
        };
        try {
          if (Object.keys(cache).length > 40) cache = {};
          cache[q] = hit;
          localStorage.setItem('cinemelb-geocache', JSON.stringify(cache));
        } catch (e) { /* private mode */ }
        return hit;
      });
  }

  function nearestCinemaOverall() {
    var best = null;
    for (var i = 0; i < S.cinemas.length; i++) {
      var km = kmTo(i);
      if (km != null && (best === null || km < best.km)) {
        best = { km: km, name: S.cinemas[i][0] };
      }
    }
    return best;
  }

  function setNearStatus(html, cls) {
    var el = document.getElementById('near-status');
    if (!el) return;
    el.className = 'near-status' + (cls ? ' ' + cls : '');
    el.innerHTML = html;
  }

  function renderNear() {
    var btn = document.getElementById('near-me');
    if (!btn) return;
    if (!S.place) {
      btn.classList.remove('is-on');
      btn.setAttribute('aria-pressed', 'false');
      setNearStatus('');
      return;
    }
    btn.classList.add('is-on');
    btn.setAttribute('aria-pressed', 'true');
    var near = S.bundle ? nearestCinemaOverall() : null;
    var bits = ['Near <strong>' + esc(S.place.name) + '</strong>'];
    if (near) {
      bits.push('nearest cinema ' + fmtKm(near.km) + ' (' + fmtDrive(near.km) + ')');
    }
    setNearStatus(bits.join(' · ') +
      ' <button type="button" class="near-clear" data-near-clear>Clear</button>', 'is-on');
  }

  function setPlace(place, persist) {
    S.place = place;
    try {
      // Typed places are worth remembering; precise GPS coordinates are not
      // written to disk at all.
      if (persist) localStorage.setItem('cinemelb-place', JSON.stringify(place));
      else localStorage.removeItem('cinemelb-place');
    } catch (e) { /* private mode */ }
    var input = document.getElementById('near-q');
    if (input && place.source !== 'geolocation') input.value = place.name;
    renderNear();
    render();
  }

  function clearPlace() {
    S.place = null;
    // The drive-time filter is meaningless without a place, and leaving it set
    // would keep hiding sessions behind a control that is no longer visible.
    S.filters.within = 0;
    try { localStorage.removeItem('cinemelb-place'); } catch (e) { /* private mode */ }
    var input = document.getElementById('near-q');
    if (input) input.value = '';
    renderNear();
    render();
  }

  /* iOS Safari's failure modes are worth naming: a permission that was denied
     once never prompts again, an in-app browser (Messages, Instagram) blocks
     location outright, and a page not on HTTPS gets nothing at all. */
  function geoHint() {
    var ua = navigator.userAgent || '';
    if (/FBAN|FBAV|Instagram|Line\/|Twitter|Snapchat|Pinterest|WhatsApp/i.test(ua)) {
      return ' This looks like an in-app browser, which usually blocks location — open the site in Safari or Chrome.';
    }
    if (/iPhone|iPad|iPod/i.test(ua)) {
      return ' On iPhone: Settings → Privacy & Security → Location Services must be on, and allow location for this site (tap aA → Website Settings).';
    }
    if (/Android/i.test(ua)) {
      return ' On Android, allow location for this site from the padlock menu in the address bar.';
    }
    return ' Allow location for this site in your browser settings.';
  }

  function requestLocation() {
    var btn = document.getElementById('near-me');
    var input = document.getElementById('near-q');

    function offerAddress(message) {
      setNearStatus(esc(message) + ' You can type a suburb or address instead.', 'is-error');
      if (input) input.focus();
    }

    if (!window.isSecureContext) {
      offerAddress('The browser only shares your location over HTTPS.');
      return;
    }
    if (!navigator.geolocation) {
      offerAddress('This browser has no location support.');
      return;
    }

    // A prompt the user never answers can leave the callback pending forever
    // (common in in-app browsers), which looks exactly like a dead button.
    var settled = false;
    var timer = setTimeout(function () {
      if (settled) return;
      settled = true;
      if (btn) btn.disabled = false;
      offerAddress('Still waiting for the browser to answer the location request.' + geoHint());
    }, 15000);

    function finish() {
      settled = true;
      clearTimeout(timer);
      if (btn) btn.disabled = false;
    }

    if (btn) btn.disabled = true;
    setNearStatus('Asking your browser for your location…', 'is-on');

    navigator.geolocation.getCurrentPosition(function (pos) {
      finish();
      setPlace({
        name: 'your location',
        lat: pos.coords.latitude,
        lon: pos.coords.longitude,
        source: 'geolocation'
      }, false);
    }, function (err) {
      finish();
      var code = err && err.code;
      if (code === 1) {
        offerAddress('Location permission was denied.' + geoHint());
      } else if (code === 2) {
        offerAddress('The browser could not work out where you are right now.');
      } else if (code === 3) {
        offerAddress('The location request timed out.');
      } else {
        offerAddress('Could not get your location' +
          (err && err.message ? ' (' + err.message + ')' : '') + '.');
      }
    }, { enableHighAccuracy: false, timeout: 12000, maximumAge: 120000 });
  }

  function submitNear(event) {
    if (event) event.preventDefault();
    var input = document.getElementById('near-q');
    var q = input ? input.value.trim() : '';
    if (!q) return;

    var coords = parseLatLon(q);
    if (coords) { setPlace(coords, true); return; }

    var hit = bundlePlace(q);
    if (hit) { setPlace(hit, true); return; }

    if (input) input.disabled = true;
    setNearStatus('Looking up “' + esc(q) + '”…', 'is-on');
    geocodeRemote(q).then(function (place) {
      if (input) input.disabled = false;
      setPlace(place, true);
    }).catch(function (err) {
      if (input) input.disabled = false;
      setNearStatus('Could not find “' + esc(q) + '”. ' + esc(err.message || '') +
        ' Try a suburb name, or “street, suburb”.', 'is-error');
    });
  }

  // ------------------------------------------------------------- utilities
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function hasAttrs(row, wanted) {
    var set = S.bundle.attrs[S.rowAttr[row]];
    if (!set) return false;
    for (var i = 0; i < wanted.length; i++) {
      if (set.indexOf(wanted[i]) >= 0) return true;
    }
    return false;
  }

  /* Minutes from now on one absolute timeline: listing-day index * 1440 plus
     the (possibly shifted) minute. Negative means the session has started. */
  function minutesUntil(row) {
    return (S.rowDate[row] * 1440 + S.rowMinute[row]) -
           (S.today * 1440 + S.nowMinute);
  }

  /* A session is past when its start time has gone, on the day it belongs to.
     Days before today are entirely past even though the bundle still carries
     them, which is why this compares the date index and not just the clock. */
  function isPast(row) {
    return minutesUntil(row) < 0;
  }

  function isTooSoon(row) {
    var d = minutesUntil(row);
    return d >= 0 && d < LEAD_MINUTES;
  }

  /* "Leaves time to get there" is travel-aware once a place is set: a session
     five minutes away across town is not catchable, while one five minutes
     away next door is. The fixed LEAD_MINUTES stays the floor. */
  function leavesTooLittleTime(row) {
    var lead = LEAD_MINUTES;
    if (S.place) {
      var km = kmTo(S.rowCinema[row]);
      if (km != null) lead = Math.max(lead, driveMinutes(km) + 5);
    }
    var d = minutesUntil(row);
    return d >= 0 && d < lead;
  }

  function rowPasses(row) {
    if (S.filters.accessible && !hasAttrs(row, ['CC', 'AD', 'Open Captions'])) return false;
    // Cinema rows are arrays: [name, chain, suburb, ...]. Reaching for .chain
    // here silently compared against undefined and hid every row.
    if (S.filters.chain && S.cinemas[S.rowCinema[row]][1] !== S.filters.chain) return false;
    if (S.filters.later && leavesTooLittleTime(row)) return false;
    if (S.filters.after && S.rowMinute[row] < S.filters.after) return false;
    if (S.filters.within) {
      var km = kmTo(S.rowCinema[row]);
      if (km == null || driveMinutes(km) > S.filters.within) return false;
    }
    return true;
  }

  // --------------------------------------------------------------- indexing
  function loadBundle() {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', './data/bundle.json', true);
    xhr.onload = function () {
      // Pages serves the SPA shell for a missing file with a 200. Parsing that
      // as JSON would fail confusingly, so check the type before the parse.
      var type = xhr.getResponseHeader('Content-Type') || '';
      if (xhr.status !== 200) {
        return fail('The server answered HTTP ' + xhr.status + ' for the data file.');
      }
      if (type.indexOf('json') < 0) {
        return fail('The data file came back as ' + type + ' instead of JSON. ' +
                    'The deploy is missing data/bundle.json.');
      }
      var bundle;
      try { bundle = JSON.parse(xhr.responseText); }
      catch (e) { return fail('The data file is not valid JSON: ' + e.message); }
      index(bundle);
      render();
    };
    xhr.onerror = function () { fail('Could not reach the data file.'); };
    xhr.send();
  }

  function index(bundle) {
    S.bundle = bundle;
    S.cinemas = bundle.cinemas;
    S.films = bundle.films;

    // A session after midnight belongs to the previous listing day: the 00:31
    // on the 27th is the 26th's late-night session. Shift those rows onto the
    // previous date and add a day's minutes, so the day slice, the film page
    // and the time buckets all agree while the clock still reads "12:31am".
    // Rows on the first exported day are dropped — their listing day is not in
    // the build.
    var kept = [];
    var raw = bundle.screenings;
    for (var i = 0; i < raw.length; i++) {
      var r = raw[i];
      var dateIdx = r[0];
      var minute = r[2];
      if (minute < LATE_CUTOFF) {
        if (dateIdx === 0) continue;
        dateIdx -= 1;
        minute += 24 * 60;
      }
      kept.push([dateIdx, r[1], minute, r[3], r[4], r[5], r[6], r[7], r[8]]);
    }

    var n = kept.length;
    S.rowFilm = new Int32Array(n);
    S.rowCinema = new Int32Array(n);
    S.rowMinute = new Int32Array(n);
    S.rowDate = new Int32Array(n);
    S.rowFormat = new Int32Array(n);
    S.rowAttr = new Int32Array(n);
    S.rowUrl = new Int32Array(n);
    S.rowScreen = new Int32Array(n);
    S.rowSold = new Uint8Array(n);

    var byDate = new Map();
    var byFilm = new Map();
    for (var i2 = 0; i2 < n; i2++) {
      var row = kept[i2];
      S.rowDate[i2] = row[0];
      S.rowFilm[i2] = row[1];
      S.rowMinute[i2] = row[2];
      S.rowCinema[i2] = row[3];
      S.rowFormat[i2] = row[4];
      S.rowAttr[i2] = row[5];
      S.rowSold[i2] = row[6];
      S.rowUrl[i2] = row[7];
      S.rowScreen[i2] = row[8];

      var d = byDate.get(row[0]);
      if (!d) byDate.set(row[0], d = []);
      d.push(i2);

      var f = byFilm.get(row[1]);
      if (!f) byFilm.set(row[1], f = []);
      f.push(i2);
    }
    S.byDate = byDate;
    S.byFilm = byFilm;

    // Only offer chains that appear in the exported window.
    var seen = Object.create(null);
    S.chains = [];
    for (var c = 0; c < S.cinemas.length; c++) {
      var chain = S.cinemas[c][1];
      if (chain && !seen[chain]) { seen[chain] = 1; S.chains.push(chain); }
    }
    S.chains.sort();

    var now = melbourneNow();
    S.nowMinute = now.minute;
    var todayIdx = bundle.dates.indexOf(now.date);
    if (todayIdx < 0) {
      // The export can start tomorrow (or, with a stale scrape, before today).
      // Open on the first day that is not already behind us.
      todayIdx = bundle.dates.length - 1;
      for (var d0 = 0; d0 < bundle.dates.length; d0++) {
        if (bundle.dates[d0] >= now.date) { todayIdx = d0; break; }
      }
    }
    S.today = Math.max(0, todayIdx);
    renderFoot();
    renderNear();
  }

  function renderFoot() {
    var el = document.getElementById('foot-meta');
    if (!el || !S.bundle) return;
    var n = S.bundle.counts;
    var when = '';
    try {
      when = new Intl.DateTimeFormat('en-AU', {
        timeZone: TZ, day: 'numeric', month: 'short',
        hour: 'numeric', minute: '2-digit'
      }).format(new Date(S.bundle.generated));
    } catch (e) { when = ''; }
    var dot = ' <span class="dot">·</span> ';
    var last = S.bundle.dates[S.bundle.dates.length - 1];
    el.innerHTML =
      n.screenings.toLocaleString('en-AU') + ' sessions' + dot +
      n.films + ' films' + dot +
      n.cinemas + ' cinemas' + dot +
      'through ' + esc(coverDay(last)) +
      (when ? dot + 'built ' + esc(when) : '');
  }

  // -------------------------------------------------------------- rendering
  function render() {
    var main = document.getElementById('main');
    if (!S.bundle) return;
    // Route first: the rail highlights whichever day the route names, so
    // reading S.route before it is refreshed left the highlight one click behind.
    var route = parseRoute();
    S.route = route;
    renderRail();
    renderFilters();
    if (route.name === 'film') renderFilm(main, route.film);
    else renderDay(main, route.date);
  }

  function parseRoute() {
    var h = location.hash.replace(/^#/, '');
    if (h.indexOf('/film/') === 0) {
      return { name: 'film', film: decodeURIComponent(h.slice(6)), date: null };
    }
    if (h.indexOf('/day/') === 0) {
      return { name: 'day', date: h.slice(5), film: null };
    }
    return { name: 'day', date: null, film: null };
  }

  function renderRail() {
    var rail = document.getElementById('rail');
    var dates = S.bundle.dates;
    var start = Math.max(0, S.today);
    var end = Math.min(dates.length, start + RAIL_DAYS);
    var current = S.route.name === 'day' ? S.route.date : null;
    var html = '';

    for (var i = start; i < end; i++) {
      var iso = dates[i];
      var p = dayParts(iso);
      var rel = relative(iso);
      var active = (iso === current) || (current === null && i === S.today);
      html += '<a class="rail-day' + (active ? ' is-active' : '') +
              '" href="#/day/' + iso + '"' +
              (active ? ' aria-current="date"' : '') + '>' +
              '<span class="rail-dow">' + (rel || p.dow) + '</span>' +
              '<span class="rail-num">' + p.day + '</span>' +
              '<span class="rail-mon">' + p.mon + '</span>' +
              '</a>';
    }
    rail.innerHTML = html;
  }

  function renderFilters() {
    var box = document.getElementById('filters');
    var html = '<button type="button" class="chip' +
      (S.filters.accessible ? ' is-on' : '') +
      '" data-filter="accessible" aria-pressed="' + S.filters.accessible + '">' +
      'Accessible screenings</button>';

    html += '<button type="button" class="chip' +
      (S.filters.later ? ' is-on' : '') +
      '" data-filter="later" aria-pressed="' + S.filters.later + '">' +
      'Leaves time to get there</button>';

    if (S.chains.length > 1) {
      html += '<select id="chain" class="select" aria-label="Filter by cinema chain">' +
        '<option value="">All cinemas</option>';
      for (var i = 0; i < S.chains.length; i++) {
        var c = S.chains[i];
        html += '<option value="' + esc(c) + '"' +
          (c === S.filters.chain ? ' selected' : '') + '>' + esc(c) + '</option>';
      }
      html += '</select>';
    }

    html += '<select id="after" class="select" aria-label="Show sessions starting after">';
    for (var a = 0; a < AFTER_PRESETS.length; a++) {
      var ap = AFTER_PRESETS[a];
      html += '<option value="' + ap[0] + '"' +
        (ap[0] === S.filters.after ? ' selected' : '') + '>' + esc(ap[1]) + '</option>';
    }
    html += '</select>';

    // Distance only makes sense once there is a point to measure from.
    if (S.place) {
      html += '<select id="within" class="select" aria-label="Show sessions within a drive time">';
      for (var w = 0; w < WITHIN_PRESETS.length; w++) {
        var wp = WITHIN_PRESETS[w];
        html += '<option value="' + wp[0] + '"' +
          (wp[0] === S.filters.within ? ' selected' : '') + '>' + esc(wp[1]) + '</option>';
      }
      html += '</select>';
    }

    if (filtersActive()) {
      html += '<button type="button" class="chip chip-clear" data-clear>Clear</button>';
    }
    box.innerHTML = html;
  }

  function filtersActive() {
    return !!(S.filters.accessible || S.filters.chain || S.filters.later ||
      S.filters.after || S.filters.within);
  }

  /* The "group by" control. It is rendered into the results, not the filter
     bar, because it changes the shape of the list rather than its contents. */
  function viewBar(kind) {
    var options = kind === 'film'
      ? [['day', 'By day'], ['cinema', 'By cinema'], ['time', 'By time']]
      : [['film', 'By film'], ['cinema', 'By cinema'], ['time', 'By time']];
    var current = kind === 'film' ? S.filmGroup : S.group;
    var html = '<div class="view-bar" role="group" aria-label="Group sessions">';
    for (var i = 0; i < options.length; i++) {
      var on = options[i][0] === current;
      html += '<button type="button" class="seg-btn' + (on ? ' is-on' : '') +
        '" data-view="' + options[i][0] + '" data-view-kind="' + kind +
        '" aria-pressed="' + on + '">' + esc(options[i][1]) + '</button>';
    }
    return html + '</div>';
  }

  function renderDay(main, dayIso) {
    var dates = S.bundle.dates;
    if (!dayIso) dayIso = dates[S.today];
    var dateIdx = dates.indexOf(dayIso);
    if (dateIdx < 0) {
      main.innerHTML = '<div class="empty"><h2>No screenings on that day</h2>' +
        '<p>This build covers ' + esc(shortDay(dates[0])) + ' to ' +
        esc(shortDay(dates[dates.length - 1])) + '.</p></div>';
      return;
    }

    var all = S.byDate.get(dateIdx) || [];
    var rows = [];
    var films = new Set();
    for (var i = 0; i < all.length; i++) {
      var row = all[i];
      if (!rowPasses(row)) continue;
      if (isPast(row)) continue;
      rows.push(row);
      films.add(S.rowFilm[row]);
    }

    var rel = relative(dayIso);
    var head = '<div class="day-head">' +
      '<h1>' + esc(longDay(dayIso)) + '</h1>' +
      (rel ? '<span class="badge">' + esc(rel) + '</span>' : '') +
      '<span class="count">' + films.size + ' film' + (films.size === 1 ? '' : 's') +
      '<span class="count-sessions"> · ' + rows.length + ' session' +
      (rows.length === 1 ? '' : 's') + '</span></span>' + viewBar('day') + '</div>';

    if (!rows.length) {
      // "Nothing left" and "nothing matched your filters" are different facts,
      // and telling a user their day is over when a chip hid everything is a lie.
      var upcoming = 0;
      for (var u = 0; u < all.length; u++) if (!isPast(all[u])) upcoming++;
      var body;
      if (!all.length) {
        body = '<h2>No screenings on that day</h2>' +
          '<p>This build covers ' + esc(shortDay(dates[0])) + ' to ' +
          esc(shortDay(dates[dates.length - 1])) + '.</p>';
      } else if (!upcoming) {
        body = '<h2>Nothing left today</h2>' +
          '<p>Every remaining session has already started. Try tomorrow.</p>';
      } else {
        body = '<h2>No sessions match those filters</h2>' +
          '<p>' + upcoming + ' session' + (upcoming === 1 ? '' : 's') + ' on ' +
          esc(shortDay(dayIso)) + ' hidden by the current filters. ' +
          '<button type="button" class="btn" data-clear>Clear filters</button></p>';
      }
      main.innerHTML = head + '<div class="empty">' + body + '</div>';
      document.title = shortDay(dayIso) + ' — cinemelb';
      return;
    }

    var html = head;
    if (S.group === 'cinema') html += dayByCinema(rows);
    else if (S.group === 'time') html += dayByTime(rows);
    else html += dayByFilm(rows);
    main.innerHTML = html;
    document.title = shortDay(dayIso) + ' — cinemelb';
  }

  function groupByFilm(rows) {
    var groups = new Map();
    for (var i = 0; i < rows.length; i++) {
      var f = S.rowFilm[rows[i]];
      var g = groups.get(f);
      if (!g) groups.set(f, g = []);
      g.push(rows[i]);
    }
    return groups;
  }

  /* Order by distance when a location is set, otherwise by the next session
     still to come, so the page reads as a schedule. */
  function orderFilms(groups) {
    var order = Array.from(groups.keys());
    if (S.place) {
      order.sort(function (a, b) {
        var da = nearestKmOf(groups.get(a));
        var db = nearestKmOf(groups.get(b));
        if (da == null) da = Infinity;
        if (db == null) db = Infinity;
        return da - db || nextTime(groups.get(a)) - nextTime(groups.get(b));
      });
    } else {
      order.sort(function (a, b) {
        return nextTime(groups.get(a)) - nextTime(groups.get(b));
      });
    }
    return order;
  }

  function dayByFilm(rows) {
    var groups = groupByFilm(rows);
    var order = orderFilms(groups);
    var html = '<div class="films">';
    for (var k = 0; k < order.length; k++) {
      html += filmCard(order[k], groups.get(order[k]));
    }
    return html + '</div>';
  }

  /* A day holds ~1,900 sessions, so "by cinema" collapses each venue into one
     expandable card rather than listing every session. */
  function dayByCinema(rows) {
    var byCinema = new Map();
    for (var i = 0; i < rows.length; i++) {
      var c = S.rowCinema[rows[i]];
      var g = byCinema.get(c);
      if (!g) byCinema.set(c, g = []);
      g.push(rows[i]);
    }
    var order = Array.from(byCinema.keys());
    order.sort(function (a, b) {
      if (S.place) {
        var da = kmTo(a), db = kmTo(b);
        if (da != null || db != null) {
          return (da == null ? Infinity : da) - (db == null ? Infinity : db);
        }
      }
      return S.cinemas[a][0].localeCompare(S.cinemas[b][0]);
    });

    var html = '<div class="films">';
    for (var k = 0; k < order.length; k++) {
      html += cinemaCard(order[k], byCinema.get(order[k]));
    }
    return html + '</div>';
  }

  function cinemaCard(cinemaIdx, rows) {
    var cinema = S.cinemas[cinemaIdx];
    var byFilm = groupByFilm(rows);
    var order = orderFilms(byFilm);
    var first = 24 * 60;
    for (var i = 0; i < rows.length; i++) first = Math.min(first, S.rowMinute[rows[i]]);

    var nearKm = S.place ? kmTo(cinemaIdx) : null;
    var meta = byFilm.size + ' film' + (byFilm.size === 1 ? '' : 's') +
      ' · ' + rows.length + ' session' + (rows.length === 1 ? '' : 's');
    if (nearKm != null) {
      meta = fmtKm(nearKm) + ' · ~' + driveMinutes(nearKm) + ' min · ' + meta;
    }

    var body = venueMeta(cinemaIdx);
    for (var k = 0; k < order.length; k++) {
      var film = S.films[order[k]];
      var times = byFilm.get(order[k]).slice().sort(function (a, b) {
        return S.rowMinute[a] - S.rowMinute[b];
      });
      body += '<div class="venue">' +
        '<div class="venue-head"><span class="venue-name">' + esc(film[1]) + '</span>' +
        (film[3] ? '<span class="venue-sub">' + esc(film[3]) + '</span>' : '') +
        (film[4] ? '<span class="venue-sub">' + film[4] + ' min</span>' : '') +
        '</div><div class="sessions">' + sessions(times) + '</div></div>';
    }

    return '<details class="film cinema-card">' +
      '<summary><div class="film-main">' +
        '<h2 class="film-title">' + esc(cinema[0]) + '</h2>' +
        '<p class="film-meta">' + esc(meta) + '</p>' +
      '</div>' +
      '<div class="film-times"><span class="time">from ' + clock(first) + '</span></div>' +
      '</summary>' +
      '<div class="film-body">' + body + '</div></details>';
  }

  /* "By time" pivots the day: the same film cards, but split into the part of
     the day their sessions fall in. A film playing at 1pm and 7pm appears in
     both the afternoon and the evening, which is the point of the pivot. */
  function dayByTime(rows) {
    var html = '';
    for (var b = 0; b < TIME_BUCKETS.length; b++) {
      var bucket = TIME_BUCKETS[b];
      var inBucket = [];
      for (var i = 0; i < rows.length; i++) {
        var m = S.rowMinute[rows[i]];
        if (m >= bucket.from && m < bucket.to) inBucket.push(rows[i]);
      }
      if (!inBucket.length) continue;
      var groups = groupByFilm(inBucket);
      var order = orderFilms(groups);
      html += '<details class="time-block" open><summary class="time-head">' +
        esc(bucket.name) +
        '<span class="count">' + inBucket.length + ' session' +
        (inBucket.length === 1 ? '' : 's') + ' · ' + groups.size + ' film' +
        (groups.size === 1 ? '' : 's') + '</span></summary><div class="films">';
      for (var k = 0; k < order.length; k++) {
        html += filmCard(order[k], groups.get(order[k]));
      }
      html += '</div></details>';
    }
    return html;
  }

  function nextTime(rows) {
    var best = Infinity;
    for (var i = 0; i < rows.length; i++) {
      var m = S.rowMinute[rows[i]];
      if (m < best && !isPast(rows[i])) best = m;
    }
    return best === Infinity ? 30 * 60 : best;
  }

  function filmCard(filmIdx, rows) {
    var film = S.films[filmIdx];
    var sorted = rows.slice().sort(function (a, b) {
      return S.rowMinute[a] - S.rowMinute[b];
    });

    var venues = Object.create(null);
    var venueCount = 0;
    for (var i = 0; i < sorted.length; i++) {
      var c = S.rowCinema[sorted[i]];
      if (!venues[c]) { venues[c] = 1; venueCount++; }
    }

    var times = '';
    // One pill per distinct start time: "10am 10am 10am" wastes the row, and
    // the venue count in the meta line already says how many cinemas run it.
    var distinct = [];
    var lastMinute = -1;
    for (var d = 0; d < sorted.length; d++) {
      var dm = S.rowMinute[sorted[d]];
      if (dm !== lastMinute) { distinct.push(sorted[d]); lastMinute = dm; }
    }
    var shown = 0;
    for (var j = 0; j < distinct.length && shown < 5; j++) {
      var r = distinct[j];
      var soon = isTooSoon(r);
      times += '<span class="time' + (soon ? ' is-soon' : '') + '">' +
        clock(S.rowMinute[r]) + '</span>';
      shown++;
    }
    if (distinct.length > shown) {
      times += '<span class="more">+' + (distinct.length - shown) + '</span>';
    }

    var meta = venueCount + (venueCount === 1 ? ' cinema' : ' cinemas');
    if (film[3]) meta += ' · ' + film[3];
    if (film[4]) meta += ' · ' + film[4] + ' min';
    if (S.place) {
      var nearKm = nearestKmOf(sorted);
      if (nearKm != null) {
        // Distance leads the meta line: it is the sort key here, and on a
        // narrow screen the tail of this line is what gets clipped.
        meta = fmtKm(nearKm) + ' · ~' + driveMinutes(nearKm) + ' min · ' + meta;
      }
    }

    var poster = film[6]
      ? '<img class="poster" src="' + esc(film[6]) + '" alt="" loading="lazy" ' +
        'referrerpolicy="no-referrer" onerror="this.remove()">'
      : '<span class="poster poster-none" aria-hidden="true">' +
        esc(film[1].charAt(0).toUpperCase()) + '</span>';

    var filmHref = '#/film/' + encodeURIComponent(film[0]);
    return '<details class="film" data-film="' + filmIdx + '">' +
      '<summary>' + poster +
      '<div class="film-main">' +
        '<h2 class="film-title">' + esc(film[1]) + '</h2>' +
        '<p class="film-meta">' + esc(meta) + '</p>' +
      '</div>' +
      '<div class="film-times">' + times + '</div>' +
      // The film page was only reachable by searching. A card-level arrow is the
      // obvious affordance; stopPropagation keeps the click from toggling the
      // <details> as well.
      '<a class="film-open" href="' + filmHref + '"' +
        ' aria-label="Open the film page for ' + esc(film[1]) + '"' +
        ' onclick="event.stopPropagation()">' +
        '<svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">' +
        '<path d="M7.5 4.5 13 10l-5.5 5.5" fill="none" stroke="currentColor"' +
        ' stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></a>' +
      '</summary>' +
      '<div class="film-body">' +
        scoreChips(film) +
        (film[7] ? '<p class="synopsis">' + esc(film[7]) + '</p>' : '') +
        venueBlocks(sorted) +
        '<p class="film-link"><a href="' + filmHref +
        '">All times for this film →</a></p>' +
      '</div></details>';
  }

  /* Ratings, when the build had them. Film row: [.., imdb_id, imdb, rt, mc,
     rt_url, mc_url]. No score means no chip — never a dash or a zero. */
  function scoreChips(film) {
    var chips = '';
    if (film[10] != null) {
      chips += '<a class="score score-imdb" href="https://www.imdb.com/title/' +
        encodeURIComponent(film[9]) + '/" target="_blank" rel="noopener noreferrer"' +
        ' aria-label="IMDb rating ' + film[10] + ' out of 10 (opens IMDb)">' +
        '<span class="score-src">IMDb</span><span class="score-val">' +
        film[10].toFixed(1) + '</span></a>';
    }
    if (film[11] != null) {
      var rtHref = film[13] || ('https://www.rottentomatoes.com/search?search=' +
        encodeURIComponent(film[1]));
      chips += '<a class="score score-rt" href="' + esc(rtHref) +
        '" target="_blank" rel="noopener noreferrer"' +
        ' aria-label="Rotten Tomatoes ' + film[11] + ' percent (opens Rotten Tomatoes)">' +
        '<span class="score-src">Tomatometer</span><span class="score-val">' +
        film[11] + '%</span></a>';
    }
    if (film[12] != null) {
      var mcHref = film[14] || ('https://www.metacritic.com/search/' +
        encodeURIComponent(film[1]) + '/');
      chips += '<a class="score score-mc" href="' + esc(mcHref) +
        '" target="_blank" rel="noopener noreferrer"' +
        ' aria-label="Metascore ' + film[12] + ' out of 100 (opens Metacritic)">' +
        '<span class="score-src">Metascore</span><span class="score-val">' +
        film[12] + '</span></a>';
    }
    return chips ? '<div class="scores">' + chips + '</div>' : '';
  }

  /* Address plus the links a venue card needs. Cinema row: [name, chain,
     suburb, address, website, tags, lat, lon, phone, booking_url]. */
  function venueMeta(cinemaIdx) {
    var c = S.cinemas[cinemaIdx];
    if (!c) return '';
    var name = c[0], address = c[3] || '', website = c[4] || '';
    var lat = c[6], lon = c[7], phone = c[8] || '', booking = c[9] || '';
    var maps = '';
    if (lat != null && lon != null) {
      maps = 'https://www.google.com/maps/search/?api=1&query=' + lat + '%2C' + lon;
    } else if (address) {
      maps = 'https://www.google.com/maps/search/?api=1&query=' +
        encodeURIComponent(name + ', ' + address);
    }
    var links = '';
    if (maps) {
      links += '<a class="venue-link" href="' + esc(maps) +
        '" target="_blank" rel="noopener noreferrer">Directions</a>';
    }
    if (booking) {
      links += '<a class="venue-link" href="' + esc(booking) +
        '" target="_blank" rel="noopener noreferrer">Tickets</a>';
    }
    if (website) {
      links += '<a class="venue-link" href="' + esc(website) +
        '" target="_blank" rel="noopener noreferrer">Website</a>';
    }
    if (phone) {
      links += '<a class="venue-link" href="tel:' + esc(phone.replace(/[^\d+]/g, '')) +
        '">' + esc(prettyPhone(phone)) + '</a>';
    }
    if (!address && !links) return '';
    return '<div class="venue-meta">' +
      (address ? '<span class="venue-addr">' + esc(address) + '</span>' : '') +
      (links ? '<span class="venue-links">' + links + '</span>' : '') +
      '</div>';
  }

  function prettyPhone(p) {
    var d = String(p || '').replace(/[^\d+]/g, '');
    if (/^0\d{9}$/.test(d)) return d.slice(0, 2) + ' ' + d.slice(2, 6) + ' ' + d.slice(6);
    if (/^1300\d{6}$/.test(d)) return d.slice(0, 4) + ' ' + d.slice(4, 7) + ' ' + d.slice(7);
    if (/^13\d{4}$/.test(d)) return d.slice(0, 2) + ' ' + d.slice(2, 4) + ' ' + d.slice(4);
    return p;
  }

  function venueBlocks(rows) {
    var byVenue = new Map();
    for (var i = 0; i < rows.length; i++) {
      var c = S.rowCinema[rows[i]];
      var g = byVenue.get(c);
      if (!g) byVenue.set(c, g = []);
      g.push(rows[i]);
    }
    var order = Array.from(byVenue.keys());
    order.sort(function (a, b) {
      if (S.place) {
        var da = kmTo(a), db = kmTo(b);
        if (da != null || db != null) {
          return (da == null ? Infinity : da) - (db == null ? Infinity : db);
        }
      }
      return S.cinemas[a][0].localeCompare(S.cinemas[b][0]);
    });

    var html = '';
    for (var k = 0; k < order.length; k++) {
      var cinema = S.cinemas[order[k]];
      var times = byVenue.get(order[k]).slice().sort(function (a, b) {
        return S.rowMinute[a] - S.rowMinute[b];
      });
      var nearKm = S.place ? kmTo(order[k]) : null;
      html += '<div class="venue">' +
        '<div class="venue-head"><span class="venue-name">' + esc(cinema[0]) + '</span>' +
        (cinema[2] ? '<span class="venue-sub">' + esc(cinema[2]) + '</span>' : '') +
        (nearKm != null
          ? '<span class="venue-dist">' + fmtKm(nearKm) + ' · ' + fmtDrive(nearKm) + '</span>'
          : '') +
        '</div>' + venueMeta(order[k]) +
        '<div class="sessions">' + sessions(times) + '</div></div>';
    }
    return html;
  }

  /* Format, accessibility badges and screen name, deduplicated: sources repeat
     themselves ("IMAX 3D" as format and "3D" as an attribute). */
  function sessionNotes(r) {
    var notes = [];
    var seen = Object.create(null);
    function add(text) {
      if (!text) return;
      var key = text.toLowerCase();
      if (seen[key]) return;
      seen[key] = 1;
      notes.push(text);
    }
    add(S.bundle.formats[S.rowFormat[r]]);
    var attrs = S.bundle.attrs[S.rowAttr[r]];
    if (attrs) {
      for (var a = 0; a < attrs.length; a++) {
        if (attrs[a] !== 'NFT') add(attrs[a]);
      }
    }
    add(S.bundle.screens[S.rowScreen[r]]);
    return notes;
  }

  function sessions(rows) {
    var html = '';
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var minute = S.rowMinute[r];
      var sold = S.rowSold[r] === 1;
      var soon = isTooSoon(r);

      var notes = sessionNotes(r);

      var label = '<span class="time">' + clock(minute) + '</span>' +
        (notes.length ? '<span class="notes">' + esc(notes.join(' · ')) + '</span>' : '');
      var cls = 'session' + (sold ? ' is-sold' : '') + (soon ? ' is-soon' : '');

      var url = S.rowUrl[r] >= 0 ? S.bundle.urls[S.rowUrl[r]] : '';
      if (url && !sold) {
        html += '<a class="' + cls + '" href="' + esc(url) +
          '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
      } else {
        html += '<span class="' + cls + '">' + label +
          (sold ? '<span class="sold-tag">Sold out</span>' : '') + '</span>';
      }
    }
    return html;
  }

  function renderFilm(main, key) {
    var filmIdx = -1;
    for (var i = 0; i < S.films.length; i++) {
      if (S.films[i][0] === key) { filmIdx = i; break; }
    }
    if (filmIdx < 0) {
      main.innerHTML = '<div class="empty"><h2>Film not found</h2>' +
        '<p>That film is not in this build. <a href="#/">Back to today</a>.</p></div>';
      return;
    }

    var film = S.films[filmIdx];
    var rows = (S.byFilm.get(filmIdx) || []).filter(function (r) {
      return rowPasses(r) && !isPast(r);
    });

    var byDate = new Map();
    for (var j = 0; j < rows.length; j++) {
      var d = S.rowDate[rows[j]];
      var g = byDate.get(d);
      if (!g) byDate.set(d, g = []);
      g.push(rows[j]);
    }
    var dayOrder = Array.from(byDate.keys()).sort(function (a, b) { return a - b; });

    var facts = [];
    if (S.place) {
      var nearKm = nearestKmOf(rows);
      if (nearKm != null) facts.push(fmtKm(nearKm) + ' away · ' + fmtDrive(nearKm));
    }
    if (film[2]) facts.push(film[2]);
    if (film[3]) facts.push(film[3]);
    if (film[4]) facts.push(film[4] + ' min');
    if (film[5] && film[5].length) facts.push(film[5].join(', '));

    var poster = film[6]
      ? '<img class="poster poster-lg" src="' + esc(film[6]) + '" alt="" ' +
        'referrerpolicy="no-referrer" onerror="this.remove()">'
      : '';

    var html = '<div class="film-hero">' + poster +
      '<div class="hero-main"><h1>' + esc(film[1]) + '</h1>' +
      (facts.length ? '<p class="film-meta">' + esc(facts.join(' · ')) + '</p>' : '') +
      scoreChips(film) +
      (film[7] ? '<p class="synopsis">' + esc(film[7]) + '</p>' : '') +
      (film[8] ? '<p><a class="trailer" href="' + esc(film[8]) +
        '" target="_blank" rel="noopener noreferrer">Watch trailer</a></p>' : '') +
      '</div></div>';

    if (!dayOrder.length) {
      var unfiltered = 0;
      var allRows = S.byFilm.get(filmIdx) || [];
      for (var u = 0; u < allRows.length; u++) if (!isPast(allRows[u])) unfiltered++;
      var body;
      if (!unfiltered) {
        body = '<h2>No sessions left</h2><p>This film has no upcoming screenings in this build.</p>';
      } else {
        body = '<h2>No sessions match those filters</h2><p>' + unfiltered +
          ' upcoming session' + (unfiltered === 1 ? '' : 's') +
          ' hidden by the current filters. ' +
          '<button type="button" class="btn" data-clear>Clear filters</button></p>';
      }
      main.innerHTML = html + '<div class="empty">' + body + '</div>';
      document.title = film[1] + ' — cinemelb';
      return;
    }

    html += '<div class="section-head"><h2>All sessions</h2><span class="count">' +
      rows.length + ' across ' + dayOrder.length + ' day' +
      (dayOrder.length === 1 ? '' : 's') + '</span>' + viewBar('film') + '</div>';

    if (S.filmGroup === 'cinema') html += filmByCinema(rows);
    else if (S.filmGroup === 'time') html += filmByTime(rows);
    else html += filmByDay(rows, byDate, dayOrder);
    main.innerHTML = html;
    document.title = film[1] + ' — cinemelb';
  }

  function filmByDay(rows, byDate, dayOrder) {
    var html = '';
    for (var k = 0; k < dayOrder.length; k++) {
      var di = dayOrder[k];
      var iso = S.bundle.dates[di];
      var rel = relative(iso);
      html += '<div class="day-block"><h3>' + esc(longDay(iso)) +
        (rel ? ' <span class="badge">' + esc(rel) + '</span>' : '') +
        '</h3>' + venueBlocks(byDate.get(di)) + '</div>';
    }
    return html;
  }

  /* Cinema first, then the days it runs on: "where can I see this, and when". */
  function filmByCinema(rows) {
    var byCinema = new Map();
    for (var i = 0; i < rows.length; i++) {
      var c = S.rowCinema[rows[i]];
      var g = byCinema.get(c);
      if (!g) byCinema.set(c, g = []);
      g.push(rows[i]);
    }
    var order = Array.from(byCinema.keys());
    order.sort(function (a, b) {
      if (S.place) {
        var da = kmTo(a), db = kmTo(b);
        if (da != null || db != null) {
          return (da == null ? Infinity : da) - (db == null ? Infinity : db);
        }
      }
      return S.cinemas[a][0].localeCompare(S.cinemas[b][0]);
    });

    var html = '';
    for (var k = 0; k < order.length; k++) {
      var cinema = S.cinemas[order[k]];
      var nearKm = S.place ? kmTo(order[k]) : null;
      var byDate = new Map();
      var list = byCinema.get(order[k]);
      for (var j = 0; j < list.length; j++) {
        var d = S.rowDate[list[j]];
        var g = byDate.get(d);
        if (!g) byDate.set(d, g = []);
        g.push(list[j]);
      }
      var days = Array.from(byDate.keys()).sort(function (a, b) { return a - b; });

      html += '<div class="cinema-block"><div class="venue-head">' +
        '<span class="venue-name">' + esc(cinema[0]) + '</span>' +
        (cinema[2] ? '<span class="venue-sub">' + esc(cinema[2]) + '</span>' : '') +
        (nearKm != null ? '<span class="venue-dist">' + fmtKm(nearKm) + ' · ' +
          fmtDrive(nearKm) + '</span>' : '') +
        '</div>';
      for (var dd = 0; dd < days.length; dd++) {
        var iso = S.bundle.dates[days[dd]];
        html += '<div class="day-sub"><h4>' + esc(shortDay(iso)) + '</h4>' +
          '<div class="sessions">' + sessions(byDate.get(days[dd])) + '</div></div>';
      }
      html += '</div>';
    }
    return html;
  }

  /* Time of day first, then the day: "I want this in the evening — which
     nights and which cinemas". */
  function filmByTime(rows) {
    var html = '';
    for (var b = 0; b < TIME_BUCKETS.length; b++) {
      var bucket = TIME_BUCKETS[b];
      var inBucket = [];
      for (var i = 0; i < rows.length; i++) {
        var m = S.rowMinute[rows[i]];
        if (m >= bucket.from && m < bucket.to) inBucket.push(rows[i]);
      }
      if (!inBucket.length) continue;

      var byDate = new Map();
      for (var j = 0; j < inBucket.length; j++) {
        var d = S.rowDate[inBucket[j]];
        var g = byDate.get(d);
        if (!g) byDate.set(d, g = []);
        g.push(inBucket[j]);
      }
      var days = Array.from(byDate.keys()).sort(function (a, b) { return a - b; });

      html += '<details class="time-block" open><summary class="time-head">' +
        esc(bucket.name) +
        '<span class="count">' + inBucket.length + ' session' +
        (inBucket.length === 1 ? '' : 's') + ' · ' + days.length + ' day' +
        (days.length === 1 ? '' : 's') + '</span></summary>';
      for (var dd = 0; dd < days.length; dd++) {
        var iso = S.bundle.dates[days[dd]];
        var rel = relative(iso);
        html += '<div class="day-block"><h3>' + esc(longDay(iso)) +
          (rel ? ' <span class="badge">' + esc(rel) + '</span>' : '') +
          '</h3>' + venueBlocks(byDate.get(days[dd])) + '</div>';
      }
      html += '</details>';
    }
    return html;
  }

  function fail(message) {
    document.getElementById('main').innerHTML =
      '<div class="error"><h2>Could not load the showtimes</h2><p>' + esc(message) +
      '</p><p>If you opened <code>index.html</code> straight from disk, serve the ' +
      'folder over HTTP instead — browsers block <code>file://</code> requests.</p></div>';
  }

  // ---------------------------------------------------------------- events
  // ---------------------------------------------------------------- search
  var sugIndex = -1;

  /* Title search, ranked the way a person types: exact, then a prefix, then a
     word start, then anywhere. Films come back in the bundle's own order for
     equal scores, so the list is stable between keystrokes. */
  function searchMatches(q) {
    var out = [];
    for (var i = 0; i < S.films.length; i++) {
      var title = S.films[i][1];
      var lower = title.toLowerCase();
      var score = -1;
      if (lower === q) score = 100;
      else if (lower.indexOf(q) === 0) score = 80;
      else if (lower.indexOf(' ' + q) > 0) score = 70;
      else if (lower.indexOf(q) > 0) score = 60;
      else if (lower.replace(/[^a-z0-9 ]/g, '').indexOf(q) >= 0) score = 40;
      if (score >= 0) out.push({ i: i, score: score, title: title });
    }
    out.sort(function (a, b) {
      return b.score - a.score || a.title.localeCompare(b.title);
    });
    return out.slice(0, 6);
  }

  function closeSuggest() {
    var box = document.getElementById('suggest');
    var input = document.getElementById('q');
    sugIndex = -1;
    if (box) { box.hidden = true; box.innerHTML = ''; }
    if (input) {
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
    }
  }

  function renderSuggest(q) {
    var box = document.getElementById('suggest');
    var input = document.getElementById('q');
    if (!box || !input) return;
    if (!q) { closeSuggest(); return; }
    var list = searchMatches(q);
    sugIndex = -1;
    input.removeAttribute('aria-activedescendant');
    if (!list.length) {
      box.innerHTML = '<div class="sug-empty">No films match \u201c' + esc(q) + '\u201d</div>';
      box.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      return;
    }
    var html = '';
    for (var k = 0; k < list.length; k++) {
      var f = S.films[list[k].i];
      var meta = [];
      if (f[2]) meta.push(f[2]);
      if (f[3]) meta.push(f[3]);
      if (f[4]) meta.push(f[4] + ' min');
      html += '<button type="button" class="sug" role="option" id="sug-' + k + '"' +
        ' data-key="' + esc(f[0]) + '" aria-selected="false">' +
        (f[6] ? '<span class="sug-thumb"><img src="' + esc(f[6]) +
          '" alt="" loading="lazy" referrerpolicy="no-referrer"' +
          ' onerror="this.parentNode.hidden=true"></span>' : '') +
        '<span class="sug-body">' +
          '<span class="sug-title">' + esc(f[1]) + '</span>' +
          (meta.length ? '<span class="sug-meta">' + esc(meta.join(' \u00b7 ')) + '</span>' : '') +
        '</span></button>';
    }
    box.innerHTML = html;
    box.hidden = false;
    input.setAttribute('aria-expanded', 'true');
  }

  function moveSuggest(delta) {
    var box = document.getElementById('suggest');
    var items = box ? box.querySelectorAll('.sug') : null;
    if (!items || !items.length) return false;
    sugIndex = (sugIndex + delta + items.length) % items.length;
    for (var i = 0; i < items.length; i++) {
      items[i].setAttribute('aria-selected', i === sugIndex ? 'true' : 'false');
    }
    var input = document.getElementById('q');
    if (input) input.setAttribute('aria-activedescendant', 'sug-' + sugIndex);
    if (items[sugIndex].scrollIntoView) {
      items[sugIndex].scrollIntoView({ block: 'nearest' });
    }
    return true;
  }

  function openFilm(key) {
    var input = document.getElementById('q');
    if (input) input.value = '';
    closeSuggest();
    location.hash = '#/film/' + encodeURIComponent(key);
  }

  function submitSearch() {
    var input = document.getElementById('q');
    var q = input ? input.value.trim().toLowerCase() : '';
    if (!q) return;
    var box = document.getElementById('suggest');
    var items = box && sugIndex >= 0 ? box.querySelectorAll('.sug') : null;
    if (items && items[sugIndex]) {
      openFilm(items[sugIndex].getAttribute('data-key'));
      return;
    }
    var list = searchMatches(q);
    if (list.length) openFilm(S.films[list[0].i][0]);
  }

  /* Chips toggle on click; selects only react to change. Handling a select on
     click would re-render the bar while its native menu is still open. */
  function onChip(event) {
    var chip = event.target.closest('[data-filter]');
    if (!chip) return;
    var key = chip.getAttribute('data-filter');
    S.filters[key] = !S.filters[key];
    render();
  }

  function onSelect(event) {
    var id = event.target.id;
    if (id === 'chain') S.filters.chain = event.target.value;
    else if (id === 'after') S.filters.after = parseInt(event.target.value, 10) || 0;
    else if (id === 'within') S.filters.within = parseInt(event.target.value, 10) || 0;
    else return;
    render();
  }

  function onView(event) {
    var btn = event.target.closest('[data-view]');
    if (!btn) return;
    var kind = btn.getAttribute('data-view-kind');
    var value = btn.getAttribute('data-view');
    if (kind === 'film') S.filmGroup = value;
    else S.group = value;
    saveView();
    render();
  }

  function saveView() {
    try {
      localStorage.setItem('cinemelb-view',
        JSON.stringify({ group: S.group, filmGroup: S.filmGroup }));
    } catch (e) { /* private mode */ }
  }

  function onTheme() {
    var root = document.documentElement;
    var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('cinemelb-theme', next); } catch (e) { /* private mode */ }
  }

  function tickClock() {
    var now = melbourneNow();
    S.nowMinute = now.minute;
    var el = document.getElementById('clock');
    if (el) el.textContent = clock(now.minute) + ' Melbourne';
    // boot() starts the clock before the bundle arrives, so there is no
    // calendar to roll yet.
    if (!S.bundle) return;
    var idx = S.bundle.dates.indexOf(now.date);
    if (idx >= 0 && idx !== S.today) { S.today = idx; render(); }
  }

  function boot() {
    try {
      var saved = localStorage.getItem('cinemelb-theme');
      if (saved) document.documentElement.setAttribute('data-theme', saved);
      var savedPlace = localStorage.getItem('cinemelb-place');
      if (savedPlace) {
        var p = JSON.parse(savedPlace);
        if (p && typeof p.lat === 'number' && typeof p.lon === 'number') {
          S.place = p;
          var savedInput = document.getElementById('near-q');
          if (savedInput) savedInput.value = p.name || '';
        }
      }
      var savedView = JSON.parse(localStorage.getItem('cinemelb-view') || 'null');
      if (savedView) {
        if (savedView.group) S.group = savedView.group;
        if (savedView.filmGroup) S.filmGroup = savedView.filmGroup;
      }
    } catch (e) { /* private mode */ }

    var input = document.getElementById('q');

    document.getElementById('near-me').addEventListener('click', requestLocation);
    document.getElementById('near-form').addEventListener('submit', submitNear);
    document.getElementById('nearbar').addEventListener('click', function (event) {
      if (event.target.closest('[data-near-clear]')) clearPlace();
    });

    document.getElementById('search').addEventListener('submit', function (e) {
      e.preventDefault();
      submitSearch();
    });

    input.addEventListener('input', function () {
      if (!S.bundle) return;
      renderSuggest(input.value.trim().toLowerCase());
    });

    input.addEventListener('keydown', function (event) {
      if (event.key === 'ArrowDown') {
        if (moveSuggest(1)) event.preventDefault();
      } else if (event.key === 'ArrowUp') {
        if (moveSuggest(-1)) event.preventDefault();
      } else if (event.key === 'Enter') {
        // Handle Enter here rather than leaning on implicit form submission:
        // it is one code path whether a suggestion is highlighted or not.
        if (sugIndex >= 0 || input.value.trim()) {
          event.preventDefault();
          submitSearch();
        }
      } else if (event.key === 'Escape') {
        closeSuggest();
        input.blur();
      }
    });

    document.getElementById('suggest').addEventListener('click', function (event) {
      var sug = event.target.closest('.sug');
      if (sug) openFilm(sug.getAttribute('data-key'));
    });

    // "/" focuses search from anywhere that is not already a text field.
    document.addEventListener('keydown', function (event) {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
      var tag = (event.target.tagName || '').toLowerCase();
      if (event.key === '/' && tag !== 'input' && tag !== 'textarea') {
        event.preventDefault();
        input.focus();
        input.select();
      }
    });

    document.addEventListener('click', function (event) {
      if (event.target.closest('[data-clear]')) {
        S.filters = { accessible: false, chain: '', later: false, after: 0, within: 0 };
        render();
      }
    });

    document.getElementById('filters').addEventListener('click', onChip);
    document.getElementById('filters').addEventListener('change', onSelect);
    document.getElementById('main').addEventListener('click', onView);
    document.getElementById('theme').addEventListener('click', onTheme);
    window.addEventListener('hashchange', function () {
      closeSuggest();
      render();
    });

    tickClock();
    setInterval(tickClock, 30000);
    loadBundle();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
