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
# searoute: computes real maritime sea-routes (used for vessel route line on map)
RUN pip install --no-cache-dir requests flask searoute

# Install Node Playwright locally in /app so require('playwright') works
WORKDIR /app
RUN npm init -y \
    && npm install playwright playwright-extra playwright-extra-plugin-stealth \
    && npx playwright install chromium \
    && npx playwright install-deps chromium

# Copy app files
COPY web.py /app/web.py
COPY detect_vessel.js /app/detect_vessel.js

EXPOSE 8080

CMD ["python", "/app/web.py"]