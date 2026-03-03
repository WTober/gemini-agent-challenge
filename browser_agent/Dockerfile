# Playwright requires system dependencies not available in the base Python image.
# This Dockerfile installs Playwright + Chromium for Cloud Functions 2nd Gen.
FROM python:3.12-slim

# Suppress debconf interactive frontend warnings during apt-get
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies for Chromium
RUN apt-get update && apt-get install -y \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libpangocairo-1.0-0 libcairo2 \
    libx11-6 libxcb1 libxext6 wget fonts-noto-color-emoji \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (Chromium only to keep image small)
RUN playwright install chromium

COPY main.py .

ENV PORT=8080
CMD ["functions-framework", "--target=RunBrowserAgent", "--port=8080"]
