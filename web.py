#!/usr/bin/env python3
"""Toyota Order Tracker — Flask web wrapper with anonymized stats collection."""
import os, sys, json, sqlite3, subprocess
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, g, jsonify

app = Flask(__name__)

USERNAME     = os.environ.get("TOYOTA_USERNAME", "")
PASSWORD     = os.environ.get("TOYOTA_PASSWORD", "")
DB_PATH      = os.environ.get("DB_PATH", "/data/stats.db")
MST_EMAIL    = os.environ.get("MST_EMAIL", "")
MST_PASSWORD = os.environ.get("MST_PASSWORD", "")

# Known Toyota Europe-route car carriers (K-Line HIGHWAY + NYK LEADER)
# These regularly serve Nagoya/Yokkaichi → Singapore → Suez → Zeebrugge → Nordic
TOYOTA_CARRIERS = {
    "431262000": "Hamburg Highway",
    "311995000": "Elbe Highway",
    "353100000": "Galveston Highway",
    "248910000": "Toreador",
    "432817000": "Altair Leader",
    "431816000": "Equuleus Leader",
    "432985000": "Garnet Leader",
    "431912000": "Sagittarius Leader",
    "354910000": "Adriatic Highway",
    "636022929": "Morning Claire",
    "477307600": "Morning Highway",
    "357795000": "Triton Leader",
    "636020245": "Spica Leader",
    "352006172": "Undine Highway",
    "372158000": "Marguerite Ace",
    "636022333": "Wild Rose Leader",
    "308688000": "Emerald Leader",
    "309905000": "Garnet Leader 2",
    "432716000": "Bishu Highway",
    "431323000": "Cepheus Leader",
    "432988000": "Libra Leader",
    "431946000": "Leo Leader",
    "477816600": "Danube Highway",
}

def get_vessel_position(mmsi: str) -> dict | None:
    """Get vessel position — scrape MyShipTracking first (free), fallback to aisstream/DataDocked."""
    # Try MST scraper (free, same login we use for detection)
    if MST_EMAIL and MST_PASSWORD:
        try:
            env = os.environ.copy()
            env['MST_EMAIL']    = MST_EMAIL
            env['MST_PASSWORD'] = MST_PASSWORD
            result = subprocess.run(
                ['node', '/app/detect_vessel.js', 'dummy', mmsi],
                capture_output=True, text=True, timeout=60, env=env
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                pos  = data.get('position', {})
                if pos.get('lat'):
                    return {
                        'mmsi':        mmsi,
                        'name':        pos.get('name') or TOYOTA_CARRIERS.get(mmsi, 'Unknown'),
                        'lat':         pos['lat'],
                        'lon':         pos['lon'],
                        'speed':       pos.get('speed', 0),
                        'course':      pos.get('course'),
                        'destination': pos.get('dest', ''),
                        'eta':         pos.get('eta', ''),
                        'updated':     pos.get('updated', ''),
                        'source':      pos.get('source', 'myshiptracking'),
                    }
        except Exception as e:
            print(f"[vessel pos scraper] {e}", file=sys.stderr)

    # Fallback: aisstream.io (free, terrestrial)
    try:
        pos = asyncio.run(_fetch_vessel_position(mmsi))
        if pos:
            return pos
    except Exception:
        pass

    # Last resort: DataDocked (satellite, uses credits)
    return _fetch_datadocked(mmsi)


def _cache_vessel(db, order_hash: str, vessel: dict, leg: str = "nagoya"):
    """Cache vessel detection result in both checks and vessel_overrides."""
    try:
        db.execute("""
            UPDATE checks SET vessel_mmsi=?, vessel_name=?, vessel_lat=?,
                              vessel_lon=?, vessel_speed=?, vessel_course=?,
                              vessel_dest=?, vessel_eta=?, vessel_updated=?
            WHERE order_hash=?
        """, (
            vessel.get("mmsi"), vessel.get("name"),
            vessel.get("lat"), vessel.get("lon"),
            vessel.get("speed"), vessel.get("course"),
            vessel.get("destination"), vessel.get("eta"),
            datetime.utcnow().isoformat(),
            order_hash
        ))
        # Also keep vessel_overrides in sync
        if vessel.get("mmsi"):
            db.execute("""
                INSERT INTO vessel_overrides
                    (order_hash, leg, detected_mmsi, detected_name, detected_at, source, created_at)
                VALUES (?, ?, ?, ?, datetime('now'), 'auto', datetime('now'))
                ON CONFLICT(order_hash, leg) DO UPDATE SET
                    detected_mmsi = excluded.detected_mmsi,
                    detected_name = excluded.detected_name,
                    detected_at   = excluded.detected_at
            """, (order_hash, leg, vessel.get("mmsi"), vessel.get("name")))
        db.commit()
    except Exception as e:
        print(f"[vessel cache] {e}", file=sys.stderr)


def detect_vessel_scraper(left_factory_date: str, leg: str = "nagoya") -> dict | None:
    """
    Detect vessel by scraping MyShipTracking port departures.
    leg: nagoya (default), zeebrugge, malmo, bremerhaven etc.
    """
    if not MST_EMAIL or not MST_PASSWORD:
        return None
    try:
        env = os.environ.copy()
        env['MST_EMAIL']    = MST_EMAIL
        env['MST_PASSWORD'] = MST_PASSWORD
        result = subprocess.run(
            ['node', '/app/detect_vessel.js', left_factory_date, '', leg],
            capture_output=True, text=True, timeout=120, env=env
        )
        if result.returncode != 0:
            print(f"[vessel scraper] error: {result.stderr[:200]}", file=sys.stderr)
            return None
        data = json.loads(result.stdout)
        matches = data.get('matches', [])
        if not matches:
            return None
        for m in matches:
            if m.get('mmsi'):
                return {
                    'mmsi':          m['mmsi'],
                    'name':          m.get('vessel', ''),
                    'source':        'scraper',
                    'time':          m.get('time', ''),
                    'leg':           leg,
                    'berth_verified': data.get('berth_verified', False),
                }
        return None
    except Exception as e:
        print(f"[vessel scraper] {e}", file=sys.stderr)
        return None


def detect_vessel(left_factory_date: str, leg: str = "nagoya") -> dict | None:
    """Auto-detect vessel via MyShipTracking port departure scraper."""
    if not left_factory_date:
        return None
    vessel = detect_vessel_scraper(left_factory_date, leg=leg)
    if vessel:
        pos = get_vessel_position(vessel['mmsi'])
        if pos:
            vessel.update(pos)
    return vessel

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE IF NOT EXISTS checks (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL,
                order_hash    TEXT,
                model         TEXT,
                engine        TEXT,
                transmission  TEXT,
                color         TEXT,
                status        TEXT,
                created_on    TEXT,
                destination   TEXT,
                dest_country  TEXT,
                is_delayed    INTEGER,
                has_damage    INTEGER,
                steps_json    TEXT,
                deliveries_json TEXT
            );
            CREATE TABLE IF NOT EXISTS step_durations (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                order_hash    TEXT NOT NULL,
                step          TEXT NOT NULL,
                model         TEXT,
                dest_country  TEXT,
                date_entered  TEXT,
                date_left     TEXT,
                duration_days INTEGER,
                observed      INTEGER DEFAULT 0,
                UNIQUE(order_hash, step)
            );
            CREATE TABLE IF NOT EXISTS hub_legs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                order_hash    TEXT NOT NULL,
                from_hub      TEXT NOT NULL,
                to_hub        TEXT NOT NULL,
                leg_key       TEXT NOT NULL,  -- e.g. "nagoya->zeebrugge"
                model         TEXT,
                dest_country  TEXT,
                date_departed TEXT,  -- when from_hub became visited
                date_arrived  TEXT,  -- when to_hub became visited/inTransit
                duration_days INTEGER,
                observed      INTEGER DEFAULT 0,  -- 1 = both dates from separate logins
                UNIQUE(order_hash, leg_key)
            );
            CREATE TABLE IF NOT EXISTS vessel_overrides (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                order_hash    TEXT NOT NULL,
                leg           TEXT NOT NULL DEFAULT 'nagoya',
                depart_date   TEXT,
                mmsi          TEXT,
                detected_mmsi TEXT,
                detected_name TEXT,
                detected_at   TEXT,
                created_at    TEXT DEFAULT (datetime('now')),
                source        TEXT,
                berth_verified INTEGER DEFAULT 0,
                UNIQUE(order_hash, leg)
            );
            CREATE TABLE IF NOT EXISTS order_names (
                order_hash  TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                created_at  TEXT
            );
            -- migrate: add created_on if upgrading from older schema
            CREATE TABLE IF NOT EXISTS _migrations (id INTEGER PRIMARY KEY);
        """)
        # add created_on column if missing (safe on existing DBs)
        cols = [r[1] for r in db.execute("PRAGMA table_info(checks)").fetchall()]
        if 'created_on' not in cols:
            db.execute("ALTER TABLE checks ADD COLUMN created_on TEXT")
        sd_cols = [r[1] for r in db.execute("PRAGMA table_info(step_durations)").fetchall()]
        if 'observed' not in sd_cols:
            db.execute("ALTER TABLE step_durations ADD COLUMN observed INTEGER DEFAULT 0")
        vo_cols = [r[1] for r in db.execute("PRAGMA table_info(vessel_overrides)").fetchall()]
        if 'berth_verified' not in vo_cols:
            db.execute("ALTER TABLE vessel_overrides ADD COLUMN berth_verified INTEGER DEFAULT 0")
        # hub_legs table created by executescript above if not exists
        # Vessel tracking columns
        for col in ['vessel_mmsi','vessel_name','vessel_lat','vessel_lon',
                    'vessel_speed','vessel_course','vessel_dest','vessel_eta','vessel_updated']:
            if col not in cols:
                db.execute(f"ALTER TABLE checks ADD COLUMN {col} TEXT")
        # Geocoding cache table
        db.execute("""
            CREATE TABLE IF NOT EXISTS geocache (
                query  TEXT PRIMARY KEY,
                lat    REAL,
                lng    REAL,
                cached_at TEXT DEFAULT (datetime('now'))
            )
        """)
        db.commit()
        g.db = db
    return g.db

@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db: db.close()

# Known Toyota logistics hubs — avoids Nominatim for common stops
_KNOWN_COORDS = {
    # Japan
    "nagoya":           (35.0883, 137.1748),
    "toyota city":      (35.0838, 137.1567),
    "yokkaichi":        (34.9657, 136.6244),
    # Europe — sea ports
    "zeebrugge":        (51.3333,   3.1956),
    "bremerhaven":      (53.5510,   8.5769),
    "southampton":      (50.8998,  -1.4044),
    "portbury":         (51.4942,  -2.7202),
    "drammen":          (59.7440,  10.2045),
    "malmö":            (55.6050,  13.0038),
    "malmo":            (55.6050,  13.0038),
    "sagunto":          (39.6779,  -0.2716),
    "livorno":          (43.5487,  10.3106),
    "piraeus":          (37.9667,  23.6333),
    "varna":            (43.2141,  27.9147),
    "constanta":        (44.1733,  28.6383),
    "koper":            (45.5481,  13.7301),
    "kotka":            (60.4664,  26.9457),
    "paldiski":         (59.3548,  24.0544),
    "göteborg":         (57.7089,  11.9746),
    "gothenburg":       (57.7089,  11.9746),
    # Other EU destinations
    "vilnius":          (54.6872,  25.2797),
    "riga":             (56.9460,  24.1059),
    "tallinn":          (59.4370,  24.7536),
    "helsinki":         (60.1699,  24.9384),
    "warsaw":           (52.2297,  21.0122),
    "prague":           (50.0755,  14.4378),
    "budapest":         (47.4979,  19.0402),
    "bucharest":        (44.4268,  26.1025),
    "sofia":            (42.6977,  23.3219),
    "zagreb":           (45.8150,  15.9819),
    "bratislava":       (48.1486,  17.1077),
    "ljubljana":        (46.0569,  14.5058),
    "amsterdam":        (52.3676,   4.9041),
    "brussels":         (50.8503,   4.3517),
    "paris":            (48.8566,   2.3522),
    "madrid":           (40.4168,  -3.7038),
    "lisbon":           (38.7169,  -9.1399),
    "vienna":           (48.2082,  16.3738),
    "berlin":           (52.5200,  13.4050),
    "munich":           (48.1351,  11.5820),
    "hamburg":          (53.5753,  10.0153),
    "rome":             (41.9028,  12.4964),
    "milan":            (45.4642,   9.1900),
    "zurich":           (47.3769,   8.5417),
    "singapore":        ( 1.3521, 103.8198),
    "port klang":       ( 2.9982, 101.3839),
    "colombo":          ( 6.9271,  79.8612),
    "suez":             (29.9668,  32.5498),
}

def geocode_location(location_name: str, country_name: str = "") -> tuple:
    """Return (lat, lng) for a delivery stop, using cache then Nominatim."""
    # Try known-coords lookup (case-insensitive substring match)
    name_lower = location_name.lower()
    for key, coords in _KNOWN_COORDS.items():
        if key in name_lower:
            return coords

    query = f"{location_name}, {country_name}".strip(", ")
    try:
        db = get_db()
        row = db.execute("SELECT lat, lng FROM geocache WHERE query=?", (query,)).fetchone()
        if row:
            return (row['lat'], row['lng']) if row['lat'] is not None else None

        import urllib.request, urllib.parse
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
            "q": query, "format": "json", "limit": 1
        })
        req = urllib.request.Request(url, headers={"User-Agent": "ToyotaOrderTracker/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            results = json.loads(resp.read())
        if results:
            lat = float(results[0]['lat'])
            lng = float(results[0]['lon'])
        else:
            lat = lng = None
        db.execute("INSERT OR REPLACE INTO geocache (query, lat, lng) VALUES (?,?,?)",
                   (query, lat, lng))
        db.commit()
        return (lat, lng) if lat is not None else None
    except Exception:
        return None

def days_between(d1, d2):
    try:
        return (datetime.strptime(d2, "%Y-%m-%d") -
                datetime.strptime(d1, "%Y-%m-%d")).days
    except Exception:
        return None

def save_stats(order: dict, step_dates: dict, today_only: bool = True, created_on: str = ""):
    try:
        import hashlib
        details    = order.get("orderDetails", {})
        status     = order.get("currentStatus", {})
        steps      = order.get("preprocessed", {}).get("steps", {})
        deliveries = order.get("intermediateDeliveries", [])

        dest, dest_country = "", ""
        if deliveries:
            last         = deliveries[-1]
            dest         = last.get("locationName", "")
            dest_country = last.get("countryName", "")

        raw_id     = details.get("orderId", "")
        order_hash = hashlib.sha256(raw_id.encode()).hexdigest()[:16] if raw_id else None
        model      = details.get("vehicleModel")

        db = get_db()

        if today_only and order_hash:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            existing = db.execute("SELECT rowid, status FROM checks WHERE order_hash=? AND ts LIKE ?",
                          (order_hash, f"{today}%")).fetchone()
            if existing:
                # Update status if it changed today
                current_status = order.get("currentStatus", {}).get("currentStatus", "")
                if current_status and current_status != existing["status"]:
                    db.execute("UPDATE checks SET status=?, ts=? WHERE rowid=?",
                               (current_status, datetime.utcnow().isoformat(), existing["rowid"]))
                    # When status changes to leftTheFactory, immediately record the date
                    if current_status == "LeftTheFactory":
                        today_date = datetime.utcnow().strftime("%Y-%m-%d")
                        db.execute("""
                            INSERT INTO step_durations (order_hash, step, model, dest_country, date_entered, observed)
                            VALUES (?,?,?,?,?,0)
                            ON CONFLICT(order_hash, step) DO UPDATE SET
                                date_entered = COALESCE(date_entered, excluded.date_entered)
                        """, (order_hash, 'leftTheFactory', model, dest_country, today_date))
                # Still update created_on if missing
                if created_on:
                    db.execute(
                        "UPDATE checks SET created_on=? WHERE order_hash=? AND created_on IS NULL",
                        (created_on[:10], order_hash)
                    )
                _save_step_durations(db, order_hash, model, dest_country, steps, step_dates)
                _save_hub_legs(db, order_hash, model, dest_country,
                               [{"loc":d.get("locationName"),"country":d.get("countryName"),
                                 "type":d.get("destinationType"),"visited":d.get("isVisited")}
                                for d in (order.get("intermediateDeliveries") or [])],
                               step_dates)
                db.commit()
                return

        db.execute("""
            INSERT INTO checks
              (ts, order_hash, model, engine, transmission, color, status,
               created_on, destination, dest_country, is_delayed, has_damage,
               steps_json, deliveries_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.utcnow().isoformat(), order_hash, model,
            details.get("engine"), details.get("transmission"),
            details.get("vehicleExternalColor"), status.get("currentStatus"),
            created_on[:10] if created_on else None,
            dest, dest_country,
            1 if status.get("isDelayed") else 0,
            1 if status.get("damageCode") else 0,
            json.dumps({k: v.get("status") for k, v in steps.items()}),
            json.dumps([{"loc": d.get("locationName"), "country": d.get("countryName"),
                         "type": d.get("destinationType"), "visited": d.get("isVisited")}
                        for d in deliveries])
        ))
        # Backfill created_on for all older rows of this order that are missing it
        if created_on and order_hash:
            db.execute(
                "UPDATE checks SET created_on=? WHERE order_hash=? AND created_on IS NULL",
                (created_on[:10], order_hash)
            )

        _save_step_durations(db, order_hash, model, dest_country, steps, step_dates)
        _save_hub_legs(db, order_hash, model, dest_country,
                       [{"loc":d.get("locationName"),"country":d.get("countryName"),
                         "type":d.get("destinationType"),"visited":d.get("isVisited")}
                        for d in deliveries],
                       step_dates)

        # Auto-assign friendly name if not already named
        if order_hash:
            existing_name = db.execute(
                "SELECT name FROM order_names WHERE order_hash=?", (order_hash,)
            ).fetchone()
            if not existing_name:
                code_map = {
                    'DENMARK':'DK','FRANCE':'FR','GREECE':'GR','ITALY':'IT',
                    'LITHUANIA':'LT','NORWAY':'NO','SLOVENIA':'SI','SPAIN':'ES',
                    'SWEDEN':'SE','UNITED KINGDOM':'UK','PORTUGAL':'PT',
                    'FINLAND':'FI','GERMANY':'DE','NETHERLANDS':'NL','BELGIUM':'BE',
                    'AUSTRIA':'AT','SWITZERLAND':'CH','POLAND':'PL','CZECHIA':'CZ',
                    'HUNGARY':'HU','ROMANIA':'RO','BULGARIA':'BG','CROATIA':'HR',
                }
                code = code_map.get(dest_country, 'XX')
                count = db.execute(
                    "SELECT COUNT(*) FROM order_names WHERE name LIKE ?", (code+'-%',)
                ).fetchone()[0]
                name = f"{code}-{count+1}"
                db.execute(
                    "INSERT OR IGNORE INTO order_names (order_hash, name, created_at) VALUES (?,?,datetime('now'))",
                    (order_hash, name)
                )

        db.commit()
    except Exception as e:
        print(f"[stats] save error: {e}", file=sys.stderr)

def _save_step_durations(db, order_hash, model, dest_country, steps, step_dates):
    """Save step durations. Only mark as observed=1 when we witnessed the transition.

    observed=1 means: we saw this step as 'current' on a previous login,
                      and now see it as 'visited' — so both dates are real observations.
    observed=0 means: step was already completed when user first logged in —
                      dates are unreliable, don't use for duration stats.
    """
    if not order_hash:
        return

    if step_dates:
        for step, dates in step_dates.get("steps", {}).items():
            entered = dates.get("current")   # date we first saw it as current
            left    = dates.get("visited")   # date we first saw it as visited

            # observed=1: we saw BOTH current and visited across separate logins — reliable
            # observed=0: step already completed when user first logged in — less reliable
            observed = 1 if (entered and left and entered != left) else 0
            # Always calculate duration when both dates exist, even if observed=0
            # (useful for statistics with relaxed filters)
            dur = days_between(entered, left) if (entered and left) else None

            # Use whichever date we have for date_entered
            date_entered = entered or left

            db.execute("""
                INSERT INTO step_durations
                  (order_hash, step, model, dest_country,
                   date_entered, date_left, duration_days, observed)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(order_hash, step) DO UPDATE SET
                  date_left      = COALESCE(excluded.date_left, date_left),
                  duration_days  = COALESCE(excluded.duration_days, duration_days),
                  observed       = MAX(observed, excluded.observed)
            """, (order_hash, step, model, dest_country,
                  date_entered, left, dur, observed))

    # Record steps visible from preprocessed (for currently-in-progress tracking)
    for step_name, step_data in steps.items():
        s = step_data.get("status", "")
        if s in ("visited", "current"):
            db.execute("""
                INSERT INTO step_durations
                  (order_hash, step, model, dest_country, observed)
                VALUES (?,?,?,?,0)
                ON CONFLICT(order_hash, step) DO NOTHING
            """, (order_hash, step_name, model, dest_country))

def _normalize_hub(loc: str) -> str:
    """Normalize delivery location name to a short hub key."""
    loc = (loc or "").lower()
    if "toyota city" in loc or "aichi" in loc: return "Nagoya"
    if "zeebrugge" in loc:   return "Zeebrugge"
    if "bremerhaven" in loc: return "Bremerhaven"
    if "southampton" in loc: return "Southampton"
    if "portbury" in loc or "bristol" in loc: return "Portbury"
    if "sagunto" in loc:     return "Sagunto"
    if "livorno" in loc:     return "Livorno"
    if "malmö" in loc or "malmo" in loc: return "Malmö"
    if "gothenburg" in loc or "göteborg" in loc: return "Gothenburg"
    if "paldiski" in loc:    return "Paldiski"
    if "drammen" in loc:     return "Drammen"
    if "piraeus" in loc:     return "Piraeus"
    if "kotka" in loc:       return "Kotka"
    if "varna" in loc:       return "Varna"
    if "koper" in loc:       return "Koper"
    return None  # destination/truck legs — skip

def _save_hub_legs(db, order_hash: str, model: str, dest_country: str,
                   deliveries: list, step_dates: dict):
    """
    Record port-to-port leg durations from delivery hub visit dates.
    
    We use the step_dates JSON which records when each delivery stop
    changed status. For vessel legs (FACTORY→HUB or HUB→HUB), when
    both the departure hub (visited) and arrival hub (inTransit/visited)
    have dates from SEPARATE logins, we record an observed leg duration.
    """
    if not order_hash or not deliveries:
        return

    # Build list of vessel hubs in order (skip truck legs to final dest)
    vessel_hubs = []
    for d in deliveries:
        if d.get("type") in ("FACTORY", "HUB", "TRANSIT") and            d.get("visited") != "notVisited":
            hub = _normalize_hub(d.get("loc", ""))
            if hub:
                vessel_hubs.append({
                    "hub":     hub,
                    "visited": d.get("visited"),
                    "loc":     d.get("loc", ""),
                })

    if len(vessel_hubs) < 2:
        return

    # Get dates from step_dates for hub transitions
    # step_dates structure: {"steps": {"leftTheFactory": {"current": "2026-05-15", "visited": "2026-05-29"}}}
    steps = step_dates.get("steps", {}) if step_dates else {}
    lf = steps.get("leftTheFactory", {})
    it = steps.get("inTransit", {})

    # Map: Nagoya departure = leftTheFactory.visited (when it left visited status)
    # Hub arrival = inTransit.current (when inTransit was first seen)
    # Hub departure = inTransit.visited (when inTransit became visited)
    hub_dates = {
        "Nagoya": {
            "departed": lf.get("visited") or lf.get("current"),
        }
    }

    # For now we track Nagoya → first European hub (the deep sea leg)
    # This is the most valuable stat — ~25-38 days depending on route
    if len(vessel_hubs) >= 2:
        from_hub = vessel_hubs[0]["hub"]  # Nagoya
        to_hub   = vessel_hubs[1]["hub"]  # Zeebrugge/Sagunto/etc

        departed = hub_dates.get(from_hub, {}).get("departed")
        # Arrival = when inTransit first seen (car arrived at European hub)
        arrived  = it.get("current") or it.get("visited")

        if departed and arrived:
            try:
                dur = (datetime.strptime(arrived, "%Y-%m-%d") -
                       datetime.strptime(departed, "%Y-%m-%d")).days
                # Only meaningful if positive and realistic (15-60 days for sea voyage)
                if 15 <= dur <= 60:
                    leg_key = f"{from_hub.lower()}->{to_hub.lower()}"
                    # observed=1 only if the two dates came from different logins
                    # (departed from leftTheFactory.visited, arrived from inTransit.current
                    #  and they are different dates = must be separate logins)
                    observed = 1 if departed != arrived else 0
                    db.execute("""
                        INSERT INTO hub_legs
                          (order_hash, from_hub, to_hub, leg_key, model, dest_country,
                           date_departed, date_arrived, duration_days, observed)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(order_hash, leg_key) DO UPDATE SET
                          date_arrived  = COALESCE(excluded.date_arrived, date_arrived),
                          duration_days = excluded.duration_days,
                          observed      = MAX(observed, excluded.observed)
                    """, (order_hash, from_hub, to_hub, leg_key, model, dest_country,
                          departed, arrived, dur, observed))
            except Exception as e:
                print(f"[hub_legs] {e}", file=__import__('sys').stderr)

    # Zeebrugge → Malmö leg (feeder)
    # Use vessel_overrides for zeebrugge visited date vs malmo inTransit date
    # This will be populated as more users pass through the feeder legs
    # For now, record what we can from deliveries isVisited status changes
    for i in range(1, len(vessel_hubs) - 1):
        fh = vessel_hubs[i]
        th = vessel_hubs[i+1]
        if fh["visited"] == "visited" and th["visited"] in ("visited", "inTransit"):
            from_hub = fh["hub"]
            to_hub   = th["hub"]
            leg_key  = f"{from_hub.lower()}->{to_hub.lower()}"
            # We don't have exact dates for feeder legs yet from step_dates
            # Just record the leg exists for this order (no duration)
            db.execute("""
                INSERT INTO hub_legs (order_hash, from_hub, to_hub, leg_key, model, dest_country)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(order_hash, leg_key) DO NOTHING
            """, (order_hash, from_hub, to_hub, leg_key, model, dest_country))


def get_stats_data():
    db    = get_db()
    total = db.execute("SELECT COUNT(DISTINCT order_hash) FROM checks WHERE order_hash IS NOT NULL").fetchone()[0]
    by_model     = db.execute("SELECT model, COUNT(DISTINCT order_hash) c FROM checks WHERE model IS NOT NULL GROUP BY model ORDER BY c DESC").fetchall()
    by_status    = db.execute("""
        SELECT status, COUNT(DISTINCT order_hash) c FROM checks c1
        WHERE status IS NOT NULL
          AND ts = (SELECT MAX(ts) FROM checks c2 WHERE c2.order_hash = c1.order_hash)
        GROUP BY status ORDER BY c DESC
    """).fetchall()
    delayed      = db.execute("SELECT COUNT(DISTINCT order_hash) FROM checks WHERE is_delayed=1").fetchone()[0]
    damaged      = db.execute("SELECT COUNT(DISTINCT order_hash) FROM checks WHERE has_damage=1").fetchone()[0]
    recent = db.execute("""
        SELECT c.ts, c.model, c.status, c.dest_country, c.order_hash,
               COALESCE(n.name, substr(c.order_hash,1,8)) as order_name
        FROM checks c
        LEFT JOIN order_names n ON c.order_hash = n.order_hash
        INNER JOIN (
            SELECT order_hash, MAX(ts) ts FROM checks GROUP BY order_hash
        ) latest ON c.order_hash = latest.order_hash AND c.ts = latest.ts
        ORDER BY c.ts DESC LIMIT 20
    """).fetchall()
    by_country   = db.execute("""
        SELECT dest_country, COUNT(DISTINCT order_hash) total,
               SUM(DISTINCT CASE WHEN is_delayed=1 THEN 1 ELSE 0 END) delayed,
               GROUP_CONCAT(DISTINCT model) models
        FROM checks WHERE dest_country != '' AND dest_country IS NOT NULL
        GROUP BY dest_country ORDER BY total DESC LIMIT 20
    """).fetchall()
    step_avgs    = db.execute("""
        SELECT sd.step,
               COUNT(*) samples,
               ROUND(AVG(sd.duration_days),1) avg_days,
               MIN(sd.duration_days) min_days,
               MAX(sd.duration_days) max_days,
               SUM(sd.observed) observed_count,
               CASE
                 WHEN c.dest_country IN ('ITALY','SPAIN','FRANCE','GREECE','SLOVENIA','CROATIA','PORTUGAL')
                   THEN 'Mediterranean'
                 WHEN c.dest_country IN ('LITHUANIA','LATVIA','ESTONIA','FINLAND','SWEDEN','NORWAY','DENMARK','POLAND')
                   THEN 'Nordic/Baltic'
                 ELSE 'Western Europe'
               END route
        FROM step_durations sd
        JOIN (
            SELECT order_hash, MAX(ts) ts FROM checks GROUP BY order_hash
        ) latest ON sd.order_hash = latest.order_hash
        JOIN checks c ON c.order_hash = latest.order_hash AND c.ts = latest.ts
        JOIN (
            SELECT order_hash,
                   COUNT(*) logins,
                   CASE
                     WHEN COUNT(*) <= 1 THEN 99
                     ELSE CAST(julianday(MAX(ts)) - julianday(MIN(ts)) AS REAL) / (COUNT(*) - 1)
                   END avg_gap
            FROM checks
            WHERE order_hash IS NOT NULL
            GROUP BY order_hash
        ) freq ON sd.order_hash = freq.order_hash
        WHERE sd.duration_days IS NOT NULL
          AND sd.duration_days >= 0
          AND sd.observed = 1
          AND freq.avg_gap <= 7
          AND freq.logins >= 2
        GROUP BY sd.step, route
        ORDER BY sd.step, route
    """).fetchall()
    step_current = db.execute("""
        SELECT sd.step,
               MIN(sd.date_entered) earliest,
               COUNT(DISTINCT sd.order_hash) order_count,
               CAST(julianday('now') - julianday(MIN(sd.date_entered)) AS INTEGER) days_so_far
        FROM step_durations sd
        INNER JOIN (
            SELECT order_hash, MAX(ts) ts FROM checks GROUP BY order_hash
        ) latest ON sd.order_hash = latest.order_hash
        INNER JOIN checks c ON c.order_hash = sd.order_hash AND c.ts = latest.ts
        WHERE sd.date_left IS NULL
          AND sd.date_entered IS NOT NULL
        GROUP BY sd.step
        ORDER BY days_so_far DESC LIMIT 10
    """).fetchall()
    # Order date → buildInProgress: only reliable if:
    # 1. processedOrder was observed as current (date_entered set while at that step)
    # 2. buildInProgress date_entered came AFTER processedOrder date_entered
    # 3. We use the API createdOn as order date (most accurate date we have)
    order_to_build = db.execute("""
        SELECT COUNT(*) samples,
               ROUND(AVG(julianday(sd_build.date_entered) - julianday(c.created_on)), 1) avg_days,
               MIN(CAST(julianday(sd_build.date_entered) - julianday(c.created_on) AS INTEGER)) min_days,
               MAX(CAST(julianday(sd_build.date_entered) - julianday(c.created_on) AS INTEGER)) max_days
        FROM step_durations sd_build
        JOIN step_durations sd_proc
          ON sd_build.order_hash = sd_proc.order_hash
         AND sd_proc.step = 'processedOrder'
         AND sd_proc.date_entered IS NOT NULL
         AND sd_proc.observed = 1
        JOIN (
            SELECT order_hash, MIN(created_on) created_on
            FROM checks WHERE created_on IS NOT NULL
            GROUP BY order_hash
        ) c ON sd_build.order_hash = c.order_hash
        -- Only include frequently logging users
        JOIN (
            SELECT order_hash,
                   COUNT(*) logins,
                   CASE
                     WHEN COUNT(*) <= 1 THEN 99
                     ELSE CAST(julianday(MAX(ts)) - julianday(MIN(ts)) AS REAL) / (COUNT(*) - 1)
                   END avg_gap
            FROM checks WHERE order_hash IS NOT NULL GROUP BY order_hash
        ) freq ON sd_build.order_hash = freq.order_hash
        WHERE sd_build.step = 'buildInProgress'
          AND sd_build.date_entered IS NOT NULL
          AND c.created_on IS NOT NULL
          AND julianday(sd_build.date_entered) > julianday(sd_proc.date_entered)
          AND julianday(sd_build.date_entered) > julianday(c.created_on)
          AND freq.avg_gap <= 7
          AND freq.logins >= 2
    """).fetchone()
    countries = db.execute(
        "SELECT COUNT(DISTINCT dest_country) FROM checks WHERE dest_country != '' AND dest_country IS NOT NULL AND order_hash IS NOT NULL"
    ).fetchone()[0]
    order_dates = db.execute("""
        SELECT created_on, model, dest_country
        FROM checks
        WHERE created_on IS NOT NULL
        GROUP BY order_hash
        ORDER BY created_on ASC
    """).fetchall()
    # Per-order journey for the live tracking section
    order_journeys_raw = db.execute("""
        SELECT c.order_hash, c.model, c.dest_country, c.status,
               c.created_on, c.ts, c.steps_json
        FROM checks c
        INNER JOIN (
            SELECT order_hash, MAX(ts) ts FROM checks GROUP BY order_hash
        ) latest ON c.order_hash = latest.order_hash AND c.ts = latest.ts
        WHERE c.model IS NOT NULL
        ORDER BY c.created_on ASC, c.ts ASC
    """).fetchall()
    # Pre-parse steps_json so template doesn't need fromjson filter
    order_journeys = []
    all_steps = ['processedOrder','buildInProgress','leftTheFactory','inTransit','arrivedAtRetailer']
    for r in order_journeys_raw:
        try:
            steps = json.loads(r['steps_json']) if r['steps_json'] else {}
            if not isinstance(steps, dict):
                steps = {}
        except Exception:
            steps = {}
        # steps_json stores {"step": "status_string"} — values are strings not dicts
        def get_status(v):
            if isinstance(v, dict): return v.get('status', 'pending')
            if isinstance(v, str):  return v
            return 'pending'
        step_statuses = [(s, get_status(steps.get(s, 'pending'))) for s in all_steps]
        order_journeys.append({
            'model':       r['model'],
            'dest_country':r['dest_country'],
            'status':      r['status'],
            'created_on':  r['created_on'],
            'step_statuses': step_statuses,
        })
    # Login frequency per order
    login_freq = db.execute("""
        SELECT order_hash, dest_country, model, status,
               COUNT(*) as logins,
               MIN(ts) as first_login,
               MAX(ts) as last_login,
               CAST((julianday(MAX(ts)) - julianday(MIN(ts))) AS INTEGER) as days_tracked
        FROM checks
        WHERE order_hash IS NOT NULL AND model IS NOT NULL
        GROUP BY order_hash
        ORDER BY logins DESC
        LIMIT 20
    """).fetchall()

    hub_leg_stats = db.execute("""
        SELECT leg_key, from_hub, to_hub,
               COUNT(*) samples,
               SUM(observed) observed_count,
               ROUND(AVG(CASE WHEN observed=1 THEN duration_days END), 1) avg_days,
               MIN(CASE WHEN observed=1 THEN duration_days END) min_days,
               MAX(CASE WHEN observed=1 THEN duration_days END) max_days
        FROM hub_legs
        WHERE duration_days IS NOT NULL AND duration_days > 0
        GROUP BY leg_key
        ORDER BY
          CASE from_hub
            WHEN 'Nagoya' THEN 1 WHEN 'Zeebrugge' THEN 2
            WHEN 'Bremerhaven' THEN 2 WHEN 'Southampton' THEN 2
            WHEN 'Sagunto' THEN 2 WHEN 'Livorno' THEN 2
            WHEN 'Malmo' THEN 3 WHEN 'Malmö' THEN 3
            WHEN 'Gothenburg' THEN 3 ELSE 4
          END, leg_key
    """).fetchall()

    return dict(total=total, by_model=by_model, by_status=by_status,
                by_country=by_country, delayed=delayed, damaged=damaged,
                recent=recent, step_avgs=step_avgs, step_current=step_current,
                countries=countries, order_dates=order_dates,
                order_journeys=order_journeys, order_to_build=order_to_build,
                login_freq=login_freq, hub_leg_stats=hub_leg_stats)

# ── Templates ─────────────────────────────────────────────────────────────────

BASE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Toyota Europe Order Tracker</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#0d1117;--surface:#161b22;--surface2:#21262d;--border:#30363d;
      --red:#e5001a;--red-dim:#7d0010;--text:#e6edf3;--muted:#8b949e;
      --green:#3fb950;--amber:#d29922;--radius:10px;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);
     min-height:100vh;font-size:14px;line-height:1.6;}
a{color:var(--red);text-decoration:none;}
a:hover{text-decoration:underline;}
.nav{background:rgba(13,17,23,0.85);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
     border-bottom:0.5px solid rgba(255,255,255,0.08);padding:0 2rem;
     display:flex;align-items:center;height:56px;
     position:sticky;top:0;z-index:100;}
.nav-brand{display:flex;align-items:center;gap:10px;text-decoration:none!important;}
.nav-brand-icon{width:28px;height:28px;border-radius:6px;background:var(--red);
                display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.nav-brand-name{font-size:14px;font-weight:500;color:var(--text);letter-spacing:-.01em;}
.nav-links{display:flex;gap:4px;margin-left:auto;align-items:center;}
.nav-link{display:flex;align-items:center;gap:6px;padding:6px 12px;
          border-radius:6px;font-size:13px;color:var(--muted);text-decoration:none!important;
          border:0.5px solid transparent;transition:all .15s;}
.nav-link:hover{color:var(--text);background:rgba(255,255,255,0.06);text-decoration:none!important;}
.nav-link.active{color:var(--text);background:rgba(229,0,26,.12);border-color:rgba(229,0,26,.25);}
.nav-link svg{width:15px;height:15px;flex-shrink:0;}
.nav-divider{width:0.5px;height:18px;background:rgba(255,255,255,0.1);margin:0 4px;}
.nav-pill{font-size:10px;font-weight:600;background:var(--red);color:#fff;
          padding:1px 6px;border-radius:20px;margin-left:2px;}
.container{max-width:860px;margin:0 auto;padding:2rem;}
.card{background:var(--surface);border:1px solid var(--border);
      border-radius:var(--radius);padding:1.5rem;margin-bottom:1.25rem;}
.card-title{font-size:11px;font-weight:600;color:var(--muted);
            text-transform:uppercase;letter-spacing:.07em;margin-bottom:1rem;}
.badge{display:inline-flex;align-items:center;padding:3px 10px;
       border-radius:20px;font-size:12px;font-weight:500;}
.badge-current,.badge-processingorder{background:rgba(229,0,26,.15);color:#ff6b7a;border:1px solid var(--red-dim);}
.badge-visited{background:rgba(63,185,80,.12);color:var(--green);border:1px solid #2ea043;}
.badge-pending{background:var(--surface2);color:var(--muted);border:1px solid var(--border);}
.badge-delayed{background:rgba(210,153,34,.12);color:var(--amber);border:1px solid #9e6a03;}
.info-grid{display:grid;grid-template-columns:1fr 1fr;gap:.6rem 2rem;}
.info-row{display:flex;flex-direction:column;gap:2px;padding:.5rem 0;
          border-bottom:1px solid var(--border);}
.info-row:last-child{border:none;}
.info-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;}
.info-value{font-size:14px;font-weight:500;color:var(--text);}
.timeline{display:flex;flex-direction:column;}
.step-item{display:flex;align-items:flex-start;gap:14px;padding:.65rem 0;position:relative;}
.step-item:not(:last-child)::after{content:'';position:absolute;left:11px;top:30px;
  width:2px;height:calc(100% - 4px);background:var(--border);}
.step-dot{width:24px;height:24px;border-radius:50%;flex-shrink:0;z-index:1;
          display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;}
.dot-current{background:var(--red);box-shadow:0 0 0 4px rgba(229,0,26,.2);
             animation:pulse 2s ease-in-out infinite;}
.dot-visited{background:var(--green);}
.dot-pending{background:var(--surface2);border:2px solid var(--border);}
.step-name{font-weight:500;font-size:14px;}
.step-meta{font-size:12px;color:var(--muted);margin-top:1px;}
.route-item{display:flex;align-items:center;gap:12px;padding:.55rem 0;
            border-bottom:1px solid var(--border);}
.route-item:last-child{border:none;}
.route-icon{width:32px;height:32px;border-radius:6px;background:var(--surface2);
            border:1px solid var(--border);display:flex;align-items:center;
            justify-content:center;font-size:15px;flex-shrink:0;}
.route-name{font-weight:500;font-size:13px;}
.route-type{font-size:11px;color:var(--muted);}
.login-wrap{max-width:440px;margin:3rem auto;}
.login-wrap h1{font-size:22px;font-weight:600;margin-bottom:.4rem;}
.login-wrap .sub{color:var(--muted);font-size:13px;margin-bottom:1.25rem;}
.benefits{display:grid;grid-template-columns:1fr 1fr;gap:.6rem;margin-bottom:1.5rem;}
.benefit{background:var(--surface2);border:1px solid var(--border);
         border-radius:8px;padding:.85rem;}
.benefit-icon{font-size:16px;margin-bottom:4px;}
.benefit-title{font-size:12px;font-weight:500;color:var(--text);margin-bottom:2px;}
.benefit-desc{font-size:11px;color:var(--muted);line-height:1.5;}
.form-group{margin-bottom:1rem;}
.form-group label{display:block;font-size:12px;font-weight:500;color:var(--muted);
                  text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px;}
.form-group input{width:100%;background:var(--surface2);border:1px solid var(--border);
                  color:var(--text);padding:9px 12px;border-radius:8px;font-size:14px;
                  font-family:'Inter',sans-serif;outline:none;transition:border-color .15s;}
.form-group input:focus{border-color:var(--red);}
.btn{background:var(--red);color:#fff;border:none;padding:10px 20px;
     border-radius:8px;font-size:14px;font-weight:500;cursor:pointer;
     width:100%;font-family:'Inter',sans-serif;transition:opacity .15s;}
.btn:hover{opacity:.88;}
.privacy{background:var(--surface2);border:1px solid var(--border);
         border-radius:var(--radius);padding:1.2rem;margin-top:1.5rem;}
.privacy-title{font-size:11px;font-weight:600;color:var(--muted);
               text-transform:uppercase;letter-spacing:.07em;margin-bottom:.7rem;}
.privacy p{font-size:12px;color:var(--muted);line-height:1.7;margin-bottom:.5rem;}
.privacy p:last-child{margin-bottom:0;}
.privacy code{background:var(--bg);padding:1px 5px;border-radius:4px;
              font-size:11px;color:var(--text);}
.alert{background:rgba(229,0,26,.1);border:1px solid var(--red-dim);
       border-radius:var(--radius);padding:1rem;color:#ff6b7a;font-size:13px;}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
           gap:.75rem;margin-bottom:1.5rem;}
.stat-card{background:var(--surface);border:1px solid var(--border);
           border-radius:var(--radius);padding:1.1rem;}
.stat-num{font-size:28px;font-weight:600;color:var(--red);line-height:1;}
.stat-lbl{font-size:11px;color:var(--muted);margin-top:4px;}
.bar-row{margin:.5rem 0;}
.bar-head{display:flex;justify-content:space-between;font-size:12px;
          color:var(--muted);margin-bottom:4px;}
.bar-head span:last-child{font-weight:600;color:var(--text);}
.bar-bg{background:var(--surface2);border-radius:3px;height:8px;}
.bar-fill{background:var(--red);border-radius:3px;height:8px;}
.bar-fill-blue{background:#1f6feb;border-radius:3px;height:8px;}
.bar-sub{font-size:10px;color:#3d444d;margin-top:2px;}
.data-table{width:100%;border-collapse:collapse;}
.data-table th{font-size:11px;font-weight:600;color:var(--muted);text-align:left;
               padding:7px 10px;border-bottom:1px solid var(--border);
               text-transform:uppercase;letter-spacing:.05em;}
.data-table td{padding:7px 10px;border-bottom:1px solid var(--border);font-size:13px;}
.data-table tr:last-child td{border:none;}
.data-table tr:hover td{background:var(--surface2);}
.section-head{font-size:13px;font-weight:600;color:var(--text);
              margin-bottom:1rem;padding-bottom:.6rem;border-bottom:1px solid var(--border);}
@keyframes pulse{0%,100%{box-shadow:0 0 0 4px rgba(229,0,26,.2)}50%{box-shadow:0 0 0 8px rgba(229,0,26,.05)}}
</style>
</head>
<body>
<nav class="nav">
  <a class="nav-brand" href="/">
    <div class="nav-brand-icon">
      <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.5" aria-hidden="true">
        <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
      </svg>
    </div>
    <span class="nav-brand-name">Toyota Europe Tracker</span>
  </a>
  <div class="nav-links">
    <a href="/" class="nav-link {% if request.path == '/' %}active{% endif %}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
        <rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>
      </svg>
      Tracker
    </a>
    <a href="/stats" class="nav-link {% if request.path == '/stats' %}active{% endif %}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
        <line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/>
        <line x1="6" y1="20" x2="6" y2="14"/>
      </svg>
      Statistics
      <span class="nav-pill" id="nav-order-count"></span>
    </a>
    {% if username %}
    <div class="nav-divider"></div>
    <div id="nav-auto-check" style="display:none;align-items:center;gap:6px;
                                    background:rgba(31,111,235,0.12);
                                    border:1px solid rgba(31,111,235,0.25);
                                    border-radius:20px;padding:4px 12px;
                                    font-size:11px;font-weight:500;color:#58a6ff;">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           style="width:12px;height:12px;flex-shrink:0;" aria-hidden="true">
        <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
      </svg>
      <span id="next-check-label">—</span>
    </div>
    {% endif %}
    <div class="nav-divider"></div>
    <a href="https://github.com/Egyras/toyota-tracker" target="_blank" class="nav-link">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
        <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"/>
      </svg>
      Source
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:11px;height:11px;opacity:.5;" aria-hidden="true">
        <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
        <polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>
      </svg>
    </a>
  </div>
</nav>
<script>
fetch('/stats/count').then(r=>r.json()).then(d=>{
  var el=document.getElementById('nav-order-count');
  if(el&&d.total>0)el.textContent=d.total;
}).catch(()=>{});
</script>
"""

TRACKER_PAGE = BASE + """
<div class="container">
{% if not username %}
  <div class="login-wrap">
    <h1>Toyota Europe Order Tracker</h1>
    <p class="sub">Know exactly where your car is — from the factory floor in Japan to your dealer's door.</p>

    <div class="benefits">
      <div class="benefit">
        <div class="benefit-icon">📍</div>
        <div class="benefit-title">Live route map</div>
        <div class="benefit-desc">Every stop from Toyota City to your city on an interactive map</div>
      </div>
      <div class="benefit">
        <div class="benefit-icon">📅</div>
        <div class="benefit-title">Step timestamps</div>
        <div class="benefit-desc">Exact dates per stage — Toyota's app doesn't show these</div>
      </div>
      <div class="benefit">
        <div class="benefit-icon">📊</div>
        <div class="benefit-title">Community stats</div>
        <div class="benefit-desc">Compare wait times with other buyers across Europe</div>
      </div>
      <div class="benefit">
        <div class="benefit-icon">🚢</div>
        <div class="benefit-title">Vessel tracking</div>
        <div class="benefit-desc">Auto-detects your ship and shows it live on the map while at sea</div>
      </div>
    </div>

    <div class="card">
      <form method="POST" id="login-form">
        <div class="form-group">
          <label>Email address</label>
          <input type="email" name="username" id="inp-email" placeholder="your@email.com" required autofocus>
        </div>
        <div class="form-group">
          <label>Password</label>
          <input type="password" name="password" id="inp-password" required>
        </div>
        <button type="submit" class="btn">Check my order →</button>
        <div style="margin-top:1rem;display:flex;align-items:center;gap:8px;">
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer;
                        font-size:12px;color:var(--muted);">
            <input type="checkbox" id="auto-refresh-toggle"
                   style="width:15px;height:15px;accent-color:var(--red);cursor:pointer;">
            Auto-refresh every 2 hours while tab is open
          </label>
        </div>
        <div style="margin-top:10px;font-size:11px;color:var(--muted);
                    display:flex;align-items:center;gap:5px;">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               style="width:12px;height:12px;flex-shrink:0;color:#e3b341;">
            <circle cx="12" cy="12" r="10"/>
            <line x1="12" y1="8" x2="12" y2="12"/>
            <line x1="12" y1="16" x2="12.01" y2="16"/>
          </svg>
          Use your <strong style="color:var(--text);margin:0 2px;">My Toyota</strong> credentials
          — same as <a href="https://my.toyota.eu" target="_blank"
             style="color:var(--muted);text-decoration:underline;">my.toyota.eu</a>
        </div>
      </form>

      <script>
      // Restore saved email
      var savedEmail = sessionStorage.getItem('tr_email');
      if(savedEmail) document.getElementById('inp-email').value = savedEmail;

      // Restore auto-refresh preference
      var autoRefresh = localStorage.getItem('tr_auto_refresh') === '1';
      document.getElementById('auto-refresh-toggle').checked = autoRefresh;
      document.getElementById('auto-refresh-toggle').addEventListener('change', function(){
        localStorage.setItem('tr_auto_refresh', this.checked ? '1' : '0');
      });

      // On submit: save credentials to sessionStorage for auto-refresh
      document.getElementById('login-form').addEventListener('submit', function(){
        var email = document.getElementById('inp-email').value;
        var pass  = document.getElementById('inp-password').value;
        if(email) sessionStorage.setItem('tr_email', email);
        if(pass)  sessionStorage.setItem('tr_pass', pass);
      });

      // Auto-refresh: if credentials already saved and timer overdue, submit now
      (function(){
        var email = sessionStorage.getItem('tr_email');
        var pass  = sessionStorage.getItem('tr_pass');
        if(!email || !pass) return;
        if(localStorage.getItem('tr_auto_refresh') !== '1') return;
        var nextCheck = parseInt(sessionStorage.getItem('tr_next_check')||'0');
        if(nextCheck && nextCheck <= Date.now()){
          // Overdue — submit immediately
          document.getElementById('inp-email').value = email;
          document.getElementById('inp-password').value = pass;
          document.getElementById('login-form').submit();
        }
      })();
      </script>
    </div>

    <div class="privacy">
      <div class="privacy-title">🔒 How your credentials are handled</div>
      <p>Your credentials go directly to Toyota's API at <code>ssoms.toyota-europe.com</code>
         — never written to disk, never logged, never stored on our server.</p>
      <p>Only anonymized stats are saved: model, step, country, delay flag.
         No name, email, or order ID is stored. See
         <a href="https://github.com/Egyras/toyota-tracker/blob/main/web.py" target="_blank">
         save_stats()</a> in the source code.</p>
      <p>If you enable <strong>auto-refresh</strong>, your email and password are saved in your
         browser's <code>sessionStorage</code> — local to this tab only, never sent to our server,
         and automatically cleared when you close the tab.</p>
    </div>
  </div>

{% elif error %}
  <div class="login-wrap">
    <div class="alert">⚠ {{ error }}</div>
    <p style="margin-top:1rem;font-size:13px;"><a href="/">← Try again</a></p>
  </div>

{% else %}
  {% for order in orders %}
  {% set od = order.orderDetails %}
  {% set st = order.currentStatus %}
  {% set steps = order.preprocessed.steps if order.preprocessed else {} %}
  {% set delivs = order.intermediateDeliveries or [] %}
  {% set step_dates = order._step_dates if order._step_dates else {} %}

  <div style="display:flex;align-items:center;justify-content:space-between;
              margin-bottom:1.25rem;flex-wrap:wrap;gap:.5rem;">
    <div style="display:flex;align-items:center;gap:1.5rem;">
      {% if od.imageUrl %}
      <img src="{{ od.imageUrl }}" alt="{{ od.vehicleModel }}"
           style="height:80px;width:auto;object-fit:contain;border-radius:8px;">
      {% endif %}
      <div>
        <div style="font-size:20px;font-weight:600;">{{ od.vehicleModel }}</div>
        <div style="font-size:12px;color:var(--muted);margin-top:2px;">
          Order {{ od.orderId }}</div>
      </div>
    </div>
    <div style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;">
      {% if st.isDelayed %}<span class="badge badge-delayed">⚠ Delayed</span>{% endif %}
      {% if st.damageCode %}<span class="badge badge-delayed">⚡ {{ st.damageCode }}</span>{% endif %}
      <span class="badge badge-current">{{ st.currentStatus }}</span>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Vehicle details</div>
    <div class="info-grid">
      <div class="info-row">
        <span class="info-label">Engine</span>
        <span class="info-value">{{ od.engine or '—' }}</span>
      </div>
      <div class="info-row">
        <span class="info-label">Transmission</span>
        <span class="info-value">{{ od.transmission or '—' }}</span>
      </div>
      <div class="info-row">
        <span class="info-label">Colour</span>
        <span class="info-value">{{ od.vehicleExternalColor or '—' }}</span>
      </div>
      <div class="info-row">
        <span class="info-label">VIN</span>
        <span class="info-value">{{ od.vin or 'Not yet assigned' }}</span>
      </div>
      <div class="info-row">
        <span class="info-label">Est. delivery</span>
        <span class="info-value">{{ order.etaToFinalDestination or 'N/A' }}</span>
      </div>
      <div class="info-row">
        <span class="info-label">Order date</span>
        <span class="info-value">
          {% if order._created_on %}{{ order._created_on[:10] }}{% else %}—{% endif %}
        </span>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Order progress</div>
    <div class="timeline">
      {% for step_name, step_data in steps.items() %}
      {% set s = step_data.status %}
      <div class="step-item">
        <div class="step-dot
          {% if s == 'current' %}dot-current{% elif s == 'visited' %}dot-visited{% else %}dot-pending{% endif %}">
          {% if s == 'visited' %}✓{% elif s == 'current' %}●{% endif %}
        </div>
        <div style="flex:1;padding-top:2px;">
          <div class="step-name">{{ step_name }}</div>
          {% if step_data.location %}
          <div class="step-meta">{{ step_data.location }}</div>
          {% endif %}
          <span class="badge badge-{{ s }}" style="margin-top:5px;">{{ s }}</span>
          {% if step_name in step_dates %}
            {% set days_gap = (order._days_tracked // (order._logins - 1 if order._logins > 1 else 1)) if order._logins > 1 else 99 %}
            {% if order._logins == 1 %}
              {% set rel_icon = '⚠️' %}
              {% set rel_label = 'Estimated' %}
              {% set rel_desc = 'First login — shows when you checked, not when step happened' %}
              {% set rel_bg = 'rgba(139,148,158,0.15)' %}
              {% set rel_border = 'rgba(139,148,158,0.3)' %}
              {% set rel_color = 'var(--muted)' %}
              {% set rel_accurate = false %}
            {% elif days_gap <= 1 %}
              {% set rel_icon = '✓' %}
              {% set rel_label = 'Accurate' %}
              {% set rel_desc = 'Logged in daily — date is reliable' %}
              {% set rel_bg = 'rgba(63,185,80,0.1)' %}
              {% set rel_border = 'rgba(63,185,80,0.3)' %}
              {% set rel_color = 'var(--green)' %}
              {% set rel_accurate = true %}
            {% elif days_gap <= 3 %}
              {% set rel_icon = '~' %}
              {% set rel_label = '±' ~ days_gap ~ ' days' %}
              {% set rel_desc = 'Checked every ' ~ days_gap ~ ' days — date may be slightly off' %}
              {% set rel_bg = 'rgba(227,179,65,0.1)' %}
              {% set rel_border = 'rgba(227,179,65,0.3)' %}
              {% set rel_color = '#e3b341' %}
              {% set rel_accurate = false %}
            {% else %}
              {% set rel_icon = '?' %}
              {% set rel_label = 'Rough estimate' %}
              {% set rel_desc = 'Checked infrequently — date could be off by ' ~ days_gap ~ '+ days' %}
              {% set rel_bg = 'rgba(229,0,26,0.08)' %}
              {% set rel_border = 'rgba(229,0,26,0.25)' %}
              {% set rel_color = 'var(--red)' %}
              {% set rel_accurate = false %}
            {% endif %}
            {% for event, date in step_dates[step_name].items() %}
            <div style="display:flex;align-items:center;gap:8px;margin-top:6px;flex-wrap:wrap;">
              <div style="font-size:12px;color:var(--red);font-weight:500;">{{ event }}: {{ date }}</div>
              <div title="{{ rel_desc }}"
                   style="display:inline-flex;align-items:center;gap:4px;
                          background:{{ rel_bg }};border:1px solid {{ rel_border }};
                          border-radius:20px;padding:2px 8px;cursor:help;
                          font-size:10px;font-weight:600;color:{{ rel_color }};
                          letter-spacing:.03em;white-space:nowrap;">
                <span>{{ rel_icon }}</span>
                <span>{{ rel_label }}</span>
              </div>
            </div>
            {% endfor %}
            {% if not rel_accurate %}
            <div style="margin-top:8px;padding:8px 10px;
                        background:rgba(229,0,26,0.06);
                        border:1px solid rgba(229,0,26,0.2);
                        border-radius:6px;font-size:11px;color:var(--muted);
                        line-height:1.5;">
              📅 For accurate vessel detection, enter the date from your
              <strong style="color:var(--text);">Toyota notification email</strong>
              in the <strong style="color:var(--text);">Delivery Route</strong> section below and click
              <strong style="color:var(--red);">🔍 Detect</strong>
            </div>
            {% endif %}
          {% endif %}
        </div>
      </div>
      {% endfor %}
    </div>
  </div>

  {% if delivs %}
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <div class="card">
    <div class="card-title">Delivery route</div>

    <div id="route-map" style="height:280px;border-radius:8px;margin-bottom:1.25rem;
         border:1px solid var(--border);overflow:hidden;"></div>

    <!-- Vessel tracking card (shown when vessel detected) -->
    <div id="vessel-info" style="display:none;background:var(--surface2);border:1px solid var(--border);
         border-radius:8px;padding:.85rem;margin-bottom:1.25rem;">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:.5rem;">
        <div>
          <div style="font-size:11px;color:var(--muted);text-transform:uppercase;
                      letter-spacing:.05em;margin-bottom:3px;">🚢 Vessel detected</div>
          <div style="font-size:14px;font-weight:600;" id="vessel-name">—</div>
          <div style="font-size:12px;color:var(--muted);margin-top:2px;">
            Speed: <span id="vessel-speed">—</span>
            &nbsp;·&nbsp; Dest: <span id="vessel-dest">—</span>
          </div>
        </div>
        <div style="text-align:right;">
          <div style="font-size:11px;color:var(--muted);">MMSI: <span id="vessel-mmsi">—</span></div>
          <div style="font-size:10px;color:#3d444d;margin-top:2px;">Based on departure timing</div>
        </div>
      </div>
      <!-- External tracking links -->
      <div style="display:flex;gap:.5rem;margin-top:.75rem;flex-wrap:wrap;" id="vessel-links"></div>
    </div>

    <!-- Vessel auto-detection runs automatically, inline controls below for manual override -->

    <!-- Vessel date prompt — shown when login frequency is too low for reliable auto-detection -->
    <div id="vessel-date-prompt" style="display:none;margin-bottom:1.25rem;padding:10px 14px;
         background:rgba(229,0,26,0.06);border:1px solid rgba(229,0,26,0.2);
         border-radius:8px;font-size:12px;line-height:1.6;">
      <strong style="color:var(--text);">🚢 Vessel detection needs your help</strong><br>
      <span style="color:var(--muted);">
        Your login frequency is too low to reliably detect the vessel automatically —
        the date we have may be off by several days, leading to the wrong ship.<br>
        <strong style="color:var(--text);">Enter the exact date from your Toyota notification email</strong>
        in the <strong style="color:var(--text);">Factory departure</strong> field below and click
        <strong style="color:var(--red);">🔍 Detect</strong> for accurate results.
      </span>
    </div>

    <!-- MST history limit warning — shown when leftTheFactory date is >25 days ago -->
    {% set lf_date = order._step_dates.leftTheFactory.current if order._step_dates and order._step_dates.leftTheFactory else '' %}
    {% if lf_date %}
    <div id="mst-limit-warning" style="display:none;margin-bottom:1.25rem;padding:10px 12px;
         background:rgba(227,179,65,0.08);border:1px solid rgba(227,179,65,0.3);
         border-radius:8px;font-size:12px;color:#e3b341;line-height:1.6;">
      <strong>⚠️ Vessel detection may not work</strong><br>
      <span style="color:var(--muted);">
        Port departure records are only available for the last ~20 days.
        Your car left the factory <span id="days-since-factory"></span> days ago —
        the Nagoya departure data may no longer be accessible.<br>
        <strong style="color:var(--text);">To track your vessel:</strong>
        enter the departure date from your Toyota email below and click 🔍 Detect,
        or enter the MMSI directly if you already found the ship on
        <a href="https://www.myshiptracking.com" target="_blank" style="color:#e3b341;">MyShipTracking</a>.
      </span>
    </div>
    <script>
    (function(){
      var lf = '{{ lf_date }}';
      if(!lf) return;
      var days = Math.floor((Date.now() - new Date(lf).getTime()) / 86400000);
      document.getElementById('days-since-factory').textContent = days;
      if(days > 20) {
        document.getElementById('mst-limit-warning').style.display = 'block';
      }
    })();
    </script>
    {% endif %}

    {% for d in delivs %}
    {% set v = d.isVisited %}
    {% set is_vessel = d.transportMethod == 'Vessel' or d.destinationType in ['FACTORY','HUB'] %}
    {% set leg_key = 'nagoya' if d.destinationType == 'FACTORY' else
                     'zeebrugge' if 'Zeebrugge' in d.locationName else
                     'malmo' if 'Malmo' in d.locationName or 'Malmö' in d.locationName else
                     'bremerhaven' if 'Bremerhaven' in d.locationName else
                     'southampton' if 'Southampton' in d.locationName else
                     'gothenburg' if 'Gothenburg' in d.locationName else 'nagoya' %}
    <div class="route-item" style="flex-direction:column;align-items:stretch;gap:0;">
      <div style="display:flex;align-items:center;gap:12px;padding:2px 0;">
        <div class="route-icon">
          {% if d.destinationType == 'FACTORY' %}🏭
          {% elif d.destinationType == 'HUB' %}🔀
          {% elif d.destinationType == 'TRANSIT' %}🚢
          {% elif d.destinationType == 'DESTINATION' %}📍
          {% else %}📦{% endif %}
        </div>
        <div style="flex:1;">
          <div class="route-name">{{ d.locationName }}, {{ d.countryName }}</div>
          <div class="route-type">{{ d.destinationType }}
            {% if d.transportMethod %} · {{ d.transportMethod }}{% endif %}</div>
        </div>
        <span class="badge
          {% if v == 'visited' %}badge-visited
          {% elif v == 'inTransit' %}badge-current
          {% else %}badge-pending{% endif %}">{{ v }}</span>
      </div>
      {% if is_vessel and v in ['inTransit', 'notVisited'] %}
      {% set step_date = order._step_dates.leftTheFactory if leg_key == 'nagoya' else
                         order._step_dates.get(leg_key, {}) if order._step_dates else {} %}
      {% set days_gap = (order._days_tracked // (order._logins - 1 if order._logins > 1 else 1)) if order._logins > 1 else 99 %}
      {% set date_reliable = order._logins >= 2 and days_gap <= 3 %}
      <div style="margin:6px 0 2px 44px;">
        <div style="display:flex;gap:6px;flex-wrap:wrap;">
          <input type="date" id="date-{{ leg_key }}"
                 placeholder="Departure date"
                 style="flex:1;min-width:120px;background:var(--bg);border:1px solid var(--border);
                        color:var(--text);padding:6px 10px;border-radius:6px;
                        font-size:12px;font-family:'Inter',sans-serif;outline:none;"
                 onfocus="this.style.borderColor='var(--red)'"
                 onblur="this.style.borderColor='var(--border)'"
                 title="Enter the date Toyota notified you the car departed this location">
          <input type="text" id="mmsi-{{ leg_key }}"
                 placeholder="MMSI (optional)"
                 style="flex:1;min-width:110px;background:var(--bg);border:1px solid var(--border);
                        color:var(--text);padding:6px 10px;border-radius:6px;
                        font-size:12px;font-family:'Inter',sans-serif;outline:none;"
                 onfocus="this.style.borderColor='var(--red)'"
                 onblur="this.style.borderColor='var(--border)'"
                 title="Enter vessel MMSI if known">
          <button onclick="detectLeg('{{ leg_key }}','{{ order._order_hash }}','{{ od.orderId }}')"
                  style="background:var(--surface2);color:var(--text);border:1px solid var(--border);
                         padding:6px 12px;border-radius:6px;font-size:12px;cursor:pointer;
                         white-space:nowrap;"
                  onmouseover="this.style.background='var(--surface)'"
                  onmouseout="this.style.background='var(--surface2)'">
            🔍 Detect
          </button>
        </div>
        {% if not date_reliable %}
        <div style="margin-top:5px;font-size:11px;color:var(--muted);
                    padding:4px 8px;background:rgba(229,0,26,0.06);
                    border-left:2px solid rgba(229,0,26,0.3);border-radius:0 4px 4px 0;">
          📧 For accurate detection enter the date from your <strong style="color:var(--text);">Toyota notification email</strong>
          {% if order._logins == 1 %}— we only have your first login date which may be days off{% endif %}
        </div>
        {% endif %}
      </div>
      {% endif %}
    </div>
    {% endfor %}
  </div>
  <script>
  // Pre-fill saved values — from DB (vessel_overrides) first, then localStorage fallback
  (function(){
    var orderId   = '{{ od.orderId }}';
    var orderHash = '{{ order._order_hash }}';
    var legs = ['nagoya','zeebrugge','malmo','bremerhaven','southampton','gothenburg','sagunto','livorno','piraeus','drammen'];

    function applyValues(leg, date, mmsi) {
      var di = document.getElementById('date-'+leg);
      var mi = document.getElementById('mmsi-'+leg);
      if(di && date && !di.value) { di.value = date; di.style.borderColor='rgba(229,0,26,0.3)'; }
      if(mi && mmsi && !mi.value) mi.value = mmsi;
    }

    // Step 1: Load from DB (works across devices)
    fetch('/api/vessel-overrides/'+orderHash)
      .then(function(r){ return r.json(); })
      .then(function(data){
        legs.forEach(function(leg){
          var d = data[leg];
          if(d) applyValues(leg, d.depart_date, d.mmsi||d.detected_mmsi);
        });
      })
      .catch(function(){})
      .finally(function(){
        // Step 2: localStorage fills any remaining gaps
        legs.forEach(function(leg){
          var savedDate = localStorage.getItem('depart_date_'+orderId+'_'+leg);
          var savedMMSI = localStorage.getItem('vessel_mmsi_'+orderId+'_'+leg);
          applyValues(leg, savedDate, savedMMSI);
        });
        // Step 3: Estimate nagoya date if still empty
        var di = document.getElementById('date-nagoya');
        if(di && !di.value) {
          var lfDate = '{{ order._step_dates.leftTheFactory.current if order._step_dates and order._step_dates.leftTheFactory else "" }}';
          if(lfDate){
            var d = new Date(lfDate);
            d.setDate(d.getDate() - 2);
            di.value = d.toISOString().slice(0,10);
            di.style.borderColor = 'rgba(229,0,26,0.4)';
            di.title = 'Estimated: 2 days before first login at leftTheFactory ('+lfDate+'). Correct if you know the real date.';
          }
        }
      });
  })();

  function detectLeg(leg, orderHash, orderId) {
    var date = (document.getElementById('date-'+leg)||{}).value||'';
    var mmsi = (document.getElementById('mmsi-'+leg)||{}).value||'';
    if(!date && !mmsi) { alert('Enter a departure date or MMSI to detect the vessel.'); return; }
    // Save to localStorage
    if(date) localStorage.setItem('depart_date_'+orderId+'_'+leg, date);
    if(mmsi) localStorage.setItem('vessel_mmsi_'+orderId+'_'+leg, mmsi);
    // If MMSI provided directly, use vessel API
    if(mmsi) {
      localStorage.setItem('vessel_mmsi_'+orderId, mmsi); // legacy key
      fetch('/api/vessel/'+mmsi).then(r=>r.json()).then(d=>{
        if(d.lat) { alert('Tracking '+( d.name||mmsi)+' at '+d.lat+', '+d.lon); location.reload(); }
        else alert('No position data for MMSI '+mmsi);
      });
      return;
    }
    // Use date-based detection
    var url = '/api/vessel-detect/'+orderHash+'?depart_date='+date+'&leg='+leg;
    fetch(url).then(r=>r.json()).then(d=>{
      if(d.mmsi||d.lat) {
        alert('✅ Vessel: '+(d.name||d.mmsi)+'\nPosition: '+d.lat+', '+d.lon+'\nSource: '+(d.source||'cache'));
        location.reload();
      } else {
        alert('❌ No Toyota carrier found for '+leg+' around '+date+'.\nTry adjusting the date by ±1-2 days.');
      }
    }).catch(function(){ alert('Detection failed. Try again.'); });
  }
  </script>

  <script>
  (function(){
    var stops = [
      {% for d in delivs %}
      {% if d.locationLatitude and d.locationLongitude %}
      {lat:{{ d.locationLatitude }},lng:{{ d.locationLongitude }},
       name:"{{ (d.locationName or '') | replace('"','\\"') }}, {{ d.countryName or '' }}",
       type:"{{ d.destinationType or '' }}",visited:"{{ d.isVisited }}"},
      {% endif %}
      {% endfor %}
    ];
    var map = L.map('route-map',{zoomControl:true,scrollWheelZoom:false});
    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{
      attribution:'&copy; OpenStreetMap &copy; CARTO',maxZoom:18
    }).addTo(map);
    var latlngs = stops.map(function(s){return[s.lat,s.lng];});
    if(latlngs.length > 1){
      L.polyline(latlngs,{color:'#e5001a',weight:2,dashArray:'6 6',opacity:.7}).addTo(map);
    }
    stops.forEach(function(s){
      var color = s.visited==='visited'?'#3fb950':s.visited==='inTransit'?'#e5001a':'#8b949e';
      var icon = L.divIcon({
        className:'',
        html:'<div style="width:14px;height:14px;border-radius:50%;background:'+color+';border:2px solid #fff;box-shadow:0 0 4px rgba(0,0,0,.5);"></div>',
        iconSize:[14,14],iconAnchor:[7,7]
      });
      L.marker([s.lat,s.lng],{icon:icon}).addTo(map)
       .bindPopup('<b>'+s.name+'</b><br>'+s.type);
    });
    if(latlngs.length > 0){
      map.fitBounds(latlngs,{padding:[30,30]});
    } else {
      map.setView([50,10],4);
    }

    // Show vessel only while car is actively at sea
    // Stop tracking once car left port depot (now on truck to dealer)
    {% set show_vessel = order.currentStatus.currentStatus in ['LeftTheFactory','leftTheFactory','InTransit','inTransit'] or order._vessel_mmsi %}
    {% if order.currentStatus.currentStatus in ['LeftTheDepot','leftTheDepot','ArrivedAtRetailer','arrivedAtRetailer','ArrivedInDestination','arrivedInDestination'] %}
      {% set show_vessel = false %}
    {% endif %}
    {% if show_vessel %}
    var vesselMarker = null;
    function loadVessel(mmsi, name, lat, lng, speed, course, dest, eta) {
      if (vesselMarker) map.removeLayer(vesselMarker);
      var icon = L.divIcon({
        className:'',
        html:'<div style="position:relative;text-align:center;">' +
             '<div style="font-size:24px;filter:drop-shadow(0 0 6px rgba(229,0,26,0.8));">🚢</div>' +
             '<div style="position:absolute;top:26px;left:50%;transform:translateX(-50%);' +
             'background:rgba(0,0,0,0.75);color:#fff;font-size:9px;font-weight:600;' +
             'padding:1px 5px;border-radius:3px;white-space:nowrap;letter-spacing:.03em;' +
             'border:1px solid rgba(229,0,26,0.5);">'+name+'</div>' +
             '</div>',
        iconSize:[80,40],iconAnchor:[40,12]
      });
      vesselMarker = L.marker([lat,lng],{icon:icon,zIndexOffset:1000}).addTo(map)
        .bindPopup(
          '<b>'+name+'</b><br>'+
          'Speed: '+speed+' kn'+((course!==null&&course!==undefined&&course!==0&&course!=='')?' · Course: '+course+'°':'')+'<br>'+
          (dest?'Dest: '+dest+'<br>':'')+
          (eta?'ETA: '+eta+'<br>':'')+
          '<small style="color:#aaa">MMSI: '+mmsi+'</small>'
        );
      // Extend map bounds to include vessel
      var bounds = latlngs.length > 0 ? L.latLngBounds(latlngs) : L.latLngBounds([[lat,lng],[lat,lng]]);
      bounds.extend([lat, lng]);
      map.fitBounds(bounds, {padding:[30,30]});
      // Add pulsing circle around vessel
      L.circle([lat,lng],{
        radius:200000,color:'#e5001a',fillColor:'#e5001a',
        fillOpacity:0.05,weight:1,dashArray:'4 4'
      }).addTo(map);
      // Show vessel info card
      var card = document.getElementById('vessel-info');
      if(card){
        document.getElementById('vessel-name').textContent = name;
        document.getElementById('vessel-speed').textContent = speed+' knots';
        document.getElementById('vessel-dest').textContent = dest||'—';
        document.getElementById('vessel-mmsi').textContent = mmsi;
        card.style.display='block';
      }
    }

    // Auto-detect vessel — only if date is reliable or vessel already known
    var savedMMSI = localStorage.getItem('vessel_mmsi_{{ od.orderId }}');
    var hash = "{{ order._order_hash if order._order_hash else '' }}";
    var hasUserDate = false;
    var hasKnownVessel = {{ 'true' if order._vessel_mmsi else 'false' }};
    var loginGap = {{ ((order._days_tracked / (order._logins - 1 if order._logins > 1 else 1))|round(1)) if order._logins > 1 else 99 }};
    var logins = {{ order._logins }};

    // Check if user entered a date in vessel_overrides (reliable)
    fetch('/api/vessel-overrides/'+hash)
      .then(r=>r.json())
      .then(function(overrides){
        var leg = 'nagoya';
        {% for d in delivs %}
        {% if d.isVisited == 'inTransit' %}
          {% set loc = d.locationName | lower %}
          {% if 'zeebrugge' in loc %}leg = 'zeebrugge';
          {% elif 'malmo' in loc or 'malmö' in loc %}leg = 'malmo';
          {% elif 'sagunto' in loc %}leg = 'sagunto';
          {% elif 'livorno' in loc %}leg = 'livorno';
          {% elif 'bristol' in loc or 'portbury' in loc %}leg = 'portbury';
          {% elif 'southampton' in loc %}leg = 'southampton';
          {% elif 'drammen' in loc %}leg = 'drammen';
          {% elif 'piraeus' in loc %}leg = 'piraeus';
          {% endif %}
        {% endif %}
        {% endfor %}

        var legOverride = overrides[leg];
        hasUserDate = !!(legOverride && legOverride.depart_date);
        // hasKnownVessel from DB takes priority over localStorage
        var dateReliable = hasUserDate || hasKnownVessel || (logins >= 2 && loginGap <= 3);

        if(hash){
          // Always use API — it handles leg-aware cache correctly
          // Never use localStorage MMSI directly as it may be stale/wrong leg
          fetch('/api/vessel-detect/'+hash+'?leg='+leg)
            .then(r=>r.json())
            .then(d=>{ if(d.lat) loadVessel(d.mmsi,d.name,d.lat,d.lon,d.speed,d.course,d.destination,d.eta); })
            .catch(()=>{});
          // Show prompt if date unreliable and no vessel known
          if(!dateReliable){
            var prompt = document.getElementById('vessel-date-prompt');
            if(prompt) prompt.style.display = 'block';
          }
        }
      })
      .catch(function(){
        // Fallback — try detection anyway
        if(hash){
          fetch('/api/vessel-detect/'+hash)
            .then(r=>r.json())
            .then(d=>{ if(d.lat) loadVessel(d.mmsi,d.name,d.lat,d.lon,d.speed,d.course,d.destination,d.eta); })
            .catch(()=>{});
        }
      });
    {% endif %}
  })();
  </script>
  {% endif %}
  {% endfor %}

  <!-- bottom spacer -->
  <script>
  // Check if status changed since last auto-refresh
  (function(){
    var lastStatus = sessionStorage.getItem('tr_last_status');
    var currentStatus = "{{ orders[0].currentStatus.currentStatus if orders else '' }}";
    if(lastStatus && currentStatus && lastStatus !== currentStatus){
      var banner = document.createElement('div');
      banner.style.cssText = 'position:fixed;top:60px;left:50%;transform:translateX(-50%);' +
        'background:#1f6feb;color:#fff;padding:10px 20px;border-radius:8px;font-size:13px;' +
        'font-weight:500;z-index:9999;box-shadow:0 4px 12px rgba(0,0,0,.4);';
      banner.textContent = '🔔 Status updated: ' + lastStatus + ' → ' + currentStatus;
      document.body.appendChild(banner);
      setTimeout(function(){ banner.style.opacity='0'; banner.style.transition='opacity .5s'; setTimeout(function(){banner.remove();},500); }, 5000);
    }
    if(currentStatus) sessionStorage.setItem('tr_last_status', currentStatus);
  })();

  // Resume auto-refresh countdown on results page too
  (function(){
    if(localStorage.getItem('tr_auto_refresh') !== '1') return;
    var INTERVAL = 2 * 60 * 60 * 1000;
    var nextCheck = parseInt(sessionStorage.getItem('tr_next_check')||'0');
    var now = Date.now();
    if(!nextCheck || nextCheck <= now){
      nextCheck = now + INTERVAL;
      sessionStorage.setItem('tr_next_check', nextCheck);
    }
    var container = document.getElementById('nav-auto-check');
    var label = document.getElementById('next-check-label');
    if(container) container.style.display = 'flex';

    function updateCountdown(){
      var r = parseInt(sessionStorage.getItem('tr_next_check')||'0') - Date.now();
      if(!label) return;
      if(r <= 0){
        label.textContent = 'Refreshing...';
        var email = sessionStorage.getItem('tr_email');
        var pass  = sessionStorage.getItem('tr_pass');
        if(email && pass){
          var f = document.createElement('form');
          f.method='POST'; f.action='/';
          var u=document.createElement('input'); u.name='username'; u.value=email; f.appendChild(u);
          var p=document.createElement('input'); p.name='password'; p.value=pass; f.appendChild(p);
          document.body.appendChild(f);
          sessionStorage.setItem('tr_next_check', Date.now()+INTERVAL);
          f.submit();
        }
        return;
      }
      var h=Math.floor(r/3600000), m=Math.floor((r%3600000)/60000);
      label.textContent = (h>0?h+'h ':'') + m + 'min';
    }
    updateCountdown();
    setInterval(updateCountdown, 30000);
  })();
  </script>
{% endif %}
</div></body></html>
"""

STATS_PAGE = BASE + """
<div class="container">
  <div style="margin-bottom:1.5rem;">
    <div style="font-size:20px;font-weight:600;">Toyota Europe — Global Statistics</div>
    <div style="font-size:12px;color:var(--muted);margin-top:3px;">
      Anonymized · no credentials or personal info stored
    </div>
  </div>

  <div class="stat-grid">
    <div class="stat-card"><div class="stat-num">{{ total }}</div>
      <div class="stat-lbl">Unique orders</div></div>
    <div class="stat-card"><div class="stat-num">{{ countries }}</div>
      <div class="stat-lbl">Countries</div></div>
    <div class="stat-card"><div class="stat-num">{{ delayed }}</div>
      <div class="stat-lbl">Delayed</div></div>
    <div class="stat-card"><div class="stat-num">{{ damaged }}</div>
      <div class="stat-lbl">Damage codes</div></div>
    <div class="stat-card"><div class="stat-num">{{ pct_delayed }}%</div>
      <div class="stat-lbl">Delay rate</div></div>
  </div>

  <div class="card">
    <div class="section-head">⏱ How long does each step take?</div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:1rem;">
      Only counts orders where we observed both the start and end of a step.
      <span style="display:inline-flex;align-items:center;gap:10px;margin-left:8px;">
        <span style="display:inline-flex;align-items:center;gap:3px;">
          <span style="background:rgba(63,185,80,0.15);border:1px solid rgba(63,185,80,0.3);
                       border-radius:10px;padding:1px 7px;font-size:10px;color:#3fb950;">✓ Observed</span>
          = transition witnessed across logins
        </span>
      </span>
    </div>

    {% if order_to_build and order_to_build['samples'] > 0 %}
    <div style="background:var(--surface2);border:1px solid var(--border);
                border-radius:8px;padding:.85rem;margin-bottom:1rem;">
      <div style="font-size:11px;color:var(--muted);text-transform:uppercase;
                  letter-spacing:.05em;margin-bottom:.4rem;">
        📦 Order placed → Production started
      </div>
      <div style="font-size:20px;font-weight:600;color:var(--text);">
        ~{{ order_to_build['avg_days'] }} days
        <span style="font-size:12px;color:var(--muted);font-weight:400;">
          &nbsp;min {{ order_to_build['min_days'] }} / max {{ order_to_build['max_days'] }}
          · {{ order_to_build['samples'] }} orders
        </span>
        {% if order_to_build['samples'] >= 5 %}
        <span style="background:rgba(63,185,80,0.15);border:1px solid rgba(63,185,80,0.3);
                     border-radius:10px;padding:2px 8px;font-size:10px;color:#3fb950;
                     font-weight:500;vertical-align:middle;">✓ Reliable</span>
        {% elif order_to_build['samples'] >= 2 %}
        <span style="background:rgba(227,179,65,0.15);border:1px solid rgba(227,179,65,0.3);
                     border-radius:10px;padding:2px 8px;font-size:10px;color:#e3b341;
                     font-weight:500;vertical-align:middle;">~ Early data</span>
        {% else %}
        <span style="background:rgba(229,0,26,0.08);border:1px solid rgba(229,0,26,0.25);
                     border-radius:10px;padding:2px 8px;font-size:10px;color:var(--red);
                     font-weight:500;vertical-align:middle;">⚠ 1 sample</span>
        {% endif %}
      </div>
      <div style="font-size:10px;color:var(--muted);margin-top:6px;">
        Order date from Toyota API (accurate) · Build start date depends on login frequency
      </div>
    </div>
    {% endif %}

    {% if step_avgs %}
      {% set max_avg = namespace(v=1) %}
      {% for r in step_avgs %}{% if r['avg_days'] > max_avg.v %}{% set max_avg.v = r['avg_days'] %}{% endif %}{% endfor %}
      {% for r in step_avgs %}
      {% set pct = ((r['avg_days'] / max_avg.v) * 100)|int %}
      {% set samples = r['samples'] %}
      {% if samples >= 5 %}
        {% set rel_label = '✓ Reliable' %}
        {% set rel_bg = 'rgba(63,185,80,0.15)' %}
        {% set rel_border = 'rgba(63,185,80,0.3)' %}
        {% set rel_color = '#3fb950' %}
      {% elif samples >= 2 %}
        {% set rel_label = '~ Early data' %}
        {% set rel_bg = 'rgba(227,179,65,0.15)' %}
        {% set rel_border = 'rgba(227,179,65,0.3)' %}
        {% set rel_color = '#e3b341' %}
      {% else %}
        {% set rel_label = '⚠ 1 sample' %}
        {% set rel_bg = 'rgba(229,0,26,0.08)' %}
        {% set rel_border = 'rgba(229,0,26,0.25)' %}
        {% set rel_color = 'var(--red)' %}
      {% endif %}
      <div class="bar-row">
        <div class="bar-head">
          <div style="display:flex;align-items:center;gap:8px;">
            <span>{{ r['step'] }}</span>
            {% if r['route'] %}
            <span style="background:rgba(139,148,158,0.15);border:1px solid rgba(139,148,158,0.3);
                         border-radius:10px;padding:1px 6px;font-size:9px;color:var(--muted);
                         font-weight:500;">{{ r['route'] }}</span>
            {% endif %}
            <span style="background:{{ rel_bg }};border:1px solid {{ rel_border }};
                         border-radius:10px;padding:1px 7px;font-size:10px;
                         color:{{ rel_color }};font-weight:500;white-space:nowrap;">{{ rel_label }}</span>
          </div>
          <span>~{{ r['avg_days'] }} days
            <span style="color:var(--muted);font-weight:400;font-size:11px;">
              min {{ r['min_days'] }} / max {{ r['max_days'] }} · {{ r['samples'] }} orders
            </span>
          </span>
        </div>
        <div class="bar-bg"><div class="bar-fill-blue" style="width:{{ pct }}%"></div></div>
      </div>
      {% endfor %}
      {% if step_current %}
      <div style="margin-top:1.25rem;padding-top:1rem;border-top:1px solid var(--border);">
        <div style="font-size:11px;color:var(--muted);margin-bottom:.75rem;
                    text-transform:uppercase;letter-spacing:.05em;">Currently in progress</div>
        <table class="data-table">
          <tr><th>Step</th><th>Orders</th><th>Days so far</th><th>Earliest seen</th></tr>
          {% for r in step_current %}
          <tr>
            <td>{{ r['step'] }}</td>
            <td style="color:var(--muted);">{{ r['order_count'] }}</td>
            <td><span class="badge badge-current">{{ r['days_so_far'] }}d</span></td>
            <td style="color:var(--muted);">{{ r['earliest'] }}</td>
          </tr>
          {% endfor %}
        </table>
      </div>
      {% endif %}
    {% else %}
      <p style="color:var(--muted);font-size:13px;">No duration data yet — check back as more users log in regularly.</p>
    {% endif %}

    {% if step_current and not step_avgs %}
    <div style="margin-top:.5rem;padding-top:1rem;border-top:1px solid var(--border);">
      <div style="font-size:11px;color:var(--muted);margin-bottom:.75rem;
                  text-transform:uppercase;letter-spacing:.05em;">Currently in progress</div>
      <table class="data-table">
        <tr><th>Step</th><th>Orders</th><th>Days so far</th><th>Earliest seen</th></tr>
        {% for r in step_current %}
        <tr>
          <td>{{ r['step'] }}</td>
          <td style="color:var(--muted);">{{ r['order_count'] }}</td>
          <td><span class="badge badge-current">{{ r['days_so_far'] }}d</span></td>
          <td style="color:var(--muted);">{{ r['earliest'] }}</td>
        </tr>
        {% endfor %}
      </table>
    </div>
    {% endif %}

    <div style="margin-top:1rem;padding:8px 12px;background:rgba(139,148,158,0.08);
                border-radius:6px;font-size:11px;color:var(--muted);line-height:1.6;">
      📊 Statistics improve as more users log in frequently.
      Reliability increases with sample count:
      <span style="color:#e3b341;">~ Early data</span> = 2-4 orders ·
      <span style="color:#3fb950;">✓ Reliable</span> = 5+ orders
    </div>
  </div>

  <div class="card">
    <div class="section-head">🚢 Port-to-port leg durations</div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:1rem;">
      How long each shipping leg takes — based on orders where we observed both departure and arrival.
    </div>
    {% if hub_leg_stats %}
    <table class="data-table">
      <tr>
        <th>Leg</th>
        <th>Avg days</th>
        <th>Min / Max</th>
        <th>Orders</th>
      </tr>
      {% for r in hub_leg_stats %}
      {% if r['avg_days'] %}
      <tr>
        <td>
          <span style="font-weight:500;">{{ r['from_hub'] }}</span>
          <span style="color:var(--muted);margin:0 4px;">→</span>
          <span style="font-weight:500;">{{ r['to_hub'] }}</span>
          <span style="font-size:10px;color:var(--muted);margin-left:6px;">
            {% if 'nagoya' in r['leg_key'] %}🌊 deep sea
            {% elif r['from_hub'] in ['Zeebrugge','Bremerhaven','Southampton','Sagunto','Livorno'] %}⚓ feeder
            {% else %}🚛 feeder{% endif %}
          </span>
        </td>
        <td style="font-weight:600;color:var(--text);">~{{ r['avg_days'] }} days</td>
        <td style="color:var(--muted);font-size:12px;">{{ r['min_days'] }} / {{ r['max_days'] }}</td>
        <td>
          <span style="font-size:11px;color:var(--muted);">{{ r['samples'] }}</span>
          {% if r['observed_count'] >= 5 %}
          <span style="background:rgba(63,185,80,0.15);border:1px solid rgba(63,185,80,0.3);
                       border-radius:10px;padding:1px 6px;font-size:9px;color:#3fb950;
                       font-weight:500;margin-left:4px;">✓ Reliable</span>
          {% elif r['observed_count'] >= 2 %}
          <span style="background:rgba(227,179,65,0.15);border:1px solid rgba(227,179,65,0.3);
                       border-radius:10px;padding:1px 6px;font-size:9px;color:#e3b341;
                       font-weight:500;margin-left:4px;">~ Early data</span>
          {% else %}
          <span style="background:rgba(229,0,26,0.08);border:1px solid rgba(229,0,26,0.25);
                       border-radius:10px;padding:1px 6px;font-size:9px;color:var(--red);
                       font-weight:500;margin-left:4px;">⚠ 1 sample</span>
          {% endif %}
        </td>
      </tr>
      {% endif %}
      {% endfor %}
    </table>
    {% else %}
    <p style="color:var(--muted);font-size:13px;">
      No leg data yet — will populate as cars arrive at European ports and users log in.
    </p>
    {% endif %}
  </div>

  <div class="card">
    <div class="section-head">🌍 By destination country</div>
    {% if by_country %}
      {% set max_c = by_country[0]['total'] %}
      {% for r in by_country %}
      {% set pct  = ((r['total'] / max_c) * 100)|int %}
      {% set dpct = ((r['delayed'] / r['total']) * 100)|int if r['total'] else 0 %}
      <div class="bar-row" style="margin-bottom:.85rem;">
        <div class="bar-head">
          <span>{{ r['dest_country'] }}</span>
          <span>{{ r['total'] }}
            {% if dpct > 0 %}
            <span style="color:var(--amber);font-weight:400;font-size:11px;">· {{ dpct }}% delayed</span>
            {% endif %}
          </span>
        </div>
        <div class="bar-bg"><div class="bar-fill" style="width:{{ pct }}%"></div></div>
        <div class="bar-sub">{{ r['models'] }}</div>
      </div>
      {% endfor %}
    {% else %}
      <p style="color:var(--muted);font-size:13px;">No country data yet.</p>
    {% endif %}
  </div>

  {% if order_journeys %}
  <div class="card">
    <div class="section-head">🚗 Order journeys</div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:1rem;">
      Each anonymous order — where it is in the pipeline right now
    </div>

    {% for r in order_journeys %}
    <div style="padding:.9rem 0;border-bottom:1px solid var(--border);">

      <div style="display:flex;align-items:center;justify-content:space-between;
                  margin-bottom:.65rem;flex-wrap:wrap;gap:.4rem;">
        <div style="display:flex;align-items:center;gap:.6rem;">
          <span style="font-size:13px;font-weight:500;">{{ r.model or '—' }}</span>
          <span style="font-size:11px;color:var(--muted);">→ {{ r.dest_country or '—' }}</span>
        </div>
        <div style="display:flex;align-items:center;gap:.5rem;">
          {% if r.created_on %}
          <span style="font-size:11px;color:var(--muted);">ordered {{ r.created_on }}</span>
          {% endif %}
          <span class="badge badge-pending" style="font-size:11px;">{{ r.status }}</span>
        </div>
      </div>

      <div style="display:flex;gap:3px;align-items:center;margin-bottom:4px;">
        {% for step_name, s in r.step_statuses %}
        <div style="flex:1;position:relative;" title="{{ step_name }}">
          <div style="height:8px;border-radius:3px;
            {% if s == 'visited' %}background:var(--green);
            {% elif s == 'current' %}background:var(--red);animation:pulse 2s ease-in-out infinite;
            {% else %}background:var(--surface2);border:1px solid var(--border);{% endif %}">
          </div>
        </div>
        {% if not loop.last %}
        <div style="width:3px;height:1px;background:var(--border);flex-shrink:0;"></div>
        {% endif %}
        {% endfor %}
      </div>

      <div style="display:flex;gap:3px;">
        {% for step_name, s in r.step_statuses %}
        <div style="flex:1;font-size:9px;color:
          {% if s == 'visited' %}var(--green)
          {% elif s == 'current' %}var(--red)
          {% else %}#3d444d{% endif %};
          text-align:center;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;"
          title="{{ step_name }}">
          {{ step_name | replace('processedOrder','Ordered') |
             replace('buildInProgress','Building') |
             replace('leftTheFactory','Left Factory') |
             replace('inTransit','In Transit') |
             replace('arrivedAtRetailer','Arrived') }}
        </div>
        {% if not loop.last %}
        <div style="width:3px;flex-shrink:0;"></div>
        {% endif %}
        {% endfor %}
      </div>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:1.25rem;">
    <div class="card">
      <div class="section-head">📋 By current status</div>
      {% set max_s = namespace(v=1) %}
      {% for r in by_status %}{% if r['c'] > max_s.v %}{% set max_s.v = r['c'] %}{% endif %}{% endfor %}
      {% for r in by_status %}
      {% set pct = ((r['c'] / max_s.v) * 100)|int %}
      <div class="bar-row">
        <div class="bar-head"><span>{{ r['status'] }}</span><span>{{ r['c'] }}</span></div>
        <div class="bar-bg"><div class="bar-fill" style="width:{{ pct }}%;opacity:.7"></div></div>
      </div>
      {% endfor %}
    </div>
    <div class="card">
      <div class="section-head">🚗 By model</div>
      <table class="data-table">
        <tr><th>Model</th><th>Orders</th></tr>
        {% for r in by_model %}
        <tr><td>{{ r['model'] }}</td><td>{{ r['c'] }}</td></tr>
        {% endfor %}
      </table>
    </div>
  </div>

  <div class="card">
    <div class="section-head">🕐 Recent checks</div>
    <table class="data-table">
      <tr><th>Time (local)</th><th>Model</th><th>Status</th><th>Country</th><th>Order ID</th></tr>
      {% for r in recent %}
      <tr>
        <td class="utc-time" data-utc="{{ r['ts'][:16] }}" style="color:var(--muted);">{{ r['ts'][:16] | replace('T',' ') }}</td>
        <td>{{ r['model'] or '—' }}</td>
        <td><span class="badge badge-pending">{{ r['status'] or '—' }}</span></td>
        <td>{{ r['dest_country'] or '—' }}</td>
        <td style="font-family:monospace;font-size:11px;color:var(--muted);">
          {{ r['order_name'] }}
        </td>
      </tr>
      {% endfor %}
    </table>
  </div>
</div>
<script>
// Convert UTC timestamps to browser's local timezone
document.querySelectorAll('.utc-time').forEach(function(el) {
  var utc = el.getAttribute('data-utc');
  if (!utc) return;
  try {
    var d = new Date(utc + 'Z'); // append Z to tell JS it's UTC
    el.textContent = d.toLocaleString(undefined, {
      year:'numeric', month:'2-digit', day:'2-digit',
      hour:'2-digit', minute:'2-digit', hour12:false
    });
    el.title = 'UTC: ' + utc;
  } catch(e) {}
});
</script>
</body></html>
"""

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def index():
    username = USERNAME
    password = PASSWORD
    orders   = []
    error    = None

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

    if username and password:
        try:
            sys.path.insert(0, '/app')
            from toyota import ToyotaSession

            # Run --store-dates first so dates file is up to date before we read it
            subprocess.run(
                [sys.executable, "/app/toyota.py", "--username", username,
                 "--password", password, "--store-dates"],
                capture_output=True, text=True, timeout=60, cwd="/data"
            )

            session = ToyotaSession(username, password)

            # Fetch full order objects to get createdOn
            _orders_resp = session.session.get(
                session.ORDERS_URL,
                params={"displayPreApprovedCars": "true", "displayVOTCars": "true"},
                timeout=10
            ).json()
            _order_dates = {o["id"]: o.get("createdOn", "") for o in _orders_resp}

            for oid in session.fetch_orders():
                details    = session.fetch_order_details(oid)
                dates_file = f"/data/{oid}.json"
                step_dates = {}
                if os.path.exists(dates_file):
                    with open(dates_file) as f:
                        step_dates = json.load(f)
                save_stats(details, step_dates, today_only=True,
                           created_on=_order_dates.get(oid, ""))
                details['_step_dates'] = step_dates.get("steps", {})
                details['_created_on'] = _order_dates.get(oid, "")
                import hashlib
                order_hash = hashlib.sha256(oid.encode()).hexdigest()[:16] if oid else ""
                details['_order_hash'] = order_hash
                # Login frequency for date reliability disclaimer
                if order_hash:
                    freq = get_db().execute("""
                        SELECT COUNT(*) logins,
                               CAST((julianday(MAX(ts)) - julianday(MIN(ts))) AS INTEGER) days_tracked,
                               MIN(ts) first_login
                        FROM checks WHERE order_hash=?
                    """, (order_hash,)).fetchone()
                    details['_logins']       = freq['logins'] if freq else 1
                    details['_days_tracked'] = freq['days_tracked'] if freq else 0
                    details['_first_login']  = (freq['first_login'] or '')[:10] if freq else ''
                    # Check if vessel already known in DB
                    v = get_db().execute("""
                        SELECT vessel_mmsi, vessel_name FROM checks
                        WHERE order_hash=? AND vessel_mmsi IS NOT NULL LIMIT 1
                    """, (order_hash,)).fetchone()
                    details['_vessel_mmsi'] = v['vessel_mmsi'] if v else ''
                    details['_vessel_name'] = v['vessel_name'] if v else ''
                else:
                    details['_logins']       = 1
                    details['_days_tracked'] = 0
                    details['_first_login']  = ''
                    details['_vessel_mmsi']  = ''
                    details['_vessel_name']  = ''
                # Enrich intermediateDeliveries with lat/lng (API doesn't provide them)
                for d in details.get('intermediateDeliveries') or []:
                    if not d.get('locationLatitude'):
                        coords = geocode_location(
                            d.get('locationName', ''), d.get('countryName', '')
                        )
                        if coords:
                            d['locationLatitude']  = coords[0]
                            d['locationLongitude'] = coords[1]
                orders.append(details)

        except Exception as e:
            error = str(e)

    return render_template_string(TRACKER_PAGE,
                                  orders=orders, username=username,
                                  error=error, request=request)

@app.route("/api/vessel/<mmsi>")
def api_vessel(mmsi):
    if mmsi not in TOYOTA_CARRIERS and not mmsi.isdigit():
        return jsonify(error="invalid mmsi"), 400
    pos = get_vessel_position(mmsi)
    if not pos:
        return jsonify(error="no position data"), 404
    return jsonify(pos)

@app.route("/api/vessel-detect/<order_hash>", methods=["GET", "POST"])
def api_vessel_detect(order_hash):
    db = get_db()
    body = request.get_json(silent=True) or {}
    depart_date_override = request.args.get('depart_date') or body.get('depart_date')
    mmsi_override        = request.args.get('mmsi')        or body.get('mmsi')
    leg_override         = request.args.get('leg', 'nagoya')

    # If user provided a date or MMSI, save it to vessel_overrides
    if depart_date_override or mmsi_override:
        db.execute("""
            INSERT INTO vessel_overrides (order_hash, leg, depart_date, mmsi, source, created_at)
            VALUES (?, ?, ?, ?, 'user', datetime('now'))
            ON CONFLICT(order_hash, leg) DO UPDATE SET
                depart_date = COALESCE(excluded.depart_date, depart_date),
                mmsi        = COALESCE(excluded.mmsi, mmsi),
                source      = 'user'
        """, (order_hash, leg_override, depart_date_override, mmsi_override))
        db.commit()

    # Check vessel_overrides for user-provided MMSI (highest priority)
    override = db.execute("""
        SELECT depart_date, mmsi, detected_mmsi, detected_name, detected_at
        FROM vessel_overrides WHERE order_hash=? AND leg=?
    """, (order_hash, leg_override)).fetchone()

    # If user set MMSI manually, use it directly
    if override and override['mmsi'] and not depart_date_override:
        pos = get_vessel_position(override['mmsi'])
        if pos:
            return jsonify({**pos, "source": "user_override", "leg": leg_override})

    # Check leg-aware cache in vessel_overrides first
    if not depart_date_override and not mmsi_override:
        # Check vessel_overrides for this specific leg (leg-aware cache)
        leg_cached = db.execute("""
            SELECT detected_mmsi, detected_name, detected_at, berth_verified
            FROM vessel_overrides
            WHERE order_hash=? AND leg=?
            AND detected_mmsi IS NOT NULL
        """, (order_hash, leg_override)).fetchone()

        if leg_cached:
            mmsi = leg_cached["detected_mmsi"]
            berth_verified = leg_cached["berth_verified"]
            age = db.execute("""
                SELECT CAST((julianday('now') - julianday(?)) * 24 AS INTEGER)
            """, (leg_cached["detected_at"],)).fetchone()
            pos_age = db.execute("""
                SELECT CAST((julianday('now') - julianday(vessel_updated)) * 24 AS INTEGER)
                FROM checks WHERE order_hash=? AND vessel_mmsi=? LIMIT 1
            """, (order_hash, mmsi)).fetchone()
            # Berth-verified vessels: never re-detect, only refresh position every 6h
            # Unverified vessels: re-detect after 6h (detection may have been wrong)
            # NULL age (no timestamp yet) = treat as stale so it refreshes.
            stale_identity = (not berth_verified) and (age is None or age[0] is None or age[0] > 6)
            stale_position = (pos_age is None or pos_age[0] is None or pos_age[0] > 6)

            if not stale_identity:
                # Serve from checks position cache if position is fresh
                cached = db.execute("""
                    SELECT vessel_mmsi, vessel_name, vessel_lat, vessel_lon,
                           vessel_speed, vessel_course, vessel_dest, vessel_eta, vessel_updated
                    FROM checks WHERE order_hash=? AND vessel_mmsi=?
                    LIMIT 1
                """, (order_hash, mmsi)).fetchone()
                if cached and cached["vessel_lat"] and not stale_position:
                    return jsonify({
                        "mmsi":        cached["vessel_mmsi"],
                        "name":        cached["vessel_name"],
                        "lat":         float(cached["vessel_lat"]),
                        "lon":         float(cached["vessel_lon"]),
                        "speed":       float(cached["vessel_speed"] or 0),
                        "course":      (float(cached["vessel_course"]) if cached["vessel_course"] not in (None, 0, 0.0) else None),
                        "destination": cached["vessel_dest"] or "",
                        "eta":         cached["vessel_eta"] or "",
                        "cached":      True, "leg": leg_override,
                        "berth_verified": bool(berth_verified),
                    })
                # Position stale — refresh position only, keep vessel identity
                pos = get_vessel_position(mmsi)
                if pos:
                    _cache_vessel(db, order_hash, pos, leg=leg_override)
                    return jsonify({**pos, "cached": False, "leg": leg_override,
                                    "berth_verified": bool(berth_verified)})
            # Identity stale and not berth-verified — fall through to re-detect

        # Fallback: check checks table (legacy, no leg info)
        # Only use for nagoya leg — hub legs (zeebrugge/malmo etc) need fresh detection
        elif not leg_cached and leg_override == 'nagoya':
            cached = db.execute("""
                SELECT vessel_mmsi, vessel_name, vessel_lat, vessel_lon,
                       vessel_speed, vessel_course, vessel_dest, vessel_eta, vessel_updated
                FROM checks WHERE order_hash=?
                AND vessel_mmsi IS NOT NULL
                LIMIT 1
            """, (order_hash,)).fetchone()

            if cached and cached["vessel_lat"]:
                age = db.execute("""
                    SELECT CAST((julianday('now') - julianday(vessel_updated)) * 24 AS INTEGER)
                    FROM checks WHERE order_hash=? AND vessel_mmsi IS NOT NULL LIMIT 1
                """, (order_hash,)).fetchone()
                stale = (age is None or age[0] is None or age[0] > 6)

                if not stale:
                    return jsonify({
                        "mmsi":        cached["vessel_mmsi"],
                        "name":        cached["vessel_name"],
                        "lat":         float(cached["vessel_lat"]),
                        "lon":         float(cached["vessel_lon"]),
                        "speed":       float(cached["vessel_speed"] or 0),
                        "course":      (float(cached["vessel_course"]) if cached["vessel_course"] not in (None, 0, 0.0) else None),
                        "destination": cached["vessel_dest"] or "",
                        "eta":         cached["vessel_eta"] or "",
                        "cached":      True,
                    })
                else:
                    pos = get_vessel_position(cached["vessel_mmsi"])
                    if pos:
                        _cache_vessel(db, order_hash, pos, leg=leg_override)
                        return jsonify({**pos, "cached": False})
                    return jsonify({
                        "mmsi":        cached["vessel_mmsi"],
                        "name":        cached["vessel_name"],
                        "lat":         float(cached["vessel_lat"]),
                        "lon":         float(cached["vessel_lon"]),
                        "speed":       float(cached["vessel_speed"] or 0),
                        "course":      (float(cached["vessel_course"]) if cached["vessel_course"] not in (None, 0, 0.0) else None),
                        "destination": cached["vessel_dest"] or "",
                        "eta":         cached["vessel_eta"] or "",
                        "cached":      True, "stale": True,
                    })

    # Determine departure date to use
    if depart_date_override:
        left_factory_date = depart_date_override
    elif override and override['depart_date']:
        left_factory_date = override['depart_date']  # use saved user date
    else:
        row = db.execute("""
            SELECT sd.date_entered FROM step_durations sd
            WHERE sd.order_hash=? AND sd.step='leftTheFactory'
            AND sd.date_entered IS NOT NULL
        """, (order_hash,)).fetchone()
        if row:
            left_factory_date = row["date_entered"]
        elif leg_override != 'nagoya':
            # For hub legs (sagunto/zeebrugge/malmo) use today as departure date
            # since the car just arrived at this hub
            from datetime import datetime
            left_factory_date = datetime.utcnow().strftime("%Y-%m-%d")
        else:
            return jsonify(error="no leftTheFactory date"), 404

    vessel = detect_vessel(left_factory_date, leg=leg_override)
    if not vessel:
        return jsonify(error="no vessel detected"), 404

    # Save detection result to vessel_overrides
    bv = 1 if vessel.get('berth_verified') else 0
    db.execute("""
        INSERT INTO vessel_overrides (order_hash, leg, depart_date, detected_mmsi, detected_name, detected_at, source, berth_verified)
        VALUES (?, ?, ?, ?, ?, datetime('now'), 'auto', ?)
        ON CONFLICT(order_hash, leg) DO UPDATE SET
            detected_mmsi  = excluded.detected_mmsi,
            detected_name  = excluded.detected_name,
            detected_at    = excluded.detected_at,
            source         = CASE WHEN mmsi IS NOT NULL THEN 'user' ELSE 'auto' END,
            berth_verified = CASE
                WHEN vessel_overrides.detected_mmsi = excluded.detected_mmsi
                    THEN MAX(vessel_overrides.berth_verified, excluded.berth_verified)
                ELSE excluded.berth_verified
            END
    """, (order_hash, leg_override, left_factory_date,
          vessel.get('mmsi'), vessel.get('name'), bv))

    _cache_vessel(db, order_hash, vessel, leg=leg_override)
    db.commit()

    return jsonify({k: v for k, v in vessel.items() if not k.startswith("_")})

@app.route("/api/vessel-overrides/<order_hash>")
def api_vessel_overrides(order_hash):
    db = get_db()
    rows = db.execute("""
        SELECT leg, depart_date, mmsi, detected_mmsi, detected_name, detected_at, source
        FROM vessel_overrides WHERE order_hash=?
    """, (order_hash,)).fetchall()
    result = {}
    for r in rows:
        result[r["leg"]] = {
            "depart_date":   r["depart_date"],
            "mmsi":          r["mmsi"],
            "detected_mmsi": r["detected_mmsi"],
            "detected_name": r["detected_name"],
            "detected_at":   r["detected_at"],
            "source":        r["source"],
        }
    return jsonify(result)

@app.route("/stats/count")
def stats_count():
    from flask import jsonify
    db = get_db()
    total = db.execute("SELECT COUNT(DISTINCT order_hash) FROM checks WHERE order_hash IS NOT NULL").fetchone()[0]
    return jsonify(total=total)

@app.route("/stats")
def stats():
    d   = get_stats_data()
    pct = int(d["delayed"] / d["total"] * 100) if d["total"] else 0
    return render_template_string(STATS_PAGE, pct_delayed=pct, request=request, **d)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)