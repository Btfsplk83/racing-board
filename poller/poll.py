"""Dailies Board poller.

Fetches the live LMU and RaceRoom daily-race data, merges what it sees into a
running state file, and writes schedule.json for the board page.

RaceRoom comes from the r3e-leaderboards.info ranked-series feed, which lists
each series' timetable and weekly track rounds. It is fetched at most hourly and
cached in state.json. The live ranked server list is the fallback: it runs every
pass when the feed is unusable, and every RR_CHECK_EVERY otherwise as a cross-check.

LMU is fetched every LMU_EVERY (and right after the Tuesday reset).
AC Evo daily races come from acevo.gg every ACEVO_EVERY, the same way as LMU.
League events (config.json "leagues") come from the JustRace API every JR_EVERY
and are cached in state.json as jrFeed. LFM official series (AMS2, RaceRoom, AC Evo,
Assetto Corsa) come from the public LFM API every LFM_EVERY, cached as lfmFeed.
LMU and the RaceRoom fallback only show races that are about to start or are
running, so each run adds the starts it sees and the page projects the rest
from the interval between them.

Usage: python poll.py --out <site dir>
Reads and writes <site dir>/state.json and writes <site dir>/schedule.json.
Standard library only.
"""
import argparse
import gzip
import html
import json
import math
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

UA = "Mozilla/5.0 (compatible; dailies-board-poller/1.0)"
LMU_URL = "https://racecontrol.gg/"
RR_SERVERS_URL = "https://game.raceroom.com/multiplayer-rating/servers/"
RR_DATA_URL = "https://raw.githubusercontent.com/sector3studios/r3e-spectator-overlay/master/r3e-data.json"
RR_FEED_URL = "https://r3e-leaderboards.info/cache/ranked_series.json.gz"
ACEVO_URL = "https://acevo.gg/daily-races"
JR_API = "https://www.justrace.net/apiv3"
JR_GAMES = {4: "ams2"}                  # JustRace game ids the board shows
LFM_API = "https://api3.lowfuelmotorsport.com/api"
LFM_SIMS = {4: "ams2", 6: "rr", 8: "acevo", 3: "ac"}  # LFM sim id -> board game (ACC is 1, not shown)
LFM_DLC = {4, 3}                        # sims whose races get an access check (AMS2, AC)

LMU_EVERY = timedelta(minutes=15)       # racecontrol.gg lists ~6 h of starts, so this misses none
RR_FEED_EVERY = timedelta(minutes=60)   # refetch the feed this often (it changes about daily)
RR_CHECK_EVERY = timedelta(minutes=30)  # server-list cross-check while the feed works
ACEVO_EVERY = timedelta(minutes=15)     # acevo.gg shows the next two starts per series
JR_EVERY = timedelta(minutes=60)        # league calendars change rarely
JR_AHEAD = timedelta(days=35)           # league events this far ahead go into schedule.json
LFM_EVERY = timedelta(minutes=30)       # LFM lists each series' next four starts
PAST = timedelta(hours=2)               # feed rows: starts from this long before UTC midnight...
AHEAD = timedelta(hours=24)             # ...to this long after the next one (the page shows 24 h)

KEEP_STARTS = timedelta(days=3)      # how far back observed starts are kept
ACTIVE = {"lmu": timedelta(hours=6), "rr": timedelta(hours=8), "acevo": timedelta(hours=6)}  # unseen this long = out of rotation
DROP_AFTER = timedelta(days=8)       # forget a series entirely after this

HERE = os.path.dirname(os.path.abspath(__file__))


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:  # r3e-data.json is not UTF-8
        return raw.decode("cp1252", "replace")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ---------- LMU (racecontrol.gg homepage) ----------

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def text(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def lmu_time(label, ref):
    """'16 Nov at 10:45pm' (UTC) -> datetime. Picks the year nearest to ref."""
    m = re.match(r"(\d{1,2})\s+([A-Za-z]{3})\w*\s+at\s+(\d{1,2}):(\d{2})\s*([ap]m)", label.strip(), re.I)
    if not m:
        return None
    day, mon, hh, mm, ap = int(m[1]), MONTHS.get(m[2].lower()), int(m[3]), int(m[4]), m[5].lower()
    if not mon:
        return None
    hh = hh % 12 + (12 if ap == "pm" else 0)
    best = None
    for y in (ref.year - 1, ref.year, ref.year + 1):
        try:
            dt = datetime(y, mon, day, hh, mm, tzinfo=timezone.utc)
        except ValueError:
            continue
        if best is None or abs(dt - ref) < abs(best - ref):
            best = dt
    return best


def lmu_duration(s):
    s = s.lower()
    h = re.search(r"(\d+)\s*h", s)
    m = re.search(r"(\d+)\s*min", s)
    total = (int(h[1]) * 60 if h else 0) + (int(m[1]) if m else 0)
    return total or None


def poll_lmu(ref):
    page = fetch(LMU_URL)
    cards = page.split('class="scheduled-race-card')[1:]
    out = []
    for c in cards:
        c = c.split("</li>")[0]
        tier = re.search(r'tier-badge">\s*<i[^>]*></i>\s*([^<]+)<', c)
        title = re.search(r"<h4[^>]*>(.*?)</h4>", c, re.S)
        fields = {}
        for label, value in re.findall(r"</i>\s*(Duration|Track)\s*</span>\s*<hr[^>]*>\s*<span[^>]*>(.*?)</span>", c, re.S | re.I):
            fields[label.lower()] = text(value)
        times = [lmu_time(text(t), ref) for t in re.findall(r'text-yellow[^"]*">([^<]+)<', c)]
        times = [t for t in times if t]
        if not (tier and title and fields.get("track") and times):
            continue
        t = text(tier[1]).title()
        name = text(title[1])
        out.append({
            "game": "lmu",
            "id": "lmu-" + slug(t + "-" + name + "-" + fields["track"]),
            "tier": t,
            "title": name,
            "track": fields["track"].title() if fields["track"].isupper() else fields["track"],
            "durationMin": lmu_duration(fields.get("duration", "")),
            "starts": [iso(x) for x in times],
        })
    if not out:
        raise RuntimeError("no race cards found; the page layout may have changed")
    return out


# ---------- RaceRoom (ranked server list) ----------

def rr_lookup():
    d = json.loads(fetch(RR_DATA_URL, timeout=60))
    layouts, liveries = {}, {}
    classes = {str(k): v["Name"] for k, v in d["classes"].items()}
    for t in d["tracks"].values():
        for l in t["layouts"]:
            layouts[l["Id"]] = (t["Name"], l["Name"], t.get("Id"))
    for car in d["cars"].values():
        cls = classes.get(str(car["Class"]), "")
        for lv in car.get("liveries", []):
            liveries[lv["Id"]] = (car["Name"], cls, car.get("Id"))
    return layouts, liveries


def ceil_to(dt, minutes):
    step = minutes * 60
    ts = math.ceil(dt.timestamp() / step) * step
    return datetime.fromtimestamp(ts, timezone.utc)


def poll_rr():
    servers = json.loads(fetch(RR_SERVERS_URL)).get("result", [])
    layouts, liveries = rr_lookup()
    combos = {}
    for s in servers:
        srv = s.get("Server") or {}
        st = srv.get("Settings") or {}
        if not st.get("IsRanked"):
            continue
        m = re.match(r"portal-(\d+)-(\d{14})-s\d+", st.get("ServerName", ""))
        if not m:
            continue
        created = datetime.strptime(m[2], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        # Servers open a few minutes before the slot; the posted schedule uses quarter-hour slots.
        start = ceil_to(created, 15)
        tracks = [layouts.get(i) for i in st.get("TrackLayoutId", [])]
        tracks = [t for t in tracks if t]
        cars = sorted({liveries[i][0] for i in st.get("LiveryId", []) if i in liveries})
        classes = sorted({liveries[i][1] for i in st.get("LiveryId", []) if i in liveries and liveries[i][1]})
        car_ids = sorted({liveries[i][2] for i in st.get("LiveryId", []) if i in liveries and liveries[i][2] is not None})
        if not tracks or not classes:
            continue
        track = rr_track_name(tracks[0])
        # One ranked series per track layout. Servers in the same race can carry
        # different class mixes, and some series alternate cars, so classes are pooled.
        sid = rr_sid(track)
        c = combos.setdefault(sid, {
            "game": "rr", "id": sid, "classes": set(), "cars": set(), "carIds": set(),
            "track": track, "trackId": tracks[0][2], "durationMin": st.get("Race1Duration") or None,
            "practiceMin": st.get("PracticeDuration") or None, "starts": set(), "servers": 0, "leadSec": 0,
        })
        # How long before the slot this server opened. Seen 2026-11-27: 256 s for 5-min-practice series.
        c["leadSec"] = max(c["leadSec"], int((start - created).total_seconds()))
        c["classes"].update(classes)
        c["cars"].update(cars)
        c["carIds"].update(car_ids)
        c["starts"].add(iso(start))
        c["servers"] += 1
    out = []
    for c in combos.values():
        for k in ("classes", "cars", "carIds", "starts"):
            c[k] = sorted(c[k])
        c["title"] = " + ".join(c["classes"])
        out.append(c)
    return out


def rr_track_name(t):
    return t[0] + (" - " + t[1] if t[1] and t[1] != t[0] else "")


def rr_sid(track):
    return "rr-" + slug(track)


# ---------- RaceRoom (r3e-leaderboards.info ranked-series feed) ----------

def rr_feed_resolve(feed, data):
    """Compact the feed to what the board needs. Names, cars and ids come from r3e-data.json when it loaded."""
    layouts, cls_names, cars_by_class = {}, {}, {}
    if data:
        for t in data["tracks"].values():
            for l in t["layouts"]:
                layouts[int(l["Id"])] = (t["Name"], l["Name"], t.get("Id"))
        cls_names = {str(k): v["Name"] for k, v in data["classes"].items()}
        for car in data["cars"].values():
            cars_by_class.setdefault(str(car["Class"]), []).append((car["Name"], car.get("Id")))
    out = []
    for s in feed.get("series", []):
        sch = s.get("schedule") or {}
        fmt = s.get("format") or {}
        race = (fmt.get("races") or [{}])[0]
        rounds = []
        for r in s.get("rounds", []):
            tr = r.get("track") or {}
            lay = layouts.get(int(tr.get("layout_id") or 0))
            if lay:
                track, track_id = rr_track_name(lay), lay[2]
            else:
                track = " - ".join(x for x in (tr.get("name"), tr.get("layout")) if x)
                track_id = int(tr["track_id"]) if str(tr.get("track_id", "")).isdigit() else None
            classes = r.get("eligible_classes") or s.get("eligible_classes") or []
            ids = [str(c.get("id")) for c in classes]
            cars = [c for i in ids for c in cars_by_class.get(i, [])]
            rounds.append({
                "start": r["starts_at"] // 1000, "end": r["ends_at"] // 1000,
                "track": track, "trackId": track_id,
                "classes": sorted({cls_names.get(i) or c.get("name", "") for i, c in zip(ids, classes)} - {""}),
                "cars": sorted({n for n, _ in cars}),
                "carIds": sorted({i for _, i in cars if i is not None}),
            })
        out.append({
            "name": s.get("name", ""),
            "cadence": sch.get("cadence_minutes") or 0,
            "anchors": sch.get("anchor_minutes") or [],
            "daily": sch.get("daily_times") or [],
            "weekdays": sch.get("weekdays") or [],
            "durationMin": race.get("length") if race.get("format") == "minutes" else race.get("estimated_minutes"),
            "practiceMin": fmt.get("practice_minutes") or None,
            "rounds": rounds,
        })
    return out


def rr_feed_refresh(state, when):
    """Refetch the feed once the cache is RR_FEED_EVERY old. On failure the cache is kept:
    it lists all rounds of the season, so it stays right until RaceRoom changes the timetable."""
    cache = state.get("rrFeed") or {}
    if cache.get("fetched") and when - parse_iso(cache["fetched"]) < RR_FEED_EVERY - timedelta(minutes=2):
        return cache
    cache["fetched"] = iso(when)  # also spaces out retries after a failure
    try:
        feed = json.loads(fetch(RR_FEED_URL))
        if not feed.get("series"):
            raise RuntimeError("feed has no series")
        try:
            data = json.loads(fetch(RR_DATA_URL, timeout=60))
        except Exception as e:
            data = None
            print(f"rr feed: r3e-data.json failed ({e}); using the feed's own names", file=sys.stderr)
        cache["series"] = rr_feed_resolve(feed, data)
        cache["feedUpdated"] = str(feed.get("updated_at", ""))[:19] + "Z"
        cache.pop("error", None)
    except Exception as e:
        cache["error"] = str(e)[:200]
        print(f"rr feed: FAILED {e}", file=sys.stderr)
    state["rrFeed"] = cache
    return cache


def rr_feed_starts(s, lo, hi):
    """Starts in [lo, hi) from a series timetable. Minutes count from UTC midnight.
    weekdays (empty in every series so far) is read as 0 = Sunday, Go's convention."""
    marks = set(s["daily"])
    if s["cadence"] > 0:
        for a in s["anchors"] or [0]:
            marks.update(range(a % s["cadence"], 1440, s["cadence"]))
    out = []
    day = datetime(lo.year, lo.month, lo.day, tzinfo=timezone.utc)
    while day < hi:
        if not s["weekdays"] or (day.weekday() + 1) % 7 in s["weekdays"]:
            out += [t for t in (day + timedelta(minutes=m) for m in marks) if lo <= t < hi]
        day += timedelta(days=1)
    return sorted(out)


def rr_feed_rows(cache, when):
    """Schedule rows for every round with starts in the window. The window is pinned to the
    UTC day (PAST before midnight to AHEAD after the next one), so schedule.json only changes
    once a day and loop.sh does not commit on every pass."""
    day = datetime(when.year, when.month, when.day, tzinfo=timezone.utc)
    lo, hi = day - PAST, day + timedelta(days=1) + AHEAD
    rows = []
    for s in cache.get("series", []):
        for r in s["rounds"]:
            r_end = datetime.fromtimestamp(r["end"], timezone.utc)
            starts = rr_feed_starts(s, max(lo, datetime.fromtimestamp(r["start"], timezone.utc)), min(hi, r_end))
            if not starts:
                continue
            rows.append({
                "game": "rr", "id": rr_sid(r["track"]), "title": s["name"] or " + ".join(r["classes"]),
                "track": r["track"], "trackId": r["trackId"],
                "durationMin": s["durationMin"], "practiceMin": s["practiceMin"],
                "classes": r["classes"], "cars": r["cars"], "carIds": r["carIds"],
                "intervalMin": s["cadence"] if s["cadence"] and len(s["anchors"]) <= 1 and not s["daily"] else None,
                "starts": [iso(t) for t in starts], "until": iso(r_end),
            })
    # Ids are by track, like the server-list rows, so favorites carry over. Two series on one track get the name added.
    count = {}
    for row in rows:
        count[row["id"]] = count.get(row["id"], 0) + 1
    for row in rows:
        if count[row["id"]] > 1:
            row["id"] += "-" + slug(row["title"])
    return rows


def rr_cross_check(seen, rows):
    """Server-list starts that no feed row with the same track has. Empty means the feed agrees."""
    have = {}
    for r in rows:
        have.setdefault(rr_sid(r["track"]), set()).update(r["starts"])
    return sorted(f"{s['track']} {t[11:16]}Z" for s in seen for t in s["starts"] if t not in have.get(s["id"], ()))


def rr_pool(cur, s, when):
    """Keep classes and cars seen on this track within the active window."""
    pool = cur.setdefault("pool", {"classes": {}, "cars": {}, "carIds": {}})
    for k in pool:
        for x in s.get(k, []):
            pool[k][str(x)] = iso(when)
    keep = when - ACTIVE["rr"]
    for k in pool:
        pool[k] = {x: t for x, t in pool[k].items() if parse_iso(t) >= keep}
    cur["classes"] = sorted(pool["classes"])
    cur["cars"] = sorted(pool["cars"])
    cur["carIds"] = sorted(int(x) for x in pool["carIds"])
    cur["title"] = " + ".join(cur["classes"])


def rr_migrate(state):
    """Old state keyed RaceRoom series by class mix + track. Re-key by track, keeping starts."""
    if state.get("rrByTrack"):
        return
    series = state.get("series", {})
    for sid in [k for k, v in series.items() if v.get("game") == "rr"]:
        old = series.pop(sid)
        new = rr_sid(old["track"])
        cur = series.get(new)
        if cur is None:
            old["id"] = new
            old["pool"] = {k: {str(x): old["lastSeen"] for x in old.get(k, [])} for k in ("classes", "cars", "carIds")}
            series[new] = old
            continue
        cur["starts"] = sorted(set(cur["starts"]) | set(old["starts"]))
        cur["firstSeen"] = min(cur["firstSeen"], old["firstSeen"])
        for k in ("classes", "cars", "carIds"):
            for x in old.get(k, []):
                t = cur["pool"][k].get(str(x))
                cur["pool"][k][str(x)] = max(t, old["lastSeen"]) if t else old["lastSeen"]
        if old["lastSeen"] > cur["lastSeen"]:
            for k in ("durationMin", "practiceMin", "trackId"):
                if k in old:
                    cur[k] = old[k]
            cur["lastSeen"] = old["lastSeen"]
    for s in series.values():
        if s.get("game") == "rr":
            p = s["pool"]
            s["classes"], s["cars"] = sorted(p["classes"]), sorted(p["cars"])
            s["carIds"] = sorted(int(x) for x in p["carIds"])
            s["title"] = " + ".join(s["classes"])
    state["rrByTrack"] = True


# ---------- AC Evo (acevo.gg daily races, run on SimGrid) ----------

def acevo_len(card, label):
    """('10 min' style) value of a session tile on an event card -> (number, unit) or None."""
    m = re.search(r'title-400[^>]*>\s*(\d+)\s*<span[^>]*>\s*(min|laps?)\s*</span>\s*</span>\s*<span[^>]*>\s*' + label + r'\s*<',
                  card, re.I)
    return (int(m[1]), m[2].lower().rstrip("s")) if m else None


def acevo_dd(card, label):
    m = re.search(r"</i>\s*" + label + r"\s*</dt>\s*<dd>(.*?)</dd>", card, re.S | re.I)
    return text(m[1]) if m else ""


def poll_acevo():
    page = fetch(ACEVO_URL)
    out = []
    for c in page.split('class="event-card')[1:]:
        title = re.search(r'<h3[^>]*>\s*<a href="/events/(\d+)">(.*?)</a>', c, re.S)
        track, cls = acevo_dd(c, "Track"), acevo_dd(c, "Class")
        starts = sorted(set(re.findall(r'<time datetime="(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)"', c)))
        if not (title and track and starts):
            continue
        name = text(title[2])
        badges = [text(b) for b in re.findall(r'class="badge badge-overlay">(.*?)</span>', c, re.S)]
        sess = [(k, acevo_len(c, k)) for k in ("Practice", "Qualifying", "Race")]
        sess = [(k, v) for k, v in sess if v]
        mins = [v[0] for _, v in sess if v[1] == "min"]
        out.append({
            "game": "acevo",
            "id": "acevo-" + slug(name + "-" + track),
            "title": name,
            "track": track,
            "classes": [cls] if cls else [],
            "kind": badges[0] if badges else "",
            "free": any(b.lower() == "free" for b in badges),
            # Whole event (practice + qualifying + race) when every session is timed, so "running" covers the grid.
            "durationMin": sum(mins) if len(mins) == len(sess) and mins else None,
            "format": " · ".join(f"{k[0]} {v[0]} {v[1]}{'s' if v[1] == 'lap' and v[0] != 1 else ''}" for k, v in sess),
            "url": "https://acevo.gg/events/" + title[1],
            "starts": starts,
        })
    if not out:
        raise RuntimeError("no event cards found; the page layout may have changed")
    return out


def acevo_due(prev, when):
    if not prev.get("ok") or not prev.get("checked"):
        return True
    return when - parse_iso(prev["checked"]) >= ACEVO_EVERY - timedelta(minutes=2)


# ---------- League events (JustRace) ----------

def jr_call(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(JR_API + path, data=data, headers={
        "User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def jr_iso(s):
    """'2026-12-04T13:43:00Z' or '...+00:00' -> datetime (UTC)."""
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def jr_names(game):
    """Reference data, AMS2 only so far: id -> display name for tracks and classes, plus DLC.
    Returns (tracks, classes, dlc) where dlc = {"track": {id: item}, "cls": {class value: [item or None per car]}}
    and item = {"appid", "name"}. None means a base-game car."""
    if game != "ams2":
        return {}, {}, {}
    tr = jr_call("/refdata/api/GetAms2Tracks")
    cl = jr_call("/refdata/api/GetAms2Classes")
    tracks = {str(t["id"]): (t.get("extra_data") or {}).get("translated_name") or t.get("name", "") for t in tr}
    classes = {str(c["value"]): c.get("translated_name") or c.get("name", "") for c in cl}
    dlc = {"track": {}, "cls": {}}
    for t in tr:
        e = t.get("extra_data") or {}
        if e.get("DlcAppId"):
            dlc["track"][str(t["id"])] = {"appid": e["DlcAppId"], "name": e.get("dlc") or ""}
    by_name = {}
    for c in jr_call("/refdata/api/GetAms2Cars"):
        e = c.get("extra_data") or {}
        item = {"appid": e["DlcAppId"], "name": e.get("dlc") or ""} if e.get("DlcAppId") else None
        by_name.setdefault(c.get("class"), []).append(item)
    for c in cl:  # cars name their class by its untranslated name
        dlc["cls"][str(c["value"])] = by_name.get(c.get("name"), [])
    return tracks, classes, dlc


def need_cars(items, classes):
    """Car requirement from one entry per allowed car (None = base game). Any one car is enough."""
    if not items:
        return None
    out, seen = [], set()
    for x in items:
        if x and (x.get("appid") or x.get("name")) not in seen:
            seen.add(x.get("appid") or x.get("name"))
            out.append(x)
    return {"free": any(x is None for x in items), "dlc": out, "classes": classes}


def jr_league(league, when, names):
    sub = league["subdomain"]
    com = jr_call("/community/api/GetCommunityBySubdomain/" + sub)
    lname = league.get("name") or com.get("Name") or sub
    seasons = jr_call("/season/api/GetFilteredSeasons", {
        "ShowAll": True, "ShowExpired": False, "ShowIfNoSchedules": True, "SeasonIds": [],
        "CommunityIds": [com["CommunityId"]], "GameIds": [], "SeriesIds": [], "CountryCodes": [],
        "OnlyMatchingSeasonIds": False, "HardReload": False}).get("Seasons", [])
    rows = []
    for se in seasons:
        game = JR_GAMES.get(se.get("GameId"))
        if not game or se.get("IsHotlap") or jr_iso(se["EndDate"]) < when - timedelta(days=1):
            continue
        if game not in names:
            names[game] = jr_names(game)
        tracks, classes, dlc = names[game]
        d = jr_call(f"/season/api/GetSeasonSchedules/{se['SeasonID']}/{se['GameId']}/false/false")
        sess = {}
        for x in d.get("ScheduleSessions", []):
            sess.setdefault(x["ServerScheduleId"], []).append(x)
        races = []
        for e in d.get("ServerScheduleDetails", []):
            ss, par = e.get("ServerSchedule") or {}, e.get("ServerScheduleParam") or {}
            parts = sess.get(e["ServerScheduleId"], [])
            if not any(x.get("SessionTypeName") == "Race" for x in parts) and not ss.get("RaceDuration"):
                continue  # practice servers and hot-lap windows
            races.append((jr_iso(e["ScheduleStartTime"]), e, ss, par, parts))
        races.sort(key=lambda r: r[0])
        for n, (start, e, ss, par, parts) in enumerate(races, 1):
            if not (when - timedelta(hours=6) <= start <= when + JR_AHEAD):
                continue
            tid = str(par.get("TrackName") or ss.get("TrackList") or "").split(",")[0]
            cids = [c for c in str(par.get("CarClassList") or ss.get("ClassList") or "").split(",") if c]
            cls = [classes.get(c, "") for c in cids]
            need = {}
            if dlc:
                t = dlc["track"].get(tid)
                need["track"] = {"name": tracks.get(tid, ""), "free": not t, "dlc": [t] if t else []}
                cars = need_cars([x for c in cids for x in dlc["cls"].get(c, [])], [c for c in cls if c])
                if cars:
                    need["cars"] = cars
            timed = [x for x in parts if x.get("SessionDurationTypeName") == "Mins"]
            fmt = " · ".join(f"{x['SessionTypeName'][0]} {x['SessionDuration']} "
                             f"{'min' if x.get('SessionDurationTypeName') == 'Mins' else 'laps'}" for x in parts)
            rows.append({
                "game": game, "id": f"{game}-jr-{e['ServerScheduleId']}",
                "title": se.get("Name", ""), "league": lname, "round": n,
                "eventName": par.get("EventName", ""),
                "track": tracks.get(tid) or par.get("EventName", ""),
                "classes": [c for c in cls if c],
                "durationMin": sum(x["SessionDuration"] for x in timed) if parts and len(timed) == len(parts) else None,
                "format": fmt,
                "url": f"https://{sub}.justrace.net/en/events",
                "starts": [iso(start)],
                **({"need": need} if need else {}),
            })
    return rows


def jr_refresh(state, when):
    """League events change rarely, so they are fetched every JR_EVERY and cached in state.json.
    On failure the cache is kept and the page still has the dates."""
    cache = state.get("jrFeed") or {}
    leagues = [x for x in load_config().get("leagues", []) if isinstance(x, dict) and x.get("subdomain")]
    if not leagues:
        state.pop("jrFeed", None)
        return {}
    if cache.get("fetched") and when - parse_iso(cache["fetched"]) < JR_EVERY - timedelta(minutes=2):
        return cache
    cache["fetched"] = iso(when)
    try:
        rows, names = [], {}
        for lg in leagues:
            rows += jr_league(lg, when, names)
        cache["rows"] = rows
        cache["lastOk"] = iso(when)
        cache.pop("error", None)
    except Exception as e:
        cache["error"] = str(e)[:200]
        print(f"leagues: FAILED {e}", file=sys.stderr)
    state["jrFeed"] = cache
    return cache


def jr_rows(cache, when):
    lo = when - timedelta(hours=6)
    return [r for r in cache.get("rows", []) if any(parse_iso(s) >= lo for s in r["starts"])]


# ---------- LFM official series (Low Fuel Motorsport) ----------
# The site needs a Steam login, but getMinifiedSeasonBySim is public. It returns every sim's series
# (keyed by sim id) with the next four start times each. The payload also holds server passwords and
# Discord webhooks, so only the fields below are copied.

def lfm_call(path):
    req = urllib.request.Request(LFM_API + path, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def lfm_names():
    """Readable AMS2 track names keyed by internal name, from JustRace. Optional: LFM's own names are the fallback."""
    try:
        return {str(t.get("name", "")).lower(): (t.get("extra_data") or {}).get("translated_name") or ""
                for t in jr_call("/refdata/api/GetAms2Tracks")}
    except Exception as e:
        print(f"lfm: track names unavailable ({e})", file=sys.stderr)
        return {}


def lfm_class(c):
    return str(c).replace("_", " ").strip()


def lfm_sessions(sim, dss):
    """[(P|Q|R, minutes)] from a series' default server settings. Each sim stores them differently."""
    dss = dss or {}
    if sim == 4:
        d = dss.get("default") or {}
        raw = [("P", d.get("PracticeLength")), ("Q", d.get("QualifyLength")), ("R", d.get("RaceLength"))]
    elif sim == 3:
        raw = [(k, (dss.get(n) or {}).get("length")) for k, n in (("P", "practice"), ("Q", "qualifying"), ("R", "race"))]
    elif sim == 6:
        gs = (dss.get("GameSettings") or [{}])[0] or {}
        ss = ((gs.get("TrackLayouts") or [{}])[0] or {}).get("Sessions") or {}
        raw = [(k, (ss.get(n) or {}).get("Duration")) for k, n in (("P", "Practice"), ("Q", "Qualify"), ("R", "Race1"))]
    elif sim == 8:
        g = (dss.get("seasondef") or {}).get("game_config") or {}
        raw = [(k, (g.get(n) or 0) / 60) for k, n in (("P", "practice_duration"), ("Q", "qualify_duration"),
                                                       ("R", "race_duration"))]
    else:
        raw = []
    return [(k, int(v)) for k, v in raw if isinstance(v, (int, float)) and v >= 1]


def lfm_link(x):
    """Content entry -> {name, url}. Only https links are kept; some names carry a German "kaufen" (buy)."""
    n = re.sub(r"\s+kaufen$", "", str(x.get("name") or "").strip())
    u = str(x.get("link") or "")
    # Pages only: file downloads (mod archives, installers) are listed by name without a link.
    page = u.startswith("https://") and not re.search(r"\.(zip|7z|rar|exe)([?#].*)?$|patreon\.com/file", u, re.I)
    return {"name": n, "url": u} if page else {"name": n}


def lfm_dlc(rec):
    """Paid content for a car or track record: {"appid"?, "name", "url"?}, or None for base game and free mods.
    LFM's non_base_content: 0 base game, 1 paid DLC (Steam, or a paid mod store), 2 free download."""
    if (rec or {}).get("non_base_content") != 1:
        return None
    link = str(rec.get("content_link") or "")
    name = re.sub(r"\s+kaufen$", "", str(rec.get("content_link_name") or "").strip())
    m = re.search(r"store\.steampowered\.com/app/(\d+)", link)
    item = {"name": name}
    if m:
        item["appid"] = int(m[1])
    if link.startswith("https://"):
        item["url"] = link
    return item


def lfm_mod(rec):
    """Free download for a car or track (AC mods), as a Content entry."""
    if (rec or {}).get("non_base_content") != 2 or not rec.get("content_link_name"):
        return None
    return lfm_link({"name": rec["content_link_name"], "link": rec.get("content_link")})


def poll_lfm(when):
    d = lfm_call("/seasons/getMinifiedSeasonBySim?sim=4")  # the response holds every sim, whatever is asked
    allsims = d.get("series") or {}
    cars = {}
    for c in lfm_call("/lists/getCars"):
        cars[(c.get("sim_id"), c.get("car_id"))] = c
    names = {}
    rows = []
    for sim, game in LFM_SIMS.items():
        for e in (allsims.get(str(sim)) or {}).get("series") or []:
            if e.get("team_event") or not e.get("slug"):
                continue
            # The track changes with the LFM week (Monday, LFM local time), and active_track is the next race's.
            # So keep only starts in the same week as the first one; later weeks appear after the rollover.
            starts, wk = [], None
            for x in [e.get("next_race_date")] + list(e.get("next3_races") or []):
                try:
                    t = datetime.strptime(x, "%Y-%m-%dT%H:%M:%S%z")
                except (TypeError, ValueError):
                    continue
                wk = wk or t.isocalendar()[:2]
                u = t.astimezone(timezone.utc)
                if t.isocalendar()[:2] == wk and u >= when - timedelta(hours=2) and iso(u) not in starts:
                    starts.append(iso(u))
            if not starts:
                continue
            st = e.get("settings") or {}
            week = e.get("active_week")
            classes, car_recs = [], []
            for c in (st.get("championship_settings") or {}).get("car_classes") or []:
                if week and c.get("season_weeks") and week not in c["season_weeks"]:
                    continue
                classes.append(lfm_class(c.get("class", "")))
                car_recs += [cars[(sim, i)] for i in c.get("allowed_cars") or [] if cars.get((sim, i))]
            car_names = [str(c.get("car_name") or "").strip() for c in car_recs]
            tr = e.get("active_track") or {}
            track = tr.get("track_name", "")
            if sim == 4:
                if "ams2" not in names:
                    names["ams2"] = lfm_names()
                track = names["ams2"].get(str(tr.get("acc_track_name", "")).lower()) or track
            sess = lfm_sessions(sim, (st.get("season_event_settings") or {}).get("default_server_settings"))
            content, seen = [], set()
            cn = e.get("content_needed")
            for group in cn.values() if isinstance(cn, dict) else []:
                for x in group or []:
                    item = lfm_link(x)
                    if item["name"] and item["name"] not in seen:
                        seen.add(item["name"])
                        content.append(item)
            need = {}
            if sim in LFM_DLC:
                # Paid content locks a race; free mods (AC) are only listed under Content.
                t = lfm_dlc(tr)
                need["track"] = {"name": track, "free": not t, "dlc": [t] if t else []}
                cars_need = need_cars([lfm_dlc(c) for c in car_recs], [c for c in classes if c])
                if cars_need:
                    need["cars"] = cars_need
                for rec in [tr] + car_recs:
                    m = lfm_mod(rec)
                    if m and m["name"] not in seen:
                        seen.add(m["name"])
                        content.append(m)
                if sim == 4:
                    content = []  # AMS2 content is all paid DLC, shown exactly by the access check instead
            lic = str(e.get("min_license") or "").title()
            rows.append({
                "game": game, "source": "lfm", "id": f"{game}-lfm-{e.get('event_id')}",
                "title": e.get("series_name", ""), "league": "LFM",
                "daily": e.get("event_style") == "daily",
                "track": track,
                "classes": [c for c in classes if c],
                "cars": sorted(set(car_names)),
                "durationMin": sum(v for _, v in sess) if len(sess) >= 2 else e.get("race_length"),
                "format": " · ".join(f"{k} {v} min" for k, v in sess),
                "dlc": content,
                "license": lic + (f", SR {e['min_sr_lic']}" if e.get("min_sr_lic") else ""),
                "week": week,
                "url": "https://lowfuelmotorsport.com/series/" + e["slug"],
                "starts": starts,
                **({"need": need} if need else {}),
            })
    if not rows:
        raise ValueError("no series in the LFM response")
    return rows


def lfm_refresh(state, when):
    """Cached in state.json as lfmFeed, refreshed every LFM_EVERY. On failure the cache is kept."""
    cache = state.get("lfmFeed") or {}
    if cache.get("fetched") and when - parse_iso(cache["fetched"]) < LFM_EVERY - timedelta(minutes=2) \
            and not cache.get("error"):
        return cache
    cache["fetched"] = iso(when)
    try:
        cache["rows"] = poll_lfm(when)
        cache["lastOk"] = iso(when)
        cache.pop("error", None)
    except Exception as e:
        cache["error"] = str(e)[:200]
        print(f"lfm: FAILED {e}", file=sys.stderr)
    state["lfmFeed"] = cache
    return cache


def lfm_rows(cache, when):
    lo = when - timedelta(hours=2)
    out = []
    for r in cache.get("rows", []):
        starts = [s for s in r["starts"] if parse_iso(s) >= lo]
        if starts:
            out.append(dict(r, starts=starts))
    return out


# ---------- merge + output ----------

def infer_interval(starts):
    """Smallest gap (10 min to 6 h) that the other gaps are near-multiples of."""
    ts = sorted({parse_iso(s).timestamp() for s in starts})
    gaps = [(b - a) / 60 for a, b in zip(ts, ts[1:])]
    gaps = [g for g in gaps if 10 <= g <= 360]
    if not gaps:
        return None
    base = min(gaps)
    if all(abs(g / base - round(g / base)) * base <= 2 for g in gaps):
        return int(round(base))
    return None


def merge(state, seen, game, when):
    series = state.setdefault("series", {})
    for s in seen:
        cur = series.get(s["id"], {"firstSeen": iso(when), "starts": []})
        starts = set(cur["starts"]) | set(s["starts"])
        cur.update({k: v for k, v in s.items() if k != "starts"})
        cur["starts"] = sorted(starts)
        cur["lastSeen"] = iso(when)
        if game == "rr":
            rr_pool(cur, s, when)
        series[s["id"]] = cur
    cutoff = when - KEEP_STARTS
    for sid in list(series):
        s = series[sid]
        s["starts"] = [x for x in s["starts"] if parse_iso(x) >= cutoff]
        if parse_iso(s["lastSeen"]) < when - DROP_AFTER:
            del series[sid]


def load_config():
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def load_free():
    return [x.lower() for x in load_config().get("rrFreeClasses", [])]


def load_series_names():
    """RaceRoom series names keyed by normalized track name (from the in-game schedule)."""
    return {norm(k): v for k, v in load_config().get("rrSeriesNames", {}).items() if not k.startswith("_")}


# ---------- content catalogs (packs, tracks, cars) ----------

DATA_DIR = os.path.join(HERE, "..", "data")
RANKS = {"beginner": "Bronze", "intermediate": "Silver", "advanced": "Gold"}


def norm(s):
    """Lower-case, keep only a-z 0-9 space and hyphen (this also hides mojibake), squeeze spaces."""
    s = re.sub(r"[^a-z0-9 \-]", "", str(s).lower())
    return re.sub(r"\s+", " ", s).strip()


def load_catalog(game):
    path = os.path.join(DATA_DIR, game + "-content.json")
    try:
        with open(path, encoding="utf-8") as f:
            cat = json.load(f)
        if not isinstance(cat, dict):
            raise ValueError("not an object")
        return cat
    except (OSError, ValueError) as e:
        print(f"warning: cannot read catalog {path}: {e}; {game} series will have no access info", file=sys.stderr)
        return None


def public_packs(cat):
    out = []
    for p in cat.get("packs", []):
        row = {"id": p.get("id"), "name": p.get("name"), "type": p.get("type", "pack"), "includes": list(p.get("includes", []))}
        if str(p.get("url", "")).startswith("https://"):
            row["url"] = p["url"]
        out.append(row)
    return out


def packs_unsure(cat, ids):
    """True if any listed pack id carries check: true in the catalog."""
    flagged = {p.get("id") for p in cat.get("packs", []) if p.get("check")}
    return any(i in flagged for i in ids)


def track_out(name, t, cat):
    """Returns (access.track dict, unsure flag)."""
    if not t:
        return {"name": name, "free": False, "packs": [], "unknown": True}, False
    packs = list(t.get("packs", []))
    free = bool(t["free"]) if "free" in t else not packs
    if free:
        packs = []
    return {"name": t["name"], "free": free, "packs": packs}, bool(t.get("check")) or packs_unsure(cat, packs)


def lmu_find_track(cat, name):
    n = norm(name)
    if not n:
        return None
    best, rank = None, None
    for t in cat.get("tracks", []):
        for key in [norm(k) for k in t.get("aliases", [])] + [norm(t.get("name", ""))]:
            if not key:
                continue
            if key == n:
                r = (2, len(key))
            elif key in n or n in key:
                r = (1, len(key))
            else:
                continue
            if rank is None or r > rank:
                best, rank = t, r
    return best


def lmu_access(s, cat):
    """Returns (classes, access) for an LMU series."""
    title = s.get("title", "").lower()
    entry = next((e for e in cat.get("series", []) if e.get("match", "").lower() in title), None)
    classes = list(entry.get("classes", [])) if entry else []
    acc = {}
    rank = RANKS.get(str(s.get("tier", "")).lower())
    if rank:
        acc["rank"] = rank
    track, unsure = track_out(s.get("track", ""), lmu_find_track(cat, s.get("track", "")), cat)
    acc["track"] = track
    unsure = unsure or bool(entry and entry.get("check"))
    cars = []
    if entry:
        wanted = {norm(c) for c in entry.get("cars", [])}
        for c in cat.get("classes", []):
            if c.get("name") in classes:
                cars += [x for x in c.get("cars", []) if not wanted or norm(x.get("name", "")) in wanted]
    if cars:
        packs = sorted({p for c in cars for p in c.get("packs", [])})
        free = any(not c.get("packs") for c in cars)
        acc["cars"] = {"free": free, "packs": [] if free else packs, "classes": classes}
        unsure = unsure or any(c.get("check") for c in cars) or packs_unsure(cat, packs)
    else:
        acc["cars"] = {"free": False, "packs": [], "classes": [], "unknown": True}
    if unsure:
        acc["unsure"] = True
    return classes, acc


def rr_find_track(cat, s):
    tracks = cat.get("tracks", [])
    tid = s.get("trackId")
    if tid is not None:
        for t in tracks:
            if t.get("id") == tid:
                return t
    n = norm(s.get("track", ""))
    best, blen = None, 0
    for t in tracks:
        tn = norm(t.get("name", ""))
        if tn and (n == tn or n.startswith(tn + " - ")) and len(tn) > blen:
            best, blen = t, len(tn)
    return best


def rr_access(s, cat):
    acc = {}
    track, unsure = track_out(s.get("track", ""), rr_find_track(cat, s), cat)
    acc["track"] = track
    by_id, by_name = {}, {}
    for c in cat.get("classes", []):
        for x in c.get("cars", []):
            by_id.setdefault(x.get("id"), x)
            by_name.setdefault(norm(x.get("name", "")), x)
    found = [by_id[i] for i in s.get("carIds") or [] if i in by_id]
    if not found:
        found = [by_name[norm(n)] for n in s.get("cars", []) if norm(n) in by_name]
    cars = list({id(x): x for x in found}.values())
    classes = list(s.get("classes", []))
    if cars:
        free = any(x.get("free") for x in cars)
        packs = sorted({p for x in cars for p in x.get("packs", [])})
        acc["cars"] = {"free": free, "packs": [] if free else packs, "classes": classes}
        unsure = unsure or any(x.get("check") for x in cars) or packs_unsure(cat, packs)
    else:
        acc["cars"] = {"free": False, "packs": [], "classes": classes, "unknown": True}
    if unsure:
        acc["unsure"] = True
    return acc


def need_access(row, cat, extra):
    """Turn a row's "need" (DLC by Steam app id or name) into the page's access format, using the catalog ids.
    DLC missing from the catalog gets an id of its own and is added to extra, so My content can list it."""
    by_app = {p["appid"]: p["id"] for p in (cat or {}).get("packs", []) if p.get("appid")}
    game = row["game"]

    def ids(items):
        out = []
        for x in items:
            pid = by_app.get(x.get("appid")) or (f"steam-{x['appid']}" if x.get("appid") else f"{game}-{slug(x['name'])}")
            if pid not in by_app.values() and pid not in extra:
                extra[pid] = {"id": pid, "name": x.get("name") or pid, "type": "pack", "includes": []}
                if str(x.get("url", "")).startswith("https://"):
                    extra[pid]["url"] = x["url"]
            if pid not in out:
                out.append(pid)
        return out

    n, acc = row.get("need") or {}, {}
    if "track" in n:
        acc["track"] = {"name": n["track"].get("name", ""), "free": n["track"]["free"], "packs": ids(n["track"]["dlc"])}
    if "cars" in n:
        acc["cars"] = {"free": n["cars"]["free"], "packs": ids(n["cars"]["dlc"]), "classes": n["cars"].get("classes", [])}
    return acc


def build_schedule(state, when, sources, rr_rows, league_rows=()):
    """rr_rows: RaceRoom rows from the feed. When there are any, they replace the server-list series.
    league_rows: dated events. One row per league race (JustRace, single start), plus LFM series
    (a few known starts each, no interval)."""
    free = load_free()
    names = load_series_names()
    cats = {g: load_catalog(g) for g in ("lmu", "rr", "ams2", "ac")}
    extra = {g: {} for g in cats}
    out = []
    for row in rr_rows:
        row = dict(row, lastSeen=iso(when))
        row["free"] = any(c.lower() in free for c in row["classes"])
        if cats["rr"]:
            row["access"] = rr_access(row, cats["rr"])
        for k in ("trackId", "carIds"):
            row.pop(k, None)
        out.append(row)
    for s in state.get("series", {}).values():
        if parse_iso(s["lastSeen"]) < when - ACTIVE[s["game"]]:
            continue
        if s["game"] == "rr" and rr_rows:
            continue
        row = {k: s[k] for k in ("game", "id", "title", "track", "durationMin", "practiceMin") if k in s and s[k] is not None}
        cat = cats.get(s["game"])
        if s["game"] == "lmu":
            row["tier"] = s.get("tier")
            row["classes"] = []
            if cat:
                row["classes"], row["access"] = lmu_access(s, cat)
        elif s["game"] == "acevo":
            row.update({k: s[k] for k in ("classes", "kind", "free", "format", "url") if k in s})
        else:
            row["classes"] = s.get("classes", [])
            row["cars"] = s.get("cars", [])
            row["free"] = any(c.lower() in free for c in row["classes"])
            row["title"] = names.get(norm(s.get("track", ""))) or row.get("title", "")
            if cat:
                row["access"] = rr_access(s, cat)
        row["intervalMin"] = infer_interval(s["starts"])
        # Every start from 12 h back (the page looks back that far). Keeping only the last few dropped the soonest ones
        # once racecontrol.gg began listing ~9 h ahead.
        lo = when - timedelta(hours=12)
        row["starts"] = [x for x in s["starts"] if parse_iso(x) >= lo]
        row["lastSeen"] = s["lastSeen"]
        out.append(row)
    for row in league_rows:
        row = dict(row, event=True, lastSeen=iso(when))
        if row.get("need") and row["game"] in cats:
            row["access"] = need_access(row, cats[row["game"]], extra[row["game"]])
        row.pop("need", None)
        out.append(row)
    out.sort(key=lambda r: (r["game"], r["title"], r["starts"][:1]))
    packs = {g: (public_packs(c) if c else []) + list(extra[g].values()) for g, c in cats.items()}
    return {"version": 2, "updated": iso(when), "sources": sources, "packs": packs, "series": out}


def last_lmu_reset(when):
    """Most recent Tuesday 10:00 UTC, when LMU swaps its rotation."""
    d = datetime(when.year, when.month, when.day, 10, tzinfo=timezone.utc)
    d -= timedelta(days=(d.weekday() - 1) % 7)
    return d if d <= when else d - timedelta(days=7)


def lmu_due(prev, when):
    """Poll LMU every LMU_EVERY, every pass after a failure, and on the first pass after the weekly reset."""
    if not prev.get("ok") or not prev.get("checked"):
        return True
    last = parse_iso(prev["checked"])
    return when - last >= LMU_EVERY - timedelta(minutes=2) or last < last_lmu_reset(when)


def run_lmu(state, sources, prev, when):
    try:
        seen = poll_lmu(when)
        merge(state, seen, "lmu", when)
        sources["lmu"] = {"ok": True, "checked": iso(when), "lastOk": iso(when), "found": len(seen)}
        print(f"lmu: {len(seen)} series")
    except Exception as e:  # keep RaceRoom and the old data
        sources["lmu"] = {"ok": False, "checked": iso(when), "lastOk": prev.get("lastOk"), "error": str(e)[:200]}
        print(f"lmu: FAILED {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="site folder holding state.json and schedule.json")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    state_path = os.path.join(a.out, "state.json")
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, ValueError):
        state = {"version": 1, "series": {}}
    rr_migrate(state)

    when = now_utc()
    sources = state.get("sources", {})

    prev = sources.get("lmu", {})
    if lmu_due(prev, when):
        run_lmu(state, sources, prev, when)
    else:
        print("lmu: skipped, checked " + prev["checked"])

    # RaceRoom: feed first. The server list runs every pass without it, every RR_CHECK_EVERY with it.
    cache = rr_feed_refresh(state, when)
    rr_rows = rr_feed_rows(cache, when)
    prev = sources.get("rr", {})
    check = prev.get("serverCheck") or {}
    last = check.get("checked")
    seen, err = None, None
    if not rr_rows or not last or when - parse_iso(last) >= RR_CHECK_EVERY - timedelta(minutes=2):
        try:
            seen = poll_rr()
            merge(state, seen, "rr", when)
            check = {"checked": iso(when), "found": len(seen),
                     "leadSec": {s["track"]: s["leadSec"] for s in seen}}
            # Latest lead per track, kept across checks, to confirm the page's "opens early" rule per series.
            leads = state.setdefault("rrLeadSec", {})
            for s in seen:
                leads[s["track"]] = {"sec": s["leadSec"], "practiceMin": s.get("practiceMin"), "seen": iso(when)}
            if rr_rows:
                check["mismatch"] = rr_cross_check(seen, rr_rows)[:20]
                if check["mismatch"]:
                    print("rr: server list has starts the feed lacks: " + ", ".join(check["mismatch"]), file=sys.stderr)
        except Exception as e:
            err = str(e)[:200]
            check = {"checked": iso(when), "error": err}
            print(f"rr servers: FAILED {e}", file=sys.stderr)
    if rr_rows:
        src = {"ok": True, "via": "feed", "found": len({r["title"] for r in rr_rows}),
               "feedUpdated": cache.get("feedUpdated")}
        if cache.get("error"):
            src["feedError"] = cache["error"]
    elif seen is not None:
        src = {"ok": True, "via": "servers", "found": len(seen)}
        if cache.get("error"):
            src["feedError"] = cache["error"]
    else:
        src = {"ok": False, "error": err or cache.get("error") or "no source"}
    src.update(checked=iso(when), lastOk=iso(when) if src["ok"] else prev.get("lastOk"), serverCheck=check)
    sources["rr"] = src
    print(f"rr: {src.get('found', 0)} series via {src.get('via', 'nothing')}")

    prev = sources.get("acevo", {})
    if acevo_due(prev, when):
        try:
            seen = poll_acevo()
            merge(state, seen, "acevo", when)
            sources["acevo"] = {"ok": True, "checked": iso(when), "lastOk": iso(when), "found": len(seen)}
            print(f"acevo: {len(seen)} series")
        except Exception as e:
            sources["acevo"] = {"ok": False, "checked": iso(when), "lastOk": prev.get("lastOk"), "error": str(e)[:200]}
            print(f"acevo: FAILED {e}", file=sys.stderr)
    else:
        print("acevo: skipped, checked " + prev["checked"])

    jr = jr_refresh(state, when)
    league_rows = jr_rows(jr, when)
    if jr:
        sources["ams2"] = {"ok": not jr.get("error"), "checked": jr.get("fetched"), "lastOk": jr.get("lastOk"),
                           "found": len(league_rows)}
        if jr.get("error"):
            sources["ams2"]["error"] = jr["error"]
        print(f"leagues: {len(league_rows)} upcoming races")
    else:
        sources.pop("ams2", None)

    lfm = lfm_refresh(state, when)
    lfm_list = lfm_rows(lfm, when)
    sources["lfm"] = {"ok": not lfm.get("error"), "checked": lfm.get("fetched"), "lastOk": lfm.get("lastOk"),
                      "found": len(lfm_list)}
    if lfm.get("error"):
        sources["lfm"]["error"] = lfm["error"]
    print(f"lfm: {len(lfm_list)} series")
    league_rows = league_rows + lfm_list

    state["sources"] = sources
    state["updated"] = iso(when)

    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    with open(os.path.join(a.out, "schedule.json"), "w", encoding="utf-8") as f:
        json.dump(build_schedule(state, when, sources, rr_rows, league_rows), f, indent=1)
    if not any(v.get("ok") for v in sources.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
