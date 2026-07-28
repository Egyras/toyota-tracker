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

# Copy app files
COPY web.py /app/web.py
COPY detect_vessel.js /app/detect_vessel.js

EXPOSE 8080

CMD ["python", "/app/web.py"]