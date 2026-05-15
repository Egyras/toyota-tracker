#!/usr/bin/env python3
"""Toyota Order Tracker — Flask web wrapper with anonymized stats collection."""
import os, sys, json, sqlite3, subprocess
from datetime import datetime
from flask import Flask, render_template_string, request, g

app = Flask(__name__)

USERNAME = os.environ.get("TOYOTA_USERNAME", "")
PASSWORD = os.environ.get("TOYOTA_PASSWORD", "")
DB_PATH  = os.environ.get("DB_PATH", "/data/stats.db")

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
                UNIQUE(order_hash, step)
            );
            -- migrate: add created_on if upgrading from older schema
            CREATE TABLE IF NOT EXISTS _migrations (id INTEGER PRIMARY KEY);
        """)
        # add created_on column if missing (safe on existing DBs)
        cols = [r[1] for r in db.execute("PRAGMA table_info(checks)").fetchall()]
        if 'created_on' not in cols:
            db.execute("ALTER TABLE checks ADD COLUMN created_on TEXT")
        db.commit()
        g.db = db
    return g.db

@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db: db.close()

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
            if db.execute("SELECT 1 FROM checks WHERE order_hash=? AND ts LIKE ?",
                          (order_hash, f"{today}%")).fetchone():
                # Still update created_on if we now have it and it was missing
                if created_on:
                    db.execute(
                        "UPDATE checks SET created_on=? WHERE order_hash=? AND created_on IS NULL",
                        (created_on[:10], order_hash)
                    )
                _save_step_durations(db, order_hash, model, dest_country, steps, step_dates)
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
        db.commit()
    except Exception as e:
        print(f"[stats] save error: {e}", file=sys.stderr)

def _save_step_durations(db, order_hash, model, dest_country, steps, step_dates):
    """Save step durations from dates file AND from preprocessed step data."""
    if not order_hash:
        return

    # From --store-dates JSON file (has exact calendar dates)
    if step_dates:
        for step, dates in step_dates.get("steps", {}).items():
            entered = dates.get("current") or dates.get("visited")
            left    = dates.get("visited") if "current" in dates else None
            dur     = days_between(entered, left) if (entered and left) else None
            db.execute("""
                INSERT INTO step_durations
                  (order_hash, step, model, dest_country,
                   date_entered, date_left, duration_days)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(order_hash, step) DO UPDATE SET
                  date_left=excluded.date_left,
                  duration_days=excluded.duration_days
            """, (order_hash, step, model, dest_country, entered, left, dur))

    # From preprocessed steps — at minimum record which steps are visited/current
    # even if we don't have exact dates yet
    for step_name, step_data in steps.items():
        s = step_data.get("status", "")
        if s in ("visited", "current"):
            db.execute("""
                INSERT INTO step_durations
                  (order_hash, step, model, dest_country)
                VALUES (?,?,?,?)
                ON CONFLICT(order_hash, step) DO NOTHING
            """, (order_hash, step_name, model, dest_country))

def get_stats_data():
    db    = get_db()
    total = (db.execute("SELECT COUNT(DISTINCT order_hash) FROM checks WHERE order_hash IS NOT NULL").fetchone()[0]
             or db.execute("SELECT COUNT(*) FROM checks").fetchone()[0])
    by_model     = db.execute("SELECT model, COUNT(*) c FROM checks WHERE model IS NOT NULL GROUP BY model ORDER BY c DESC").fetchall()
    by_status    = db.execute("SELECT status, COUNT(*) c FROM checks WHERE status IS NOT NULL GROUP BY status ORDER BY c DESC").fetchall()
    delayed      = db.execute("SELECT COUNT(*) FROM checks WHERE is_delayed=1").fetchone()[0]
    damaged      = db.execute("SELECT COUNT(*) FROM checks WHERE has_damage=1").fetchone()[0]
    recent       = db.execute("SELECT ts, model, status, dest_country FROM checks ORDER BY id DESC LIMIT 20").fetchall()
    by_country   = db.execute("""
        SELECT dest_country, COUNT(*) total, SUM(is_delayed) delayed,
               GROUP_CONCAT(DISTINCT model) models
        FROM checks WHERE dest_country != '' AND dest_country IS NOT NULL
        GROUP BY dest_country ORDER BY total DESC LIMIT 20
    """).fetchall()
    step_avgs    = db.execute("""
        SELECT step, COUNT(*) samples, ROUND(AVG(duration_days),1) avg_days,
               MIN(duration_days) min_days, MAX(duration_days) max_days
        FROM step_durations WHERE duration_days IS NOT NULL AND duration_days >= 0
        GROUP BY step ORDER BY step
    """).fetchall()
    step_current = db.execute("""
        SELECT step, date_entered,
               CAST(julianday('now') - julianday(date_entered) AS INTEGER) days_so_far
        FROM step_durations WHERE date_left IS NULL AND date_entered IS NOT NULL
        ORDER BY days_so_far DESC LIMIT 30
    """).fetchall()
    countries = db.execute(
        "SELECT COUNT(DISTINCT dest_country) FROM checks WHERE dest_country != '' AND dest_country IS NOT NULL"
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
    return dict(total=total, by_model=by_model, by_status=by_status,
                by_country=by_country, delayed=delayed, damaged=damaged,
                recent=recent, step_avgs=step_avgs, step_current=step_current,
                countries=countries, order_dates=order_dates,
                order_journeys=order_journeys)

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
.nav{border-bottom:1px solid var(--border);padding:0 2rem;
     display:flex;align-items:center;gap:2rem;height:56px;}
.nav-brand{font-weight:600;font-size:15px;color:var(--text);
           display:flex;align-items:center;gap:8px;}
.nav-links{display:flex;gap:1.5rem;margin-left:auto;}
.nav-links a{color:var(--muted);font-size:13px;}
.nav-links a:hover{color:var(--text);text-decoration:none;}
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
  <div class="nav-brand">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#e5001a" stroke-width="2.5">
      <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
    </svg>
    Toyota Europe Tracker
  </div>
  <div class="nav-links">
    <a href="/">Tracker</a>
    <a href="/stats">Statistics</a>
    <a href="https://github.com/Egyras/toyota-tracker" target="_blank">Source ↗</a>
  </div>
</nav>
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
        <div class="benefit-icon">🔗</div>
        <div class="benefit-title">Shareable link</div>
        <div class="benefit-desc">Send the URL to family — no app or account needed</div>
      </div>
    </div>

    <div class="card">
      <form method="POST">
        <div class="form-group">
          <label>Email address</label>
          <input type="email" name="username" placeholder="your@email.com" required autofocus>
        </div>
        <div class="form-group">
          <label>Password</label>
          <input type="password" name="password" required>
        </div>
        <button type="submit" class="btn">Check my order →</button>
      </form>
    </div>

    <div class="privacy">
      <div class="privacy-title">🔒 How your credentials are handled</div>
      <p>Your credentials go directly to Toyota's API at <code>ssoms.toyota-europe.com</code>
         — never written to disk, never logged, never stored.</p>
      <p>Only anonymized stats are saved: model, step, country, delay flag.
         No name, email, or order ID is stored. See
         <a href="https://github.com/Egyras/toyota-tracker/blob/main/web.py" target="_blank">
         save_stats()</a> in the source code.</p>
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
            {% for event, date in step_dates[step_name].items() %}
            <div style="font-size:11px;color:var(--red);margin-top:3px;">{{ event }}: {{ date }}</div>
            {% endfor %}
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

    {% for d in delivs %}
    {% set v = d.isVisited %}
    <div class="route-item">
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
    {% endfor %}
  </div>

  <script>
  (function(){
    var stops = [
      {% for d in delivs %}
      {lat:{{ d.locationLatitude }},lng:{{ d.locationLongitude }},
       name:"{{ d.locationName }}, {{ d.countryName }}",
       type:"{{ d.destinationType }}",visited:"{{ d.isVisited }}"}{% if not loop.last %},{% endif %}
      {% endfor %}
    ];
    var map = L.map('route-map',{zoomControl:true,scrollWheelZoom:false});
    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{
      attribution:'&copy; OpenStreetMap &copy; CARTO',maxZoom:18
    }).addTo(map);
    var latlngs = stops.map(function(s){return[s.lat,s.lng];});
    L.polyline(latlngs,{color:'#e5001a',weight:2,dashArray:'6 6',opacity:.7}).addTo(map);
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
    map.fitBounds(latlngs,{padding:[30,30]});
  })();
  </script>
  {% endif %}
  {% endfor %}

  <p style="margin-top:1rem;font-size:13px;color:var(--muted);">
    <a href="/">← Check again</a> &nbsp;·&nbsp;
    <a href="/stats">📊 Global statistics</a>
  </p>
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
    {% if step_avgs %}
      {% set max_avg = namespace(v=1) %}
      {% for r in step_avgs %}{% if r['avg_days'] > max_avg.v %}{% set max_avg.v = r['avg_days'] %}{% endif %}{% endfor %}
      {% for r in step_avgs %}
      {% set pct = ((r['avg_days'] / max_avg.v) * 100)|int %}
      <div class="bar-row">
        <div class="bar-head">
          <span>{{ r['step'] }}</span>
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
          <tr><th>Step</th><th>Days so far</th><th>Since</th></tr>
          {% for r in step_current %}
          <tr>
            <td>{{ r['step'] }}</td>
            <td><span class="badge badge-current">{{ r['days_so_far'] }}d</span></td>
            <td style="color:var(--muted);">{{ r['date_entered'] }}</td>
          </tr>
          {% endfor %}
        </table>
      </div>
      {% endif %}
    {% else %}
      <p style="color:var(--muted);font-size:13px;">No duration data yet.</p>
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
      <tr><th>Time (UTC)</th><th>Model</th><th>Status</th><th>Country</th></tr>
      {% for r in recent %}
      <tr>
        <td style="color:var(--muted);">{{ r['ts'][:16] }}</td>
        <td>{{ r['model'] or '—' }}</td>
        <td><span class="badge badge-pending">{{ r['status'] or '—' }}</span></td>
        <td>{{ r['dest_country'] or '—' }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
</div></body></html>
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
                orders.append(details)

        except Exception as e:
            error = str(e)

    return render_template_string(TRACKER_PAGE,
                                  orders=orders, username=username, error=error)

@app.route("/stats")
def stats():
    d   = get_stats_data()
    pct = int(d["delayed"] / d["total"] * 100) if d["total"] else 0
    return render_template_string(STATS_PAGE, pct_delayed=pct, **d)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)