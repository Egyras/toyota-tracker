#!/usr/bin/env python3
"""Toyota Order Tracker — Flask web wrapper with anonymized stats collection."""
import os, sys, json, sqlite3, subprocess, threading, time, hmac, secrets
from collections import defaultdict, deque
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, g, jsonify, send_from_directory

app = Flask(__name__)

USERNAME     = os.environ.get("TOYOTA_USERNAME", "")
PASSWORD     = os.environ.get("TOYOTA_PASSWORD", "")
DB_PATH      = os.environ.get("DB_PATH", "/data/stats.db")
MST_EMAIL    = os.environ.get("MST_EMAIL", "")
MST_PASSWORD = os.environ.get("MST_PASSWORD", "")

# Set BEHIND_TRUSTED_PROXY=0 if the app is ever exposed directly instead of via
# the Cloudflare tunnel — see client_key() for why this matters.
BEHIND_TRUSTED_PROXY = os.environ.get("BEHIND_TRUSTED_PROXY", "1") == "1"
# Vendored browser assets (see Dockerfile). Serving Leaflet ourselves keeps
# script-src free of third-party origins.
VENDOR_DIR = os.environ.get("VENDOR_DIR", "/app/node_modules")

# ── Roles ─────────────────────────────────────────────────────────────────────
# One image, two jobs, chosen at runtime:
#
#   ROLE=web      the Flask site. Holds the database and receives users' Toyota
#                 credentials. Owns NO MyShipTracking login and never launches a
#                 browser — it asks the scraper over the internal network.
#   ROLE=scraper  runs Chromium against myshiptracking.com. Holds ONLY the MST
#                 login: no database, no Toyota credentials, no published port,
#                 no route off the internal network except outbound HTTPS.
#
# The point of the split is blast radius. Chromium visiting a third party we do
# not control is the most likely place to get code execution; putting it in a
# container that holds nothing and can reach nothing means an exploit there wins
# a MyShipTracking account, not the database and not a foothold on the LAN.
ROLE = os.environ.get("ROLE", "web").strip().lower()
if ROLE not in ("web", "scraper"):
    print(f"[config] unknown ROLE={ROLE!r}, defaulting to 'web'", file=sys.stderr)
    ROLE = "web"

# Base URL of the scraper service, e.g. http://toyota-scraper:8080. Empty means
# single-container mode: the web role runs the browser itself, exactly as before.
SCRAPER_URL   = os.environ.get("SCRAPER_URL", "").strip()
# Shared secret for the internal endpoint. Defence in depth — the scraper is not
# published and sits on an internal Docker network, but nothing about that should
# be load-bearing on its own.
SCRAPER_TOKEN = os.environ.get("SCRAPER_TOKEN", "").strip()

# Unprivileged account the browser scraper runs as. Chromium will not enable its
# sandbox while running as root, which is the only reason detect_vessel.js used
# to pass --no-sandbox. Created in the Dockerfile. Set SCRAPER_USER="" to run the
# scraper as the current user (i.e. root) if the account is missing.
SCRAPER_USER = os.environ.get("SCRAPER_USER", "pwuser")
SCRAPER_HOME = os.environ.get("SCRAPER_HOME", "/home/pwuser")


def _drop_priv_kwargs():
    """subprocess kwargs that run the browser as SCRAPER_USER instead of root.

    Returns {} when we are already unprivileged, when no such account exists, or
    on any platform without POSIX users — in every one of those cases the caller
    behaves exactly as before, so a misconfigured image degrades to the old
    behaviour rather than failing to scrape at all.
    """
    if not SCRAPER_USER:
        return {}
    try:
        if os.geteuid() != 0:      # already unprivileged, nothing to drop
            return {}
        import pwd
        entry = pwd.getpwnam(SCRAPER_USER)
    except (AttributeError, ImportError, KeyError):
        print(f"[scraper] user {SCRAPER_USER!r} unavailable — running browser as current user",
              file=sys.stderr)
        return {}
    return {"user": entry.pw_uid, "group": entry.pw_gid}


# Legs the detector knows about — mirrors PORT_IDS in detect_vessel.js. Used to
# reject arbitrary ?leg= values before they reach the scraper or the database.
VALID_LEGS = {
    "nagoya", "yokkaichi", "hiroshima",
    "zeebrugge", "bremerhaven", "antwerp", "southampton", "portbury",
    "livorno", "sagunto", "malmo", "gothenburg", "paldiski", "drammen",
    "piraeus", "vejle",
}


# ── Abuse controls ────────────────────────────────────────────────────────────
# /api/vessel-detect spawns a headless Chromium via detect_vessel.js with a
# 120 s timeout. Unauthenticated and unthrottled, a handful of concurrent
# requests is enough to exhaust the box, so it needs both a per-client limit
# and a hard ceiling on how many scrapers can ever run at once.

# Only ever this many scraper subprocesses concurrently, process-wide. This is
# the backstop that cannot be evaded by forging client identity.
SCRAPER_SLOTS = int(os.environ.get("SCRAPER_SLOTS", "2"))
_scraper_sem  = threading.BoundedSemaphore(SCRAPER_SLOTS)

_rate_lock    = threading.Lock()
_rate_hits    = defaultdict(deque)   # client key -> deque of request timestamps


def client_key():
    """Best-effort caller identity for rate limiting.

    Production sits behind a Cloudflare tunnel, so request.remote_addr is the
    tunnel's address and is identical for every visitor — limiting on it alone
    would throttle all users as if they were one. CF-Connecting-IP is set by
    Cloudflare and cannot be forged by the client when the origin is only
    reachable through the tunnel.

    A caller who reaches the origin directly could spoof these headers, which is
    why the global semaphore above, not this function, is the real ceiling.
    """
    if BEHIND_TRUSTED_PROXY:
        cf = request.headers.get("CF-Connecting-IP")
        if cf:
            return cf.strip()
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def rate_limited(max_hits, per_seconds):
    """Sliding-window limiter. Returns 429 JSON, which the frontend already
    treats as a failed detection and degrades to the manual-entry prompt."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            key = f"{fn.__name__}:{client_key()}"
            now = time.monotonic()
            with _rate_lock:
                hits = _rate_hits[key]
                while hits and hits[0] <= now - per_seconds:
                    hits.popleft()
                if len(hits) >= max_hits:
                    retry = int(per_seconds - (now - hits[0])) + 1
                    resp = jsonify(error="rate_limited",
                                   message=f"Too many requests. Try again in {retry}s.")
                    resp.status_code = 429
                    resp.headers["Retry-After"] = str(retry)
                    return resp
                hits.append(now)
                # Opportunistic cleanup so the dict cannot grow without bound.
                if len(_rate_hits) > 4096:
                    for k in [k for k, v in _rate_hits.items() if not v]:
                        del _rate_hits[k]
            return fn(*args, **kwargs)
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator

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
    # Note: Vela Leader (MMSI 636024024) is a real NYK PCC but serves Asia/Americas/Europe
    # routes. We deliberately keep her unverified so the destination filter is the gate —
    # she's only "correct" for European orders when AIS dest indicates a European port.
}

def is_wrong_continent_for_order(vessel_dest: str, order_dest_country: str) -> bool:
    """Return True if vessel's AIS destination is on a completely different
    continent than the order's destination — e.g. order to France, vessel to Taipei.
    Used to reject obviously-wrong vessel matches even when MMSI is in TOYOTA_CARRIERS
    (since carriers serve multiple routes and prior detection cache may be stale).
    """
    if not vessel_dest or not order_dest_country:
        return False
    vdest = vessel_dest.upper()
    order_eu = order_dest_country.upper() in {
        'FRANCE','GERMANY','BELGIUM','NETHERLANDS','UNITED KINGDOM','IRELAND',
        'SPAIN','ITALY','PORTUGAL','GREECE','POLAND','LITHUANIA','LATVIA',
        'ESTONIA','FINLAND','SWEDEN','NORWAY','DENMARK','CZECH REPUBLIC',
        'SLOVAKIA','SLOVENIA','HUNGARY','CROATIA','AUSTRIA','SWITZERLAND',
        'ROMANIA','BULGARIA','CYPRUS'
    }
    non_eu_prefixes = ('TW ','TW-','CN ','CN-','KR ','KR-','US ','US-',
                       'CA ','CA-','CL ','CL-','BR ','BR-','AR ','AR-',
                       'AU ','AU-','NZ ','NZ-','MX ','MX-','PE ','PE-',
                       'TH ','TH-','MY ','MY-','PH ','PH-','VN ','VN-',
                       'IN ','IN-','ZA ','ZA-')
    non_eu_keywords = ('TAIPEI','KAOHSIUNG','SHANGHAI','BUSAN','LONG BEACH',
                       'LOS ANGELES','SAN ANTONIO','IQUIQUE','BRUNSWICK',
                       'DAVISVILLE','BALTIMORE','VERACRUZ','SYDNEY',
                       'MELBOURNE','AUCKLAND','HONG KONG',
                       'BANGKOK','MANILA')
    if not order_eu:
        return False  # only flag for EU-bound orders (most of our use case)
    if any(vdest.startswith(p) for p in non_eu_prefixes):
        return True
    if any(k in vdest for k in non_eu_keywords) and 'SINGAPORE' not in vdest:
        return True
    return False


def _detect_local(argv, timeout):
    """Run detect_vessel.js in this container and return its parsed JSON, or None.

    This is where the browser actually launches, so the concurrency ceiling and
    the privilege drop both live here — which means they apply automatically
    whichever container ends up doing the work.
    """
    env = os.environ.copy()
    env['MST_EMAIL']    = MST_EMAIL
    env['MST_PASSWORD'] = MST_PASSWORD
    env['HOME']         = SCRAPER_HOME   # Chromium needs a writable HOME
    # Hard ceiling on concurrent Chromium instances. Waiting rather than failing
    # keeps normal single-user page loads working unchanged; the short timeout
    # means a saturated queue degrades to "not detected" instead of piling up
    # processes.
    if not _scraper_sem.acquire(timeout=20):
        print("[vessel scraper] all scraper slots busy, skipping", file=sys.stderr)
        return None
    drop = _drop_priv_kwargs()
    try:
        try:
            result = subprocess.run(
                ['node', '/app/detect_vessel.js', *argv],
                capture_output=True, text=True, timeout=timeout, env=env, **drop
            )
        except PermissionError:
            # Dropping to another user needs CAP_SETUID/CAP_SETGID. Under
            # --cap-drop=ALL those are gone, so the setuid in the child fails with
            # EPERM and nothing runs at all — the hardening defeats itself.
            #
            # The proper fix is to start the container as an unprivileged user
            # (--user pwuser), which makes this drop unnecessary; see the scraper
            # stage in the Jenkinsfile. This retry is the safety net for any
            # deployment still combining root + --cap-drop=ALL: run the detector
            # without dropping rather than failing outright, and say so.
            if not drop:
                raise
            print("[vessel scraper] cannot drop to unprivileged user "
                  "(CAP_SETUID/CAP_SETGID unavailable, likely --cap-drop=ALL). "
                  "Running the browser as the current user instead — prefer "
                  "starting the container with --user.", file=sys.stderr)
            drop = {}
            result = subprocess.run(
                ['node', '/app/detect_vessel.js', *argv],
                capture_output=True, text=True, timeout=timeout, env=env
            )
    except subprocess.TimeoutExpired:
        print(f"[vessel scraper] timed out after {timeout}s", file=sys.stderr)
        return None
    except Exception as e:
        # Catch-all on purpose. subprocess.run raises for a whole family of
        # environment problems that have nothing to do with detection —
        # FileNotFoundError if `node` is missing, PermissionError if the
        # unprivileged user cannot exec it, KeyError from the user= drop, OSError
        # on a read-only path. Letting those escape turned into a bare HTTP 500
        # with an HTML body, which told the caller nothing and hid the real
        # cause. Log it loudly, return None, let the caller degrade normally.
        import traceback
        print(f"[vessel scraper] FAILED to launch detector: {type(e).__name__}: {e}",
              file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return None
    finally:
        _scraper_sem.release()
    # detect_vessel.js narrates everything it does on stderr: the port page it
    # requested, which vessels it saw, how each candidate scored, why any were
    # rejected. Previously that was only printed when the process EXITED
    # non-zero — but the interesting failure is "ran fine, found nothing", which
    # exits 0. That made an empty result indistinguishable from a login failure,
    # an empty port listing, or every candidate being filtered out.
    if result.stderr and result.stderr.strip():
        print(f"[detector stderr]\n{result.stderr[-4000:]}", file=sys.stderr)

    if result.returncode != 0:
        print(f"[vessel scraper] detector exited {result.returncode}", file=sys.stderr)
        return None
    if not result.stdout.strip():
        print("[vessel scraper] detector produced no output", file=sys.stderr)
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError as e:
        print(f"[vessel scraper] bad JSON: {e} — first 300 chars: "
              f"{result.stdout[:300]!r}", file=sys.stderr)
        return None
    n = len(data.get('matches') or []) if isinstance(data, dict) else 0
    print(f"[vessel scraper] detector ok: {n} match(es), "
          f"position={'yes' if (isinstance(data, dict) and data.get('position')) else 'no'}",
          file=sys.stderr)
    return data


def _detect_remote(argv, timeout):
    """Ask the isolated scraper container to do it, over the internal network.

    The scraper holds only the MyShipTracking login — no database, no Toyota
    credentials, no route to the LAN — so a Chromium compromise there yields
    nothing but that one account. See run_detector() for why this indirection
    exists at all.
    """
    import requests
    try:
        r = requests.post(
            SCRAPER_URL.rstrip('/') + '/internal/detect',
            json={"argv": list(argv)},
            headers={"X-Scraper-Token": SCRAPER_TOKEN},
            timeout=timeout + 15,   # allow for queueing on the scraper side
        )
    except Exception as e:
        print(f"[vessel scraper] scraper service unreachable: {e}", file=sys.stderr)
        return None
    if r.status_code != 200:
        # Surface the scraper's own diagnosis when it sends one. Truncating to
        # 200 chars previously clipped a Flask HTML error page down to its
        # doctype, which is why this looked like a mystery rather than a bug.
        detail = r.text
        try:
            j = r.json()
            detail = j.get("detail") or j.get("error") or r.text
        except ValueError:
            detail = "(non-JSON body — check `docker logs toyota-scraper`) " + r.text[:200]
        print(f"[vessel scraper] scraper service returned {r.status_code}: {detail}",
              file=sys.stderr)
        return None
    try:
        body = r.json()
    except ValueError:
        return None
    return body.get("result")


def run_detector(argv, timeout):
    """Single entry point for 'run the browser scraper and give me its JSON'.

    Both call sites below used to shell out directly. Routing them through here
    means the web container can hand the work to a separate, network-isolated
    container without either call site knowing. If SCRAPER_URL is unset the
    behaviour is byte-for-byte what it was before, so a single-container deploy
    keeps working unchanged.
    """
    if SCRAPER_URL and ROLE == "web":
        return _detect_remote(argv, timeout)
    return _detect_local(argv, timeout)


def get_vessel_position(mmsi: str, order_dest_country: str = None) -> dict | None:
    """Get vessel position — scrape MyShipTracking first (free), fallback to aisstream/DataDocked."""
    # Try MST scraper (free, same login we use for detection).
    # See detect_vessel_scraper() on why SCRAPER_URL bypasses the credential check.
    if SCRAPER_URL or (MST_EMAIL and MST_PASSWORD):
        try:
            data = run_detector(['dummy', mmsi], 60)
            if data:
                pos  = data.get('position', {})
                if pos.get('lat'):
                    # SMART VESSEL VALIDATION — multi-criteria:
                    #  Hard-reject signals (any one means it's not our cargo):
                    #   1. AIS dest = "JP *" (inbound to Japan, not Toyota outbound)
                    #   2. AIS dest is in a completely different region than the order's destination
                    #      (e.g. order to France, but ship heading to TAIPEI / SAN ANTONIO)
                    #   3. Vessel name doesn't fit any PCC naming pattern AND not in our list
                    #
                    #  Soft signals:
                    #   - MMSI in TOYOTA_CARRIERS = verified
                    #   - Name fits PCC pattern but MMSI unknown = unverified (show with warning)
                    import re
                    vname = pos.get('name') or TOYOTA_CARRIERS.get(mmsi, '')
                    vdest = (pos.get('dest') or '').upper()
                    is_known = bool(TOYOTA_CARRIERS.get(mmsi))
                    # 1. Hard reject: heading TO Japan (inbound)
                    if vdest.startswith('JP ') or vdest.startswith('JP-'):
                        print(f"[vessel pos scraper] Rejected MMSI {mmsi}: AIS dest='{vdest}' "
                              f"indicates INBOUND to Japan (Toyota cargo is OUTbound)",
                              file=sys.stderr)
                        return None
                    # 2. Hard reject: destination on different continent
                    if is_wrong_continent_for_order(vdest, order_dest_country):
                        print(f"[vessel pos scraper] Rejected MMSI {mmsi} name='{vname}': "
                              f"order is to {order_dest_country} but AIS dest='{vdest}' "
                              f"(wrong continent)", file=sys.stderr)
                        return None
                    # If known carrier, trust it
                    if is_known:
                        verified = True
                    else:
                        # Unknown MMSI — check if name looks like a PCC
                        pcc_pattern = re.compile(
                            r'\b(highway|leader|ace|carrier|cruiser|express|hawk|falcon|'
                            r'eagle|crane|swan|breeze|harvest|spirit)\b',
                            re.IGNORECASE
                        )
                        glovis_pattern = re.compile(r'^glovis\b', re.IGNORECASE)
                        if vname and (pcc_pattern.search(vname) or glovis_pattern.search(vname)):
                            verified = False  # plausibly a PCC, show with warning
                            print(f"[vessel pos scraper] UNVERIFIED carrier: MMSI {mmsi} "
                                  f"name='{vname}' — name fits PCC pattern, not in our database",
                                  file=sys.stderr)
                        elif not vname:
                            print(f"[vessel pos scraper] Rejected MMSI {mmsi}: no vessel name "
                                  f"resolved, not a known carrier", file=sys.stderr)
                            return None
                        else:
                            print(f"[vessel pos scraper] Rejected MMSI {mmsi} name='{vname}': "
                                  f"doesn't match any PCC naming pattern", file=sys.stderr)
                            return None
                    return {
                        'mmsi':        mmsi,
                        'name':        vname or f'MMSI {mmsi}',
                        'lat':         pos['lat'],
                        'lon':         pos['lon'],
                        'speed':       pos.get('speed', 0),
                        'course':      pos.get('course'),
                        'destination': pos.get('dest', ''),
                        'eta':         pos.get('eta', ''),
                        'updated':     pos.get('updated', ''),
                        'source':      pos.get('source', 'myshiptracking'),
                        'verified':    verified,
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

    # Last resort: DataDocked (satellite, uses credits) — never implemented.
    # This used to `return _fetch_datadocked(mmsi)`, a function that does not
    # exist anywhere in the file, so whenever the scraper and aisstream both came
    # up empty the endpoint raised NameError and returned 500 instead of the
    # intended "no position data" 404. Returning None restores that contract;
    # wire in a real satellite lookup here if it is ever added.
    return None


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


def detect_vessel_scraper(left_factory_date: str, leg: str = "nagoya",
                          dest_country: str = "", hub_port: str = "") -> dict | None:
    """
    Detect vessel by scraping MyShipTracking port departures.
    leg: nagoya (default), zeebrugge, malmo, bremerhaven etc.
    dest_country: order destination country, used for route region matching.
    hub_port: intermediate hub (e.g. SAGUNTO, ZEEBRUGGE) to override region inference.
    """
    # MST credentials live on whichever container actually runs the browser. In
    # the split deployment that is the scraper, and the web container has none —
    # so only gate on them when we are the one doing the work.
    if not SCRAPER_URL and (not MST_EMAIL or not MST_PASSWORD):
        return None
    try:
        data = run_detector(
            [left_factory_date, '', leg, dest_country or '', hub_port or ''], 120)
        if not data:
            return None
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


def detect_vessel(left_factory_date: str, leg: str = "nagoya",
                  dest_country: str = "", hub_port: str = "") -> dict | None:
    """Auto-detect vessel via MyShipTracking port departure scraper."""
    if not left_factory_date:
        return None
    vessel = detect_vessel_scraper(left_factory_date, leg=leg, dest_country=dest_country, hub_port=hub_port)
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
        # add created_on column if missing (safe on existing DBs) fix
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

        # Snapshots of the two history columns, built once so the same values are
        # used for the change-detection below and for the INSERT further down.
        steps_snapshot  = {k: v.get("status") for k, v in steps.items()}
        delivs_snapshot = [{"loc": d.get("locationName"), "country": d.get("countryName"),
                            "type": d.get("destinationType"), "visited": d.get("isVisited")}
                           for d in deliveries]

        if today_only and order_hash:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            # Compare against the LATEST row for today — there can now be more
            # than one (see the change-detection below).
            existing = db.execute(
                "SELECT rowid, status, steps_json, deliveries_json FROM checks "
                "WHERE order_hash=? AND ts LIKE ? ORDER BY ts DESC LIMIT 1",
                (order_hash, f"{today}%")).fetchone()

            # Did the route or step state actually move since the last check today?
            #
            # This branch used to unconditionally return after touching only
            # `status`, which meant steps_json and deliveries_json kept whatever
            # they held at the FIRST check of the day. Any transition that
            # happened later the same day — e.g. Malmo going notVisited ->
            # current at midday — was silently discarded and never recorded at
            # all, because by the next day the row already existed too. That is
            # why per-stop history lagged behind reality by a full day and
            # get_current_stop_info could not find the stop's real arrival date.
            #
            # When something HAS changed we now fall through to the INSERT so the
            # transition gets its own row and the history stays complete.
            route_changed = False
            if existing:
                try:
                    prev_steps  = json.loads(existing["steps_json"] or "null")
                    prev_delivs = json.loads(existing["deliveries_json"] or "null")
                except Exception:
                    prev_steps, prev_delivs = None, None
                route_changed = (prev_steps  != steps_snapshot or
                                 prev_delivs != delivs_snapshot)

            if existing and not route_changed:
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
            json.dumps(steps_snapshot),
            json.dumps(delivs_snapshot)
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
        if fh["visited"] == "visited" and th["visited"] in ("visited", "inTransit", "current"):
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


def get_current_stop_info(db, order_hash, deliveries):
    """Return {'location','country','state','date'} for the stop the car is at NOW.

    The order-progress timeline takes its LOCATION from Toyota's live
    preprocessed.steps but its DATE from step_dates[step]['current'] — the date
    the *order step* first became current. Those two move independently: the
    inTransit step starts once at the first European hub and never restarts,
    while the location advances Zeebrugge -> Malmo -> ... within that same step.
    Result: the UI reads "Malmo ... current: 2026-07-20" when the car only
    reached Malmo on 07-28.

    This recovers the real per-stop date by replaying deliveries_json history and
    finding the first check in which THIS stop held its current state.
    """
    if not order_hash or not deliveries:
        return None

    stop = next((d for d in deliveries if d.get("isVisited") == "current"), None)
    if stop is None:
        # Fall back to the last stop the car is en route to.
        in_transit = [d for d in deliveries if d.get("isVisited") == "inTransit"]
        stop = in_transit[-1] if in_transit else None
    if stop is None:
        return None

    loc_name = stop.get("locationName") or ""
    state    = stop.get("isVisited") or ""
    if not loc_name:
        return None
    loc_key = loc_name.upper()

    first_seen = None
    try:
        rows = db.execute("""
            SELECT ts, deliveries_json FROM checks
            WHERE order_hash=? AND deliveries_json IS NOT NULL
            ORDER BY ts ASC
        """, (order_hash,)).fetchall()
    except Exception:
        rows = []

    for row in rows:
        try:
            stored = json.loads(row["deliveries_json"])
        except Exception:
            continue
        if not isinstance(stored, list):
            continue
        for d in stored:
            # deliveries_json is written with keys "loc"/"visited"; tolerate the
            # raw API spelling too in case of older rows.
            sloc = (d.get("loc") or d.get("locationName") or "").upper()
            sstate = (d.get("visited") or d.get("isVisited") or "")
            if sloc == loc_key and sstate == state:
                first_seen = row["ts"][:10]
                break
        if first_seen:
            break

    if not first_seen:
        return None
    return {
        "location": loc_name,
        "country":  stop.get("countryName") or "",
        "state":    state,
        "date":     first_seen,
    }


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
                   COUNT(DISTINCT substr(ts,1,10)) logins,
                   CASE
                     WHEN COUNT(DISTINCT substr(ts,1,10)) <= 1 THEN 99
                     ELSE CAST(julianday(MAX(ts)) - julianday(MIN(ts)) AS REAL) / (COUNT(DISTINCT substr(ts,1,10)) - 1)
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
                   COUNT(DISTINCT substr(ts,1,10)) logins,
                   CASE
                     WHEN COUNT(DISTINCT substr(ts,1,10)) <= 1 THEN 99
                     ELSE CAST(julianday(MAX(ts)) - julianday(MIN(ts)) AS REAL) / (COUNT(DISTINCT substr(ts,1,10)) - 1)
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
               COUNT(DISTINCT substr(ts,1,10)) as logins,
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
:root{
  --bg:#0a0d12;
  --bg-elev:#0f131a;
  --surface:#141821;
  --surface2:#1c2230;
  --surface3:#252b3a;
  --border:#252b36;
  --border-strong:#323a4a;
  --red:#e5001a;
  --red-bright:#ff2640;
  --red-dim:#7d0010;
  --red-glow:rgba(229,0,26,.35);
  --text:#d1d7e0;
  --text-strong:#f0f3f8;
  --muted:#8b95a8;
  --muted-soft:#5f6776;
  --green:#3fb950;
  --green-soft:#56d364;
  --amber:#d29922;
  --blue:#58a6ff;
  --radius:12px;
  --radius-lg:16px;
  --radius-sm:8px;
  --ease:cubic-bezier(.4,0,.2,1);
  --shadow-sm:0 1px 2px rgba(0,0,0,.4);
  --shadow:0 4px 12px rgba(0,0,0,.35),0 1px 3px rgba(0,0,0,.25);
  --shadow-lg:0 12px 32px rgba(0,0,0,.45),0 4px 12px rgba(0,0,0,.3);
}
*{box-sizing:border-box;margin:0;padding:0;}
html{-webkit-text-size-adjust:100%;}
body{
  font-family:'Inter','-apple-system','Segoe UI',system-ui,sans-serif;
  background:var(--bg);
  background-image:
    radial-gradient(ellipse 80% 50% at 50% -10%,rgba(229,0,26,.05),transparent 70%),
    radial-gradient(ellipse 60% 40% at 100% 0%,rgba(31,111,235,.035),transparent 60%);
  background-attachment:fixed;
  color:var(--text);
  min-height:100vh;
  font-size:14px;line-height:1.6;
  -webkit-font-smoothing:antialiased;
  -moz-osx-font-smoothing:grayscale;
  font-feature-settings:'cv02','cv03','cv04','cv11';
}
a{color:var(--red-bright);text-decoration:none;transition:color .2s var(--ease);}
a:hover{color:var(--red);}

/* NAV */
.nav{
  background:rgba(10,13,18,.75);
  backdrop-filter:blur(20px) saturate(180%);
  -webkit-backdrop-filter:blur(20px) saturate(180%);
  border-bottom:1px solid rgba(255,255,255,.06);
  padding:0 1.5rem;
  display:flex;align-items:center;height:60px;
  position:sticky;top:0;z-index:1100;
}
.nav-brand{display:flex;align-items:center;gap:10px;text-decoration:none!important;}
.nav-brand-icon{
  width:30px;height:30px;border-radius:7px;
  background:linear-gradient(135deg,var(--red) 0%,#b80014 100%);
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
  box-shadow:0 2px 8px rgba(229,0,26,.4),inset 0 1px 0 rgba(255,255,255,.15);
}
.nav-brand-name{
  font-size:14px;font-weight:600;color:var(--text-strong);
  letter-spacing:-.015em;
}
.nav-toggle{
  display:none;background:none;border:1px solid var(--border);
  color:var(--text);width:36px;height:36px;border-radius:8px;
  margin-left:auto;cursor:pointer;align-items:center;justify-content:center;
  transition:all .2s var(--ease);
}
.nav-toggle:hover{background:var(--surface2);border-color:var(--border-strong);}
.nav-toggle svg{width:18px;height:18px;}
.nav-links{display:flex;gap:4px;margin-left:auto;align-items:center;}
.nav-link{
  display:flex;align-items:center;gap:7px;padding:7px 12px;
  border-radius:8px;font-size:13px;font-weight:500;
  color:var(--muted);text-decoration:none!important;
  border:1px solid transparent;
  transition:all .2s var(--ease);
}
.nav-link:hover{
  color:var(--text);background:rgba(255,255,255,.04);
  text-decoration:none!important;
}
.nav-link.active{
  color:var(--text-strong);
  background:linear-gradient(180deg,rgba(229,0,26,.15) 0%,rgba(229,0,26,.08) 100%);
  border-color:rgba(229,0,26,.3);
}
.nav-link svg{width:15px;height:15px;flex-shrink:0;}
.nav-divider{width:1px;height:20px;background:rgba(255,255,255,.08);margin:0 6px;}
.nav-pill{
  font-size:10px;font-weight:700;background:var(--red);color:#fff;
  padding:2px 7px;border-radius:20px;margin-left:2px;letter-spacing:.02em;
}

/* CONTAINER */
.container{max-width:920px;margin:0 auto;padding:2rem 1.5rem;}

/* CARDS */
.card{
  background:linear-gradient(180deg,var(--surface) 0%,var(--bg-elev) 100%);
  border:1px solid var(--border);
  border-radius:var(--radius);
  padding:1.5rem;margin-bottom:1.25rem;
  box-shadow:var(--shadow-sm);
  transition:border-color .25s var(--ease);
}
.card:hover{border-color:var(--border-strong);}
.card-title{
  font-size:11px;font-weight:600;color:var(--muted);
  text-transform:uppercase;letter-spacing:.08em;margin-bottom:1rem;
  display:flex;align-items:center;gap:8px;
}
.card-title::before{
  content:'';display:inline-block;width:3px;height:14px;
  background:var(--red);border-radius:2px;
}

/* BADGES */
.badge{
  display:inline-flex;align-items:center;padding:4px 11px;
  border-radius:20px;font-size:11px;font-weight:600;
  letter-spacing:.01em;white-space:nowrap;
}
.badge-current,.badge-processingorder{
  background:linear-gradient(180deg,rgba(229,0,26,.2) 0%,rgba(229,0,26,.1) 100%);
  color:#ff8590;border:1px solid var(--red-dim);
}
.badge-visited{
  background:linear-gradient(180deg,rgba(63,185,80,.15) 0%,rgba(63,185,80,.08) 100%);
  color:var(--green-soft);border:1px solid #2ea043;
}
.badge-pending{background:var(--surface2);color:var(--muted);border:1px solid var(--border);}
.badge-delayed{
  background:linear-gradient(180deg,rgba(210,153,34,.18) 0%,rgba(210,153,34,.08) 100%);
  color:#f0c674;border:1px solid #9e6a03;
}
.badge-ontrack{
  background:linear-gradient(180deg,rgba(70,180,90,.14) 0%,rgba(70,180,90,.06) 100%);
  color:#7bd189;border:1px solid #2f7a3a;
}

/* INFO GRID */
.info-grid{display:grid;grid-template-columns:1fr 1fr;gap:.4rem 2rem;}
.info-row{display:flex;flex-direction:column;gap:3px;padding:.65rem 0;border-bottom:1px solid var(--border);}
.info-row:last-child,.info-row:nth-last-child(2):nth-child(odd){border:none;}
.info-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;font-weight:500;}
.info-value{font-size:14px;font-weight:500;color:var(--text-strong);}

/* TIMELINE */
.timeline{display:flex;flex-direction:column;}
.step-item{display:flex;align-items:flex-start;gap:16px;padding:.75rem 0;position:relative;}
.step-item:not(:last-child)::after{
  content:'';position:absolute;left:13px;top:34px;
  width:2px;height:calc(100% - 4px);background:var(--border);
}
.step-dot{
  width:28px;height:28px;border-radius:50%;flex-shrink:0;z-index:1;
  display:flex;align-items:center;justify-content:center;
  font-size:12px;font-weight:700;
  transition:all .3s var(--ease);
}
.dot-current{
  background:linear-gradient(135deg,var(--red-bright) 0%,var(--red) 100%);
  box-shadow:0 0 0 5px rgba(229,0,26,.18),0 0 20px rgba(229,0,26,.4);
  color:#fff;
  animation:pulse 2.5s ease-in-out infinite;
}
.dot-visited{
  background:linear-gradient(135deg,var(--green-soft) 0%,var(--green) 100%);
  color:#fff;box-shadow:0 2px 8px rgba(63,185,80,.3);
}
.dot-pending{background:var(--surface2);border:2px solid var(--border-strong);}
.step-name{font-weight:500;font-size:14px;color:var(--text-strong);}
.step-meta{font-size:12px;color:var(--muted);margin-top:2px;}

/* ROUTE LIST */
.route-item{display:flex;align-items:center;gap:14px;padding:.7rem 0;border-bottom:1px solid var(--border);}
.route-item:last-child{border:none;}
.route-icon{
  width:36px;height:36px;border-radius:8px;
  background:linear-gradient(180deg,var(--surface2) 0%,var(--surface) 100%);
  border:1px solid var(--border);
  display:flex;align-items:center;justify-content:center;
  font-size:16px;flex-shrink:0;
}
.route-name{font-weight:500;font-size:13px;color:var(--text-strong);}
.route-type{font-size:11px;color:var(--muted);margin-top:2px;}

/* LOGIN */
.login-wrap{max-width:460px;margin:3rem auto;padding:0 1rem;}
.login-wrap h1{
  font-size:26px;font-weight:600;margin-bottom:.5rem;
  letter-spacing:-.015em;color:var(--text-strong);
}
.login-wrap .sub{color:var(--muted);font-size:14px;margin-bottom:1.75rem;line-height:1.6;}
.benefits{display:grid;grid-template-columns:1fr 1fr;gap:.7rem;margin-bottom:1.75rem;}
.benefit{
  background:linear-gradient(180deg,var(--surface2) 0%,var(--surface) 100%);
  border:1px solid var(--border);
  border-radius:10px;padding:.95rem;
  transition:all .25s var(--ease);
}
.benefit:hover{border-color:var(--border-strong);transform:translateY(-1px);box-shadow:var(--shadow);}
.benefit-icon{font-size:18px;margin-bottom:6px;}
.benefit-title{font-size:12px;font-weight:600;color:var(--text-strong);margin-bottom:3px;}
.benefit-desc{font-size:11px;color:var(--muted);line-height:1.5;}

/* AUTO-REFRESH TOGGLE — clean dedicated styles instead of inline */
.auto-refresh-toggle{
  display:flex;align-items:center;gap:10px;
  padding:12px 14px;margin-top:1rem;
  background:var(--surface2);border:1px solid var(--border);
  border-radius:10px;cursor:pointer;
  transition:all .2s var(--ease);
}
.auto-refresh-toggle:hover{border-color:var(--border-strong);}
.auto-refresh-toggle input{
  width:16px;height:16px;accent-color:var(--red);
  cursor:pointer;flex-shrink:0;margin:0;
}
.auto-refresh-toggle-text{
  font-size:13px;color:var(--text);line-height:1.4;
}
.credentials-note{
  display:flex;align-items:flex-start;gap:8px;
  margin-top:10px;padding:10px 12px;
  background:rgba(227,179,65,.06);border:1px solid rgba(227,179,65,.18);
  border-radius:8px;
  font-size:12px;color:var(--muted);line-height:1.55;
}
.credentials-note svg{
  flex-shrink:0;margin-top:1px;color:#e3b341;
  width:13px;height:13px;
}
.credentials-note strong{color:var(--text);font-weight:600;}
.credentials-note a{color:var(--red-bright);text-decoration:underline;}

/* FORMS */
.form-group{margin-bottom:1.1rem;}
.form-group label{
  display:block;font-size:11px;font-weight:600;color:var(--muted);
  text-transform:uppercase;letter-spacing:.06em;margin-bottom:7px;
}
.form-group input{
  width:100%;background:var(--surface2);border:1px solid var(--border);
  color:var(--text-strong);
  padding:11px 13px;border-radius:9px;font-size:14px;
  font-family:inherit;outline:none;
  transition:border-color .2s var(--ease),box-shadow .2s var(--ease),background .2s var(--ease);
}
.form-group input:hover{border-color:var(--border-strong);}
.form-group input:focus{
  border-color:var(--red);background:var(--bg-elev);
  box-shadow:0 0 0 3px rgba(229,0,26,.15);
}
.btn{
  background:linear-gradient(180deg,var(--red-bright) 0%,var(--red) 100%);
  color:#fff;border:none;
  padding:12px 22px;border-radius:9px;
  font-size:14px;font-weight:600;cursor:pointer;width:100%;
  font-family:inherit;letter-spacing:.005em;
  box-shadow:0 2px 8px rgba(229,0,26,.3),inset 0 1px 0 rgba(255,255,255,.15);
  transition:all .2s var(--ease);
}
.btn:hover{transform:translateY(-1px);box-shadow:0 6px 16px rgba(229,0,26,.4),inset 0 1px 0 rgba(255,255,255,.2);}
.btn:active{transform:translateY(0);}

/* PRIVACY / ALERT */
.privacy{
  background:linear-gradient(180deg,var(--surface2) 0%,var(--surface) 100%);
  border:1px solid var(--border);border-radius:var(--radius);
  padding:1.35rem;margin-top:1.75rem;
}
.privacy-title{
  font-size:11px;font-weight:600;color:var(--muted);
  text-transform:uppercase;letter-spacing:.07em;margin-bottom:.8rem;
}
.privacy p{font-size:12px;color:var(--muted);line-height:1.7;margin-bottom:.5rem;}
.privacy p:last-child{margin-bottom:0;}
.privacy code{
  background:var(--bg);padding:2px 6px;border-radius:5px;
  font-size:11px;color:var(--text);
  font-family:'SF Mono',Menlo,Consolas,monospace;
}
.alert{
  background:linear-gradient(180deg,rgba(229,0,26,.12) 0%,rgba(229,0,26,.05) 100%);
  border:1px solid var(--red-dim);border-radius:var(--radius);
  padding:1.1rem;color:#ff8590;font-size:13px;
}

/* STATS */
.stat-grid{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:.85rem;margin-bottom:1.75rem;
}
.stat-card{
  background:linear-gradient(180deg,var(--surface) 0%,var(--bg-elev) 100%);
  border:1px solid var(--border);border-radius:var(--radius);
  padding:1.25rem;
  transition:all .25s var(--ease);
  position:relative;overflow:hidden;
}
.stat-card::before{
  content:'';position:absolute;inset:0;
  background:radial-gradient(circle at 100% 0%,rgba(229,0,26,.035) 0%,transparent 60%);
  pointer-events:none;
}
.stat-card:hover{border-color:var(--border-strong);transform:translateY(-1px);}
.stat-num{
  font-size:30px;font-weight:600;color:#ff5a6e;
  line-height:1;letter-spacing:-.02em;position:relative;
}
.stat-lbl{
  font-size:11px;color:var(--muted);margin-top:8px;
  text-transform:uppercase;letter-spacing:.05em;font-weight:500;position:relative;
}

/* PAGE HEADER */
.page-header{margin-bottom:1.75rem;}
.page-title{
  font-size:22px;font-weight:600;color:var(--text-strong);
  letter-spacing:-.015em;line-height:1.3;margin-bottom:4px;
}
.page-sub{font-size:13px;color:var(--muted);line-height:1.5;}

/* BARS */
.bar-row{margin:.65rem 0;}
.bar-head{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-bottom:5px;}
.bar-head span:last-child{font-weight:600;color:var(--text-strong);}
.bar-bg{background:var(--surface2);border-radius:4px;height:8px;overflow:hidden;}
.bar-fill{
  background:linear-gradient(90deg,var(--red) 0%,var(--red-bright) 100%);
  border-radius:4px;height:8px;
  box-shadow:0 0 8px rgba(229,0,26,.4);
  transition:width .6s var(--ease);
}
.bar-fill-blue{
  background:linear-gradient(90deg,#1f6feb 0%,#58a6ff 100%);
  border-radius:4px;height:8px;box-shadow:0 0 8px rgba(31,111,235,.4);
}
.bar-sub{font-size:10px;color:var(--muted-soft);margin-top:3px;}

/* TABLES */
.data-table{width:100%;border-collapse:collapse;}
.data-table th{
  font-size:11px;font-weight:600;color:var(--muted);text-align:left;
  padding:9px 12px;border-bottom:1px solid var(--border);
  text-transform:uppercase;letter-spacing:.06em;
  background:rgba(0,0,0,.15);
}
.data-table td{padding:10px 12px;border-bottom:1px solid var(--border);font-size:13px;color:var(--text);}
.data-table tr:last-child td{border:none;}
.data-table tr{transition:background .15s var(--ease);}
.data-table tr:hover td{background:var(--surface2);}

/* SECTION HEAD */
.section-head{
  font-size:14px;font-weight:600;color:var(--text-strong);
  margin-bottom:1.1rem;padding-bottom:.7rem;
  border-bottom:1px solid var(--border);letter-spacing:-.005em;
}

/* VESSEL CARD */
.route-map{
  height:380px;border-radius:10px;margin-bottom:1.25rem;
  border:1px solid var(--border);overflow:hidden;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.03),0 4px 16px rgba(0,0,0,.25);
}
@media (max-width:768px){
  .route-map{height:300px;}
}

/* VESSEL SKELETON (loading state) */
.vessel-skeleton{
  background:linear-gradient(180deg,var(--surface) 0%,var(--bg-elev) 100%);
  border:1px solid var(--border);border-radius:var(--radius);
  padding:1.15rem 1.25rem;margin-bottom:1.25rem;
  position:relative;overflow:hidden;
}
.vessel-skeleton::before{
  content:'';position:absolute;left:0;top:0;bottom:0;width:3px;
  background:linear-gradient(180deg,#444 0%,#222 100%);
}
.skel-line{
  height:12px;background:var(--surface2);border-radius:5px;
  margin-bottom:8px;position:relative;overflow:hidden;
}
.skel-line::after{
  content:'';position:absolute;inset:0;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.05),transparent);
  animation:shimmer 1.5s linear infinite;
}
@keyframes shimmer{
  from{transform:translateX(-100%);}
  to{transform:translateX(100%);}
}
.skel-line.skel-label{width:35%;height:8px;}
.skel-line.skel-title{width:55%;height:18px;margin-top:4px;}
.skel-line.skel-meta{width:75%;}
.skel-status{
  font-size:11px;color:var(--muted);margin-top:10px;
  display:flex;align-items:center;gap:7px;
}
.skel-spinner{
  width:11px;height:11px;border-radius:50%;
  border:2px solid var(--surface2);border-top-color:var(--red);
  animation:spin 1s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg);}}

/* VESSEL STALE INDICATOR */
.vessel-stale{
  display:inline-flex;align-items:center;gap:5px;
  font-size:10px;font-weight:600;
  background:rgba(210,153,34,.12);color:#e3b341;
  border:1px solid rgba(210,153,34,.3);
  padding:2px 8px;border-radius:20px;
  margin-left:8px;text-transform:none;letter-spacing:0;
}
.vessel-stale::before{
  content:'';width:5px;height:5px;border-radius:50%;background:#e3b341;
}

/* VOYAGE PROGRESS */
.voyage-progress{
  margin-top:.85rem;padding-top:.85rem;
  border-top:1px solid var(--border);
}
.voyage-head{
  display:flex;justify-content:space-between;align-items:baseline;
  font-size:11px;color:var(--muted);margin-bottom:6px;
  font-weight:500;
}
.voyage-from,.voyage-to{
  color:var(--text);font-size:12px;font-weight:600;
}
.voyage-pct{
  color:var(--red-bright);font-weight:700;font-size:11px;
  font-feature-settings:'tnum';letter-spacing:.02em;
}
.voyage-bar-bg{
  height:6px;background:var(--surface2);border-radius:3px;
  overflow:hidden;position:relative;
}
.voyage-bar{
  height:100%;
  background:linear-gradient(90deg,var(--red) 0%,var(--red-bright) 100%);
  box-shadow:0 0 8px rgba(229,0,26,.5);
  border-radius:3px;
  transition:width 1s var(--ease);
  position:relative;
}
.voyage-bar::after{
  content:'';position:absolute;right:-2px;top:50%;transform:translateY(-50%);
  width:10px;height:10px;border-radius:50%;background:#fff;
  box-shadow:0 0 0 2px var(--red-bright),0 0 12px rgba(229,0,26,.6);
}

/* ETA BANNER */
.eta-banner{
  display:flex;align-items:center;gap:14px;
  background:linear-gradient(135deg,rgba(31,111,235,.12) 0%,rgba(31,111,235,.05) 100%);
  border:1px solid rgba(31,111,235,.25);
  border-radius:var(--radius);
  padding:1.1rem 1.25rem;margin-bottom:1.25rem;
}
.eta-icon{
  width:42px;height:42px;border-radius:10px;flex-shrink:0;
  background:linear-gradient(135deg,#1f6feb 0%,#0d4ea1 100%);
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 4px 12px rgba(31,111,235,.3);
}
.eta-icon svg{width:22px;height:22px;color:#fff;}
.eta-content{flex:1;min-width:0;}
.eta-label{
  font-size:10px;font-weight:600;color:#7bb3ff;
  text-transform:uppercase;letter-spacing:.08em;margin-bottom:3px;
}
.eta-value{
  font-size:17px;font-weight:600;color:var(--text-strong);
  letter-spacing:-.01em;
}
.eta-detail{font-size:12px;color:var(--muted);margin-top:2px;}
@media (max-width:520px){
  .eta-banner{padding:.95rem 1rem;gap:11px;}
  .eta-icon{width:38px;height:38px;}
  .eta-value{font-size:16px;}
}

/* VESSEL CARD */
.vessel-card{
  background:
    linear-gradient(180deg,rgba(31,111,235,.04) 0%,transparent 100%),
    linear-gradient(180deg,var(--surface) 0%,var(--bg-elev) 100%);
  border:1px solid var(--border);border-radius:var(--radius);
  padding:1.15rem 1.25rem;margin-bottom:1.25rem;
  position:relative;overflow:hidden;
  box-shadow:var(--shadow-sm);
}
.vessel-card::before{
  content:'';position:absolute;left:0;top:0;bottom:0;width:3px;
  background:linear-gradient(180deg,var(--red-bright) 0%,var(--red) 100%);
  box-shadow:0 0 12px rgba(229,0,26,.5);
}
.vessel-card-header{
  display:flex;justify-content:space-between;align-items:flex-start;
  gap:1rem;flex-wrap:wrap;
}
.vessel-card-main{flex:1;min-width:0;}
.vessel-card-label{
  display:flex;align-items:center;gap:7px;
  font-size:10px;font-weight:600;color:var(--muted);
  text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px;
}
.vessel-pulse{
  display:inline-block;width:8px;height:8px;border-radius:50%;
  background:var(--green-soft);
  box-shadow:0 0 0 3px rgba(63,185,80,.2),0 0 8px rgba(63,185,80,.6);
  animation:pulseGreen 2s ease-in-out infinite;
}
@keyframes pulseGreen{
  0%,100%{box-shadow:0 0 0 3px rgba(63,185,80,.2),0 0 8px rgba(63,185,80,.6);}
  50%{box-shadow:0 0 0 6px rgba(63,185,80,.05),0 0 12px rgba(63,185,80,.3);}
}
.vessel-card-name{
  font-size:17px;font-weight:600;color:var(--text-strong);
  letter-spacing:-.01em;margin-bottom:6px;
}
.vessel-card-meta{display:flex;gap:1.2rem;flex-wrap:wrap;font-size:13px;color:var(--muted);}
.vessel-meta-item{display:flex;align-items:center;gap:6px;}
.vessel-meta-item svg{flex-shrink:0;opacity:.7;}
.vessel-meta-item span{color:var(--text);}
.vessel-card-side{text-align:right;flex-shrink:0;}
.vessel-card-mmsi{
  font-size:12px;color:var(--muted);
  font-family:'SF Mono',Menlo,Consolas,monospace;letter-spacing:-.01em;
}
.vessel-card-mmsi span{color:var(--text-strong);font-weight:500;}
.vessel-card-source{
  font-size:10px;color:var(--muted-soft);margin-top:3px;
  text-transform:uppercase;letter-spacing:.05em;
}
.vessel-card-links{
  display:flex;gap:.5rem;margin-top:.85rem;flex-wrap:wrap;
  padding-top:.85rem;border-top:1px solid var(--border);
}
.vessel-card-links a{
  font-size:12px;padding:5px 11px;
  background:var(--surface2);border:1px solid var(--border);
  border-radius:7px;color:var(--text);
  display:inline-flex;align-items:center;gap:5px;
  transition:all .2s var(--ease);
}
.vessel-card-links a:hover{
  background:var(--surface3);border-color:var(--border-strong);
  color:var(--text-strong);text-decoration:none;
}
@media (max-width:520px){
  .vessel-card-side{text-align:left;width:100%;}
  .vessel-card-name{font-size:17px;}
}

/* ANIMATIONS */
@keyframes pulse{
  0%,100%{box-shadow:0 0 0 5px rgba(229,0,26,.18),0 0 20px rgba(229,0,26,.4);}
  50%{box-shadow:0 0 0 9px rgba(229,0,26,.04),0 0 28px rgba(229,0,26,.2);}
}
@keyframes fadeUp{
  from{opacity:0;transform:translateY(8px);}
  to{opacity:1;transform:translateY(0);}
}
.card{animation:fadeUp .4s var(--ease) backwards;}
.card:nth-child(1){animation-delay:0s;}
.card:nth-child(2){animation-delay:.05s;}
.card:nth-child(3){animation-delay:.1s;}
.card:nth-child(4){animation-delay:.15s;}

/* MOBILE */
@media (max-width: 768px){
  .nav{padding:0 1rem;height:56px;}
  .nav-brand-name{font-size:13px;}
  .nav-toggle{display:flex;}
  .nav-links{
    display:none;
    position:absolute;top:56px;left:0;right:0;
    flex-direction:column;align-items:stretch;
    background:rgba(10,13,18,.98);
    backdrop-filter:blur(20px) saturate(180%);
    -webkit-backdrop-filter:blur(20px) saturate(180%);
    border-bottom:1px solid var(--border);
    padding:.75rem 1rem 1rem;gap:2px;
    box-shadow:var(--shadow-lg);
  }
  .nav-links.open{display:flex;}
  .nav-link{padding:11px 14px;font-size:14px;border-radius:9px;width:100%;}
  .nav-divider{display:none;}
  #nav-auto-check{align-self:flex-start!important;}

  .container{padding:1rem .85rem;max-width:100%;}
  .card{padding:1.1rem;margin-bottom:.85rem;border-radius:var(--radius);}
  .card-title{margin-bottom:.85rem;}

  .info-grid{grid-template-columns:1fr;gap:0;}
  .info-row{padding:.55rem 0;}
  .info-row:nth-last-child(2):nth-child(odd){border-bottom:1px solid var(--border);}

  .step-item{gap:13px;padding:.65rem 0;}
  .step-item:not(:last-child)::after{left:13px;top:32px;}

  .login-wrap{margin:1.5rem auto;}
  .login-wrap h1{font-size:24px;}
  .benefits{grid-template-columns:1fr;}

  .stat-grid{grid-template-columns:repeat(2,1fr);gap:.65rem;}
  .stat-card{padding:1rem;}
  .stat-num{font-size:26px;}

  .data-table{font-size:12px;}
  .data-table th,.data-table td{padding:7px 8px;}

  .btn{padding:13px 22px;font-size:15px;}
  .form-group input{padding:13px 14px;font-size:16px;}
}
@media (max-width: 420px){
  .stat-grid{grid-template-columns:1fr;}
  .container{padding:.85rem .7rem;}
  .card{padding:1rem;}
}
@media (hover: none){
  .card:hover{border-color:var(--border);}
  .btn:hover{transform:none;}
}
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
  <button class="nav-toggle" id="nav-toggle" aria-label="Toggle menu">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
      <line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/>
    </svg>
  </button>
  <div class="nav-links" id="nav-links">
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
(function(){
  var t=document.getElementById('nav-toggle');
  var l=document.getElementById('nav-links');
  if(!t||!l)return;
  t.addEventListener('click',function(e){e.stopPropagation();l.classList.toggle('open');});
  document.addEventListener('click',function(e){
    if(!l.contains(e.target)&&!t.contains(e.target))l.classList.remove('open');
  });
  l.querySelectorAll('a').forEach(function(a){
    a.addEventListener('click',function(){l.classList.remove('open');});
  });
})();
</script>
"""

TRACKER_PAGE = BASE + """
<div class="container">
{% if not username %}
  <div class="login-wrap">
    <h1>Toyota Europe Order Tracker</h1>
    <p class="sub">Know exactly where your car is — from the factory floor in Japan to your dealer's door.</p>

    {# A rejected login (CSRF mismatch, or too many attempts) leaves username
       empty, so it lands on this branch rather than the elif-error branch
       below. Without this the request was refused completely silently and the
       page just reappeared with no explanation. #}
    {% if error %}
    <div class="alert" style="margin-bottom:1rem;">⚠ {{ error }}</div>
    {% endif %}

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
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <div class="form-group">
          <label>Email address</label>
          <input type="email" name="username" id="inp-email" placeholder="your@email.com" required autofocus>
        </div>
        <div class="form-group">
          <label>Password</label>
          <input type="password" name="password" id="inp-password" required>
        </div>
        <button type="submit" class="btn">Check my order →</button>
        <label class="auto-refresh-toggle">
          <input type="checkbox" id="auto-refresh-toggle">
          <span class="auto-refresh-toggle-text">Auto-refresh every 2 hours while tab is open</span>
        </label>
        <div class="credentials-note">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <circle cx="12" cy="12" r="10"/>
            <line x1="12" y1="8" x2="12" y2="12"/>
            <line x1="12" y1="16" x2="12.01" y2="16"/>
          </svg>
          <span>Use your <strong>My Toyota</strong> credentials — same as <a href="https://my.toyota.eu" target="_blank">my.toyota.eu</a></span>
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
      <p>Your email and password are sent to this server, which passes them straight to
         Toyota's API at <code>ssoms.toyota-europe.com</code> to authenticate you. They are
         held in memory for the duration of the request only — never saved to our database
         and never written to a log.</p>
      <p>Only anonymized stats are stored: model, step, country, delay flag, and a one-way
         hash of your order ID. Your name, email and password are never stored. Toyota's own
         order-status files, which contain your raw order ID, are cached on the server so
         step dates can be tracked over time. See
         <a href="https://github.com/Egyras/toyota-tracker/blob/main/web.py" target="_blank">
         save_stats()</a> in the source code.</p>
      <p>If you enable <strong>auto-refresh</strong>, your email and password are also kept in
         your browser's <code>sessionStorage</code> so the page can re-submit them every two
         hours. They are cleared when you close the tab. If you would rather they were not
         held in the browser at all, leave auto-refresh off and log in manually.</p>
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
      {% if st.isDelayed %}
        <span class="badge badge-delayed">⚠ Delayed</span>
      {% else %}
        <span class="badge badge-ontrack">✓ No delays</span>
      {% endif %}
      {% if st.damageCode %}
        <span class="badge badge-delayed">⚡ {{ st.damageCode }}</span>
      {% else %}
        <span class="badge badge-ontrack">✓ No damage</span>
      {% endif %}
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
        <span class="info-value">{{ order.etaToFinalDestination or order.currentStatus.estimatedDeliveryToFinalDestination or 'N/A' }}</span>
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
            {% set step_observed = order._step_observed.get(step_name, -1) if order._step_observed else -1 %}
            {% set step_bounded = order._lf_bounded if step_name == 'leftTheFactory' else (step_observed == 1 or order._logins > 1) %}
            {% if order._logins == 1 or (step_name == 'leftTheFactory' and not order._lf_bounded) %}
              {% set rel_icon = '⚠️' %}
              {% set rel_label = 'Estimated' %}
              {% set rel_desc = order._logins == 1 and 'First login — shows when you checked, not when step happened' or 'Step was already completed on first login — actual date unknown, could be days earlier' %}
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
            {% if event == 'visited' and step_name == 'leftTheFactory' %}
            {# 'visited' for leftTheFactory means when we OBSERVED it was already done,
               not when the car actually left — skip it to avoid confusion (e.g. showing
               July 20 as "visited" when the car left in late May, just because nobody
               logged in until July to notice the transition) #}
            {% else %}
            {# When this is the active step AND the car has since moved to a later
               stop within the same step, 'current' is the date the STEP began —
               not the date it reached the location shown above. Relabel so the
               two dates can't be read as the same event. #}
            {% set stop_moved = (s == 'current' and event == 'current'
                                 and order._current_stop
                                 and order._current_stop.date != date) %}
            <div style="display:flex;align-items:center;gap:8px;margin-top:6px;flex-wrap:wrap;">
              <div style="font-size:12px;color:var(--red);font-weight:500;"
                   {% if stop_moved %}title="When this stage began, at the first hub of the stage"{% endif %}>
                {{ 'stage started' if stop_moved else event }}: {{ date }}</div>
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
            {% endif %}
            {% endfor %}
            {# Per-stop date: when the car actually reached the location shown
               above. Derived from deliveries history, so it advances every time
               the car moves to a new hub — unlike the step-level date. #}
            {% if s == 'current' and order._current_stop
                  and order._current_stop.date != step_dates[step_name].get('current') %}
            <div style="display:flex;align-items:center;gap:8px;margin-top:6px;flex-wrap:wrap;">
              <div style="font-size:12px;color:var(--red);font-weight:500;"
                   title="First check in which {{ order._current_stop.location }} reported this state">
                {{ 'at' if order._current_stop.state == 'current' else 'en route to' }}
                {{ order._current_stop.location }} since: {{ order._current_stop.date }}</div>
            </div>
            {% endif %}
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
  {# Served from our own origin (see /vendor/leaflet) so the CSP can forbid
     third-party script entirely — this page holds credentials in sessionStorage. #}
  <link rel="stylesheet" href="/vendor/leaflet/leaflet.css"/>
  <script src="/vendor/leaflet/leaflet.js"></script>
  <div class="card">
    <div class="card-title">Delivery route</div>

    <div id="route-map" class="route-map"></div>

    <!-- ETA banner (estimated dealer arrival) -->
    <div id="eta-banner" class="eta-banner" style="display:none;">
      <div class="eta-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
        </svg>
      </div>
      <div class="eta-content">
        <div class="eta-label">Estimated dealer arrival</div>
        <div class="eta-value" id="eta-value">—</div>
        <div class="eta-detail" id="eta-detail">Based on current vessel position and route</div>
      </div>
    </div>

    <!-- Skeleton while vessel detection runs -->
    <div id="vessel-skeleton" class="vessel-skeleton" style="display:none;">
      <div class="skel-line skel-label"></div>
      <div class="skel-line skel-title"></div>
      <div class="skel-line skel-meta"></div>
      <div class="skel-status">
        <div class="skel-spinner"></div>
        <span>Locating vessel · checking departures and Toyota carriers…</span>
      </div>
    </div>

    <!-- Vessel tracking card (shown when vessel detected) -->
    <div id="vessel-info" class="vessel-card" style="display:none;">
      <div class="vessel-card-header">
        <div class="vessel-card-main">
          <div class="vessel-card-label">
            <span class="vessel-pulse"></span>Vessel detected · Live
            <span class="vessel-stale" id="vessel-stale" style="display:none;">Updated ?h ago</span>
            <span id="vessel-unverified" style="display:none;margin-left:8px;
                  background:rgba(227,179,65,0.15);border:1px solid rgba(227,179,65,0.4);
                  border-radius:10px;padding:1px 8px;font-size:10px;color:#e3b341;
                  font-weight:500;cursor:help;"
                  title="This MMSI is not in our Toyota carrier database. The name matches a PCC pattern, so it may be a new charter — but please verify on MyShipTracking.">
              ⚠ Unverified carrier
            </span>
          </div>
          <div class="vessel-card-name" id="vessel-name">—</div>
          <div class="vessel-card-meta">
            <span class="vessel-meta-item">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:13px;height:13px;">
                <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>
              </svg>
              <span id="vessel-speed">—</span>
            </span>
            <span class="vessel-meta-item">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:13px;height:13px;">
                <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/>
              </svg>
              <span id="vessel-dest">—</span>
            </span>
          </div>
        </div>
        <div class="vessel-card-side">
          <div class="vessel-card-mmsi">MMSI <span id="vessel-mmsi">—</span></div>
          <div class="vessel-card-source" id="vessel-updated" style="display:none;"></div>
        </div>
      </div>
      <!-- Voyage progress -->
      <div class="voyage-progress">
        <div class="voyage-head">
          <span><span class="voyage-from" id="voyage-from">Origin</span> → <span class="voyage-to" id="voyage-to">Destination</span></span>
          <span class="voyage-pct" id="voyage-pct">0%</span>
        </div>
        <div class="voyage-bar-bg">
          <div class="voyage-bar" id="voyage-bar" style="width:0%;"></div>
        </div>
        <div id="voyage-next-stop" style="display:none;margin-top:6px;font-size:11px;color:var(--muted);">
          Next stop: <span id="voyage-next-dest" style="color:var(--text);font-weight:600;"></span>
          <span id="voyage-next-eta" style="color:var(--red-bright);font-weight:600;margin-left:4px;"></span>
        </div>
      </div>
      <div class="vessel-card-links" id="vessel-links"></div>
    </div>

    <!-- Vessel auto-detection runs automatically, inline controls below for manual override -->

    <!-- Vessel date prompt — shown when:
         (a) leftTheFactory date is NOT reliable (BIP→LF transition not observed), OR
         (b) date was set but auto-detection found no European-bound PCC matching
         The user can either correct the date or enter the MMSI directly from MyShipTracking. -->
    <div id="vessel-date-prompt" style="display:none;margin-bottom:1.25rem;padding:12px 16px;
         background:rgba(229,0,26,0.06);border:1px solid rgba(229,0,26,0.2);
         border-radius:8px;font-size:13px;line-height:1.65;">
      <div style="font-weight:600;color:var(--text);margin-bottom:6px;">
        🚢 Carrier unknown — help us identify your vessel
      </div>
      <div style="color:var(--muted);">
        We couldn't automatically identify the carrier for your order. Either we didn't
        witness the factory departure transition, or no European-bound Toyota PCC matched
        the date we have.
        <br><br>
        Toyota sent you an email when your car left the factory
        (subject: <em>"Your vehicle has left the factory"</em> or similar).
        <strong style="color:var(--text);">Enter the date from that email</strong>
        in the <strong style="color:var(--text);">Factory departure</strong> field below
        and click <strong style="color:var(--red);">🔍 Detect</strong>.
        <br><br>
        <span style="font-size:11px;opacity:0.85;">
          📅 <strong>Note:</strong> Port departure data is only available for the last ~20 days.
          If your car left the factory more than three weeks ago — or if you already entered
          the date and detection still failed — find your ship at
          <a href="https://www.myshiptracking.com" target="_blank"
             style="color:#e3b341;">MyShipTracking</a> and paste the MMSI directly into the
          <strong>MMSI</strong> field below.
        </span>
      </div>
    </div>

    <!-- MST history limit warning — only shown from the detection-FAILURE
         branch below (see fetch('/api/vessel-detect...').then(...) else-if),
         never eagerly on page load. Previously this script unconditionally
         set display:block based purely on days-since-factory > 20, with no
         check for whether detection actually succeeded — so a confirmed,
         live, berth-verified vessel would still show "detection may not
         work" right next to it, which is contradictory and confusing. Now
         we only compute/stash the day count here; actual visibility is
         decided where we know the real outcome. -->
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
      // Stash for the detection-failure branch to consult — it decides
      // whether to actually show this warning, only once we know
      // detection genuinely failed (not just based on day count alone).
      window.daysSinceFactoryDeparture = days;
    })();
    </script>
    {% endif %}

    {# ── The one active leg ───────────────────────────────────────────────────
       Exactly one voyage is ever being detected, and `active.key` names the port
       it DEPARTED from (same rule as the leg resolver in the script below: the
       last vessel hub marked 'visited').

       `active.row` is the stop that represents where the car is now, so the
       Detect controls render on that row only. Previously every unvisited vessel
       stop rendered its own controls keyed to that stop's OWN name — so on the
       Malmo row you got id="date-malmo", while auto-detection was reading
       overrides['zeebrugge']. Anything typed there went into a leg nothing reads.
       Keying the controls off `active.key` guarantees the manual override and the
       auto-detection address the same leg. #}
    {% set active = namespace(key='nagoya', row='') %}
    {% for d in delivs %}
    {% set aloc = d.locationName | lower %}
    {% if d.isVisited == 'visited' and d.destinationType in ['HUB','TRANSIT'] %}
      {% if 'zeebrugge' in aloc %}{% set active.key = 'zeebrugge' %}
      {% elif 'malmo' in aloc or 'malmö' in aloc %}{% set active.key = 'malmo' %}
      {% elif 'sagunto' in aloc %}{% set active.key = 'sagunto' %}
      {% elif 'livorno' in aloc %}{% set active.key = 'livorno' %}
      {% elif 'bristol' in aloc or 'portbury' in aloc %}{% set active.key = 'portbury' %}
      {% elif 'southampton' in aloc %}{% set active.key = 'southampton' %}
      {% elif 'drammen' in aloc %}{% set active.key = 'drammen' %}
      {% elif 'piraeus' in aloc %}{% set active.key = 'piraeus' %}
      {% elif 'gothenburg' in aloc or 'göteborg' in aloc %}{% set active.key = 'gothenburg' %}
      {% endif %}
    {% endif %}
    {% endfor %}
    {# Current position: the stop the car is AT, else the one it is en route to,
       else the next one it has not reached. First match wins. #}
    {% for d in delivs %}
      {% if not active.row and d.isVisited == 'current' %}{% set active.row = d.locationName %}{% endif %}
    {% endfor %}
    {% for d in delivs %}
      {% if not active.row and d.isVisited == 'inTransit' %}{% set active.row = d.locationName %}{% endif %}
    {% endfor %}
    {% for d in delivs %}
      {% if not active.row and d.isVisited == 'notVisited'
            and (d.transportMethod == 'Vessel' or d.destinationType in ['HUB','TRANSIT']) %}
        {% set active.row = d.locationName %}
      {% endif %}
    {% endfor %}

    {% for d in delivs %}
    {% set v = d.isVisited %}
    {% set is_vessel = d.transportMethod == 'Vessel' or d.destinationType in ['FACTORY','HUB'] %}
    {% set leg_key = 'nagoya' if d.destinationType == 'FACTORY' else
                     'zeebrugge' if 'Zeebrugge' in d.locationName else
                     'malmo' if 'Malmo' in d.locationName or 'Malmö' in d.locationName else
                     'bremerhaven' if 'Bremerhaven' in d.locationName else
                     'southampton' if 'Southampton' in d.locationName else
                     'gothenburg' if 'Gothenburg' in d.locationName else
                     'sagunto' if 'Sagunto' in d.locationName else
                     'livorno' if 'Livorno' in d.locationName else
                     'portbury' if 'Portbury' in d.locationName or 'Bristol' in d.locationName else
                     'drammen' if 'Drammen' in d.locationName else
                     'piraeus' if 'Piraeus' in d.locationName else 'nagoya' %}
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
          {% elif v in ['inTransit', 'current'] %}badge-current
          {% else %}badge-pending{% endif %}">{{ v }}</span>
      </div>
      {# Controls render on the car's current position only, keyed to the active
         leg — see the `active` namespace above for why they are not keyed to
         this row's own location. #}
      {# TRANSIT stops count too: Toyota labels Malmo → Paldiski as 'Truck', but the
         crossing is still a ship departure from Malmo, so the user needs the
         controls there to correct the leg. #}
      {% if d.locationName == active.row and (is_vessel or d.destinationType == 'TRANSIT') %}
      {% set leg_key = active.key %}
      {% set step_date = order._step_dates.leftTheFactory if leg_key == 'nagoya' else
                         order._step_dates.get(leg_key, {}) if order._step_dates else {} %}
      {% set days_gap = (order._days_tracked // (order._logins - 1 if order._logins > 1 else 1)) if order._logins > 1 else 99 %}
      {% set date_reliable = order._logins >= 2 and days_gap <= 3 and order._lf_bounded %}
      <div style="margin:6px 0 2px 44px;">
        {# Name the voyage explicitly. The controls sit on the current stop but
           describe the leg INTO it, which departs from a different port. #}
        <div style="font-size:11px;color:var(--muted);margin-bottom:4px;">
          Voyage {{ 'Toyota City' if leg_key == 'nagoya' else leg_key|capitalize }}
          → {{ d.locationName }} · departure date from {{ 'Toyota City' if leg_key == 'nagoya' else leg_key|capitalize }}
        </div>
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
        /* NB: \\n not \n — TRACKER_PAGE is a plain Python string, so a single
           backslash-n is turned into a REAL newline before the browser sees it,
           and a literal newline inside a '...' JS string is a syntax error that
           kills this entire <script> block (and with it the map and the vessel
           detection call further down). */
        alert('✅ Vessel: '+(d.name||d.mmsi)+'\\nPosition: '+d.lat+', '+d.lon+'\\nSource: '+(d.source||'cache'));
        location.reload();
      } else {
        alert('❌ No Toyota carrier found for '+leg+' around '+date+'.\\nTry adjusting the date by ±1-2 days.');
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
    // Expose hub names for voyage-progress display
    window.routeHubs = stops.map(function(s){
      // Extract city name from "City, Country" — take first segment
      var n = s.name || '';
      return n.split(',')[0].trim() || 'Hub';
    });
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
    {# Is there still a sea leg ahead of the car? #}
    {% set has_unvisited_hub = namespace(value=false) %}
    {% for d in (order.intermediateDeliveries or []) %}
      {% if d.isVisited in ['notVisited', 'inTransit', 'current'] and d.destinationType in ['HUB','TRANSIT'] %}
        {% set has_unvisited_hub.value = true %}
      {% endif %}
    {% endfor %}
    {% set _status = order.currentStatus.currentStatus %}

    {# Show the vessel section when:
        - the car is at sea on the deep-sea leg (LeftTheFactory / InTransit), or
        - we already have a cached vessel for it, or
        - it has LeftTheDepot at an INTERMEDIATE hub and another hub is still
          ahead — that means it is on a feeder vessel (e.g. Zeebrugge -> Malmo),
          which is exactly when people most want to see the ship.

       That third case was previously unreachable. show_vessel was initialised
       from a list that does not contain LeftTheDepot (so: false), and the
       has_unvisited_hub block below could only ever set it to false again —
       never back to true. The comment claimed to handle the case; the code
       could not. The whole vessel block, including the fetch that triggers
       detection, was therefore never rendered for a car sitting mid-route, and
       the symptom was silence: no /api/vessel-detect request, no error.

       ArrivedAtRetailer / ArrivedInDestination stay hidden regardless — the car
       is delivered, so remaining hubs are irrelevant. #}
    {% set show_vessel =
         _status in ['LeftTheFactory','leftTheFactory','InTransit','inTransit']
         or order._vessel_mmsi
         or (_status in ['LeftTheDepot','leftTheDepot'] and has_unvisited_hub.value) %}
    {% if show_vessel %}
    var vesselMarker = null;
    var vesselPulse = null;
    function loadVessel(mmsi, name, lat, lng, speed, course, dest, eta, ageMin, verified, departDate) {
      // verified: true if MMSI in TOYOTA_CARRIERS (known carrier), false if name fits
      // a PCC pattern but MMSI is unknown (possibly new Toyota charter we haven't catalogued).
      // Default true if not passed (backward compat with direct calls).
      if (verified == null) verified = true;
      // loadVessel only runs when detection actually succeeded (we have a
      // real mmsi/position to show). The "vessel detection may not work"
      // warning further down the page is a pure days-since-departure
      // heuristic with no knowledge of whether detection succeeded — so
      // without this, a confirmed live vessel would still show "detection
      // may not work" right below it, which is contradictory. Hide it here.
      var mstWarning = document.getElementById('mst-limit-warning');
      if (mstWarning) mstWarning.style.display = 'none';
      // Stash departure date for renderVoyageProgress (day-based progress %).
      window.currentDepartDate = departDate || null;
      if (vesselMarker) map.removeLayer(vesselMarker);
      if (vesselPulse) map.removeLayer(vesselPulse);
      // Rotation: AIS course (0-360°). Default 0 if missing.
      var rot = (course != null && course !== '' && course !== 0) ? course : 0;
      // Stale check: position > 3h old fades the icon
      var isStale = ageMin != null && ageMin > 180;
      var iconColor = isStale ? '#888' : '#e5001a';
      var glowOpacity = isStale ? '0.25' : '0.75';
      // SVG ship icon, rotates with course
      var shipSvg =
        '<svg viewBox="0 0 32 32" width="32" height="32" '+
        'style="transform:rotate('+rot+'deg);filter:drop-shadow(0 0 8px rgba(229,0,26,'+glowOpacity+'));transition:transform .4s ease;">'+
          '<defs><linearGradient id="sg" x1="0" y1="0" x2="0" y2="1">'+
            '<stop offset="0" stop-color="#ff5060"/><stop offset="1" stop-color="'+iconColor+'"/>'+
          '</linearGradient></defs>'+
          '<path d="M16 2 L22 14 L22 26 L10 26 L10 14 Z" fill="url(#sg)" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/>'+
          '<circle cx="16" cy="11" r="1.5" fill="#fff"/>'+
        '</svg>';
      var icon = L.divIcon({
        className:'',
        html:'<div style="position:relative;width:32px;">' +
             shipSvg +
             '<div style="position:absolute;top:34px;left:50%;transform:translateX(-50%);' +
             'background:rgba(10,13,18,0.92);color:#fff;font-size:10px;font-weight:600;' +
             'padding:2px 7px;border-radius:4px;white-space:nowrap;letter-spacing:.02em;' +
             'border:1px solid '+(isStale?'rgba(136,136,136,0.5)':'rgba(229,0,26,0.5)')+';' +
             'box-shadow:0 2px 6px rgba(0,0,0,0.4);">'+name+'</div>' +
             '</div>',
        iconSize:[32,52],iconAnchor:[16,16]
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
      // Subtle pulsing circle around vessel (skip if stale)
      if(!isStale){
        vesselPulse = L.circle([lat,lng],{
          radius:80000,color:'#e5001a',fillColor:'#e5001a',
          fillOpacity:0.05,weight:1.5,dashArray:'5 5'
        }).addTo(map);
      }
      // Show vessel info card
      var card = document.getElementById('vessel-info');
      var skeleton = document.getElementById('vessel-skeleton');
      if(skeleton) skeleton.style.display='none';
      if(card){
        document.getElementById('vessel-name').textContent = name;
        document.getElementById('vessel-speed').textContent = speed+' knots';
        document.getElementById('vessel-dest').textContent = dest||'—';
        document.getElementById('vessel-mmsi').textContent = mmsi;
        // Last updated time
        var updEl = document.getElementById('vessel-updated');
        if(updEl && ageMin != null){
          var updText;
          if(ageMin < 60) updText = ageMin+' min ago';
          else if(ageMin < 1440) updText = Math.floor(ageMin/60)+'h '+(ageMin%60)+'m ago';
          else updText = Math.floor(ageMin/1440)+'d ago';
          updEl.textContent = 'AIS: '+updText;
          updEl.style.display = 'inline';
        }
        // Stale badge
        var staleEl = document.getElementById('vessel-stale');
        if(staleEl){
          if(isStale){
            var hrs = Math.floor(ageMin/60);
            staleEl.textContent = hrs+'h stale';
            staleEl.style.display = 'inline-flex';
          } else {
            staleEl.style.display = 'none';
          }
        }
        // Unverified-carrier badge (shown when MMSI is not in TOYOTA_CARRIERS
        // but vessel name matches PCC naming pattern — possibly a new charter)
        var unvEl = document.getElementById('vessel-unverified');
        if(unvEl){
          unvEl.style.display = verified ? 'none' : 'inline-block';
        }
        // Voyage progress line
        renderVoyageProgress(lat, lng, dest, eta);
        // ETA estimate banner
        renderEtaBanner(lat, lng, eta, speed, dest);
        card.style.display='block';
      }
    }

    // Port coordinates database for major Toyota PCC ports + bunkering stops.
    // Used to resolve AIS DESTINATION text → lat/lng so we can detect detours.
    var PORT_COORDS = {
      // Major Toyota European destination ports
      'DERINCE':      [40.760,  29.834],  // Turkey, Sea of Marmara
      'SAGUNTO':      [39.640,  -0.218],  // Spain (Mediterranean)
      'LIVORNO':      [43.548,  10.305],  // Italy
      'PIRAEUS':      [37.940,  23.640],  // Greece
      'LIMASSOL':     [34.670,  33.040],  // Cyprus
      'ISKENDERUN':   [36.595,  36.175],  // Turkey (SE coast)
      'LAS PALMAS':   [28.140, -15.420],  // Canary Islands
      'BEIRUT':       [33.900,  35.500],  // Lebanon
      // Northern European hub ports
      'ZEEBRUGGE':    [51.320,   3.215],  // Belgium
      'BREMERHAVEN':  [53.580,   8.580],  // Germany
      'SOUTHAMPTON':  [50.900,  -1.420],  // UK
      'MALMO':        [55.620,  13.000],  // Sweden
      'MALMÖ':        [55.620,  13.000],
      'PALDISKI':     [59.350,  24.080],  // Estonia
      'HAMBURG':      [53.530,   9.950],
      'ROTTERDAM':    [51.970,   4.150],
      'DRAMMEN':      [59.745,  10.220],  // Norway
      // Origin ports
      'NAGOYA':       [35.183, 136.910],
      'TOYOTA CITY':  [35.180, 136.910],
      'YOKOHAMA':     [35.455, 139.650],
      'KOBE':         [34.680, 135.200],
      'HITACHI':      [36.490, 140.650],
      'SHIMIZU':      [35.013, 138.500],
      'YOKKAICHI':    [34.965, 136.620],
      // Bunkering / transit
      'SINGAPORE':    [ 1.270, 103.840],
      'SUEZ':         [29.970,  32.560],
      // Common LOCODE / abbreviation prefixes (AIS often uses these)
      'SG SIN':       [ 1.270, 103.840],
      'TR DRC':       [40.760,  29.834],  // Derince LOCODE
      'TR DER':       [40.760,  29.834],
      'ES SAG':       [39.640,  -0.218],
      'BE ZEE':       [51.320,   3.215],
      'DE BRV':       [53.580,   8.580],  // Bremerhaven LOCODE
      'NL RTM':       [51.970,   4.150],  // Rotterdam LOCODE
    };

    function resolvePort(destText){
      if(!destText) return null;
      var t = destText.toUpperCase().trim();
      if(PORT_COORDS[t]) return PORT_COORDS[t];
      // Try substring match (e.g., "SG SIN PEBGA" contains "SG SIN")
      for(var key in PORT_COORDS){
        if(key.length > 3 && t.indexOf(key) !== -1) return PORT_COORDS[key];
      }
      // Word-level match (e.g., "DERINCE PORT" or "TR DERINCE" contains "DERINCE")
      var words = t.split(/[\s,\-_]+/);
      for(var i=0; i<words.length; i++){
        if(words[i].length > 3 && PORT_COORDS[words[i]]) return PORT_COORDS[words[i]];
      }
      return null;
    }

    // ETA: given current vessel position and route, estimate arrival at final dealer.
    // Now detour-aware: if vessel's AIS DESTINATION is an off-route port (e.g., Bishu
    // heading to DERINCE before continuing to Zeebrugge), inserts it as a virtual
    // waypoint and adds port-dwell time. This handles multi-port PCC voyages where
    // a ship visits 2-4 European ports before reaching the planned Northern hub.
    function renderEtaBanner(lat, lng, eta, speed, dest){
      var banner = document.getElementById('eta-banner');
      var valEl = document.getElementById('eta-value');
      var detEl = document.getElementById('eta-detail');
      if(!banner || !latlngs || latlngs.length < 2) return;
      // Compute total route distance + traveled distance
      function dist(a,b){
        var R=6371,dLat=(b[0]-a[0])*Math.PI/180,dLon=(b[1]-a[1])*Math.PI/180;
        var x=Math.sin(dLat/2)**2+Math.cos(a[0]*Math.PI/180)*Math.cos(b[0]*Math.PI/180)*Math.sin(dLon/2)**2;
        return 2*R*Math.asin(Math.sqrt(x));
      }
      var totalKm=0;
      for(var i=1;i<latlngs.length;i++) totalKm += dist(latlngs[i-1],latlngs[i]);
      // Find closest segment to vessel
      var bestProgress=0, bestD=Infinity, cumKm=0, bestSegEndIdx=-1;
      for(var i=1;i<latlngs.length;i++){
        var segKm=dist(latlngs[i-1],latlngs[i]);
        var d=Math.min(dist([lat,lng],latlngs[i-1]),dist([lat,lng],latlngs[i]));
        if(d<bestD){
          bestD=d;
          bestSegEndIdx=i;  // index of segment END = next planned hub
          var dStart=dist([lat,lng],latlngs[i-1]);
          var dEnd=dist([lat,lng],latlngs[i]);
          bestProgress=cumKm+segKm*(dStart/(dStart+dEnd));
        }
        cumKm+=segKm;
      }
      var remainingKm=totalKm-bestProgress;
      // OCEAN ROUTING CORRECTION (per-segment, applied to remaining only):
      var correctedRemainingKm = 0;
      cumKm = 0;
      for(var i=1;i<latlngs.length;i++){
        var segKm = dist(latlngs[i-1],latlngs[i]);
        var segStart = cumKm;
        var segEnd = cumKm + segKm;
        var overlapStart = Math.max(segStart, bestProgress);
        var overlapEnd = segEnd;
        if(overlapEnd > overlapStart){
          var segRemaining = overlapEnd - overlapStart;
          var mult = segKm > 2500 ? 2.1 : segKm > 800 ? 1.55 : 1.1;
          correctedRemainingKm += segRemaining * mult;
        }
        cumKm += segKm;
      }
      if(correctedRemainingKm < 1) correctedRemainingKm = remainingKm;

      // ───────── DETOUR DETECTION ─────────
      // If vessel's AIS DESTINATION is in a different SEA REGION than the planned
      // final destination, it's making a detour (e.g., Med port like Derince while
      // final destination is Northern Europe like Vilnius). We can't use great-circle
      // distance because Asia→Europe great-circles cut through landmass (Eurasia),
      // misleadingly suggesting Derince is "on the way" to Zeebrugge.
      function portRegion(coords){
        if(!coords) return null;
        var lat = coords[0], lon = coords[1];
        if(lat >= 50 && lat <= 65 && lon >= -10 && lon <= 35) return 'northern';
        if(lat >= 30 && lat <= 47 && lon >= -6 && lon <= 42) return 'mediterranean';
        if(lat >= 0 && lat <= 50 && lon >= 95 && lon <= 145) return 'asia';
        if(lat >= 20 && lat <= 36 && lon >= -18 && lon <= -10) return 'canaries';
        return null;
      }
      // Route-type aware multiplier — actual sea distance / great-circle.
      // CALIBRATED against real observed voyages of Toyota carriers from shipinfo.net
      // 120-day tracks (Bishu, Triton, Cepheus, Hamburg). See research notes.
      function routeMultiplier(fromCoords, toCoords){
        var fr = portRegion(fromCoords), to = portRegion(toCoords);
        if(!fr || !to) return 1.8;
        if(fr === to) return 1.15;
        // Asia → Med via Suez: observed Bishu Singapore→Derince = 33.5d at ~14 kn = 11000+ km actual
        // Great-circle Singapore→Derince = 8500 km. Ratio: 1.3-1.4x. But we need TIME accuracy,
        // and ships go slower than nominal speed in this leg (Suez transit, traffic). Use 1.7x.
        if(fr === 'asia' && to === 'mediterranean') return 1.7;
        if(fr === 'mediterranean' && to === 'asia') return 1.7;
        // Asia → Northern Europe: observed Triton Singapore→Sagunto (28d) + Sagunto→Zeebrugge (11.7d)
        // = ~40 days for full Asia→Northern. Calibrated multiplier ~1.85x.
        if(fr === 'asia' && to === 'northern') return 1.85;
        if(fr === 'northern' && to === 'asia') return 1.85;
        // Med → Northern via Gibraltar+Channel: with 0.70 efficiency,
        // Triton Sagunto→Zee (11.7d) and Bishu Piraeus→Zee (11.1d) calibrate to 2.0-2.3x.
        if(fr === 'mediterranean' && to === 'northern') return 2.2;
        if(fr === 'northern' && to === 'mediterranean') return 2.2;
        return 1.8;
      }

      var detourPort = null;
      var detourCoords = null;
      var detourActive = false;
      if(dest && bestSegEndIdx > 0){
        detourCoords = resolvePort(dest);
        if(detourCoords){
          var matchesPlannedHub = false;
          for(var pi=0; pi<latlngs.length; pi++){
            if(dist(detourCoords, latlngs[pi]) < 150){ matchesPlannedHub = true; break; }
          }
          if(!matchesPlannedHub){
            // Region check is the reliable detour test
            var detourRegion = portRegion(detourCoords);
            var finalRegion = portRegion(latlngs[latlngs.length - 1]);
            if(detourRegion && finalRegion && detourRegion !== finalRegion){
              detourPort = dest;
              detourActive = true;
            }
          }
        }
      }

      // Compute daysRemaining
      // Speed efficiency 0.70: real port-to-port voyages include Suez transit, Strait of
      // Malacca, Gibraltar funnel, port approach maneuvering, and occasional bunker stops.
      // Real observed avg = 11-13 kn effective for nominally 14-17 kn ships. (Bishu Feb-Mar 2026
      // Singapore→Derince = 33.5d for ~12000 km actual = ~358 km/day = ~9.6 kn effective.)
      var kmPerDay = (speed && speed > 5) ? Math.round(speed * 1.852 * 24 * 0.70) : 450;
      kmPerDay = Math.max(280, Math.min(640, kmPerDay));
      var daysRemaining;
      var detourEtaAnchor = null;

      if(detourActive){
        // AIS ETA is reliable as anchor ONLY if more than 24h in future
        // (otherwise it's likely stale — set for a previous waypoint the ship already passed)
        if(eta){
          var anchorDate = new Date(eta.replace(' UTC','Z').replace(/^(\d{4}-\d{2}-\d{2}) /,'$1T'));
          if(!isNaN(anchorDate.getTime()) && (anchorDate.getTime() - Date.now()) > 86400000){
            detourEtaAnchor = anchorDate;
          }
        }
        var nextHubD = latlngs[bestSegEndIdx];
        // Use route-aware multipliers for the detour leg
        var multToDetour = routeMultiplier([lat,lng], detourCoords);
        var multDetourToNext = routeMultiplier(detourCoords, nextHubD);
        var detourToNextKm = dist(detourCoords, nextHubD) * multDetourToNext;
        var detourToNextDays = detourToNextKm / kmPerDay;
        // Remaining planned route AFTER reaching the next hub
        var remainingAfterNext = 0;
        for(var i=bestSegEndIdx; i<latlngs.length-1; i++){
          var sk = dist(latlngs[i], latlngs[i+1]);
          var m = sk > 2500 ? 2.1 : sk > 800 ? 1.55 : 1.1;
          remainingAfterNext += sk * m;
        }
        var remainingAfterDays = remainingAfterNext / kmPerDay;
        // Dwell budget — calibrated from real voyages:
        //   Bishu Zeebrugge 2.3d, Triton Zeebrugge 7.1d → avg ~5d at major hub
        //   Plus ~3.5d wait for feeder departure at each downstream hub
        // 2.5d at detour port (Derince) + 5d at first European hub (Zeebrugge) +
        // 3.5d per subsequent feeder hub + 0.5d truck
        var plannedHubsAhead = latlngs.length - bestSegEndIdx - 1;
        var dwellDays = 2.5 + 5.0 + Math.max(0, plannedHubsAhead - 1) * 3.5 + 0.5;
        if(detourEtaAnchor){
          var daysToDetour = (detourEtaAnchor - Date.now()) / 86400000;
          daysRemaining = daysToDetour + detourToNextDays + remainingAfterDays + dwellDays;
        } else {
          var toDetourKm = dist([lat,lng], detourCoords) * multToDetour;
          var daysToDetourEst = toDetourKm / kmPerDay;
          daysRemaining = daysToDetourEst + detourToNextDays + remainingAfterDays + dwellDays;
        }
      } else {
        daysRemaining = correctedRemainingKm / kmPerDay;
        if(correctedRemainingKm > 8000) daysRemaining += 7;
        else if(correctedRemainingKm > 3000) daysRemaining += 4;
        else if(correctedRemainingKm > 800)  daysRemaining += 2;
        else                                  daysRemaining += 1;
      }
      // Uncertainty window — calibrated from real observed variance:
      //  • MST ETA precision: ±1d (K-Line publishes tight schedules, we anchor on it)
      //  • Detour port dwell (e.g. Derince): ±1d (Bishu prior voyage: 1.0d dwell)
      //  • Sailing leg variance per ocean segment: ±1.5d (Suez/Gibraltar queues, weather)
      //  • Each Baltic feeder hub: ±1.5d (feeders run 2-3×/week, wait varies)
      //  • Truck final leg: ±0.5d
      // For a detour-with-feeders voyage (worst case): √(1² + 1² + 2.25 + 2.25 + 2.25 + 0.25)
      //   ≈ ±2.9d standard deviation → display ±1.7σ ≈ ±5d to cover ~90% of cases
      // Scales DOWN further as deep-sea uncertainty resolves (vessel reaches Europe).
      var uncertainty;
      if(daysRemaining > 50)      uncertainty = 6;  // deep-sea + multi-port still ahead
      else if(daysRemaining > 30) uncertainty = 5;  // approaching first European port
      else if(daysRemaining > 15) uncertainty = 4;  // in European feeder phase
      else if(daysRemaining > 7)  uncertainty = 3;  // close — final feeders only
      else                         uncertainty = 2;  // truck/final week
      // Detour adds modest variance (multi-port schedule risk), but MST anchor
      // already captures most of it. Only add +1 day.
      if(detourActive) uncertainty += 1;
      // ────────── END DETOUR ──────────

      var arriveMs = Date.now() + daysRemaining*86400000;
      var arrive = new Date(arriveMs);
      var earlyArrive = new Date(arriveMs - uncertainty*86400000);
      var lateArrive  = new Date(arriveMs + uncertainty*86400000);
      function fmt(d){ return d.toLocaleDateString('en-US',{month:'short',day:'numeric'}); }
      valEl.textContent = fmt(earlyArrive)+' – '+fmt(lateArrive);
      var fullYear = arrive.toLocaleDateString('en-US',{year:'numeric',month:'long',day:'numeric'});
      // Show next-stop ETA anchor if we have AIS ETA
      var nextStopNote = '';
      if(eta){
        var etaDateN = new Date(eta.replace(' UTC','Z').replace(/^(\d{4}-\d{2}-\d{2}) /,'$1T'));
        if(!isNaN(etaDateN.getTime()) && etaDateN > Date.now()){
          var msLeft = etaDateN - Date.now();
          var dLeft = Math.floor(msLeft/86400000);
          var hLeft = Math.floor((msLeft%86400000)/3600000);
          nextStopNote = ' · Next stop in '+(dLeft>0?dLeft+'d ':'')+hLeft+'h';
        }
      }
      var detourNote = detourActive ? ' · via '+detourPort : '';
      detEl.textContent = 'Most likely '+fullYear+' · ~'+Math.round(daysRemaining)+' days'+detourNote+nextStopNote;
      banner.style.display='flex';
    }

    // Voyage progress: shows progress from last hub to next hub based on lat/lng
    function renderVoyageProgress(lat, lng, destText, eta){
      var bar = document.getElementById('voyage-bar');
      var fromEl = document.getElementById('voyage-from');
      var toEl = document.getElementById('voyage-to');
      var pctEl = document.getElementById('voyage-pct');
      if(!bar) return;
      // Use route waypoints (latlngs) to compute progress
      if(!latlngs || latlngs.length < 2){ return; }
      // Find which segment the vessel is closest to, by projecting onto each
      function dist(a,b){
        var R=6371,dLat=(b[0]-a[0])*Math.PI/180,dLon=(b[1]-a[1])*Math.PI/180;
        var x=Math.sin(dLat/2)**2+Math.cos(a[0]*Math.PI/180)*Math.cos(b[0]*Math.PI/180)*Math.sin(dLon/2)**2;
        return 2*R*Math.asin(Math.sqrt(x));
      }
      // Deep-sea phase detection: if vessel is far from ALL route hubs (>500 km),
      // she's still on the deep-sea voyage from Asia to first European hub. The
      // closest-segment heuristic gives nonsense in that case (e.g. picks Paldiski→
      // Vilnius because Vilnius happens to be at a closer latitude even when vessel
      // is in the Indian Ocean). Short-circuit and show "Toyota City → <AIS dest>"
      // with progress estimated against typical 18,000 km Asia→Europe distance.
      var nearestHubKm = Infinity;
      for(var i=0; i<latlngs.length; i++){
        var d = dist([lat,lng], latlngs[i]);
        if(d < nearestHubKm) nearestHubKm = d;
      }
      if(nearestHubKm > 500){
        fromEl.textContent = 'Toyota City';
        toEl.textContent = destText ? destText.split(',')[0] : 'Europe';
        // Day-based progress: elapsed / total days (departure → AIS ETA).
        // Both values are real observations, not geometry-based estimates.
        var deepSeaPct = null;
        var departDateStr = window.currentDepartDate;
        if(departDateStr && eta){
          var departMs = new Date(departDateStr+'T00:00:00Z').getTime();
          var etaDateForPct = new Date(eta.replace(' UTC','Z').replace(/^(\d{4}-\d{2}-\d{2}) /,'$1T'));
          if(!isNaN(departMs) && !isNaN(etaDateForPct.getTime())){
            var totalMs = etaDateForPct.getTime() - departMs;
            var elapsedMs = Date.now() - departMs;
            if(totalMs > 0){
              deepSeaPct = Math.max(0, Math.min(99, (elapsedMs/totalMs)*100));
            }
          }
        }
        if(deepSeaPct === null){
          // Missing departure date or AIS ETA — nothing reliable to base a
          // percentage on. Hide the bar rather than show a guessed number.
          bar.style.width = '0%';
          pctEl.textContent = '—';
        } else {
          bar.style.width = deepSeaPct.toFixed(1)+'%';
          pctEl.textContent = deepSeaPct.toFixed(0)+'%';
        }
        // Show next-stop ETA from AIS (vessel's reported destination + ETA)
        var nextStopEl = document.getElementById('voyage-next-stop');
        var nextDestEl = document.getElementById('voyage-next-dest');
        var nextEtaEl  = document.getElementById('voyage-next-eta');
        if(nextStopEl && destText && eta){
          var etaDate = new Date(eta.replace(' UTC','Z').replace(/^(\d{4}-\d{2}-\d{2}) /,'$1T'));
          if(!isNaN(etaDate.getTime()) && etaDate > Date.now()){
            var msLeft = etaDate - Date.now();
            var dLeft = Math.floor(msLeft/86400000);
            var hLeft = Math.floor((msLeft%86400000)/3600000);
            var timeStr = dLeft > 0 ? 'in '+dLeft+'d '+hLeft+'h' : 'in '+hLeft+'h';
            if(nextDestEl) nextDestEl.textContent = destText;
            if(nextEtaEl)  nextEtaEl.textContent  = timeStr;
            nextStopEl.style.display = 'block';
          } else {
            nextStopEl.style.display = 'none';
          }
        } else if(nextStopEl) { nextStopEl.style.display = 'none'; }
        return;
      }
      // Vessel is near a European hub — use segment-based progress
      // Total route distance
      var totalKm=0, segs=[];
      for(var i=1;i<latlngs.length;i++){
        var d=dist(latlngs[i-1],latlngs[i]);
        segs.push({from:latlngs[i-1],to:latlngs[i],km:d,start:totalKm});
        totalKm+=d;
      }
      // Find closest segment to vessel by min distance to either endpoint
      var bestSeg=segs[0], bestD=Infinity;
      for(var s of segs){
        var d = Math.min(dist([lat,lng],s.from), dist([lat,lng],s.to));
        if(d<bestD){bestD=d;bestSeg=s;}
      }
      // Progress within best segment: how close to start vs end
      var dFromStart=dist([lat,lng],bestSeg.from);
      var dToEnd=dist([lat,lng],bestSeg.to);
      var segProgress = dFromStart/(dFromStart+dToEnd);
      var traveledKm = bestSeg.start + bestSeg.km*segProgress;
      var pct = Math.max(0, Math.min(100, (traveledKm/totalKm)*100));
      // Find hub names
      var fromIdx = segs.indexOf(bestSeg);
      var toIdx = fromIdx + 1;
      var fromName = (window.routeHubs && window.routeHubs[fromIdx]) || 'Origin';
      var toName = (window.routeHubs && window.routeHubs[toIdx]) || 'Destination';
      bar.style.width = pct.toFixed(1)+'%';
      fromEl.textContent = fromName;
      toEl.textContent = toName;
      pctEl.textContent = pct.toFixed(0)+'%';
      // Next stop ETA from AIS
      var nextStopEl = document.getElementById('voyage-next-stop');
      var nextDestEl = document.getElementById('voyage-next-dest');
      var nextEtaEl  = document.getElementById('voyage-next-eta');
      if(nextStopEl && destText && eta){
        var etaDate = new Date(eta.replace(' UTC','Z').replace(/^(\d{4}-\d{2}-\d{2}) /,'$1T'));
        if(!isNaN(etaDate.getTime()) && etaDate > Date.now()){
          var msLeft = etaDate - Date.now();
          var dLeft = Math.floor(msLeft/86400000);
          var hLeft = Math.floor((msLeft%86400000)/3600000);
          var mLeft = Math.floor((msLeft%3600000)/60000);
          var timeStr = dLeft > 0 ? 'in '+dLeft+'d '+hLeft+'h' : 'in '+hLeft+'h '+mLeft+'m';
          if(nextDestEl) nextDestEl.textContent = destText;
          if(nextEtaEl)  nextEtaEl.textContent  = timeStr;
          nextStopEl.style.display = 'block';
        } else {
          nextStopEl.style.display = 'none';
        }
      } else if(nextStopEl) { nextStopEl.style.display = 'none'; }
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
        {# Same leg the Detect controls above are keyed to — computed once in the
           `active` namespace so the manual override and auto-detection can never
           address different legs. See that block for the full reasoning. #}
        var leg = '{{ active.key }}';

        var legOverride = overrides[leg];
        // hasUserDate: depart_date must have been set BY THE USER (manual entry from
        // their Toyota email). System-auto dates (source=auto) are first-login defaults
        // — those have no relationship to actual factory departure and shouldn't be trusted.
        hasUserDate = !!(legOverride && legOverride.depart_date && legOverride.source === 'user');
        // hasUserMmsi: user explicitly typed an MMSI in the manual field
        var hasUserMmsi = !!(legOverride && legOverride.mmsi);
        // lfBounded: server confirms the buildInProgress→leftTheFactory transition was
        // witnessed (requires step_durations.observed=1 for buildInProgress).
        var lfBounded = {{ 'true' if order._lf_bounded else 'false' }};
        // dateReliable: we trust the detection result ONLY when ONE OF:
        //   1. lfBounded — we witnessed the transition (most authoritative)
        //   2. hasUserDate — user explicitly typed the email date (user-provided ground truth)
        //   3. hasUserMmsi — user explicitly typed the MMSI (user-provided ground truth)
        // NOTE: hasKnownVessel (cached from prior auto-detection) is NOT sufficient.
        // A cached vessel found via auto-detection on an unreliable date is still a guess.
        // The user should see the disclaimer to verify/correct, not a confident vessel card.
        var dateReliable = lfBounded || hasUserDate || hasUserMmsi;

        if(hash){
          if(!dateReliable){
            // Date is unreliable — skip auto-detection (would produce a guess), show prompt instead.
            // Hide skeleton, vessel card stays hidden, user enters date from Toyota email.
            var skel0 = document.getElementById('vessel-skeleton');
            if(skel0) skel0.style.display = 'none';
            var prompt = document.getElementById('vessel-date-prompt');
            if(prompt) prompt.style.display = 'block';
            return;
          }
          // Show skeleton while detection runs
          var skel = document.getElementById('vessel-skeleton');
          if(skel) skel.style.display='block';
          // Always use API — it handles leg-aware cache correctly
          // Never use localStorage MMSI directly as it may be stale/wrong leg
          fetch('/api/vessel-detect/'+hash+'?leg='+leg)
            .then(r=>r.json())
            .then(d=>{
              if(skel) skel.style.display='none';
              if(d.lat){
                // Compute ageMin from updated timestamp
                var ageMin = null;
                if(d.updated){
                  var t = new Date(d.updated.replace(' ','T')+'Z');
                  if(!isNaN(t)) ageMin = Math.floor((Date.now()-t.getTime())/60000);
                }
                loadVessel(d.mmsi,d.name,d.lat,d.lon,d.speed,d.course,d.destination,d.eta,ageMin,d.verified,d.depart_date);
              } else if(d.error || !d.mmsi){
                // Detection genuinely failed — no European-bound PCC matched.
                // Show the carrier-unknown prompt so user can correct the date or enter MMSI.
                var prompt = document.getElementById('vessel-date-prompt');
                if(prompt) prompt.style.display = 'block';
                // Only ALSO show the "records expire after ~20 days" explanation
                // if that's actually a plausible reason for the failure — i.e.
                // we're past that window. This only ever runs on the FAILURE
                // path now, never just because the order happens to be old
                // (a successful, live detection hides/skips this entirely —
                // see loadVessel()).
                if((window.daysSinceFactoryDeparture||0) > 20){
                  var mstW = document.getElementById('mst-limit-warning');
                  if(mstW) mstW.style.display = 'block';
                }
              }
            })
            .catch(()=>{ if(skel) skel.style.display='none'; });
        }
      })
      .catch(function(){
        // Fallback — try detection anyway
        if(hash){
          var skel2 = document.getElementById('vessel-skeleton');
          if(skel2) skel2.style.display='block';
          fetch('/api/vessel-detect/'+hash)
            .then(r=>r.json())
            .then(d=>{
              if(skel2) skel2.style.display='none';
              if(d.lat){
                var ageMin = null;
                if(d.updated){
                  var t = new Date(d.updated.replace(' ','T')+'Z');
                  if(!isNaN(t)) ageMin = Math.floor((Date.now()-t.getTime())/60000);
                }
                loadVessel(d.mmsi,d.name,d.lat,d.lon,d.speed,d.course,d.destination,d.eta,ageMin,null,d.depart_date);
              }
            })
            .catch(()=>{ if(skel2) skel2.style.display='none'; });
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
  <div class="page-header">
    <h1 class="page-title">Toyota Europe — Global Statistics</h1>
    <p class="page-sub">Anonymized · no credentials or personal info stored</p>
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

# ── Scraper role ──────────────────────────────────────────────────────────────

@app.before_request
def scraper_role_lockdown():
    """In scraper mode, serve the internal API and nothing else.

    The scraper image is the same image as the web app, so every user-facing
    route technically exists in it. None of them should ever answer: this
    container has no database and no business rendering pages. Refusing
    everything but /internal/* keeps that true even if the container is
    accidentally published or reachable from somewhere unexpected.
    """
    if ROLE != "scraper":
        return None
    if request.path == "/healthz" or request.path.startswith("/internal/"):
        return None
    return jsonify(error="not_available", role="scraper"), 404


@app.route("/healthz")
def healthz():
    return jsonify(status="ok", role=ROLE)


@app.route("/internal/detect", methods=["POST"])
def internal_detect():
    """Run the browser scraper on behalf of the web container.

    Deliberately dumb: it takes the detector's positional arguments, validates
    them, runs it, and returns the raw JSON. No database access and no knowledge
    of orders — if this container is compromised there is nothing here worth
    stealing beyond the MyShipTracking session it already holds.
    """
    if ROLE != "scraper":
        return jsonify(error="not_a_scraper"), 404
    # constant-time compare so a wrong token cannot be discovered by timing
    supplied = request.headers.get("X-Scraper-Token", "")
    if not SCRAPER_TOKEN or not hmac.compare_digest(supplied, SCRAPER_TOKEN):
        return jsonify(error="forbidden"), 403

    body = request.get_json(silent=True) or {}
    argv = body.get("argv")
    if not isinstance(argv, list) or not (2 <= len(argv) <= 5):
        return jsonify(error="bad argv"), 400
    argv = ["" if a is None else str(a) for a in argv]

    # Re-validate here rather than trusting the caller. The web container already
    # checks these at its own boundary, but a service that runs a browser on
    # request should never depend on someone else having sanitised its input.
    date_or_dummy, mmsi = argv[0], argv[1]
    if date_or_dummy != "dummy":
        try:
            datetime.strptime(date_or_dummy, "%Y-%m-%d")
        except ValueError:
            return jsonify(error="bad date"), 400
    if mmsi and not (mmsi.isdigit() and len(mmsi) <= 15):
        return jsonify(error="bad mmsi"), 400
    if len(argv) >= 3 and argv[2] and argv[2] not in VALID_LEGS:
        return jsonify(error="bad leg"), 400
    for extra in argv[3:]:
        if len(extra) > 64 or not all(c.isalnum() or c in " -_." for c in extra):
            return jsonify(error="bad argument"), 400

    timeout = 60 if date_or_dummy == "dummy" else 120
    # Never let an exception here become Flask's HTML 500 page: the caller parses
    # JSON, so an HTML body is doubly useless — it fails to parse AND carries no
    # diagnosis. Return structured JSON and put the detail in the log.
    try:
        return jsonify(result=_detect_local(argv, timeout))
    except Exception as e:
        import traceback
        print(f"[internal_detect] unhandled: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return jsonify(error="detector_failed", detail=f"{type(e).__name__}: {e}"), 500


@app.route("/vendor/leaflet/<path:filename>")
def vendor_leaflet(filename):
    """Serve Leaflet from our own origin.

    It used to load from unpkg with no Subresource Integrity. Any compromise or
    version-hijack of that CDN would have executed script on a page that holds a
    My Toyota password in sessionStorage — a complete credential-theft path
    through a dependency we do not control. Serving it ourselves lets the CSP
    below forbid third-party script origins outright, which is stronger than SRI
    because there is no external origin left to trust.
    """
    return send_from_directory(os.path.join(VENDOR_DIR, "leaflet", "dist"), filename,
                               max_age=86400)


# Content-Security-Policy.
#
# 'unsafe-inline' is required for scripts: the templates rely heavily on inline
# <script> blocks and inline handlers (onclick=, onfocus=), and removing those
# would be a rewrite, not a hardening pass. It is a real weakening, so the value
# here comes from the other directives rather than script-src:
#   connect-src 'self'  — every fetch() in the app is same-origin, so injected
#                         script cannot POST stolen credentials anywhere.
#   img-src             — pinned to the map tile host, closing the classic
#                         "exfiltrate via new Image().src" trick.
#   form-action 'self'  — the login form cannot be repointed at another origin.
#   frame-ancestors     — no clickjacking of the credential form.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    # 'https:' rather than a fixed allowlist: the vehicle photo comes from
    # od.imageUrl, a Toyota-controlled CDN whose hostname we do not know ahead of
    # time and which they can change without notice. Pinning it broke the image
    # (that is a regression this directive caused, not a hypothetical).
    #
    # The cost is that img-src no longer blocks the "new Image().src = evil"
    # exfiltration trick. That was never the load-bearing part of this policy —
    # script-src already permits 'unsafe-inline' because the templates depend on
    # inline handlers, so the real controls here are connect-src 'self' (no
    # fetch/XHR off-origin), form-action 'self', and frame-ancestors 'none'.
    # Images cannot execute; they can only leak, and only over a channel an
    # attacker who already has script execution has cheaper ways to use.
    "img-src 'self' data: blob: https:",
    "connect-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "object-src 'none'",
])


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Permissions-Policy",
                            "geolocation=(), microphone=(), camera=(), interest-cohort=()")
    # Only meaningful over TLS; the Cloudflare tunnel terminates HTTPS.
    if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https":
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains")
    return resp


# ── CSRF (double-submit cookie) ───────────────────────────────────────────────
# Deliberately not Flask sessions: those need a persistent SECRET_KEY, and a
# missing or rotating one is its own failure mode. Double-submit needs no server
# state and no key. Applied only to POST / — the endpoint that forwards
# credentials to Toyota. The /api/* routes are all same-origin GETs whose only
# write requires already knowing an unguessable order_hash, so a token there
# would add breakage risk without closing anything.
CSRF_COOKIE = "tr_csrf"


def issue_csrf():
    token = request.cookies.get(CSRF_COOKIE)
    if not token or len(token) < 32:
        token = secrets.token_urlsafe(32)
    g.csrf_token = token
    return token


def csrf_ok():
    cookie = request.cookies.get(CSRF_COOKIE, "")
    form   = request.form.get("csrf_token", "")
    return bool(cookie) and bool(form) and hmac.compare_digest(cookie, form)


@app.route("/", methods=["GET", "POST"])
def index():
    username = USERNAME
    password = PASSWORD
    orders   = []
    error    = None

    csrf_token = issue_csrf()

    if request.method == "POST":
        # The app forwards these straight to Toyota's auth endpoint, so without a
        # limit it doubles as an open credential-stuffing proxy against
        # ssoms.toyota-europe.com. Generous enough that a real person retyping a
        # password never notices.
        now = time.monotonic()
        lkey = f"login:{client_key()}"
        with _rate_lock:
            hits = _rate_hits[lkey]
            while hits and hits[0] <= now - 300:
                hits.popleft()
            if len(hits) >= 10:
                error = "Too many login attempts. Please wait a few minutes and try again."
            else:
                hits.append(now)

        if not error and not csrf_ok():
            # Cross-origin form post, or a page loaded before this cookie existed.
            error = "Your session expired or the request came from an untrusted page. Please reload and try again."

        if not error:
            username = request.form.get("username", "")
            password = request.form.get("password", "")

    if not error and username and password:
        try:
            sys.path.insert(0, '/app')
            from toyota import ToyotaSession

            # Run --store-dates first so dates file is up to date before we read it.
            #
            # SECURITY: this used to pass --username/--password as argv, which
            # puts every user's My Toyota password into /proc/<pid>/cmdline —
            # world-readable inside the container, including by the headless
            # Chromium the vessel scraper runs. toyota.py has no env/stdin option
            # for credentials, so we invoke its importable main() through a tiny
            # wrapper and hand the values over in the environment instead
            # (/proc/<pid>/environ is readable only by the owning user and root).
            # Same interpreter, same cwd=/data, same --store-dates behaviour, so
            # the JSON date files land exactly where the reader below expects.
            _cred_env = os.environ.copy()
            _cred_env["TOYOTA_TRACKER_U"] = username
            _cred_env["TOYOTA_TRACKER_P"] = password
            subprocess.run(
                [sys.executable, "-c",
                 "import os,sys; sys.path.insert(0,'/app'); import toyota; "
                 "toyota.main(os.environ['TOYOTA_TRACKER_U'], "
                 "os.environ['TOYOTA_TRACKER_P'], True)"],
                capture_output=True, text=True, timeout=60, cwd="/data",
                env=_cred_env
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
                # Real date the car reached the stop it is at NOW. The step-level
                # date only records when the step began (at the FIRST hub), so it
                # goes stale as the car moves between hubs inside the same step.
                details['_current_stop'] = get_current_stop_info(
                    get_db(), order_hash, details.get('intermediateDeliveries') or [])
                # Load observed flags and determine if leftTheFactory date is bounded
                # by a prior witnessed step (observed=1 step with earlier date).
                # Unbounded = leftTheFactory was already completed on FIRST ever login
                # (no prior step with observed=1 to constrain the transition window).
                if order_hash:
                    obs_rows = get_db().execute(
                        "SELECT step, date_entered, date_left, observed FROM step_durations WHERE order_hash=?",
                        (order_hash,)
                    ).fetchall()
                    details['_step_observed'] = {r[0]: r[3] for r in obs_rows}
                    lf_row  = next((r for r in obs_rows if r[0] == 'leftTheFactory'), None)
                    bip_row = next((r for r in obs_rows if r[0] == 'buildInProgress'), None)
                    lf_observed = lf_row[3] if lf_row else 0
                    # leftTheFactory date is RELIABLE if either:
                    #  (a) leftTheFactory.observed == 1 (full lifecycle witnessed), OR
                    #  (b) buildInProgress.observed == 1 (BIP exit = LF entry, same observation)
                    # Same-day dates are correct here: when one login captures the transition,
                    # both BIP.date_left and LF.date_entered are set to that login's date.
                    if lf_observed == 1 or (bip_row and bip_row[3] == 1):
                        details['_lf_bounded'] = True
                    else:
                        details['_lf_bounded'] = False
                else:
                    details['_step_observed'] = {}
                    details['_lf_bounded'] = False
                # Login frequency for date reliability disclaimer.
                # Counts DISTINCT DAYS, not rows: save_stats now writes an extra
                # row whenever the route state changes mid-day, so COUNT(*) would
                # inflate "logins" and make the date-reliability badge claim more
                # precision than we actually have. Days is also what days_gap
                # (days_tracked / logins) was always meant to measure.
                if order_hash:
                    freq = get_db().execute("""
                        SELECT COUNT(DISTINCT substr(ts,1,10)) logins,
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

    resp = app.make_response(
        render_template_string(TRACKER_PAGE,
                               orders=orders, username=username,
                               error=error, request=request,
                               csrf_token=csrf_token))
    # HttpOnly is deliberately NOT set: the double-submit pattern needs the value
    # readable only by our own server-rendered form, and the cookie carries no
    # authority on its own — it is compared against the posted field, nothing more.
    resp.set_cookie(CSRF_COOKIE, csrf_token, samesite="Lax",
                    secure=(request.is_secure or
                            request.headers.get("X-Forwarded-Proto") == "https"),
                    max_age=60 * 60 * 12, path="/")
    return resp

@app.route("/api/vessel/<mmsi>")
@rate_limited(max_hits=20, per_seconds=60)
def api_vessel(mmsi):
    if mmsi not in TOYOTA_CARRIERS and not (mmsi.isdigit() and len(mmsi) <= 15):
        return jsonify(error="invalid mmsi"), 400
    pos = get_vessel_position(mmsi)
    if not pos:
        return jsonify(error="no position data"), 404
    return jsonify(pos)

def get_depart_date_for_order(db, order_hash, leg):
    """
    Return the departure date (YYYY-MM-DD string) this leg started from, or
    None if unknown. Used for day-based voyage progress: elapsed days since
    departure / total days until the AIS-reported ETA at the next stop —
    no route geometry involved at all.

    Checks, in order: a saved override (vessel_overrides.depart_date), then
    the observed leftTheFactory date from step_durations (nagoya leg only).
    """
    override = db.execute(
        "SELECT depart_date FROM vessel_overrides WHERE order_hash=? AND leg=?",
        (order_hash, leg)
    ).fetchone()
    if override and override["depart_date"]:
        return override["depart_date"]

    if leg == 'nagoya':
        row = db.execute("""
            SELECT date_entered FROM step_durations
            WHERE order_hash=? AND step='leftTheFactory' AND date_entered IS NOT NULL
        """, (order_hash,)).fetchone()
        if row:
            return row["date_entered"]

    return None


def enrich_with_route(db, resp, order_hash, leg):
    """
    Attach depart_date to the vessel response so the frontend can compute
    voyage progress as a day-based ratio: elapsed / total days (departure → AIS ETA).
    """
    resp["depart_date"] = get_depart_date_for_order(db, order_hash, leg)
    return resp


@app.route("/api/vessel-detect/<order_hash>", methods=["GET", "POST"])
@rate_limited(max_hits=20, per_seconds=60)
def api_vessel_detect(order_hash):
    db = get_db()
    body = request.get_json(silent=True) or {}
    depart_date_override = request.args.get('depart_date') or body.get('depart_date')
    mmsi_override        = request.args.get('mmsi')        or body.get('mmsi')
    leg_override         = request.args.get('leg', 'nagoya')

    # Validate at the boundary. All three are forwarded to detect_vessel.js and
    # end up inside scraper URLs; detect_vessel.js allowlists them again on its
    # side, but rejecting junk here means it never reaches the scraper at all and
    # never gets written into vessel_overrides.
    if leg_override not in VALID_LEGS:
        return jsonify(error="invalid leg"), 400
    if depart_date_override:
        try:
            datetime.strptime(depart_date_override, "%Y-%m-%d")
        except (ValueError, TypeError):
            return jsonify(error="invalid depart_date, expected YYYY-MM-DD"), 400
    if mmsi_override:
        mmsi_override = str(mmsi_override).strip()
        if not (mmsi_override.isdigit() and len(mmsi_override) <= 15):
            return jsonify(error="invalid mmsi"), 400

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

    # Get order's destination country (used by get_vessel_position to filter
    # out vessels heading to the wrong continent — e.g. PCC bound for Taiwan
    # when order is going to France)
    dest_country_row = db.execute("""
        SELECT dest_country FROM checks WHERE order_hash=?
          AND dest_country IS NOT NULL AND dest_country != ''
        ORDER BY ts DESC LIMIT 1
    """, (order_hash,)).fetchone()
    order_dest_country = (dest_country_row['dest_country'] if dest_country_row else None)

    # If user set MMSI manually, use it directly
    if override and override['mmsi'] and not depart_date_override:
        pos = get_vessel_position(override['mmsi'], order_dest_country)
        if pos:
            return jsonify(enrich_with_route(db, {**pos, "source": "user_override", "leg": leg_override}, order_hash, leg_override))

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
                    # Re-validate cached vessel: even if MMSI is "berth-verified" from a
                    # prior detection, the vessel may have changed direction since. If the
                    # cached destination is on the wrong continent for the order, clear the
                    # cache and fall through to re-detection.
                    if is_wrong_continent_for_order(cached["vessel_dest"] or "", order_dest_country):
                        print(f"[api_vessel_detect] Clearing stale cache for order={order_hash[:10]}: "
                              f"cached vessel {cached['vessel_name']} heading to "
                              f"'{cached['vessel_dest']}' but order is to "
                              f"{order_dest_country} (wrong continent)", file=sys.stderr)
                        db.execute("""UPDATE checks SET vessel_mmsi=NULL, vessel_name=NULL,
                                      vessel_lat=NULL, vessel_lon=NULL, vessel_speed=NULL,
                                      vessel_course=NULL, vessel_dest=NULL, vessel_eta=NULL,
                                      vessel_updated=NULL WHERE order_hash=?""", (order_hash,))
                        db.execute("""UPDATE vessel_overrides SET detected_mmsi=NULL,
                                      detected_name=NULL, berth_verified=0
                                      WHERE order_hash=? AND leg=?""",
                                   (order_hash, leg_override))
                        db.commit()
                        # Fall through to re-detection
                    # If we have fresh position but NULL dest/eta, force a refresh
                    # to backfill them — likely a leftover from before fast-path fix.
                    elif not cached["vessel_dest"] and not cached["vessel_eta"]:
                        pass  # fall through to refresh below
                    else:
                        return jsonify(enrich_with_route(db, {
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
                            "verified": cached["vessel_mmsi"] in TOYOTA_CARRIERS,
                        }, order_hash, leg_override))
                # Position stale — refresh position only, keep vessel identity
                pos = get_vessel_position(mmsi, order_dest_country)
                if pos:
                    _cache_vessel(db, order_hash, pos, leg=leg_override)
                    return jsonify(enrich_with_route(db, {**pos, "cached": False, "leg": leg_override,
                                    "berth_verified": bool(berth_verified)}, order_hash, leg_override))
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
                    return jsonify(enrich_with_route(db, {
                        "mmsi":        cached["vessel_mmsi"],
                        "name":        cached["vessel_name"],
                        "lat":         float(cached["vessel_lat"]),
                        "lon":         float(cached["vessel_lon"]),
                        "speed":       float(cached["vessel_speed"] or 0),
                        "course":      (float(cached["vessel_course"]) if cached["vessel_course"] not in (None, 0, 0.0) else None),
                        "destination": cached["vessel_dest"] or "",
                        "eta":         cached["vessel_eta"] or "",
                        "cached":      True,
                    }, order_hash, leg_override))
                else:
                    pos = get_vessel_position(cached["vessel_mmsi"], order_dest_country)
                    if pos:
                        _cache_vessel(db, order_hash, pos, leg=leg_override)
                        return jsonify(enrich_with_route(db, {**pos, "cached": False}, order_hash, leg_override))
                    return jsonify(enrich_with_route(db, {
                        "mmsi":        cached["vessel_mmsi"],
                        "name":        cached["vessel_name"],
                        "lat":         float(cached["vessel_lat"]),
                        "lon":         float(cached["vessel_lon"]),
                        "speed":       float(cached["vessel_speed"] or 0),
                        "course":      (float(cached["vessel_course"]) if cached["vessel_course"] not in (None, 0, 0.0) else None),
                        "destination": cached["vessel_dest"] or "",
                        "eta":         cached["vessel_eta"] or "",
                        "cached":      True, "stale": True,
                    }, order_hash, leg_override))

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
            # For feeder legs (zeebrugge/malmo/etc.) we need the date the car
            # left that specific hub — NOT today's date (which would make the
            # scraper search the wrong departure window).
            # Best source: the timestamp of the first check where that hub's
            # delivery step appeared as 'visited' or 'current' in deliveries_json.
            # This is the earliest date we know the car was at this hub.
            hub_map = {
                'zeebrugge': ['ZEEBRUGGE', 'ZEEBRUGG'],
                'malmo':     ['MALMO', 'MALMÖ', 'MALMOE'],
                'sagunto':   ['SAGUNTO'],
                'livorno':   ['LIVORNO'],
                'portbury':  ['PORTBURY', 'BRISTOL'],
                'southampton': ['SOUTHAMPTON'],
                'drammen':   ['DRAMMEN'],
                'piraeus':   ['PIRAEUS'],
            }
            hub_names = hub_map.get(leg_override, [])
            hub_date = None
            if hub_names:
                # Look through checks for the first time this hub appeared visited.
                # NOTE: deliveries_json is WRITTEN with the key "visited" (see
                # save_stats), not "isVisited". Reading only d["isVisited"] here
                # always yielded "" — the status test never matched, hub_date
                # stayed None, and every feeder-leg detection silently fell back
                # to today's date. Accept both key spellings, as the location
                # lookup on the line above already does.
                #
                # Also split the states by strength: 'current'/'visited' mean the
                # car actually reached this hub, which is the departure window we
                # want. 'inTransit' only means it is en route to the hub, so that
                # date can be many days early — keep it as a fallback only.
                checks = db.execute("""
                    SELECT ts, deliveries_json FROM checks
                    WHERE order_hash=? AND deliveries_json IS NOT NULL
                    ORDER BY ts ASC
                """, (order_hash,)).fetchall()
                hub_date_enroute = None
                for check in checks:
                    try:
                        delivs = json.loads(check["deliveries_json"])
                        for d in (delivs if isinstance(delivs, list) else []):
                            loc = (d.get("loc") or d.get("locationName") or "").upper()
                            visited = (d.get("visited") or d.get("isVisited") or "")
                            if not any(h in loc for h in hub_names):
                                continue
                            if visited in ('current', 'visited'):
                                hub_date = check["ts"][:10]  # YYYY-MM-DD
                                break
                            if visited == 'inTransit' and not hub_date_enroute:
                                hub_date_enroute = check["ts"][:10]
                    except Exception:
                        pass
                    if hub_date:
                        break
                if not hub_date:
                    hub_date = hub_date_enroute
            if hub_date:
                left_factory_date = hub_date
            else:
                # True last resort: use today. This will likely produce a wrong
                # detection window — user should enter the date manually.
                # (No local `from datetime import datetime` here: a function-scoped
                # import makes `datetime` local to the WHOLE function, shadowing the
                # module-level one and breaking every earlier use of it.)
                left_factory_date = datetime.utcnow().strftime("%Y-%m-%d")
                print(f"[api_vessel_detect] WARNING: no hub date found for leg={leg_override}, "
                      f"order={order_hash[:10]} — using today as fallback, detection may be inaccurate",
                      file=sys.stderr)
        else:
            return jsonify(error="no leftTheFactory date"), 404

    # Fetch order's destination country for region-aware reverse-lookup
    dest_row = db.execute(
        "SELECT dest_country FROM checks WHERE order_hash=? AND dest_country IS NOT NULL "
        "ORDER BY ts DESC LIMIT 1", (order_hash,)
    ).fetchone()
    dest_country = dest_row["dest_country"] if dest_row else ""

    # Fetch intermediate hub port (Sagunto/Zeebrugge/etc.) from last known deliveries
    # so detect_vessel.js can discriminate Med sub-rotation (France via Sagunto vs Zeebrugge)
    hub_port = ""
    hub_row = db.execute(
        "SELECT deliveries_json FROM checks WHERE order_hash=? AND deliveries_json IS NOT NULL "
        "ORDER BY ts DESC LIMIT 1", (order_hash,)
    ).fetchone()
    if hub_row:
        try:
            delivs = json.loads(hub_row["deliveries_json"])
            for d in (delivs if isinstance(delivs, list) else []):
                loc = (d.get("loc") or d.get("locationName") or "").upper()
                if any(p in loc for p in ["SAGUNTO", "ZEEBRUGGE", "BREMERHAVEN", "SOUTHAMPTON",
                                           "PIRAEUS", "MALMO", "GOTHENBURG", "DRAMMEN",
                                           "LIVORNO", "ANTWERP", "PALDISKI"]):
                    hub_port = loc.split()[0]  # first word e.g. "SAGUNTO"
                    break
        except Exception:
            pass

    # Fallback: if hub still unknown, infer from dest_country
    # Most French/German/Belgian/Dutch orders route via Zeebrugge; Italy via Livorno/Sagunto
    if not hub_port and dest_country:
        _dc = dest_country.upper()
        if _dc in ('GERMANY', 'BELGIUM', 'NETHERLANDS', 'UNITED KINGDOM',
                   'IRELAND', 'DENMARK', 'NORWAY', 'SWEDEN', 'FINLAND', 'POLAND',
                   'LITHUANIA', 'LATVIA', 'ESTONIA'):
            hub_port = 'ZEEBRUGGE'
        elif _dc in ('FRANCE', 'SPAIN', 'PORTUGAL'):
            hub_port = 'SAGUNTO'
        elif _dc in ('GREECE', 'CYPRUS', 'TURKEY', 'LEBANON', 'ISRAEL'):
            hub_port = 'PIRAEUS'
        elif _dc in ('ITALY', 'CROATIA', 'SLOVENIA', 'MALTA'):
            hub_port = 'LIVORNO'

    vessel = detect_vessel(left_factory_date, leg=leg_override, dest_country=dest_country, hub_port=hub_port)
    if not vessel:
        return jsonify(error="no vessel detected"), 404

    # Save detection result to vessel_overrides
    # For feeder legs, the depart_date saved here becomes the search anchor for
    # the NEXT leg's detection. We add typical transit time between hubs so that
    # the next leg's D-6 to D-1 window correctly covers when the vessel actually
    # arrives at the next port. Without this, the Malmö leg would search Jul 14-19
    # when D=Jul 20 (Zeebrugge observed date), but Danube Highway arrives Malmö
    # Jul 20 and departs Jul 21-22 — outside that window.
    # Typical transit times between consecutive hubs (conservative, in days):
    NEXT_HUB_TRANSIT_DAYS = {
        'nagoya':      0,   # depart_date is actual departure, no offset needed
        'zeebrugge':   2,   # Zeebrugge → Malmö ~2 days
        'malmo':       2,   # Malmö → Paldiski ~2 days
        'bremerhaven': 2,   # Bremerhaven → next hub ~2 days
        'portbury':    2,   # Portbury → Zeebrugge ~2 days
        'southampton': 2,
        'sagunto':     3,   # Sagunto → further Med ports
        'livorno':     2,
        'drammen':     1,
        'piraeus':     1,
    }
    transit_offset = NEXT_HUB_TRANSIT_DAYS.get(leg_override, 0)
    if transit_offset > 0:
        # Uses the module-level datetime/timedelta — see note above on shadowing.
        try:
            base = datetime.strptime(left_factory_date, "%Y-%m-%d")
            save_depart_date = (base + timedelta(days=transit_offset)).strftime("%Y-%m-%d")
        except Exception:
            save_depart_date = left_factory_date
    else:
        save_depart_date = left_factory_date

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
    """, (order_hash, leg_override, save_depart_date,
          vessel.get('mmsi'), vessel.get('name'), bv))

    _cache_vessel(db, order_hash, vessel, leg=leg_override)
    db.commit()

    return jsonify(enrich_with_route(db, {k: v for k, v in vessel.items() if not k.startswith("_")}, order_hash, leg_override))

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

def _startup_checks():
    """Fail loudly on a misconfigured split deployment rather than silently
    falling back to the old single-container behaviour, which would put Chromium
    back in the web container without anyone noticing."""
    if ROLE == "scraper":
        if not SCRAPER_TOKEN:
            sys.exit("FATAL: ROLE=scraper requires SCRAPER_TOKEN")
        if not MST_EMAIL or not MST_PASSWORD:
            print("WARNING: scraper has no MST credentials — detection will return nothing",
                  file=sys.stderr)
        if os.environ.get("DB_PATH"):
            print("WARNING: DB_PATH is set on the scraper. It has no need for the "
                  "database; unset it so a compromise cannot reach one.", file=sys.stderr)
    else:
        if SCRAPER_URL and not SCRAPER_TOKEN:
            sys.exit("FATAL: SCRAPER_URL is set but SCRAPER_TOKEN is empty")
        if not SCRAPER_URL:
            print("NOTE: SCRAPER_URL unset — running the browser in this container "
                  "(single-container mode).", file=sys.stderr)
    print(f"[startup] role={ROLE} scraper={'remote' if (SCRAPER_URL and ROLE=='web') else 'local'}",
          file=sys.stderr)


if __name__ == "__main__":
    _startup_checks()
    app.run(host="0.0.0.0", port=8080)