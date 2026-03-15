# 🏌️ GolfStatus Browser Agent – Gemini Live Agent Challenge

> **Category:** UI Navigator ☸️ | **Built with:** Gemini Vision + Google Cloud

## 🎯 What It Does

An **autonomous UI Navigator** that navigates complex web interfaces using a **hybrid approach** – combining Gemini Vision for intelligent decisions with deterministic DOM-based actions for reliability. The agent observes the screen through screenshots, interprets UI elements with **Gemini Vision**, and executes actions to complete multi-step workflows.

### Real-World Use Case: Automated Tee Time Booking

Golf clubs use booking systems like **PC Caddy** where reservations open exactly 6 days in advance at 21:00. Booking a tee time requires 10+ clicks through calendars, time selectors, and forms. Our agent:

1. **Navigates** the booking portal autonomously
2. **Finds** the right date and available time slot
3. **Fills** the reservation form
4. **Can be scheduled** to run at exactly 21:00 when the reservation window opens

> *"You're on the couch. The agent books your Saturday 8am tee time the second it becomes available."*

## 🏗️ Architecture

![Architecture Diagram](architecture.png)

### Screenshots

| Skill Editor | Agent Running | Booking Confirmed |
|---|---|---|
| ![Skill Editor](screenshots/skill_editor.jpg) | ![Agent Running](screenshots/agent_running.jpg) | ![PC Caddy Confirmed](screenshots/pccaddy_booking_confirmed.jpg) |

```
┌─────────────────┐     ┌──────────────────────┐     ┌─────────────────┐
│   Flutter App    │────▶│  Cloud Functions (Go) │────▶│  Cloud Run      │
│   (User/Admin)   │     │  Orchestration        │     │  Browser Agent  │
│                  │◀────│                        │◀────│  (Playwright +  │
│  • Agent Config  │     │  • Start/Stop Agent    │     │   Gemini Vision)│
│  • Dashboard     │     │  • Manage Runs         │     │                 │
│  • Skill Editor  │     │  • Firestore Logging   │     │  • Screenshot   │
└─────────────────┘     └──────────────────────┘     │  • Gemini Locate│
                                │                      │  • Click/Input  │
                                ▼                      │  • Vision Loop  │
                        ┌──────────────────┐           └────────┬────────┘
                        │   Firestore       │                    │
                        │  • Agent Runs     │◀───────────────────┘
                        │  • Step Logs      │    (live step updates
                        │  • Screenshots    │     + screenshots)
                        └──────────────────┘
```

### Tech Stack

| Component | Technology | Google Cloud Service |
|---|---|---|
| **Vision AI (Execution)** | Gemini 3.1 Flash Lite (multimodal) | Vertex AI / GenAI SDK |
| **Reasoning (Self-Healing)** | Gemini 3.1 Pro | Vertex AI / GenAI SDK |
| **NL Skill Compiler** | Gemini 3.1 Flash Lite | Vertex AI / GenAI SDK |
| **Browser** | Playwright (headless Chromium) | Cloud Run |
| **Orchestration** | Go Cloud Functions (2nd Gen) | Cloud Functions |
| **Data Store** | Firestore | Firestore |
| **Mobile App** | Flutter (Dart) | Firebase |
| **Scheduling** | Cloud Scheduler | Cloud Scheduler |

### Cloud Functions (Go Orchestration)

The `cloud_functions/` directory contains the Go Cloud Functions that orchestrate agent runs:

- **`scheduler.go`** – Triggered every 15 minutes by Cloud Scheduler. Matches user-configured schedules (weekly/daily/monthly) against the current time and triggers due agent runs.
- **`run_agent.go`** – Core agent execution: creates run documents in Firestore, routes to the appropriate executor (Gemini prompt or browser automation), and manages credits.

> **Key fix (March 2026):** Scheduled runs were stuck at 0 steps because `go executeAgentRun(...)` goroutines were killed when the Cloud Function returned its HTTP response. Fixed with `sync.WaitGroup` to ensure all runs complete before the function exits.

## 🔄 How It Works

### The Vision-Action Loop

```
Screenshot → Gemini Vision → Action Decision → Execute → Screenshot → ...
```

1. **Screenshot**: Playwright captures the current browser state
2. **Gemini Vision**: Image is sent to Gemini with a description of what to find
3. **Action**: Gemini returns coordinates (x, y) of the target element
4. **Execute**: Agent clicks, types, or scrolls at the exact position
5. **Repeat** until all steps are complete or a success condition is met

### Skill Definition (DSL)

Admins define skills as a sequence of steps using 18+ available commands:

```yaml
# Deterministic steps (Playwright)
navigate: https://booking.example.com/login
js: document.getElementById('login').value='{username}'
playwright: page.get_by_role("button", name="Sign In").click()

# Intelligent slot selection (Hybrid: JS reads DOM attributes + Python time logic)
find_slot: {target_time} -> {max_time}

# Visual search (Gemini Vision)
find_click: first available Saturday tee time

# Fully autonomous mode (Gemini decides all actions)
autonomous: Book a tee time on {target_date} at {target_time}
```

### 🎯 Hybrid Approach: `find_slot` (NEW)

The `find_slot` command demonstrates the **best of both worlds**:

1. **JavaScript** reads DOM attributes (`data-time`, `data-status="bookable"`) to find all available slots
2. **Python logic** selects the nearest time ≥ target within the allowed range
3. **Playwright** performs a real mouse click on the selected row

This is more reliable than pure vision (Gemini can't read small text in calendars) and more adaptive than hardcoded selectors (it finds the *best available* time, not a fixed one).

### 🤖 Autonomous Mode

For maximum flexibility, the `autonomous` action hands full control to Gemini. The agent:
1. Extracts page content as text
2. Sends it to Gemini with the goal description
3. Gemini decides which action to take (click, input, scroll, etc.)
4. Repeats up to 15 steps until the goal is achieved

This is ideal for one-off tasks on unfamiliar websites where defining a skill would be overkill.

### 🇩🇪 Natural Language Skill Compiler (NEW)

Skills can also be defined in **plain German** and compiled into executable commands:

```
Input:  "Navigiere zur Login-Seite, gib Benutzername und Passwort ein,
         klicke auf Anmelden und warte auf die Startseite."

Output:
  # Navigiere zur Login-Seite
  navigate: https://booking.example.com/login
  # gib Benutzername ein
  input: E-Mail-Feld -> {username}
  # und Passwort ein
  input: Passwort-Feld -> {password}
  # klicke auf Anmelden
  click: Anmelden-Button
  # warte auf die Startseite
  wait_for: Dashboard
```

- **Bidirectional**: Compile (NL → Commands) **and** Decompile (Commands → NL)
- **Knowledge Base**: A comprehensive system prompt teaches the model all 18+ available actions, Playwright API calls, placeholder syntax, and conventions
- **Lint & Validation**: Each compiled line is validated against known commands
- **Admin-configurable model**: Defaults to `gemini-3.1-flash-lite-preview` (fast & cost-efficient), changeable via Firestore config
- **Original text preserved**: Natural language is kept as `# comments` for documentation

This is powered by a dedicated **Go Cloud Function** (`CompileSkill`) using Vertex AI.

### 📹 Knowledge Base Segment Recording (NEW)

A **no-code workflow recorder** that captures browser interactions as reusable segments:

| Step | What happens |
|------|--------------|
| **1. Record** | Admin uses the in-app Chrome recorder to capture a workflow segment (login, club selection, date picker, etc.) |
| **2. Store** | Segment is saved to Firestore with exact CSS selectors captured from the DOM |
| **3. Compose** | Load 4 segments into a new skill — the app generates the full DSL automatically |
| **4. Run** | Agent executes with deterministic CSS selectors + Gemini Vision as fallback |

**Why this matters:** Onboarding a new booking portal goes from **days of coding** to **under 30 minutes of recording**. The CSS selectors are captured precisely during the recording — no selector guessing, no prompt engineering needed.

### Sandbox Mode

Every agent run can be executed in **Sandbox mode** – the agent navigates and screenshots everything, but does NOT submit forms or trigger real bookings. Perfect for testing and demos.

### 🔧 Self-Healing Loop (LIVE DEMO)

When the target website **completely redesigned its interface** during live operations – new selectors, dropdowns replaced by buttons, removed UI elements – the agent didn't break permanently. It **autonomously detected each failure**, analyzed screenshots with Gemini Vision, and proposed fixes. Over **5 cycles, the agent recovered from 11% to 100%** – completing a full booking on the redesigned website without re-recording the skill.

> **📄 [Full Self-Healing Demo – 5 Cycles, 5 Problems, 100% Recovery](SELF_HEALING_DEMO.md)**

## 🚀 Spin-Up Instructions

### Prerequisites

- Google Cloud account with billing enabled
- `gcloud` CLI installed and authenticated
- Docker installed (for local testing)

### 1. Clone & Configure

```bash
git clone https://github.com/WTober/gemini-agent-challenge.git
cd gemini-agent-challenge
```

### 2. Set Environment Variables

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="europe-west3"
export GEMINI_MODEL="gemini-3.1-flash-lite-preview"
```

### 3. Deploy to Cloud Run

```bash
cd deploy
chmod +x deploy_cloudrun.sh
./deploy_cloudrun.sh
```

### 4. Local Testing (Optional)

```bash
cd browser_agent
docker build -t browser-agent .
docker run -p 8080:8080 \
  -e GCP_PROJECT=$PROJECT_ID \
  -e GEMINI_MODEL=$GEMINI_MODEL \
  browser-agent
```

### 5. Test the Agent

```bash
curl -X POST http://localhost:8080 \
  -H "Content-Type: application/json" \
  -d '{
    "runId": "test-001",
    "agentId": "demo",
    "userId": "demo-user",
    "targetUrl": "https://example-golf-booking.com",
    "inputValues": {"player_name": "John Doe"},
    "actionSequence": [
      {"action": "click", "target": "https://example-golf-booking.com"},
      {"action": "find_click", "target": "booking calendar"}
    ],
    "successCondition": {"type": "visual_verification", "indicator": "Booking confirmed"},
    "dryRun": true
  }'
```

## 📹 Demo Video

[![Demo Video](https://img.youtube.com/vi/7VSr2ahW3uk/maxresdefault.jpg)](https://youtu.be/7VSr2ahW3uk)

**▶️ [Watch the Demo on YouTube](https://youtu.be/7VSr2ahW3uk)** (2:09 min)

The demo shows:
1. **Skill Definition** – Admin creates 31-step booking skill (DSL)
2. **Agent Configuration** – User sets date, tee time, playing partners
3. **Live Execution** – Agent navigates PC Caddy booking portal autonomously
4. **Real-Time Logging** – Step-by-step admin dashboard with screenshots
5. **Booking Confirmation** – Real reservation confirmed on the portal
6. **Scheduled Execution** – Switch to weekly automated scheduling

## 👨‍💻 About the Developer

Built by **Wolfgang Tober** – a 70-year-old developer with over **50 years of hands-on coding and architecture experience**.

From **IBM mainframes** to **personal computers** to **mobile apps** – Wolfgang has seen and built it all:
- 🖥️ Mainframe systems (COBOL, assembler)
- 🗄️ Databases across generations (DB2, Oracle, MySQL, PostgreSQL, Firestore, and more)
- 💻 Languages from Fortran to Go, Dart, Python, and Swift
- 📱 Modern mobile development with Flutter (Android + iOS)
- ☁️ Cloud-native architectures on Google Cloud

> *"Age is just a number. The passion for building things never gets old."*

## 📱 The Bigger Picture: From Technology Proof to Production App

GolfStatus started as a **technology proof-of-concept** — can Gemini navigate web UIs autonomously? It has since evolved into a **production-ready golf companion app** on Android and iOS, uniting **8+ distinct AI capabilities** in a single codebase. The Browser Agent is just one piece of a much larger ecosystem:

| AI Capability | Google Cloud Service | What It Does |
|---|---|---|
| 🌐 **Browser Agent** | Cloud Run + Gemini Vision | Autonomous booking on external portals (this submission) |
| 🔧 **Self-Healing Loop** | Gemini Vision + Firestore | Agent detects UI failures, proposes fixes from screenshots ([demo](SELF_HEALING_DEMO.md)) |
| 📊 **Course Status** | Cloud Functions + Gemini | Scrapes club websites, determines real-time course status (open/closed/restricted) |
| 📅 **Weekly Briefing** | Gemini + Google Search | AI-generated weekly analysis: weather, tournaments, course conditions |
| 🦊 **Deal Finder** | Gemini + Google Search | Discovers hidden green fee deals and discounts across the web |
| ✈️ **Travel Planner** | Gemini + Maps + Search | Complete golf trip planning with hotels, routes, course recommendations |
| 🦔 **Rules Hedgehog** | Gemini Vision (multimodal) | Snap a photo of your ball → instant, official rule guidance from the image |
| 💬 **Chat Moderation** | Gemini | Community chat with automatic moderation, smart replies, and photo analysis |
| 🎙️ **Agent TTS** | Cloud Text-to-Speech | Every agent result can be read aloud – Neural2 voices in German and English |
| 🤖 **Support Agent** | Gemini + Knowledge Base | In-app support with curated knowledge base for instant answers |
| 📰 **News Briefing** | Gemini + Google Search | Personalized golf news tailored to player interests |
| 🎨 **Presentation Generator** | Gemini + External API | Auto-compiles agent results into rich, shareable presentations |

> **One app, one developer, 8+ AI features – all powered by Google Cloud.** What began as a technology proof has grown into a comprehensive AI-powered golf companion that demonstrates the full breadth of the Google Cloud AI stack.

> **Want to try it?** The app is currently in testing. Access can be granted on request.
>
> 🌐 **Website:** [golfstatus-a8d6c.web.app](https://golfstatus-a8d6c.web.app/)
> - 🤖 [Android – Google Play](https://play.google.com/store/apps/details?id=de.wolfgangtober.golfstatus)
> - 🍎 [iOS – TestFlight](https://testflight.apple.com/join/SKzQgswR)

## 🏆 Built for the Gemini Live Agent Challenge

Created for the [Gemini Live Agent Challenge](https://geminiliveagentchallenge.devpost.com/) – `#GeminiLiveAgentChallenge`

**Author:** Wolfgang Tober | **License:** MIT
