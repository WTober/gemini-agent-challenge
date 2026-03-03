#!/bin/bash
# Deploy GolfStatus Browser Agent to Cloud Run
# Required: gcloud CLI authenticated, Docker installed
# Usage: ./deploy_cloudrun.sh

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────────
PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-europe-west3}"
SERVICE_NAME="${SERVICE_NAME:-browser-agent}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

if [ -z "$PROJECT_ID" ]; then
    echo "❌ Error: PROJECT_ID environment variable is required."
    echo "   Usage: PROJECT_ID=your-project-id ./deploy_cloudrun.sh"
    exit 1
fi

echo "🚀 Deploying Browser Agent to Cloud Run"
echo "   Project:  ${PROJECT_ID}"
echo "   Region:   ${REGION}"
echo "   Service:  ${SERVICE_NAME}"
echo "   Model:    ${GEMINI_MODEL}"
echo ""

# ── Build Docker Image ───────────────────────────────────────────────────────────
echo "📦 Building Docker image..."
docker build -t "${IMAGE}" ../browser_agent/

# ── Push to Container Registry ───────────────────────────────────────────────────
echo "☁️  Pushing to Container Registry..."
docker push "${IMAGE}"

# ── Deploy to Cloud Run ──────────────────────────────────────────────────────────
echo "🏗️  Deploying to Cloud Run..."
gcloud run deploy "${SERVICE_NAME}" \
    --image "${IMAGE}" \
    --region "${REGION}" \
    --project "${PROJECT_ID}" \
    --platform managed \
    --memory 2Gi \
    --cpu 2 \
    --timeout 300 \
    --concurrency 1 \
    --min-instances 0 \
    --max-instances 5 \
    --set-env-vars "GCP_PROJECT=${PROJECT_ID},GEMINI_MODEL=${GEMINI_MODEL}" \
    --no-allow-unauthenticated

echo ""
echo "✅ Deployment complete!"
echo "   Service URL: $(gcloud run services describe ${SERVICE_NAME} --region ${REGION} --project ${PROJECT_ID} --format='value(status.url)')"
