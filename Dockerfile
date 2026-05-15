FROM python:3.11-slim

WORKDIR /app

# Install git to clone toyota.py from source repo
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Fetch the Toyota API script
RUN git clone https://github.com/rmudingay/toyota.git /tmp/toyota \
    && cp /tmp/toyota/toyota.py /app/toyota.py \
    && rm -rf /tmp/toyota

# Install Python dependencies
RUN pip install --no-cache-dir requests flask

# Copy web wrapper
COPY web.py /app/web.py

EXPOSE 8080

CMD ["python", "/app/web.py"]
