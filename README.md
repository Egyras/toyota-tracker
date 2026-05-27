# Toyota Europe Order Tracker

> Know exactly where your car is — from the factory floor in Japan to your dealer's door. Community timestamps, live vessel tracking, and an interactive route map that Toyota's own app doesn't offer.

**Live:** [toyotatracking.vorupe.eu](https://toyotatracking.vorupe.eu)

---

## What it does

Toyota's official portal shows your order status but hides when each step happened, offers no community data, and can't be shared with family. This tracker fills every gap.

| Feature | Toyota official | This tracker |
|---|---|---|
| Order status | ✅ | ✅ |
| Car image | ✅ | ✅ |
| Step timestamps | ❌ | ✅ |
| Interactive route map | ❌ | ✅ |
| **Live vessel tracking** | ❌ | ✅ |
| Delay rates by country | ❌ | ✅ |
| Community step durations | ❌ | ✅ |
| Shareable public URL | ❌ | ✅ |
| Works for any EU country | ❌ | ✅ |
| Open source / verifiable | ❌ | ✅ |

---

## Vessel tracking

Once your car leaves the factory, the tracker automatically identifies which ship is carrying it and shows its live position on the map.

### How it works

```
leftTheFactory date recorded
        ↓
MyShipTracking login (Playwright headless)
        ↓
Scrape Nagoya port departures ±5 days around that date
        ↓
Filter for Toyota Europe carriers (K-Line HIGHWAY, NYK LEADER, MOL ACE, etc.)
        ↓
If multiple matches → score each by European port call history
  (Zeebrugge × N, Bremerhaven × N, Southampton × N → pick highest)
        ↓
Fetch live position via MST internal API → ShipFinder fallback
        ↓
🚢 Ship appears on Leaflet map with speed, course, destination
```

### Multi-leg detection

Toyota vehicles travel on **three separate vessel legs** before reaching the dealer:

| Leg | Route | Port scraped |
|---|---|---|
| 1 | Japan → Zeebrugge | Nagoya (pid 4715) |
| 2 | Zeebrugge → Malmö | Zeebrugge (pid 187) |
| 3 | Malmö → Paldiski | Malmö (pid 286) |

Each leg is detected independently. When a delivery hub becomes `inTransit`, the tracker automatically checks that hub's departures for the next leg's vessel.

### Supported carriers

**Leg 1 — Deep sea (Japan → Europe ~38 days):**
K-Line: Hamburg Highway, Elbe Highway, Galveston Highway, Adriatic Highway, Hera Highway, Danube Highway  
NYK: Altair Leader, Equuleus Leader, Garnet Leader, Sagittarius Leader, Triton Leader, Spica Leader  
MOL: Emerald Ace, Morning Claire, Morning Highway

**Leg 2 — North Sea feeder (Zeebrugge → Nordic):**
HIGHWAY class, MORNING class, Viking, Siem, Höegh, Celtic

**Leg 3 — Baltic feeder (Malmö → Paldiski):**
HIGHWAY class, LEADER class, Nordana, Siem, Celtic

### Vessel position sources

| Source | Coverage | Cost |
|---|---|---|
| MyShipTracking internal API | Terrestrial AIS — good near coasts | Free (scraped) |
| ShipFinder fallback | Satellite AIS — works in open ocean | Free (scraped) |

Position is cached per order for 6 hours. When stale, only the position is refreshed — vessel identity is retained.

---

## Privacy

**Credentials are never stored.** Your Toyota email and password go directly to Toyota's API and are discarded immediately after the response.

Only anonymised statistics are saved per login:

| Saved | Not saved |
|---|---|
| Vehicle model | Email / username |
| Engine & transmission | Password |
| Destination country | Order ID |
| Current step status | Full name |
| Delayed / damage flags | VIN |
| Step transition dates | Dealer details |

Verify this yourself in [`save_stats()`](web.py).

---

## Architecture

```
Git push → Jenkins (TrueNAS) → docker build + push → Docker Hub
                                                           ↓
                                               toyota-tracker (:8889)
                                                           ↑
                                           Cloudflared tunnel
                                                           ↑
                                     toyotatracking.vorupe.eu
```

### Stack

| Component | Technology |
|---|---|
| Web app | Python 3.11 / Flask |
| Toyota API client | [rmudingay/toyota](https://github.com/rmudingay/toyota) |
| Vessel scraper | Node.js + Playwright (headless Chromium) |
| Database | SQLite (persistent Docker volume) |
| Map | Leaflet.js + CartoDB dark tiles |
| CI/CD | Jenkins on TrueNAS SCALE |
| Tunnel | Cloudflare Zero Trust |
| Registry | Docker Hub (`vaikis/toyota-tracker`) |

---

## Repository structure

```
toyota-tracker/
├── web.py              # Flask app — tracker UI, stats, vessel API, DB logic
├── detect_vessel.js    # Playwright scraper — vessel detection + position
├── Dockerfile          # Python + Node + Playwright Chromium
├── Jenkinsfile         # CI/CD: build → push → deploy
├── docker-compose.yml  # Local development only
└── README.md
```

---

## Running locally

```bash
git clone https://github.com/Egyras/toyota-tracker.git
cd toyota-tracker

# Set credentials (never committed)
export MST_EMAIL=your@email.com
export MST_PASSWORD=yourpassword

docker compose up --build
open http://localhost:8889
```

---

## CI/CD — Jenkins setup

Jenkins runs as a TrueNAS App with Docker socket access. Any push to `main` triggers a full redeploy within 60 seconds.

### Required credentials

| Credential ID | Type | Value |
|---|---|---|
| `dockerhub` | Username / Password | Docker Hub token for `vaikis` |
| `mst-email` | Secret text | MyShipTracking account email |
| `mst-password` | Secret text | MyShipTracking account password |

### Pipeline stages

```
Checkout → Build image → Push to Docker Hub → Deploy on TrueNAS
```

> **Note:** First build takes 5–10 minutes — Playwright downloads ~300MB of Chromium. Subsequent builds use Docker layer cache.

---

## API endpoints

### Toyota (read-only, no official API)

| Endpoint | Purpose |
|---|---|
| `ssoms.toyota-europe.com/authenticate` | Authentication |
| `weblos.toyota-europe.com/leads/ordered` | Order list + `createdOn` |
| `cpb2cs.toyota-europe.com/api/orderTracker/user/{uuid}/orderStatus/{id}` | Order details, steps, delivery route |

Toyota does not expose step timestamps, vessel names, or real-time GPS via their API. All of those are derived by this tracker.

### Tracker (internal)

| Endpoint | Purpose |
|---|---|
| `GET /api/vessel-detect/{hash}` | Auto-detect vessel + live position (6h cache) |
| `GET /api/vessel-detect/{hash}?depart_date=YYYY-MM-DD&leg=zeebrugge` | Manual date/leg override |
| `GET /api/vessel/{mmsi}` | Live position for known MMSI |
| `GET /stats/count` | Total tracked orders (nav badge) |

---

## Data accuracy notes

**Step dates** are approximate — Toyota's API exposes only `isVisited` flags, not timestamps. Dates are recorded the first time a user logs in while their order is at a given step. Accuracy improves with frequent logins.

**Vessel detection** is heuristic — it matches departure timing and European port history. Accuracy is high (~95%) for the Japan→Europe leg, and good for feeder legs when the visited date is known.

**Vessel position** is real-time AIS from MyShipTracking (terrestrial) with ShipFinder satellite fallback. Coverage gaps exist in open ocean between Japan and Singapore.

---

## Contributing

Issues and PRs welcome.

If you find a hidden Toyota API endpoint with step timestamps or vessel information — please open an issue immediately, that would be a game-changer.