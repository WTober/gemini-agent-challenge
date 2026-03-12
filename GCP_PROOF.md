# GolfStatus Browser Agent – Google Cloud Deployment Proof

> **Gemini Live Agent Challenge** | Project: `golfstatus-a8d6c` | Region: `europe-west3` (Frankfurt)

## Google Cloud Services Used

| Service | Purpose | Code Reference |
|---------|---------|----------------|
| **Cloud Functions (2nd Gen)** | Go backend – Agent orchestration, Firestore triggers, AI prompts | [deploy/](deploy/) |
| **Cloud Run** | Python Browser Agent – Playwright + Gemini Vision | [browser_agent/Dockerfile](browser_agent/Dockerfile), [browser_agent/main.py](browser_agent/main.py) |
| **Vertex AI (Gemini)** | LLM inference via `genai.BackendVertexAI` | [cloud_functions/run_agent.go](cloud_functions/run_agent.go) |
| **Cloud Firestore** | Real-time database for users, agents, runs, chat | [cloud_functions/run_agent.go](cloud_functions/run_agent.go) |
| **Firebase Authentication** | User auth with ID token verification | [cloud_functions/run_agent.go](cloud_functions/run_agent.go) |
| **Cloud Build** | Docker image builds for Cloud Run | [deploy/](deploy/) |
| **Artifact Registry** | Container image storage | `europe-west3-docker.pkg.dev/golfstatus-a8d6c/gcf-artifacts` |

## Key Code Demonstrating GCP Usage

### 1. Vertex AI Gemini API

From [`cloud_functions/run_agent.go`](cloud_functions/run_agent.go) – the agent uses Vertex AI Gemini for both text prompts and browser vision:

```go
aiClient, err := genai.NewClient(ctx, &genai.ClientConfig{
    Project:  ProjectID,           // golfstatus-a8d6c
    Location: location,            // europe-west3
    Backend:  genai.BackendVertexAI,
})

// Google Search Grounding for live data
config.Tools = []*genai.Tool{
    {GoogleSearch: &genai.GoogleSearch{}},
}

resp, err := aiClient.Models.GenerateContent(ctx, model, genai.Text(prompt), config)
```

### 2. Cloud Run Browser Agent

From [`browser_agent/main.py`](browser_agent/main.py) – deployed as a Cloud Run service with Playwright + Gemini Vision:

- **Cloud Run**: 2 vCPUs, 2GB RAM, `europe-west3`
- **Gemini Vision**: Screenshot analysis for autonomous browser navigation
- **Deployed URL**: `https://run-browser-agent-*.europe-west3.run.app`

### 3. Cloud Firestore

```go
fsClient, err := firestore.NewClient(ctx, ProjectID)
doc, err := fsClient.Collection("agent_runs").Doc(runID).Get(ctx)
_, err = fsClient.Collection("agent_runs").Doc(runID).Update(ctx, updates)
```

## Video Proof

> Screen recording demonstrating live GCP deployment:

https://github.com/WTober/gemini-agent-challenge/blob/main/gcp_proof.mov

The recording shows:
1. Active GCP project (`golfstatus-a8d6c`)
2. **42 deployed Cloud Functions** (2nd Gen, `europe-west3`)
3. **Cloud Run service** (`run-browser-agent`) with Gemini 2.5 Flash
4. **Vertex AI** integration code (`genai.BackendVertexAI`)
5. Environment variables confirming GCP configuration
