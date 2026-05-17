# Toyota Europe Order Tracker

> Track your Toyota order from factory floor to dealer door — with community statistics, interactive maps, and step timestamps that Toyota's own app doesn't show.

**Live:** [toyotatracking.vorupe.eu](https://toyotatracking.vorupe.eu)

---

## Why this exists

Toyota's official portal shows your order status but hides step timestamps, has no community data, and can't be shared with family. This tracker fills those gaps.

| Feature | Toyota official | This tracker |
|---|---|---|
| Order status | ✅ | ✅ |
| Car image | ✅ | ✅ |
| Step timestamps | ❌ | ✅ |
| Interactive route map | ❌ | ✅ |
| Delay rates by country | ❌ | ✅ |
| Community step durations | ❌ | ✅ |
| Shareable public URL | ❌ | ✅ |
| Works for any EU country | ❌ | ✅ |
| Open source / verifiable | ❌ | ✅ |

---

## Screenshots

### Order tracker
- Car photo from Toyota CDN
- Vehicle details (engine, transmission, colour, VIN, order date)
- Animated step timeline with exact dates
- Interactive Leaflet map showing the full delivery route

### Statistics (`/stats`)
- Orders tracked, countries, delay rate
- Per-order journey progress bars
- Average days per step (community data)
- Breakdown by country, model, and status

---

## Privacy

**Credentials are never stored.** Your email and password go directly to Toyota's API at `ssoms.toyota-europe.com` and are discarded immediately after the response.

Only anonymized statistics are saved per login:

| Saved | Not saved |
|---|---|
| Vehicle model | Email / username |
| Engine & transmission | Password |
| Destination country | Order ID |
| Current step status | Full name |
| Delayed / damage flags | VIN |
| Step transition dates | Dealer details |

Verify this yourself in [`save_stats()`](web.py#L60).

---

## Architecture

```
Git push (main)
    ↓
Jenkins (TrueNAS App)
    ↓  docker build + push
Docker Hub (vaikis/toyota-tracker)
    ↓  docker pull + run
toyota-tracker container (:8889)
    ↑
Cloudflared (TrueNAS App)
    ↑
toyotatracking.vorupe.eu (Cloudflare Tunnel)
```

### Stack

| Component | Technology |
|---|---|
| Web app | Python / Flask |
| Toyota API client | [rmudingay/toyota](https://github.com/rmudingay/toyota) |
| Database | SQLite (persistent Docker volume) |
| Map | Leaflet.js + CartoDB dark tiles |
| CI/CD | Jenkins on TrueNAS SCALE |
| Tunnel | Cloudflare Zero Trust |
| Registry | Docker Hub (`vaikis/toyota-tracker`) |

---

## Repository structure

```
toyota-tracker/
├── web.py           # Flask app — tracker UI, stats, DB logic
├── Dockerfile       # Builds image, fetches toyota.py from source
├── Jenkinsfile      # CI/CD pipeline: build → push → deploy
├── docker-compose.yml  # Local development only
└── README.md
```

---

## Running locally

```bash
# Clone
git clone https://github.com/Egyras/toyota-tracker.git
cd toyota-tracker

# Build and start
docker compose up --build

# Open
open http://localhost:8889
```

---

## CI/CD — Jenkins setup

Jenkins runs as a TrueNAS App with Docker socket access.

### Required credentials

| Credential ID | Type | Value |
|---|---|---|
| `dockerhub` | Username / Password | Docker Hub login for `vaikis` |

### Pipeline stages

```
Checkout → Build image → Push to Docker Hub → Deploy on TrueNAS
```

Triggered automatically by SCM polling every minute. Any `git push` to `main` triggers a full redeploy within 60 seconds.

---

## Data notes

Step transition dates are **approximate** — Toyota's API does not expose timestamps. Dates are recorded by the tracker the first time a user logs in while their order is at a given step. Accuracy depends on how frequently users check.

---

## API endpoints used

| Endpoint | Purpose |
|---|---|
| `ssoms.toyota-europe.com/authenticate` | Authentication |
| `weblos.toyota-europe.com/leads/ordered` | Order list + `createdOn` |
| `cpb2cs.toyota-europe.com/api/orderTracker/user/{uuid}/orderStatus/{id}` | Order details, steps, delivery route |

Toyota does not provide step timestamps, vessel names, or real-time location data via their API.

---

## Contributing

Issues and PRs welcome. If you find a hidden API endpoint with better data — especially step timestamps or vessel tracking — please open an issue.