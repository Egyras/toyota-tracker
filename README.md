# Toyota Order Tracker

Track your Toyota order status and collect anonymized global statistics.

## Public URL
`https://toyotatracking.vorupe.eu`

## Architecture

```
Git push → Jenkins → Docker Hub → TrueNAS container
                                        ↑
                          Cloudflared TrueNAS App
                                        ↑
                          toyotatracking.vorupe.eu
```

## Repo structure

| File | Purpose |
|---|---|
| `Dockerfile` | Builds the image — fetches toyota.py + installs Flask |
| `web.py` | Flask app — login form, order display, stats collection |
| `Jenkinsfile` | CI/CD pipeline — build → push → deploy |
| `docker-compose.yml` | Local development only |

## Jenkins credentials required

| ID | Type | Value |
|---|---|---|
| `dockerhub` | Username/Password | Docker Hub login for `vaikis` |

## Local development

```bash
docker compose up --build
# open http://localhost:8888
```

## Stats

Anonymous statistics collected per check (no credentials stored):
- Vehicle model, engine, transmission, colour
- Current step status + dates (for duration tracking)
- Destination country
- Delayed / damage flags

Available at `/stats`.
