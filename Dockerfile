FROM python:3.11-slim

# System dependencies for Playwright
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium for Playwright
RUN playwright install chromium --with-deps

COPY src/ ./src/

# Cases folder is mounted as a volume at runtime
# so outputs persist outside the container
VOLUME ["/app/cases"]

ENTRYPOINT ["python", "src/main.py"]
