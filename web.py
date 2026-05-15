#!/usr/bin/env python3
"""Toyota Order Tracker — Flask web wrapper with anonymized stats collection."""
import os, sys, re, html, json, sqlite3
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
        """)
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

def save_stats(order: dict, step_dates: dict, today_only: bool = True):
    """Store anonymized snapshot — no credentials, no order IDs, no names stored.
    today_only=True means one row per order per calendar day maximum."""
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

        # Skip if we already recorded this order today
        if today_only and order_hash:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            exists = db.execute(
                "SELECT 1 FROM checks WHERE order_hash=? AND ts LIKE ?",
                (order_hash, f"{today}%")
            ).fetchone()
            if exists:
                return

        db.execute("""
            INSERT INTO checks
              (ts, order_hash, model, engine, transmission, color, status,
               destination, dest_country, is_delayed, has_damage,
               steps_json, deliveries_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.utcnow().isoformat(), order_hash, model,
            details.get("engine"), details.get("transmission"),
            details.get("vehicleExternalColor"), status.get("currentStatus"),
            dest, dest_country,
            1 if status.get("isDelayed") else 0,
            1 if status.get("damageCode") else 0,
            json.dumps({k: v.get("status") for k, v in steps.items()}),
            json.dumps([{"loc": d.get("locationName"), "country": d.get("countryName"),
                         "type": d.get("destinationType"), "visited": d.get("isVisited")}
                        for d in deliveries])
        ))

        # Save step durations from toyota.py --store-dates JSON file
        if order_hash and step_dates:
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
                """, (order_hash, step, model, dest_country,
                      entered, left, dur))
        db.commit()
    except Exception as e:
        print(f"[stats] save error: {e}", file=sys.stderr)

def get_stats_data():
    db    = get_db()
    total = (db.execute("SELECT COUNT(DISTINCT order_hash) FROM checks WHERE order_hash IS NOT NULL").fetchone()[0]
             or db.execute("SELECT COUNT(*) FROM checks").fetchone()[0])
    by_model   = db.execute("SELECT model, COUNT(*) c FROM checks WHERE model IS NOT NULL GROUP BY model ORDER BY c DESC").fetchall()
    by_status  = db.execute("SELECT status, COUNT(*) c FROM checks WHERE status IS NOT NULL GROUP BY status ORDER BY c DESC").fetchall()
    delayed    = db.execute("SELECT COUNT(*) FROM checks WHERE is_delayed=1").fetchone()[0]
    damaged    = db.execute("SELECT COUNT(*) FROM checks WHERE has_damage=1").fetchone()[0]
    recent     = db.execute("SELECT ts, model, status, dest_country FROM checks ORDER BY id DESC LIMIT 20").fetchall()
    by_country = db.execute("""
        SELECT dest_country, COUNT(*) total, SUM(is_delayed) delayed,
               GROUP_CONCAT(DISTINCT model) models
        FROM checks
        WHERE dest_country != '' AND dest_country IS NOT NULL
        GROUP BY dest_country ORDER BY total DESC LIMIT 20
    """).fetchall()
    step_avgs  = db.execute("""
        SELECT step, COUNT(*) samples,
               ROUND(AVG(duration_days),1) avg_days,
               MIN(duration_days) min_days,
               MAX(duration_days) max_days
        FROM step_durations
        WHERE duration_days IS NOT NULL AND duration_days >= 0
        GROUP BY step ORDER BY step
    """).fetchall()
    step_current = db.execute("""
        SELECT step, date_entered,
               CAST(julianday('now') - julianday(date_entered) AS INTEGER) days_so_far
        FROM step_durations
        WHERE date_left IS NULL AND date_entered IS NOT NULL
        ORDER BY days_so_far DESC LIMIT 30
    """).fetchall()
    return dict(total=total, by_model=by_model, by_status=by_status,
                by_country=by_country, delayed=delayed, damaged=damaged,
                recent=recent, step_avgs=step_avgs, step_current=step_current)

# ── Templates ─────────────────────────────────────────────────────────────────

BASE_STYLE = """
<style>
* { box-sizing: border-box; }
body { background:#0f0f1a; color:#ddd; font-family:monospace; margin:0; padding:1.5rem; }
a { color:#cc0000; text-decoration:none; }
h1 { color:#cc0000; margin-bottom:.2rem; font-size:1.6rem; }
h2 { color:#aaa; font-size:.9rem; border-bottom:1px solid #1e1e2e; padding-bottom:.4rem; margin:2rem 0 .8rem; letter-spacing:.08em; }
.nav { margin-bottom:1.5rem; font-size:.9rem; }
.nav a { margin-right:1.2rem; }
.meta { color:#555; font-size:.8rem; margin-bottom:1.5rem; }
pre { background:#080816; border:1px solid #1e1e2e; padding:1.2rem; border-radius:6px;
      white-space:pre-wrap; word-break:break-word; font-size:.85rem; line-height:1.7; }
table { border-collapse:collapse; width:100%; }
th { color:#cc0000; text-align:left; padding:.4rem .8rem; border-bottom:1px solid #1e1e2e;
     font-size:.75rem; text-transform:uppercase; letter-spacing:.07em; }
td { padding:.35rem .8rem; border-bottom:1px solid #111; font-size:.82rem; }
tr:hover td { background:#0d0d1e; }
.stat-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:.8rem; margin:1rem 0 1.5rem; }
.stat-box  { background:#0d0d1e; border:1px solid #1e1e2e; border-radius:6px; padding:.9rem; text-align:center; }
.stat-box .num { font-size:2rem; color:#cc0000; font-weight:bold; }
.stat-box .lbl { font-size:.7rem; color:#555; margin-top:.2rem; }
.section { background:#0d0d1e; border:1px solid #1e1e2e; border-radius:6px; padding:1.1rem 1.2rem; margin-bottom:1.2rem; }
.bar-wrap { margin:.4rem 0; }
.bar-label { font-size:.8rem; color:#888; margin-bottom:.2rem; display:flex; justify-content:space-between; align-items:baseline; }
.bar-label .val { color:#ccc; font-weight:bold; font-size:.85rem; }
.bar-label .sub { color:#444; font-size:.72rem; font-weight:normal; margin-left:.5rem; }
.bar-bg   { background:#080816; border-radius:3px; height:10px; }
.bar-red  { background:#cc0000; border-radius:3px; height:10px; }
.bar-blue { background:#1e3a6e; border-radius:3px; height:10px; }
.bar-models { font-size:.7rem; color:#333; margin-top:.15rem; }
.badge { display:inline-block; padding:.12rem .5rem; border-radius:3px; font-size:.72rem; }
.badge-current { background:#2a1010; color:#cc4444; }
.badge-pending  { background:#111; color:#444; }
.badge-visited  { background:#0f1f0f; color:#4a8f4a; }
input[type=text],input[type=password] {
  background:#080816; border:1px solid #2a2a3a; color:#eee;
  padding:.4rem .8rem; border-radius:4px; width:280px; margin-bottom:.6rem; display:block; }
input[type=submit] { background:#cc0000; border:none; color:#fff;
  padding:.5rem 1.5rem; border-radius:4px; cursor:pointer; font-size:.95rem; margin-top:.4rem; }
label { color:#888; font-size:.85rem; margin-bottom:.2rem; display:block; }
</style>
"""

TRACKER_PAGE = BASE_STYLE + """
<div class="nav"><a href="/">🚗 Tracker</a><a href="/stats">📊 Statistics</a></div>
<h1>Toyota Order Tracker</h1>
<p class="meta">Check your Toyota order status · credentials never stored</p>
{% if not username %}
<form method="POST" style="max-width:320px">
  <label>Toyota account email</label>
  <input type="text" name="username" placeholder="your@email.com" required>
  <label>Password</label>
  <input type="password" name="password" required>
  <input type="submit" value="Check my order →">
</form>
{% else %}
<pre>{{ output }}</pre>
<p style="margin-top:1rem;"><a href="/">← Check again</a> &nbsp; <a href="/stats">📊 Global stats</a></p>
{% endif %}
"""

STATS_PAGE = BASE_STYLE + """
<div class="nav"><a href="/">🚗 Tracker</a><a href="/stats">📊 Statistics</a></div>
<h1>📊 Global Order Statistics</h1>
<p class="meta">Anonymized · no credentials or personal info stored · updates on each login</p>

<div class="stat-grid">
  <div class="stat-box"><div class="num">{{ total }}</div><div class="lbl">Unique orders</div></div>
  <div class="stat-box"><div class="num">{{ delayed }}</div><div class="lbl">Delayed</div></div>
  <div class="stat-box"><div class="num">{{ damaged }}</div><div class="lbl">Damage codes</div></div>
  <div class="stat-box"><div class="num">{{ pct_delayed }}%</div><div class="lbl">Delay rate</div></div>
</div>

<h2>⏱ HOW LONG DOES EACH STEP TAKE?</h2>
<div class="section">
{% if step_avgs %}
  {% set max_avg = namespace(v=1) %}
  {% for row in step_avgs %}{% if row['avg_days'] > max_avg.v %}{% set max_avg.v = row['avg_days'] %}{% endif %}{% endfor %}
  {% for row in step_avgs %}
  {% set pct = ((row['avg_days'] / max_avg.v) * 100)|int %}
  <div class="bar-wrap">
    <div class="bar-label">
      <span>{{ row['step'] }}</span>
      <span class="val">~{{ row['avg_days'] }} days
        <span class="sub">min {{ row['min_days'] }} / max {{ row['max_days'] }} · {{ row['samples'] }} orders</span>
      </span>
    </div>
    <div class="bar-bg"><div class="bar-blue" style="width:{{ pct }}%"></div></div>
  </div>
  {% endfor %}
  {% if step_current %}
  <div style="border-top:1px solid #1e1e2e;margin-top:1rem;padding-top:.9rem;">
    <div style="font-size:.72rem;color:#444;margin-bottom:.6rem;letter-spacing:.07em;">CURRENTLY IN PROGRESS</div>
    <table>
      <tr><th>Step</th><th>Days so far</th><th>Since</th></tr>
      {% for row in step_current %}
      <tr>
        <td>{{ row['step'] }}</td>
        <td style="color:#cc0000;">{{ row['days_so_far'] }}d</td>
        <td style="color:#333;">{{ row['date_entered'] }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
  {% endif %}
{% else %}
  <p style="color:#444;font-size:.85rem;">No duration data yet — populates as orders advance through steps.</p>
{% endif %}
</div>

<h2>🌍 BY DESTINATION COUNTRY</h2>
<div class="section">
{% if by_country %}
  {% set max_c = by_country[0]['total'] %}
  {% for row in by_country %}
  {% set pct  = ((row['total'] / max_c) * 100)|int %}
  {% set dpct = ((row['delayed'] / row['total']) * 100)|int if row['total'] else 0 %}
  <div class="bar-wrap" style="margin-bottom:.8rem;">
    <div class="bar-label">
      <span>{{ row['dest_country'] }}</span>
      <span class="val">{{ row['total'] }}
        {% if dpct > 0 %}<span class="sub" style="color:#6a2a2a;">{{ dpct }}% delayed</span>{% endif %}
      </span>
    </div>
    <div class="bar-bg"><div class="bar-red" style="width:{{ pct }}%"></div></div>
    <div class="bar-models">{{ row['models'] }}</div>
  </div>
  {% endfor %}
{% else %}
  <p style="color:#444;font-size:.85rem;">No country data yet.</p>
{% endif %}
</div>

<h2>📋 BY CURRENT STATUS</h2>
<div class="section">
{% set max_s = namespace(v=1) %}
{% for row in by_status %}{% if row['c'] > max_s.v %}{% set max_s.v = row['c'] %}{% endif %}{% endfor %}
{% for row in by_status %}
{% set pct = ((row['c'] / max_s.v) * 100)|int %}
<div class="bar-wrap">
  <div class="bar-label"><span>{{ row['status'] }}</span><span class="val">{{ row['c'] }}</span></div>
  <div class="bar-bg"><div class="bar-red" style="width:{{ pct }}%;opacity:.7"></div></div>
</div>
{% endfor %}
</div>

<h2>🚗 BY MODEL</h2>
<div class="section">
<table>
<tr><th>Model</th><th>Orders</th></tr>
{% for row in by_model %}
<tr><td>{{ row['model'] }}</td><td>{{ row['c'] }}</td></tr>
{% endfor %}
</table>
</div>

<h2>🕐 RECENT CHECKS</h2>
<div class="section">
<table>
<tr><th>Time (UTC)</th><th>Model</th><th>Status</th><th>Country</th></tr>
{% for row in recent %}
<tr>
  <td style="color:#333">{{ row['ts'][:16] }}</td>
  <td>{{ row['model'] or '—' }}</td>
  <td><span class="badge badge-{{ row['status'] }}">{{ row['status'] or '—' }}</span></td>
  <td>{{ row['dest_country'] or '—' }}</td>
</tr>
{% endfor %}
</table>
</div>
"""

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def index():
    username = USERNAME
    password = PASSWORD
    output   = None

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

    if username and password:
        try:
            sys.path.insert(0, '/app')
            from toyota import ToyotaSession

            # Single API session — used for display, stats and date tracking
            session = ToyotaSession(username, password)
            lines   = []

            for oid in session.fetch_orders():
                details = session.fetch_order_details(oid)

                # Read step dates written by previous --store-dates runs
                dates_file = f"/data/{oid}.json"
                step_dates = {}
                if os.path.exists(dates_file):
                    with open(dates_file) as f:
                        step_dates = json.load(f)

                # Save stats — max one row per order per day
                save_stats(details, step_dates, today_only=True)

                # Build display output from raw data
                od     = details.get("orderDetails", {})
                st     = details.get("currentStatus", {})
                steps  = details.get("preprocessed", {}).get("steps", {})
                delivs = details.get("intermediateDeliveries", [])

                lines += [
                    f"\n  Order {od.get('orderId', '')}",
                    f"\n  Status:            {st.get('currentStatus', '')}",
                    f"  Estimated delivery:{details.get('etaToFinalDestination', 'N/A')}",
                    f"\n  Delayed:  {st.get('isDelayed', False)}",
                    f"  Damage:   {st.get('damageCode') or 'None'}",
                    f"\n  Vehicle:      {od.get('vehicleModel', '')}",
                    f"  Engine:       {od.get('engine', '')}",
                    f"  Transmission: {od.get('transmission', '')}",
                    f"  Colour:       {od.get('vehicleExternalColor', '')}",
                    f"  VIN:          {od.get('vin') or 'not yet assigned'}",
                    "\n  Steps:",
                ]
                for k, v in steps.items():
                    lines.append(f"    {k:<28} {v.get('status', '')}")

                if delivs:
                    lines.append("\n  Delivery route:")
                    for d in delivs:
                        lines.append(
                            f"    {d.get('locationName','')}, "
                            f"{d.get('countryName','')} "
                            f"[{d.get('destinationType','')}] "
                            f"— {d.get('isVisited','')}"
                        )

            output = html.escape("\n".join(lines)) if lines else html.escape("No orders found.")

        except Exception as e:
            output = html.escape(f"Error: {e}")

    return render_template_string(TRACKER_PAGE, output=output or "", username=username)

@app.route("/stats")
def stats():
    d   = get_stats_data()
    pct = int(d["delayed"] / d["total"] * 100) if d["total"] else 0
    return render_template_string(STATS_PAGE, pct_delayed=pct, **d)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)