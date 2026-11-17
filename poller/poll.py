"""Dailies Board poller.

Fetches the live LMU and RaceRoom daily-race data, merges what it sees into a
running state file, and writes schedule.json for the board page.

Both sources only show races that are about to start or are running, so each
run adds the starts it sees and the page projects the rest from the interval
between them.

Usage: python poll.py --out <site dir>
Reads and writes <site dir>/state.json and writes <site dir>/schedule.json.
Standard library only.
"""
import argparse
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

KEEP_STARTS = timedelta(days=3)      # how far back observed starts are kept
ACTIVE = {"lmu": timedelta(hours=6), "rr": timedelta(hours=8)}  # unseen this long = out of rotation
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
        track = tracks[0][0] + (" - " + tracks[0][1] if tracks[0][1] and tracks[0][1] != tracks[0][0] else "")
        title = " + ".join(classes)
        sid = "rr-" + slug(title + "-" + track)
        c = combos.setdefault(sid, {
            "game": "rr", "id": sid, "title": title, "classes": classes, "cars": cars,
            "track": track, "trackId": tracks[0][2], "carIds": car_ids, "durationMin": st.get("Race1Duration") or None,
            "practiceMin": st.get("PracticeDuration") or None, "starts": set(), "servers": 0,
        })
        c["starts"].add(iso(start))
        c["servers"] += 1
    out = []
    for c in combos.values():
        c["starts"] = sorted(c["starts"])
        out.append(c)
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
        series[s["id"]] = cur
    cutoff = when - KEEP_STARTS
    for sid in list(series):
        s = series[sid]
        s["starts"] = [x for x in s["starts"] if parse_iso(x) >= cutoff]
        if parse_iso(s["lastSeen"]) < when - DROP_AFTER:
            del series[sid]


def load_free():
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            return [x.lower() for x in json.load(f).get("rrFreeClasses", [])]
    except FileNotFoundError:
        return []


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
    return [{"id": p.get("id"), "name": p.get("name"), "type": p.get("type", "pack"),
             "includes": list(p.get("includes", []))} for p in cat.get("packs", [])]


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


def build_schedule(state, when, sources):
    free = load_free()
    cats = {g: load_catalog(g) for g in ("lmu", "rr")}
    out = []
    for s in state.get("series", {}).values():
        if parse_iso(s["lastSeen"]) < when - ACTIVE[s["game"]]:
            continue
        row = {k: s[k] for k in ("game", "id", "title", "track", "durationMin") if k in s}
        cat = cats.get(s["game"])
        if s["game"] == "lmu":
            row["tier"] = s.get("tier")
            row["classes"] = []
            if cat:
                row["classes"], row["access"] = lmu_access(s, cat)
        else:
            row["classes"] = s.get("classes", [])
            row["cars"] = s.get("cars", [])
            row["free"] = any(c.lower() in free for c in row["classes"])
            if cat:
                row["access"] = rr_access(s, cat)
        row["intervalMin"] = infer_interval(s["starts"])
        row["starts"] = s["starts"][-12:]
        row["lastSeen"] = s["lastSeen"]
        out.append(row)
    out.sort(key=lambda r: (r["game"], r["title"]))
    packs = {g: public_packs(c) if c else [] for g, c in cats.items()}
    return {"version": 2, "updated": iso(when), "sources": sources, "packs": packs, "series": out}


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

    when = now_utc()
    sources = state.get("sources", {})
    for game, fn in (("lmu", lambda: poll_lmu(when)), ("rr", poll_rr)):
        prev = sources.get(game, {})
        try:
            seen = fn()
            merge(state, seen, game, when)
            sources[game] = {"ok": True, "checked": iso(when), "lastOk": iso(when), "found": len(seen)}
            print(f"{game}: {len(seen)} series")
        except Exception as e:  # keep the other source and the old data
            sources[game] = {"ok": False, "checked": iso(when), "lastOk": prev.get("lastOk"),
                             "error": str(e)[:200]}
            print(f"{game}: FAILED {e}", file=sys.stderr)
    state["sources"] = sources
    state["updated"] = iso(when)

    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    with open(os.path.join(a.out, "schedule.json"), "w", encoding="utf-8") as f:
        json.dump(build_schedule(state, when, sources), f, indent=1)
    if not any(v.get("ok") for v in sources.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
