# GolfStatus Browser Agent – Devpost Submission

> Copy these sections into the Devpost submission form.

---

## 📃 Project Title

**GolfStatus Browser Agent – AI That Books Your Tee Time**

---

## 📃 Category

**UI Navigator ☸️**

---

## 📃 Text Description

### Inspiration

Golf clubs use legacy booking systems like PC Caddy where popular tee times open exactly 6 days in advance – often at 9 PM. Miss that window by minutes, and the slots are gone. We built an AI agent that solves this: it watches the clock, navigates the booking portal the moment it opens, and secures your tee time. Automatically. Every week.

### What it does

The **GolfStatus Browser Agent** is a fully autonomous UI Navigator that:

- **Observes** the browser through screenshots (no DOM access, no APIs)
- **Understands** each page using **Gemini 2.5 Flash** multimodal vision
- **Acts** by clicking, typing, and scrolling at precise screen coordinates
- **Completes** complex 74-step workflows across real booking portals
- **Runs on schedule** – weekly at a specific day and time, fully unattended

The agent navigates login forms, calendars, time pickers, player registration, and booking confirmation – all through visual understanding alone.

This implements the **"Digital Optimus" paradigm**: a software agent that performs physical-world tasks (booking a tee time, securing a reservation) through digital interfaces – combining deterministic DOM access with AI vision, just like a robot combines sensors with cameras.

### Knowledge Base Segment Recording (NEW)

Admins can **record browser sessions** as reusable knowledge base segments – no coding required:

1. **Record** – Use the in-app recorder to capture a workflow segment (e.g., "login", "club selection", "date picker")
2. **Enrich** – Add validation steps (`wait_for`, `screenshot`) at critical checkpoints so the agent fails fast instead of continuing on the wrong page
3. **Compose** – Load 4 segments into a new skill in the Admin UI
4. **Run** – The skill uses a **3-tier execution strategy**: Playwright CSS → Gemini Vision → Text search

This "record once, reuse forever" approach dramatically reduces the time to support a new booking portal from days to under 30 minutes. Each segment stores Playwright selectors with built-in validation checkpoints. A **KB Feedback Loop** automatically writes execution results (which selectors worked, which needed fallbacks, timing per step) back to Firestore after each real run – building the foundation for self-improving skills.

### 🌐 Broader Multimodal Ecosystem

The same Gemini Vision infrastructure powers two additional features in GolfStatus:

- **🦔 Rules Hedgehog** – Snap a photo of your ball on the course → Gemini Vision analyzes the exact position and delivers instant, official rule guidance. Vision in, ruling out.
- **🎨 AI Presentation Generator (Gamma)** – After each agent run, results are auto-compiled into a rich visual presentation: screenshots, step log, booking confirmation – all in one shareable document. The agent doesn't just act, it reports.

Both features share the same Gemini multimodal pipeline, demonstrating that the architecture generalises far beyond browser automation.


### How we built it

**Architecture:** Flutter mobile app → Cloud Functions (Go) → Cloud Run (Playwright + Gemini Vision)

- **Browser Agent (Cloud Run):** A headless Chromium browser controlled by Playwright, guided by Gemini Vision. Each step follows a Screenshot → Gemini → Action → Screenshot loop. Gemini receives the screenshot and a description of what to find, returns (x, y) coordinates, and the agent clicks.

- **Skill DSL:** Admins define agent behavior as a sequence of steps in a simple JSON-based DSL. Actions include `click`, `find_click` (visual search), `input`, `screenshot`, `wait`, `abort_if`, and more. The `find_click` action is key – it tells Gemini to visually locate the best matching UI element, perfect for finding the first available slot in a calendar grid.

- **Natural Language Skill Compiler (NEW):** Skills can also be written in **plain German prose** and compiled into executable commands by a dedicated Cloud Function powered by Gemini 3.1 Flash Lite. The compiler is bidirectional – commands can be decompiled back to human-readable text. A built-in lint pass validates each line against the 17 known actions.

- **Orchestration (Cloud Functions, Go):** Manages agent lifecycle, resolves input placeholders, enforces sandbox mode, and writes real-time step logs to Firestore.

- **Sandbox Mode:** Every run can be executed in sandbox mode – the agent navigates and screenshots everything but does NOT click submit buttons. Safe for testing and demos.

- **Scheduling:** Users switch from manual to scheduled execution. Pick a weekday and time, and the agent runs via Cloud Scheduler. Book every Saturday tee time at exactly 9 PM on Tuesday – automatically.

### Technologies Used

| Component | Technology | Google Cloud Service |
|---|---|---|
| Vision AI | Gemini 2.5 Flash (multimodal) | Vertex AI / GenAI SDK |
| NL Skill Compiler | Gemini 3.1 Flash Lite | Vertex AI / GenAI SDK |
| Browser | Playwright (headless Chromium) | **Cloud Run** |
| Orchestration | Go Cloud Functions (2nd Gen) | **Cloud Functions** |
| Data Store | Firestore real-time | **Firestore** |
| Mobile App | Flutter (Dart) | **Firebase** |
| Scheduling | Cron triggers | **Cloud Scheduler** |
| Video Gen | Veo 3.1 (intro/outro) | **Vertex AI** |
| TTS | Neural2 voice | **Cloud Text-to-Speech** |

**Google Cloud Services used:** Cloud Run, Cloud Functions, Firestore, Cloud Scheduler, Vertex AI (Gemini + Veo), Cloud Text-to-Speech, Firebase Auth, Cloud Storage

### Challenges and Learnings

1. **Vision is a safety net, not a primary strategy.** Gemini Vision struggles when the same text appears in multiple places (e.g., a club name in both search input and result list). We learned that **direct DOM access** (Playwright CSS selectors, `has-text()`, `>>` text chaining) is far more reliable for known flows – with Vision as the fallback for unknown pages or changed layouts.

2. **Recorded selectors need curation.** CSS selectors captured during recording are often too specific (`div.pcco-country-wrap > div.pcco-club > a.pcco-club-select`) and break on minor DOM changes. We refined them to robust alternatives: `#container >> text={variable}` for scoped text matching, `a:has-text('Menu Item')` for navigation.

3. **Validation checkpoints are essential.** Adding `wait_for` + `screenshot` after every critical step (login, page transitions) catches failures immediately instead of letting the agent continue on the wrong page for 20+ steps.

4. **Timing matters.** A booking agent needs to act at the exact right moment. We integrated Cloud Scheduler to trigger runs precisely when reservation windows open – making this a truly autonomous system.

5. **Sandbox mode was essential.** During development, the agent accidentally booked real tee times. Sandbox mode (Gemini still plans every action, but form submissions are skipped) became crucial for safe iteration.

6. **The human touch.** Built by a 70-year-old developer with 50+ years of experience – from IBM mainframes to modern cloud-native architecture. This project proves that passion for building never gets old.

### What's next

- **Multi-portal support:** KB Segment Recording enables any golf booking portal to be onboarded in under 30 minutes – already in progress for additional systems
- **Self-healing selectors:** The KB Feedback Loop already captures which selectors fail per run. Next step: automatic selector correction – when a CSS selector fails repeatedly, the agent proposes a Vision-based or text-based alternative and updates the KB segment
- **Voice control:** "Hey GolfStatus, book me a tee time for Saturday" via Gemini Live API
- **Intelligent slot selection:** Let Gemini choose the optimal time based on weather forecasts and player availability
- **Vision-first mode:** As Gemini Vision improves, progressively shift from CSS-primary to Vision-primary execution – the 3-tier architecture is already designed for this transition

---

## 👨‍💻 Public Code Repository

**https://github.com/WTober/gemini-agent-challenge**

Includes spin-up instructions in the README.

---

## 📹 Demo Video

**https://youtu.be/7VSr2ahW3uk** (2:09 min)

---

## 🏗️ Architecture Diagram

Upload: `architecture.png` from the repo root.

---

## 🖥️ GCP Deployment Proof

Links to code files demonstrating Google Cloud deployment:

1. **Cloud Run – Browser Agent with Gemini Vision:**
   https://github.com/WTober/gemini-agent-challenge/blob/main/browser_agent/main.py
   → Playwright + Gemini `generateContent()` for screenshot analysis, deployed as Cloud Run service

2. **Cloud Run – Dockerfile:**
   https://github.com/WTober/gemini-agent-challenge/blob/main/browser_agent/Dockerfile
   → Container deployed to Cloud Run (europe-west3)

3. **Cloud Run – Deployment Script:**
   https://github.com/WTober/gemini-agent-challenge/blob/main/deploy/deploy_cloudrun.sh
   → `gcloud run deploy` to europe-west3 with Gemini model configuration

4. **Vertex AI / Veo 3.1 – Video Generation:**
   https://github.com/WTober/gemini-agent-challenge/blob/main/generate_videos.py
   → Uses Vertex AI Veo 3.1 API for intro/outro video generation

5. **Cloud Text-to-Speech – Voiceover:**
   https://github.com/WTober/gemini-agent-challenge/blob/main/generate_voiceover.py
   → Uses Cloud TTS Neural2-J API for English voiceover
