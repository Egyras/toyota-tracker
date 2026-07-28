FROM python:3.11-slim

WORKDIR /app

# System deps for Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl nodejs npm \
    libnspr4 libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libatspi2.0-0 \
    libasound2t64 libx11-6 libxcb1 libxext6 \
    && rm -rf /var/lib/apt/lists/*

# Fetch the Toyota API script
RUN git clone https://github.com/rmudingay/toyota.git /tmp/toyota \
    && cp /tmp/toyota/toyota.py /app/toyota.py \
    && rm -rf /tmp/toyota

# Install Python dependencies (no playwright here - using Node version)
RUN pip install --no-cache-dir requests flask

# Install Node Playwright locally in /app so require('playwright') works
WORKDIR /app

# Install browsers to a shared, world-readable location instead of /root/.cache,
# which only root can read. The scraper runs as an unprivileged user (below) so
# that Chromium can enable its sandbox, and it must be able to find the binaries.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# leaflet is vendored (not pulled from a CDN at runtime) so the page's
# Content-Security-Policy can forbid third-party script origins outright.
# Served by the /vendor/leaflet route in web.py.
RUN npm init -y \
    && npm install playwright playwright-extra playwright-extra-plugin-stealth leaflet@1.9.4 \
    && npx playwright install chromium \
    && npx playwright install-deps chromium

# Fail the build rather than ship an image whose map silently 404s.
RUN test -f /app/node_modules/leaflet/dist/leaflet.js \
    && test -f /app/node_modules/leaflet/dist/leaflet.css

# Unprivileged account for the browser. Chromium refuses to enable its sandbox
# as root, which is why detect_vessel.js previously had to pass --no-sandbox.
# The Flask process still starts as root so it can keep writing the existing
# root-owned /data volume; only the browser subprocess drops to this user
# (see _drop_priv_kwargs in web.py).
RUN useradd --create-home --home-dir /home/pwuser --shell /usr/sbin/nologin pwuser \
    && chmod -R a+rX /ms-playwright /app/node_modules \
    && test -d /home/pwuser

# Sanity-check that the unprivileged user can actually reach the browser binary —
# otherwise the failure only shows up at runtime as "browser not found".
RUN su -s /bin/sh pwuser -c 'ls /ms-playwright' >/dev/null

# Copy app files
COPY web.py /app/web.py
COPY detect_vessel.js /app/detect_vessel.js

EXPOSE 8080

# One image, two roles — selected at runtime with the ROLE env var:
#
#   ROLE=web      (default) the Flask site: database, user credentials, no browser
#   ROLE=scraper  Chromium only: MyShipTracking login, no database, no LAN route
#
# Deliberately one image rather than two. The scraper needs the same web.py and
# detect_vessel.js, and a single build means the pair can never drift out of
# sync — a mismatched detector and caller would fail in confusing ways.
ENV ROLE=web
CMD ["python", "/app/web.py"]