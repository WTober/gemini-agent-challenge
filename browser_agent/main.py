"""
Python 3.12 Cloud Function (2nd Gen): RunBrowserAgent
Receives an HTTP request from the Go RunUserAgent backend,
executes a browser automation sequence using Playwright + Gemini Vision,
and writes live step updates to Firestore agent_runs/{runId}.
"""
import base64
import json
import logging
import os
import time
import functions_framework
from flask import Request, jsonify
from google.cloud import firestore
from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout
from knowledge_base import get_kb_grounding_tool, extract_grounding_info

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("GCP_PROJECT", os.environ.get("PROJECT_ID", ""))
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
_GEMINI_LOCATION = os.environ.get("AI_LOCATION", "us-central1")

# Reasoning model (Gemini Pro) for self-healing KB analysis / failure diagnosis
REASONING_MODEL = os.environ.get("REASONING_MODEL", "gemini-2.5-pro")
_REASONING_LOCATION = os.environ.get("REASONING_LOCATION", "global")

_db: firestore.Client | None = None
_genai_client: genai.Client | None = None
_reasoning_client: genai.Client | None = None


def _firestore() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(project=PROJECT_ID)
    return _db


def _genai() -> genai.Client:
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(
            vertexai=True,
            project=PROJECT_ID,
            location=_GEMINI_LOCATION,
        )
    return _genai_client


def _reasoning_genai() -> genai.Client:
    """Gemini Pro client for reasoning tasks (self-healing, KB analysis)."""
    global _reasoning_client
    if _reasoning_client is None:
        _reasoning_client = genai.Client(
            vertexai=True,
            project=PROJECT_ID,
            location=_REASONING_LOCATION,
        )
    return _reasoning_client


# ─── Cloud Function Entry Point ────────────────────────────────────────────────

@functions_framework.http
def RunBrowserAgent(request: Request):
    """
    Expected JSON body:
    {
      "runId":        "...",
      "agentId":      "...",
      "userId":       "...",
      "targetUrl":    "https://...",
      "inputValues":  {"username": "...", "password": "...", ...},
      "actionSequence": [...],
      "successCondition": {"type": "visual_verification", "indicator": "..."}
    }
    """
    try:
        data = request.get_json(force=True) or {}
    except Exception as e:
        return jsonify({"error": f"Invalid JSON: {e}"}), 400

    run_id = data.get("runId", "")
    agent_id = data.get("agentId", "")
    user_id = data.get("userId", "")
    target_url = data.get("targetUrl", "")
    input_values: dict = data.get("inputValues", {})
    action_sequence: list = data.get("actionSequence", [])
    success_condition: dict = data.get("successCondition", {})
    precondition: str = data.get("precondition", "")
    dry_run: bool = bool(data.get("dryRun", False))

    # Accept dynamic model/location from Go backend (AppConfig or per-skill override)
    global GEMINI_MODEL, _GEMINI_LOCATION, _genai_client
    req_model = data.get("geminiModel", "")
    req_location = data.get("geminiLocation", "")
    if req_model:
        GEMINI_MODEL = req_model
    if req_location and req_location != _GEMINI_LOCATION:
        _GEMINI_LOCATION = req_location
        _genai_client = None  # Force re-creation with new location

    # Accept reasoning model/location from Go backend (AppConfig)
    global REASONING_MODEL, _REASONING_LOCATION, _reasoning_client
    req_reasoning = data.get("reasoningModel", "")
    req_reasoning_loc = data.get("reasoningLocation", "")
    if req_reasoning:
        REASONING_MODEL = req_reasoning
    if req_reasoning_loc and req_reasoning_loc != _REASONING_LOCATION:
        _REASONING_LOCATION = req_reasoning_loc
        _reasoning_client = None  # Force re-creation with new location

    if not run_id or not target_url:
        return jsonify({"error": "runId and targetUrl are required"}), 400

    log.info(f"[RunBrowserAgent] runId={run_id} url={target_url} steps={len(action_sequence)} model={GEMINI_MODEL} reasoning={REASONING_MODEL}")

    try:
        _execute_browser_run(
            run_id=run_id,
            agent_id=agent_id,
            user_id=user_id,
            target_url=target_url,
            input_values=input_values,
            action_sequence=action_sequence,
            success_condition=success_condition,
            precondition=precondition,
            dry_run=dry_run,
        )
        return jsonify({"status": "done"}), 200
    except Exception as e:
        log.exception("RunBrowserAgent failed")
        _write_step(run_id, "❌ Fehler", str(e), is_error=True)
        _set_run_status(run_id, "error", error_msg=str(e))
        return jsonify({"error": str(e)}), 500


# ─── Natural Language Date Resolution ────────────────────────────────────────

import re
import re as _re
from datetime import datetime as _datetime, timedelta as _timedelta

_DATE_PATTERN = _re.compile(r'^\d{1,2}\.\d{1,2}\.\d{4}$')
_DATE_KEYWORDS = [
    'montag', 'dienstag', 'mittwoch', 'donnerstag', 'freitag', 'samstag', 'sonntag',
    'morgen', 'übermorgen', 'nächst', 'übernächst', 'woche', 'tag', 'heute',
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
    'tomorrow', 'next', 'week', 'day', 'today',
]

# Map German/English day names to Python weekday numbers (0=Monday ... 6=Sunday)
_WEEKDAY_MAP = {
    'montag': 0, 'monday': 0,
    'dienstag': 1, 'tuesday': 1,
    'mittwoch': 2, 'wednesday': 2,
    'donnerstag': 3, 'thursday': 3,
    'freitag': 4, 'friday': 4,
    'samstag': 5, 'saturday': 5,
    'sonntag': 6, 'sunday': 6,
}


def _looks_like_natural_date(value: str) -> bool:
    """Check if a value looks like a natural language date expression."""
    v = value.strip()
    if not v:
        return False
    # Already DD.MM.YYYY — no resolution needed
    if _DATE_PATTERN.match(v):
        return False
    # "+N" shorthand for N days from now
    if _re.match(r'^\+\d+$', v):
        return True
    # Contains known date keywords
    vl = v.lower()
    return any(kw in vl for kw in _DATE_KEYWORDS)


def _resolve_date_locally(value: str) -> str | None:
    """
    Try to resolve common German/English date expressions using pure Python.
    Returns DD.MM.YYYY or None if not resolvable locally.
    """
    today = _datetime.now().date()
    v = value.strip().lower()

    # "+N" days from now
    m = _re.match(r'^\+(\d+)$', v)
    if m:
        target = today + _timedelta(days=int(m.group(1)))
        return target.strftime('%d.%m.%Y')

    # Simple keywords
    if v == 'heute' or v == 'today':
        return today.strftime('%d.%m.%Y')
    if v == 'morgen' or v == 'tomorrow':
        return (today + _timedelta(days=1)).strftime('%d.%m.%Y')
    if v == 'übermorgen':
        return (today + _timedelta(days=2)).strftime('%d.%m.%Y')

    # "nächsten Montag", "nächster Freitag", "next Monday", etc.
    for day_name, target_weekday in _WEEKDAY_MAP.items():
        if day_name in v:
            is_next = any(kw in v for kw in ('nächst', 'next', 'kommend'))
            is_over_next = any(kw in v for kw in ('übernächst',))
            days_ahead = (target_weekday - today.weekday()) % 7
            if days_ahead == 0:
                # Same weekday: "nächsten" means +7, plain name means today
                days_ahead = 7 if is_next or is_over_next else 0
            if is_over_next:
                days_ahead += 7
            elif is_next and days_ahead > 0:
                pass  # Already correct: next occurrence
            target = today + _timedelta(days=days_ahead)
            return target.strftime('%d.%m.%Y')

    return None


def _resolve_natural_dates(input_values: dict, run_id: str) -> None:
    """Resolve natural language date expressions to DD.MM.YYYY using local logic + Gemini fallback."""
    for key, value in list(input_values.items()):
        if not _looks_like_natural_date(value):
            continue

        # Step 1: Try local Python resolution (fast, no API call needed)
        local_result = _resolve_date_locally(value)
        if local_result and _DATE_PATTERN.match(local_result):
            _write_step(run_id, "📅 Datum aufgelöst",
                        f'"{value}" → {local_result}')
            input_values[key] = local_result
            continue

        # Step 2: Fallback to Gemini for complex expressions
        today = _datetime.now().strftime('%A, %d.%m.%Y')
        prompt = (
            f"Heute ist {today}.\n"
            f"Wandle den folgenden Ausdruck in ein konkretes Datum um.\n"
            f"Ausdruck: \"{value}\"\n\n"
            f"Antworte NUR mit dem Datum im Format TT.MM.JJJJ, nichts anderes."
        )
        try:
            response = _genai().models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config={"temperature": 0.0},
            )
            resolved = response.text.strip()
            # Verify the AI returned a valid DD.MM.YYYY
            if _DATE_PATTERN.match(resolved):
                _write_step(run_id, "📅 Datum aufgelöst",
                            f'"{value}" → {resolved}')
                input_values[key] = resolved
            else:
                log.warning(f"Date resolution returned unexpected format: {resolved}")
                _write_step(run_id, "⚠️ Datum nicht aufgelöst",
                            f'"{value}" → Gemini antwortete: "{resolved}" (ungültiges Format). Wert bleibt unverändert.')
        except Exception as e:
            log.warning(f"Date resolution failed for '{value}': {e}")
            _write_step(run_id, "⚠️ Datum-Auflösung fehlgeschlagen",
                        f'"{value}" konnte nicht aufgelöst werden: {str(e)[:100]}. Wert bleibt unverändert.')


# ─── Main Execution Engine ──────────────────────────────────────────────────────

def _execute_browser_run(
    run_id: str,
    agent_id: str,
    user_id: str,
    target_url: str,
    input_values: dict,
    action_sequence: list,
    success_condition: dict,
    precondition: str = "",
    dry_run: bool = False,
) -> None:
    _set_run_status(run_id, "running")

    # ── Sandbox banner ───────────────────────────────────────────────────
    if dry_run:
        _write_step(run_id, "🧪 SANDBOX-MODUS AKTIV",
                    "Keine echten Klicks oder Eingaben. Agent protokolliert nur was er tun würde.")

    # ── Precondition check (before opening browser) ──────────────────────
    if precondition.strip():
        skip_reason = _check_precondition(precondition, input_values, run_id)
        if skip_reason:
            _write_step(run_id, "⏭️ Vorbedingung nicht erfüllt", skip_reason)
            _set_run_status(run_id, "skipped", result=skip_reason)
            return

    # ── Resolve natural-language dates in input values ─────────────────────
    _resolve_natural_dates(input_values, run_id)

    # ── Derive short date for calendar dropdown (dd.mm.yyyy → dd.mm) ──
    # PCCaddie calendar dropdown shows dates as "dd.mm Wochentag" (e.g. "15.03 Sonntag")
    # We use dd.mm (without trailing dot) so regex /{datum_short}/ matches the list entry.
    if "datum" in input_values:
        parts = input_values["datum"].split(".")
        if len(parts) == 3:
            short = f"{parts[0]}.{parts[1]}"
            input_values["datum_short"] = short
            log.info(f"datum_short derived for calendar: {short}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
            ignore_https_errors=True,
            extra_http_headers={
                "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
        # Hide navigator.webdriver + pre-set CookieConsent + auto-dismiss cookie banners
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            // Pre-set CookieBot consent cookie so banner never appears
            document.cookie = 'CookieConsent={stamp:"-1",necessary:true,preferences:true,' +
                'statistics:true,marketing:true,ver:1,utc:' + Date.now() + ',region:"de"}; path=/';
            // Auto-dismiss any remaining cookie banners every 200ms
            const _dismissCookies = () => {
                const ids = [
                    'CybotCookiebotDialogBodyLevelButtonAccept',
                    'CybotCookiebotDialogBodyButtonAccept',
                    'CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
                    'onetrust-accept-btn-handler',
                ];
                for (const id of ids) {
                    const btn = document.getElementById(id);
                    if (btn) { btn.click(); return; }
                }
                if (window.Cookiebot) {
                    try { window.Cookiebot.acceptAllCookies(); } catch(e) {}
                }
            };
            setInterval(_dismissCookies, 200);
            document.addEventListener('DOMContentLoaded', _dismissCookies);
        """)
        page = context.new_page()

        try:
            # Step 0: Navigate to start URL
            _write_step(run_id, "🌐 Browser gestartet", f"Navigiere zu {target_url}")
            page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
            _write_step(run_id, "✅ Seite geladen", target_url,
                        screenshot=_screenshot_jpeg_b64(page))

            # Execute each step in the action sequence
            abort_reason = None
            step_results = []  # Track execution results for KB feedback
            for step_def in action_sequence:
                step_start = time.time()
                step_num = step_def.get("step", "?")
                step_action = step_def.get("action", "")
                try:
                    _execute_step(page, step_def, input_values, run_id, dry_run=dry_run)
                    step_results.append({
                        "step": step_num,
                        "action": step_action,
                        "status": "success",
                        "executionMs": int((time.time() - step_start) * 1000),
                        "description": step_def.get("description", "")[:80],
                        "target_description": step_def.get("target_description", "")[:80],
                    })
                except AbortStepError as e:
                    step_results.append({
                        "step": step_num,
                        "action": step_action,
                        "status": "aborted",
                        "reason": str(e)[:100],
                        "executionMs": int((time.time() - step_start) * 1000),
                    })
                    abort_reason = str(e)
                    break
                except Exception as e:
                    step_results.append({
                        "step": step_num,
                        "action": step_action,
                        "status": "error",
                        "error": str(e)[:200],
                        "executionMs": int((time.time() - step_start) * 1000),
                    })
                    raise

            # Final result
            if dry_run:
                # Check if steps had sandboxOff (real execution despite sandbox mode)
                # Exclude non-executable steps (comment, screenshot) from totals
                _non_exec = {"comment", "screenshot"}
                executable = [s for s in action_sequence if s.get("action") not in _non_exec]
                total = len(executable)
                real_count = sum(1 for s in executable if s.get("sandboxOff"))
                if real_count >= total and total > 0:
                    # ALL steps ran for real — do success verification if available
                    if success_condition.get("indicator", ""):
                        screenshot_b64 = _screenshot_b64(page)
                        indicator = success_condition["indicator"]
                        _write_step(run_id, "🔍 Prüfe Ergebnis", f'Suche: "{indicator}"',
                                    screenshot=_screenshot_jpeg_b64(page))
                        result = _verify_success(screenshot_b64, indicator)
                    else:
                        result = (f"✅ Alle {total} Schritte wurden echt ausgeführt "
                                  f"(Sandbox-Bypass). Automation abgeschlossen.")
                        _write_step(run_id, "🏁 Abgeschlossen", result,
                                    screenshot=_screenshot_jpeg_b64(page))
                elif real_count > 0:
                    result = (f"🧪 Sandbox abgeschlossen. {real_count} von {total} "
                              f"Schritten wurden echt ausgeführt (sandboxOff).")
                else:
                    result = "🧪 Sandbox abgeschlossen. Kein echter Buchungsvorgang durchgeführt."
            elif abort_reason:
                result = f"Abgebrochen: {abort_reason}"
            elif success_condition.get("indicator", ""):
                screenshot_b64 = _screenshot_b64(page)
                indicator = success_condition["indicator"]
                _write_step(run_id, "🔍 Prüfe Ergebnis", f'Suche: "{indicator}"',
                            screenshot=_screenshot_jpeg_b64(page))
                result = _verify_success(screenshot_b64, indicator)
            else:
                result = "Automation abgeschlossen."
                _write_step(run_id, "🏁 Abgeschlossen", result,
                            screenshot=_screenshot_jpeg_b64(page))

            _set_run_status(run_id, "done", result=result)

            # ── KB Feedback Loop ──────────────────────────────────────────
            # Only write feedback for REAL runs (not sandbox/dry_run).
            # This ensures only verified, successful execution data enters
            # the KB, creating a clean quality gate.
            if not dry_run:
                try:
                    _write_run_feedback(
                        run_id=run_id,
                        agent_id=agent_id,
                        target_url=target_url,
                        action_sequence=action_sequence,
                        step_results=step_results,
                        final_status="done",
                        result=result,
                    )
                except Exception as fb_err:
                    log.warning(f"KB feedback write failed (non-critical): {fb_err}")

        except Exception as e:
            # Capture failure context for self-healing analysis
            log.info(f"[Self-Healing] Entering error handler for run {run_id}, error: {str(e)[:200]}")
            fail_screenshot = ""
            dom_snippet = ""
            try:
                fail_screenshot = _screenshot_jpeg_b64(page)
                dom_snippet = page.evaluate("document.body.innerText.substring(0, 3000)")
                log.info(f"[Self-Healing] Captured screenshot ({len(fail_screenshot)} chars) and DOM ({len(dom_snippet)} chars)")
            except Exception as cap_err:
                log.warning(f"[Self-Healing] Screenshot/DOM capture failed: {cap_err}")

            # Trigger self-healing analysis with Gemini Pro
            # NOTE: Runs in ALL modes (incl. sandbox) because UI-change failures
            # (e.g. selector not found) are genuine and should be analyzed.
            try:
                # Find the actual failed step: last entry in step_results with status "error"
                failed_step_result = next(
                    (r for r in reversed(step_results) if r.get("status") == "error"),
                    None,
                )
                if failed_step_result:
                    # Match by step number from step_results back to action_sequence
                    failed_step_num = failed_step_result.get("step", "?")
                    failed_step_def = next(
                        (s for s in action_sequence if s.get("step") == failed_step_num),
                        {},
                    )
                else:
                    # Fallback: use index
                    failed_idx = len(step_results)
                    failed_step_def = action_sequence[failed_idx] if failed_idx < len(action_sequence) else {}
                log.info(f"[Self-Healing] Calling analysis: model={REASONING_MODEL}, location={_REASONING_LOCATION}, failedStep={failed_step_def.get('step', '?')} action={failed_step_def.get('action', '?')}")
                _analyze_failure_and_suggest_fix(
                    run_id=run_id,
                    agent_id=agent_id,
                    target_url=target_url,
                    failed_step=failed_step_def,
                    error_msg=str(e)[:500],
                    page_screenshot_b64=fail_screenshot,
                    dom_snippet=dom_snippet,
                )
                log.info(f"[Self-Healing] Analysis completed successfully for run {run_id}")
            except Exception as heal_err:
                log.exception(f"[Self-Healing] Analysis trigger failed: {heal_err}")
            raise
        finally:
            browser.close()


class AbortStepError(Exception):
    """Raised by abort_if action to stop the sequence gracefully."""


def _execute_step(page: Page, step_def: dict, input_values: dict, run_id: str, dry_run: bool = False) -> None:
    """Execute a single action sequence step using Gemini Vision for element location."""
    DRY = dry_run  # shorthand
    step_num = step_def.get("step", "?")
    action = step_def.get("action", "")
    
    # Skip comment-only steps (preserved from skill editor)
    if action == "comment":
        return
    
    _fs_description = step_def.get("description", "")
    _fs_target_description = step_def.get("target_description", "")
    # Prefer 'description' when it looks like a CSS selector (starts with # or .),
    # otherwise use human-readable 'target_description' for Vision fallback.
    if _fs_description and _is_css_selector(_fs_description):
        target_desc = _fs_description
    else:
        target_desc = _fs_target_description or _fs_description
    # visual_anchor = human-readable text for Vision (always prefer target_description)
    visual_anchor = step_def.get("visual_anchor") or _fs_target_description or target_desc
    raw_value = step_def.get("value", "")
    # Per-step sandbox bypass: step runs for real even if global dry_run is active
    sandbox_off = bool(step_def.get("sandboxOff", False))
    if DRY and sandbox_off:
        DRY = False
        _write_step(run_id, f"⚡ Sandbox-Bypass: Schritt {step_num} wird echt ausgeführt ({action})", target_desc)

    # Resolve template placeholders: {username} → actual value
    value = raw_value
    for key, val in input_values.items():
        value = value.replace(f"{{{key}}}", val)
        target_desc = target_desc.replace(f"{{{key}}}", val)
        visual_anchor = visual_anchor.replace(f"{{{key}}}", val)

    # Auto-skip steps where a required value resolved to empty
    # (e.g. optional player2/player3 inputs that user left blank)
    if action in ("input_text", "select") and raw_value.strip().startswith('{') and raw_value.strip().endswith('}') and not value.strip():
        _write_step(run_id, f"⏭️ Schritt übersprungen (leer)", f"{target_desc} – Platzhalter nicht gesetzt")
        return

    # Resolve relative date expressions in the final value
    # e.g. "nächsten Montag", "+7", "morgen" → "24.02.2026"
    if action in ("input_text", "select") and value and not _looks_like_date(value):
        resolved = _resolve_date_expression_if_needed(value, run_id)
        if resolved:
            value = resolved

    _write_step(run_id, f"▶ Schritt {step_num}: {action}", target_desc)

    # Small wait to let the page settle after prior action
    page.wait_for_timeout(800)

    if action == "click":
        if DRY:
            shot = _screenshot_b64(page)
            coords = _gemini_locate(shot, visual_anchor, run_id)
            _write_step(run_id, f"🧪 Würde klicken: {visual_anchor}",
                        f"Position: ({coords['x']},{coords['y']})" if coords else "Element nicht eindeutig lokalisiert")
        else:
            _vision_click(page, visual_anchor, run_id)
    elif action == "input_text":
        if DRY:
            shot = _screenshot_b64(page)
            coords = _gemini_locate(shot, visual_anchor, run_id)
            _write_step(run_id, f"🧪 Würde eingeben in: {visual_anchor}",
                        f"Wert: \"{value}\" @ ({coords['x']},{coords['y']})" if coords else f"Wert: \"{value}\"")
        else:
            _vision_fill(page, visual_anchor, value, run_id)
    elif action == "select":
        if DRY:
            _write_step(run_id, f"🧪 Würde auswählen: {visual_anchor}", f"Option: \"{value}\"")
        else:
            _vision_select(page, visual_anchor, value, run_id)
    elif action == "press":
        key = value if value else raw_value
        if DRY:
            _write_step(run_id, f"🧪 Würde Taste drücken", key)
        else:
            page.keyboard.press(key)
            _write_step(run_id, "↩ Taste gedrückt", key)
    elif action == "scroll":
        try:
            px = int(value) if str(value).lstrip('-').isdigit() else 300
        except (ValueError, AttributeError):
            px = 300
        if DRY:
            _write_step(run_id, f"🧪 Würde scrollen", f"{px}px")
        else:
            page.mouse.wheel(0, px)
            _write_step(run_id, "📜 Gescrollt", f"{px}px")
    elif action == "hover":
        if DRY:
            _write_step(run_id, f"🧪 Würde hovern über", visual_anchor)
        else:
            _vision_hover(page, visual_anchor, run_id)
    elif action == "checkbox":
        if DRY:
            _write_step(run_id, f"🧪 Würde Checkbox umschalten", visual_anchor)
        else:
            _vision_checkbox(page, visual_anchor, run_id)
    elif action == "wait_for":
        if DRY:
            _write_step(run_id, f"🧪 Würde warten auf", visual_anchor)
        else:
            _write_step(run_id, "⏳ Warte auf", visual_anchor)
            try:
                page.get_by_text(visual_anchor, exact=False).first.wait_for(
                    state="visible", timeout=15_000
                )
                _write_step(run_id, "✅ Element erschienen", visual_anchor)
            except Exception:
                for _ in range(7):
                    page.wait_for_timeout(2_000)
                    shot = _screenshot_b64(page)
                    coords = _gemini_locate(shot, visual_anchor, run_id)
                    if coords:
                        _write_step(run_id, "✅ Element erkannt", visual_anchor)
                        break
                else:
                    raise RuntimeError(f"Element nicht erschienen: '{visual_anchor}'")
    elif action == "abort_if":
        # Take screenshot and ask Gemini if abort condition is met
        _write_step(run_id, "🔍 Prüfe Abbruchbedingung", visual_anchor)
        shot = _screenshot_b64(page)
        prompt = (
            f'Schau auf den Screenshot. Ist folgende Bedingung erfüllt: "{visual_anchor}"?\n'
            f'Antworte NUR mit JA oder NEIN.'
        )
        try:
            resp = _genai().models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    types.Part.from_bytes(data=base64.b64decode(shot), mime_type="image/png"),
                    types.Part.from_text(text=prompt),
                ],
            )
            if resp.text.strip().upper().startswith("JA"):
                _write_step(run_id, "⏭️ Abbruch", visual_anchor)
                raise AbortStepError(visual_anchor)
            else:
                _write_step(run_id, "✅ Bedingung nicht erfüllt, weiter", visual_anchor)
        except AbortStepError:
            raise
        except Exception as e:
            log.warning(f"abort_if vision check failed: {e}")
    elif action == "js":
        # Execute arbitrary JavaScript — substitute {placeholders} from input_values
        js_code = target_desc or raw_value
        for k, v in input_values.items():
            js_code = js_code.replace(f"{{{k}}}", v)
        if DRY:
            log_code = js_code
            for k, v in input_values.items():
                _SENS = ("password", "passwort", "kennwort", "secret", "pin", "token")
                if any(s in k.lower() for s in _SENS):
                    log_code = log_code.replace(v, "***")
            _write_step(run_id, f"🧪 Würde JS ausführen", log_code[:80])
        else:
            try:
                result = page.evaluate(js_code)
                log.info(f"JS result: {result}")
                _write_step(run_id, "⚙️ JS ausgeführt", f"{(target_desc or raw_value)[:60]} → {result}")
            except Exception as e:
                _write_step(run_id, "⚠️ JS Fehler", str(e)[:80])
    elif action == "playwright":
        # Execute raw Playwright API call (from codegen output) with {placeholder} substitution.
        # Example: page.get_by_role("button", name="Login").click()
        pw_code = target_desc or raw_value
        # Strip any accidental playwright:/!playwright: prefix
        for _pfx in ('!playwright:', 'playwright:'):
            if pw_code.lstrip().startswith(_pfx):
                pw_code = pw_code.lstrip()[len(_pfx):].lstrip()
                break
        for k, v in input_values.items():
            pw_code = pw_code.replace(f"{{{k}}}", v)
        # Normalize smart/typographic quotes from mobile keyboards → standard ASCII
        pw_code = (pw_code
            .replace('\u201c', '"').replace('\u201d', '"')   # "double curly"
            .replace('\u2018', "'").replace('\u2019', "'")   # 'single curly'
            .replace('\u201e', '"').replace('\u201a', "'"))  # German „low"
        # Auto-convert German date format DD.MM.YYYY → YYYY-MM-DD (for DAY| PcCaddie selectors)
        import re as _re
        def _de_to_iso(m):
            d, mo, y = m.group(1), m.group(2), m.group(3)
            return f"DAY|{y}-{mo}-{d}"
        pw_code = _re.sub(r'DAY\|(\d{2})\.(\d{2})\.(\d{4})', _de_to_iso, pw_code)
        # Build safe log (mask sensitive values)
        log_code = pw_code
        for k, v in input_values.items():
            _SENS = ("password", "passwort", "kennwort", "secret", "pin", "token")
            if any(s in k.lower() for s in _SENS):
                log_code = log_code.replace(v, "***")
        if DRY:
            _write_step(run_id, f"🧪 Würde Playwright ausführen", log_code[:120])
        else:
            try:
                exec(pw_code, {"page": page, "expect": None})  # noqa: S102
                _write_step(run_id, "🎭 Playwright", log_code[:120])
            except Exception as e:
                # Smart fallback: if select_option failed, retry with partial matching
                if "select_option" in pw_code and "select_option" in str(e):
                    log.info(f"playwright select_option failed, trying smart fallback: {e}")
                    _write_step(run_id, "🔄 Retry mit Smart Select", str(e)[:80])
                    try:
                        _playwright_smart_select(page, pw_code, run_id)
                        _write_step(run_id, "🎭 Playwright (Smart Select)", log_code[:120])
                    except Exception as e2:
                        raise RuntimeError(f"Playwright-Fehler (auch Smart Select): {e2}")
                else:
                    raise RuntimeError(f"Playwright-Fehler: {e}")
    elif action == "screenshot":
        # Explicit debug screenshot — attach to run log with optional label
        label = target_desc or "Screenshot"
        _write_step(run_id, f"📷 {label}", "", screenshot=_screenshot_jpeg_b64(page))
    elif action == "wait":
        raw_num = int(raw_value) if str(raw_value).strip().lstrip('-').isdigit() else 2
        # Treat values ≤ 60 as seconds, larger values as milliseconds
        wait_ms = raw_num * 1000 if raw_num <= 60 else raw_num
        page.wait_for_timeout(wait_ms)
        _write_step(run_id, "⏳ Warten", f"{wait_ms / 1000:.1f}s")
    elif action == "navigate":
        if DRY:
            _write_step(run_id, f"🧪 Würde navigieren zu", value)
        else:
            page.goto(value, wait_until="domcontentloaded", timeout=30_000)
            _write_step(run_id, "🌐 Navigiert", value)
    elif action == "find_slot":
        # Smart slot selection: finds the nearest available bookable time slot
        # Syntax: find_slot: {target_time} -> {max_time}
        # Uses data-time and data-status HTML attributes on <tr> elements.
        param = (target_desc or raw_value or "").strip()
        for k, v in input_values.items():
            param = param.replace(f"{{{k}}}", v)
        parts = [p.strip() for p in param.split("->")]
        slot_target = parts[0] if len(parts) >= 1 else "08:00"
        slot_max = parts[1] if len(parts) >= 2 else "18:00"
        if DRY:
            _write_step(run_id, "🧪 Würde Slot suchen", f"{slot_target} bis {slot_max}")
        else:
            _find_and_click_slot(page, slot_target, slot_max, run_id)
    elif action == "find_click":
        # Smart visual search: finds FIRST available/matching element (e.g. first free tee slot)
        _vision_find_click(page, visual_anchor, run_id, dry_run=DRY)
    elif action == "smart_select":
        # Smart text-based search: extracts page text via JS, sends to Gemini as TEXT,
        # gets back a click target, then clicks via Playwright text locator.
        # Much more reliable than vision-based pixel coordinate approach.
        _smart_select(page, visual_anchor, run_id, dry_run=DRY)
    elif action == "try_click":
        # Like find_click but silently continues if element is not found (optional elements, e.g. cookie banners)
        if DRY:
            _write_step(run_id, f"🧪 Würde optional klicken", visual_anchor)
        else:
            try:
                _vision_find_click(page, visual_anchor, run_id, dry_run=False)
            except Exception as e:
                _write_step(run_id, f"⏭️ Optionales Element nicht gefunden, weiter", visual_anchor)
                log.info(f"try_click: '{visual_anchor}' not found, continuing. ({e})")
    elif action == "if_input":
        # Condition syntax: "{variable} == value" or "{variable} != value"
        _check_if_input(target_desc, input_values, run_id)
    elif action == "autonomous":
        # Autonomous loop: Gemini analyzes page text and decides next action iteratively
        # Use !autonomous: to bypass sandbox (admin decides via ! prefix)
        goal = target_desc
        max_steps = int(value) if value and str(value).strip().isdigit() else 15
        _autonomous_loop(page, goal, input_values, run_id, max_steps=max_steps, dry_run=DRY)
    else:
        log.warning(f"Unknown action: {action}")


# ─── Gemini Vision Helpers ──────────────────────────────────────────────────────

def _screenshot_b64(page: Page) -> str:
    """Take a screenshot and return as base64."""
    raw = page.screenshot(type="png")
    return base64.b64encode(raw).decode()


def _gemini_locate(screenshot_b64: str, description: str, run_id: str = "") -> dict | None:
    """
    Ask Gemini Vision: where is the element described by `description`?
    Returns {"x": int, "y": int} or None.
    Uses KB grounding to leverage known selectors from recorded sessions.
    """
    prompt = (
        f"You are a browser automation assistant. Look at this screenshot and find the element: "
        f'"{description}". '
        f"If you have knowledge base information about this website's selectors, use it to improve accuracy. "
        f"Return ONLY a JSON object with the EXACT pixel coordinates at its center: "
        f'{{\"x\": <int>, \"y\": <int>}}. '
        f"Do NOT include any other text, explanation, or markdown. "
        f"If the element is not visible, return {{\"x\": -1, \"y\": -1}}."
    )

    client = _genai()
    try:
        kb_tool = get_kb_grounding_tool()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(
                    data=base64.b64decode(screenshot_b64),
                    mime_type="image/png",
                ),
                types.Part.from_text(text=prompt),
            ],
            config=types.GenerateContentConfig(tools=[kb_tool]),
        )
    except Exception as e:
        log.warning(f"Gemini locate with KB failed, retrying without: {e}")
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(
                    data=base64.b64decode(screenshot_b64),
                    mime_type="image/png",
                ),
                types.Part.from_text(text=prompt),
            ],
        )

    # Log KB grounding usage for admin transparency
    kb_info = extract_grounding_info(response)
    if kb_info and run_id:
        _write_step(run_id, kb_info, f"Element: {description}")

    try:
        raw = response.text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        coords = json.loads(raw.strip())
        if coords.get("x", -1) == -1:
            return None
        return coords
    except Exception as e:
        log.warning(f"Gemini locate parse error: {e} | raw: {response.text[:200]}")
        return None


# Known iFrames on target sites – selector prefix → iframe CSS selector
# NOTE: PCCaddie uses divs (not iframes), so #pcco-frame entries are NOT listed here.
# All #pcco-frame selectors work as regular page.locator() CSS selectors.
_IFRAME_PREFIXES: list[tuple[str, str]] = [
    # Add real iframe prefixes here if needed for other sites
    # ("#some-real-iframe ", "#some-real-iframe"),
]


def _resolve_locator(page: Page, selector: str):
    """Return a Playwright Locator for selector, crossing into iFrames if needed.

    If selector starts with a known iFrame prefix (e.g. '#pcco-frame #password'),
    the part after the first token is looked up inside the iFrame via frame_locator.
    For compound selectors like '#pcco-timetable-selection #timetable_selection_date',
    we route the whole selector into the known parent iFrame.
    Falls back to page.locator() for normal selectors.
    """
    for prefix, iframe_sel in _IFRAME_PREFIXES:
        if selector.startswith(prefix):
            # Strip the iFrame anchor from the selector
            inner = selector[len(prefix):].strip()
            if not inner:
                inner = selector
            try:
                frame = page.frame_locator(iframe_sel)
                return frame.locator(inner)
            except Exception:
                pass  # fall through to normal locator
        elif selector.startswith(prefix.rstrip()):
            # e.g. '#pcco-frame' alone
            pass
    # Default: check if any known iframe ID appears as first token
    # e.g. '#pcco-timetable-selection #timetable_selection_date'
    for _, iframe_sel in _IFRAME_PREFIXES:
        if iframe_sel in selector or selector.startswith(iframe_sel):
            inner = selector[len(iframe_sel):].strip()
            if inner:
                try:
                    return page.frame_locator(iframe_sel).locator(inner)
                except Exception:
                    pass
    return page.locator(selector)


def _resolve_wait(page: Page, selector: str, timeout: int = 15000):
    """Wait for selector, crossing iFrame boundaries if needed."""
    for prefix, iframe_sel in _IFRAME_PREFIXES:
        if selector.startswith(prefix):
            inner = selector[len(prefix):].strip() or selector
            try:
                page.frame_locator(iframe_sel).locator(inner).wait_for(timeout=timeout)
                return
            except Exception:
                pass
        elif iframe_sel in selector:
            inner = selector[len(iframe_sel):].strip()
            if inner:
                try:
                    page.frame_locator(iframe_sel).locator(inner).wait_for(timeout=timeout)
                    return
                except Exception:
                    pass
    # Default: use page.locator().wait_for() — more lenient CSS parsing than wait_for_selector
    # (wait_for_selector rejects attribute selectors with single quotes like a[href*='...'])
    try:
        page.locator(selector).first.wait_for(state="visible", timeout=timeout)
    except Exception:
        pass  # Element may not be visible but still interactable


def _vision_click(page: Page, description: str, run_id: str) -> None:
    """Click an element using a multi-tier fallback strategy:
    1. Try Playwright CSS selector directly (if description looks like one)
    2. Try Gemini Vision with a human-readable description
    3. Try text-based Playwright search as last resort
    """
    # --- Tier 1: Direct Playwright CSS selector (iFrame-aware) ---
    if _is_css_selector(description):
        try:
            _resolve_wait(page, description, timeout=15000)
            loc = _resolve_locator(page, description)
            if loc.count() > 0:
                loc.first.click(timeout=3000)
                _write_step(run_id, f"👆 Klick (CSS)", description)
                page.wait_for_timeout(1000)
                return
        except Exception as e:
            log.info(f"_vision_click: CSS selector '{description}' failed: {e}")
            _write_step(run_id, "⚠️ CSS-Selector fehlgeschlagen, versuche Vision", description)

    # --- Tier 1b: Direct Playwright text click (for non-CSS descriptions) ---
    if not _is_css_selector(description):
        try:
            loc = page.get_by_text(description, exact=False).first
            loc.wait_for(state="visible", timeout=5000)
            loc.click(timeout=3000)
            _write_step(run_id, f"👆 Klick (Text)", description)
            page.wait_for_timeout(1000)
            return
        except Exception as e:
            log.info(f"_vision_click: Text click '{description}' failed: {e}")
            _write_step(run_id, "⚠️ Text-Klick fehlgeschlagen, versuche Vision", description)

    # --- Tier 2: Gemini Vision locate ---
    vision_desc = _css_to_readable(description) if _is_css_selector(description) else description
    screenshot_b64 = _screenshot_b64(page)
    coords = _gemini_locate(screenshot_b64, vision_desc, run_id)

    if coords:
        x, y = coords["x"], coords["y"]
        _write_step(run_id, f"👆 Klick via Vision ({x},{y})", vision_desc)
        page.mouse.click(x, y)
        page.wait_for_timeout(1000)
        return

    # --- Tier 3: Fallback text search ---
    _write_step(run_id, "⚠️ Position nicht erkannt, versuche Text-Suche", description)
    # Try has-text content extraction
    has_text_match = re.search(r"has-text\(['\"]([^'\"]+)", description)
    text_to_find = has_text_match.group(1) if has_text_match else description
    try:
        page.get_by_text(text_to_find, exact=False).first.click(timeout=5000)
        return
    except Exception:
        raise RuntimeError(
            f"Element nicht gefunden: '{description}'. Kein Klick möglich."
        )

def _find_and_click_slot(page: Page, target_time: str, max_time: str, run_id: str) -> None:
    """
    Smart slot selection: finds the nearest available bookable time slot.
    Uses <tr data-time="HH:MM" data-status="bookable"> attributes from PC Caddie timetables.
    Picks the first visible slot with time >= target_time and <= max_time.
    Uses Playwright's real mouse click (not JS element.click()).
    """
    t_h, t_m = (int(x) for x in target_time.split(":"))
    target_min = t_h * 60 + t_m
    m_h, m_m = (int(x) for x in max_time.split(":"))
    max_min = m_h * 60 + m_m

    # Get all visible, bookable time slots via their data attributes
    times = page.evaluate('''() => {
        const rows = document.querySelectorAll('tr[data-time][data-status="bookable"]');
        const visible = [];
        rows.forEach(r => {
            if (r.style.display !== 'none' && r.offsetHeight > 0) {
                visible.push(r.getAttribute('data-time'));
            }
        });
        return visible;
    }''')

    log.info(f"find_slot: visible bookable times = {times}")

    # Find nearest time >= target, <= max
    best = None
    best_diff = 999
    for t in times:
        h, m = (int(x) for x in t.split(":"))
        mins = h * 60 + m
        if mins < target_min:
            continue
        if mins > max_min:
            continue
        diff = mins - target_min
        if diff < best_diff:
            best_diff = diff
            best = t

    if not best:
        _write_step(run_id, "❌ Kein passender Slot", f"Keine Zeit zwischen {target_time} und {max_time}. Sichtbar: {times}")
        raise RuntimeError(f"Kein verfügbarer Slot zwischen {target_time} und {max_time}. Sichtbare Zeiten: {times}")

    log.info(f"find_slot: selected {best} (target={target_time}, max={max_time})")
    _write_step(run_id, "🎯 Slot gefunden", f"{best} (Ziel: {target_time}, Max: {max_time})")

    # Click with Playwright's real mouse click via data-time selector
    page.locator(f'tr[data-time="{best}"][data-status="bookable"]').first.click()


def _vision_find_click(page: Page, description: str, run_id: str, dry_run: bool = False) -> None:
    """
    Smart visual search: finds the FIRST available/matching element and clicks it.
    Unlike _vision_click, this explicitly instructs Gemini to find the best match
    even if it's not an exact text hit – ideal for finding first free tee slots,
    first available button in a grid, etc.
    """
    screenshot_b64 = _screenshot_b64(page)
    # Attach the screenshot Gemini will analyze so admin can see what it sees
    _write_step(run_id, f"🔎 Suche: {description}", "Analysiere Seite...",
                screenshot=_screenshot_jpeg_b64(page))

    prompt = (
        f"Schau auf den Screenshot und finde das Element, das am besten zu dieser Beschreibung passt:\n"
        f"\"{description}\"\n\n"
        f"Wichtige Hinweise:\n"
        f"- Suche das ERSTE passende/verfügbare Element (nicht unbedingt exakter Text-Match)\n"
        f"- Ignoriere ausgegraut/deaktivierte Elemente\n"
        f"- Bei Zeitslots: nimm den ersten Slot der Bedingung erfüllt, z.B. 'ab 11:00' = erster freier Slot >= 11:00\n\n"
        f"Gib NUR JSON zurück: {{\"x\": <pixel_x>, \"y\": <pixel_y>}} oder {{\"found\": false}} wenn nichts passt."
    )
    try:
        kb_tool = get_kb_grounding_tool()
        response = _genai().models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=base64.b64decode(screenshot_b64), mime_type="image/png"),
                types.Part.from_text(text=prompt),
            ],
            config=types.GenerateContentConfig(tools=[kb_tool]),
        )
    except Exception as e:
        log.warning(f"find_click with KB failed, retrying without: {e}")
        response = _genai().models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=base64.b64decode(screenshot_b64), mime_type="image/png"),
                types.Part.from_text(text=prompt),
            ],
        )
    # Log KB grounding for admin transparency
    kb_info = extract_grounding_info(response)
    if kb_info:
        _write_step(run_id, kb_info, f"find_click: {description}")

    try:
        import json, re as _re
        raw = response.text.strip()
        log.info(f"find_click Gemini raw response: {raw}")
        _write_step(run_id, f"🤖 Gemini Antwort", raw[:200])
        m = _re.search(r'\{[^}]+\}', raw)
        if m:
            data = json.loads(m.group())
            if data.get("found") is False:
                raise RuntimeError(f"Kein passendes Element gefunden: '{description}'")
            x, y = int(data["x"]), int(data["y"])
            if dry_run:
                _write_step(run_id, f"🧪 Würde klicken: {description}",
                            f"Gefundene Position: ({x},{y})")
            else:
                _write_step(run_id, f"🎯 Element gefunden und geklickt ({x},{y})", description)
                page.mouse.click(x, y)
                page.wait_for_timeout(1200)
        else:
            raise RuntimeError(f"Gemini konnte kein Element lokalisieren für: '{description}'")
    except RuntimeError:
        raise
    except Exception as e:
        log.warning(f"find_click failed: {e}")
        raise RuntimeError(f"find_click fehlgeschlagen: {e}")


def _smart_select(page: Page, description: str, run_id: str, dry_run: bool = False) -> None:
    """
    Smart text-based search: extracts visible page content as text via JavaScript,
    sends it to Gemini as TEXT (not image), gets back a clickable text label,
    then clicks via Playwright's text locator.
    
    Much more reliable than vision-based pixel coordinate approach because:
    - Gemini excels at text/table analysis
    - Playwright text locators are precise
    - No pixel coordinate guessing needed
    """
    _write_step(run_id, f"🧠 Smart Select: {description}", "Extrahiere Seiteninhalt...")
    
    # Step 1: Extract visible page content as structured text via JavaScript
    page_text = page.evaluate("""() => {
        // Extract all visible tables
        const tables = document.querySelectorAll('table');
        let result = '';
        tables.forEach((table, idx) => {
            result += '=== Tabelle ' + (idx+1) + ' ===\\n';
            const rows = table.querySelectorAll('tr');
            rows.forEach(row => {
                const cells = row.querySelectorAll('th, td');
                const texts = [];
                cells.forEach(cell => {
                    const t = cell.innerText.trim();
                    texts.push(t || '(leer)');
                });
                result += texts.join(' | ') + '\\n';
            });
            result += '\\n';
        });
        // If no tables found, extract visible text content
        if (!result.trim()) {
            result = document.body.innerText.substring(0, 3000);
        }
        return result.substring(0, 4000);
    }""")
    
    log.info(f"smart_select page text ({len(page_text)} chars): {page_text[:200]}")
    _write_step(run_id, "📋 Seiteninhalt extrahiert", f"{len(page_text)} Zeichen, sende an Gemini...")
    
    # Step 2: Ask Gemini to analyze the text and find the right element
    prompt = (
        f"Analysiere den folgenden Seiteninhalt und finde das Element das am besten passt:\n\n"
        f'Aufgabe: "{description}"\n\n'
        f"Seiteninhalt:\n{page_text}\n\n"
        f"Antworte NUR mit einem JSON-Objekt:\n"
        f'- Wenn gefunden: {{"click_text": "<exakter sichtbarer Text zum Anklicken>"}}\n'
        f'- Wenn nicht gefunden: {{"found": false, "reason": "<kurze Begründung>"}}\n\n'
        f"WICHTIG: click_text muss EXAKT dem Text entsprechen, wie er auf der Seite steht."
    )
    
    try:
        kb_tool = get_kb_grounding_tool()
        response = _genai().models.generate_content(
            model=GEMINI_MODEL,
            contents=[types.Part.from_text(text=prompt)],
            config=types.GenerateContentConfig(tools=[kb_tool]),
        )
    except Exception as e:
        log.warning(f"smart_select with KB failed, retrying without: {e}")
        response = _genai().models.generate_content(
            model=GEMINI_MODEL,
            contents=[types.Part.from_text(text=prompt)],
        )
    # Log KB grounding for admin transparency
    kb_info = extract_grounding_info(response)
    if kb_info:
        _write_step(run_id, kb_info, f"smart_select: {description}")

    try:
        import json, re as _re
        raw = response.text.strip()
        log.info(f"smart_select Gemini response: {raw}")
        _write_step(run_id, "🤖 Gemini Antwort", raw[:200])
        
        m = _re.search(r'\{[^}]+\}', raw)
        if not m:
            raise RuntimeError(f"Gemini lieferte kein JSON: {raw[:100]}")
        
        data = json.loads(m.group())
        
        if data.get("found") is False:
            reason = data.get("reason", "unbekannt")
            raise RuntimeError(f"Smart Select: nicht gefunden – {reason}")
        
        click_text = data.get("click_text", "")
        if not click_text:
            raise RuntimeError(f"Gemini lieferte keinen click_text: {raw[:100]}")
        
        if dry_run:
            _write_step(run_id, f"🧪 Würde klicken auf: '{click_text}'", description)
        else:
            _write_step(run_id, f"🎯 Klicke auf: '{click_text}'", description)
            # Try exact text match first, then partial
            try:
                page.get_by_text(click_text, exact=True).first.click(timeout=5000)
            except Exception:
                try:
                    page.get_by_text(click_text, exact=False).first.click(timeout=5000)
                except Exception:
                    # Last resort: try as cell in a table
                    page.get_by_role("cell", name=click_text).first.click(timeout=5000)
            page.wait_for_timeout(1200)
            _write_step(run_id, "✅ Element angeklickt", click_text,
                        screenshot=_screenshot_jpeg_b64(page))
    except RuntimeError:
        raise
    except Exception as e:
        log.warning(f"smart_select failed: {e}")
        raise RuntimeError(f"Smart Select fehlgeschlagen: {e}")


def _check_if_input(condition: str, input_values: dict, run_id: str) -> None:
    """
    Evaluates a simple condition against input_values.
    Syntax: "{variable} == value"  or  "{variable} != value"
    If condition is NOT met, raises AbortStepError to skip remaining steps.
    """
    import re as _re
    # Resolve placeholders first
    resolved = condition
    for key, val in input_values.items():
        resolved = resolved.replace(f"{{{key}}}", val)

    # Parse: left_side operator right_side
    m = _re.match(r'^(.+?)\s*(==|!=|>=|<=|>|<)\s*(.+)$', resolved.strip())
    if not m:
        _write_step(run_id, f"⚠️ if_input: Ungültige Bedingung", condition)
        return  # Don't abort on parse error

    left, op, right = m.group(1).strip(), m.group(2), m.group(3).strip()
    met = False
    try:
        l, r = float(left), float(right)
        met = eval(f"{l} {op} {r}")
    except ValueError:
        if op == "==":  met = left.lower() == right.lower()
        elif op == "!=": met = left.lower() != right.lower()

    if met:
        _write_step(run_id, f"✅ Bedingung erfüllt, weiter", f"{resolved}")
    else:
        _write_step(run_id, f"⏭️ Bedingung nicht erfüllt, überspringe Rest", f"{resolved}")
        raise AbortStepError(f"if_input: {resolved}")


# ─── Autonomous Loop ─────────────────────────────────────────────────────────

def _autonomous_loop(
    page: Page,
    goal: str,
    input_values: dict,
    run_id: str,
    max_steps: int = 15,
    dry_run: bool = False,
) -> None:
    """
    Autonomous agent loop: Gemini analyzes page content as TEXT (not vision)
    and decides the next browser action. Repeats until goal is achieved or
    max_steps is reached.

    Much more reliable than vision-based pixel coordinates because:
    - Gemini excels at text/table analysis
    - Playwright text locators are precise
    - No pixel coordinate guessing needed
    """
    # Resolve {placeholders} in the goal text
    resolved_goal = goal
    for key, val in input_values.items():
        resolved_goal = resolved_goal.replace(f"{{{key}}}", val)

    # Build safe display of inputs (mask passwords for logs)
    _SENSITIVE_KEYS = ("password", "passwort", "kennwort", "secret", "pin", "token")
    safe_inputs_display = ", ".join(
        f"{k}=***" if any(s in k.lower() for s in _SENSITIVE_KEYS) else f"{k}={v}"
        for k, v in input_values.items()
    ) if input_values else "(keine)"

    _write_step(run_id, "🤖 Autonomer Modus gestartet",
                f"Ziel: {resolved_goal} (max {max_steps} Schritte, Daten: {safe_inputs_display})")

    # Build input data section for Gemini (include passwords so it can type them)
    input_data_str = ""
    if input_values:
        input_lines = []
        for k, v in input_values.items():
            if v:  # skip empty values
                input_lines.append(f"  - {k}: {v}")
        if input_lines:
            input_data_str = "\nVerfügbare Eingabedaten:\n" + "\n".join(input_lines) + "\n"

    history: list[dict] = []

    for i in range(max_steps):
        # Step 1: Extract visible page content as structured text
        page_text = page.evaluate("""() => {
            let result = '';

            // Page context
            result += 'URL: ' + window.location.href + '\\n';
            result += 'Titel: ' + document.title + '\\n\\n';

            // ─── FORM ELEMENTS FIRST (most important for agent decisions) ───
            const inputs = document.querySelectorAll('input, select, textarea, button');
            const formEls = [];
            inputs.forEach(el => {
                const tag = el.tagName.toLowerCase();
                const type = el.type || '';
                const name = el.name || el.id || '';
                // Find label: aria-label, placeholder, or nearby <label> element
                let label = el.getAttribute('aria-label') || el.placeholder || '';
                if (!label && el.id) {
                    const labelEl = document.querySelector(`label[for="${el.id}"]`);
                    if (labelEl) label = labelEl.innerText.trim();
                }
                if (!label && el.closest('label')) {
                    label = el.closest('label').innerText.trim();
                }
                // Also check preceding text/label sibling
                if (!label) {
                    const prev = el.previousElementSibling;
                    if (prev && (prev.tagName === 'LABEL' || prev.tagName === 'SPAN')) {
                        label = prev.innerText.trim();
                    }
                }
                // Check parent TD text for table-based layouts
                if (!label && el.closest('td')) {
                    const row = el.closest('tr');
                    if (row) {
                        const cells = row.querySelectorAll('td, th');
                        if (cells.length >= 2 && cells[0] !== el.closest('td')) {
                            label = cells[0].innerText.trim();
                        }
                    }
                }
                const val = el.value || '';
                // Robust visibility check
                const style = window.getComputedStyle(el);
                const visible = style.display !== 'none' && style.visibility !== 'hidden'
                    && (el.offsetWidth > 0 || el.offsetHeight > 0 || tag === 'select' || tag === 'input');
                if (visible) {
                    if (tag === 'select') {
                        const selectedText = el.options[el.selectedIndex]?.text || '(nichts)';
                        const opts = Array.from(el.options).map(o => o.text).join(', ');
                        formEls.push(`<select name="${name}" label="${label}" aktuell="${selectedText}"> Optionen: [${opts}]`);
                    } else if (tag === 'button') {
                        const btnText = el.innerText.trim();
                        if (btnText) formEls.push(`<button> ${btnText}`);
                    } else if (type !== 'hidden') {
                        formEls.push(`<${tag} type="${type}" name="${name}" label="${label}" value="${val}">`);
                    }
                }
            });
            if (formEls.length > 0) {
                result += '=== Formular-Elemente ===\\n';
                result += formEls.join('\\n') + '\\n\\n';
            }

            // ─── NAVIGATION LINKS ───
            const links = document.querySelectorAll('a[href]');
            const linkTexts = [];
            links.forEach(el => {
                const text = el.innerText.trim();
                const visible = el.offsetParent !== null || el.offsetWidth > 0;
                if (visible && text && text.length > 1 && text.length < 100) {
                    linkTexts.push(text);
                }
            });
            if (linkTexts.length > 0) {
                result += '=== Navigation / Links ===\\n';
                const unique = [...new Set(linkTexts)];
                result += unique.join(' | ') + '\\n\\n';
            }

            // ─── TABLES (truncated to prevent bloat) ───
            const tables = document.querySelectorAll('table');
            tables.forEach((table, idx) => {
                result += '=== Tabelle ' + (idx+1) + ' ===\\n';
                const rows = table.querySelectorAll('tr');
                const maxRows = 15; // Limit rows to prevent huge output
                let rowCount = 0;
                rows.forEach(row => {
                    if (rowCount >= maxRows) return;
                    const cells = row.querySelectorAll('th, td');
                    const texts = [];
                    cells.forEach(cell => {
                        const t = cell.innerText.trim();
                        texts.push(t || '(leer)');
                    });
                    result += texts.join(' | ') + '\\n';
                    rowCount++;
                });
                if (rows.length > maxRows) {
                    result += `... (${rows.length - maxRows} weitere Zeilen)\\n`;
                }
                result += '\\n';
            });

            // ─── PAGE TEXT (for general context) ───
            const bodyText = document.body.innerText.substring(0, 2000);
            if (bodyText.trim()) {
                result += '=== Seitentext ===\\n';
                result += bodyText + '\\n';
            }

            return result.substring(0, 10000);
        }""")

        # Step 2: Ask Gemini for next action
        history_str = "\n".join(
            f"  {h['step']}. {h['action']}: {h.get('target', '')} {h.get('value', '')} → {h.get('result', 'OK')}"
            for h in history
        ) or "  (noch keine Schritte)"

        prompt = (
            f"Du bist ein autonomer Browser-Agent. Dein Ziel:\n"
            f"\"{resolved_goal}\"\n\n"
            f"{input_data_str}"
            f"Aktueller Seiteninhalt:\n{page_text}\n\n"
            f"Bisherige Schritte:\n{history_str}\n\n"
            f"ANALYSIERE zuerst (denke Schritt für Schritt):\n"
            f"- Welche Seite ist gerade geöffnet? (URL, Titel)\n"
            f"- Stimmen die angezeigten Werte mit dem Ziel überein? (z.B. Datum, Filter, Auswahl)\n"
            f"- Falls NEIN: Was muss ZUERST geändert werden?\n"
            f"- Falls JA: Was ist der nächste Schritt zum Ziel?\n\n"
            f"Dann wähle EINE Aktion und antworte als JSON:\n"
            f'{{"reasoning": "<deine kurze Analyse>", "action": "<aktion>", "target": "<ziel>", "value": "<wert>"}}\n\n'
            f"Verfügbare Aktionen:\n"
            f'- click: Klicke auf Text/Button/Link. target = exakter sichtbarer Text\n'
            f'- type: Text in Feld eingeben. target = Feldname/Label/Name, value = Text\n'
            f'- select: Dropdown-Wert ändern. target = name-Attribut des Select-Elements, value = exakter Optionstext\n'
            f'- scroll: Seite scrollen. value = Pixel (z.B. 300)\n'
            f'- wait: Warten. value = Sekunden\n'
            f'- done: Ziel erreicht. reasoning = Begründung\n'
            f'- error: Unmöglich. reasoning = was fehlt/blockiert\n\n'
            f"WICHTIGE REGELN:\n"
            f"- Schau dir die Formular-Elemente an! Bei <select> steht aktuell= welcher Wert JETZT gewählt ist\n"
            f"- Wenn aktuell= NICHT zum Ziel passt (z.B. falsches Datum): SOFORT mit select-Aktion ändern!\n"
            f"- select: target = name-Attribut (z.B. 'datum'), value = gewünschter Optionstext (z.B. '08.03.2026 - Sonntag')\n"
            f"- Nach Dropdown-Änderung: Klicke 'Anzeigen' oder ähnlichen Button um die Seite zu aktualisieren\n"
            f"- NAVIGIERE über Links statt endlos zu scrollen (max 2x scrollen)\n"
            f"- target muss EXAKT dem sichtbaren Text/name-Attribut entsprechen\n"
            f"- Verwende die Eingabedaten für Login, Formulare etc."
        )

        try:
            kb_tool = get_kb_grounding_tool()
            response = _genai().models.generate_content(
                model=GEMINI_MODEL,
                contents=[types.Part.from_text(text=prompt)],
                config=types.GenerateContentConfig(tools=[kb_tool], temperature=0.1),
            )
        except Exception as e:
            log.warning(f"autonomous with KB failed, retrying without: {e}")
            response = _genai().models.generate_content(
                model=GEMINI_MODEL,
                contents=[types.Part.from_text(text=prompt)],
                config={"temperature": 0.1},
            )
        # Log KB grounding for admin transparency
        kb_info = extract_grounding_info(response)
        if kb_info:
            _write_step(run_id, kb_info, f"autonomous step {i+1}")

        try:
            raw = response.text.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            log.info(f"autonomous step {i+1} raw: {raw}")

            import re as _re
            # Find the JSON object in Gemini's response
            # Common issue: Gemini omits opening { (e.g. 'reasoning": "...')
            first_brace = raw.find('{')
            last_brace = raw.rfind('}')

            # If no { found but response looks like JSON without opening brace
            if first_brace == -1 and ('"action"' in raw or '"reasoning"' in raw):
                raw = '{' + raw
                first_brace = 0
                last_brace = raw.rfind('}')
                if last_brace == -1:
                    raw += '}'
                    last_brace = len(raw) - 1
                log.info(f"autonomous step {i+1}: fixed missing opening brace")

            action_data = None

            if first_brace == -1 or last_brace == -1 or last_brace <= first_brace:
                m = None
            else:
                m = raw[first_brace:last_brace + 1]

            # Try JSON first
            if m:
                try:
                    action_data = json.loads(m)
                except json.JSONDecodeError:
                    log.info(f"autonomous step {i+1}: JSON decode failed on extracted block")
                    action_data = None

            # Fallback: YAML-like text extraction (reasoning:...\naction:click\ntarget:...)
            if not action_data or not action_data.get('action'):
                log.info(f"autonomous step {i+1}: trying YAML-like text extraction")
                action_data = {}
                for key in ['action', 'target', 'value', 'reasoning', 'reason']:
                    # Match key:"value" or key:value patterns
                    pat = _re.search(rf'"{key}"\s*:\s*"([^"]*)"', raw)
                    if pat:
                        action_data[key] = pat.group(1)
                    else:
                        # Try unquoted key:value (YAML-like)
                        pat = _re.search(rf'(?:^|\n)\s*{key}\s*:\s*(.+?)(?:\n|$)', raw, _re.IGNORECASE)
                        if pat:
                            action_data[key] = pat.group(1).strip().strip('"')

            if not action_data or not action_data.get('action'):
                _write_step(run_id, f"⚠️ Schritt {i+1}: Parse-Fehler", raw[:200])
                history.append({"step": i+1, "action": "parse_error", "result": raw[:100]})
                continue

            act = action_data.get("action", "")
            target = action_data.get("target", "")
            value = action_data.get("value", "")
            reason = action_data.get("reason", "")
            reasoning = action_data.get("reasoning", reason)  # fallback to reason

            # Log reasoning for admin visibility
            if reasoning:
                log.info(f"autonomous step {i+1} reasoning: {reasoning}")
                _write_step(run_id, f"💭 Schritt {i+1}: Analyse", reasoning)

        except Exception as e:
            _write_step(run_id, f"⚠️ Schritt {i+1}: Gemini-Fehler", str(e)[:200])
            history.append({"step": i+1, "action": "gemini_error", "result": str(e)[:100]})
            continue

        # Step 3: Execute the action
        step_result = "OK"
        try:
            if act == "done":
                # Wait for page to settle before confirming success
                page.wait_for_timeout(2000)
                _write_step(run_id, f"🏁 Autonomes Ziel erreicht (Schritt {i+1})",
                            reason or "Ziel erfolgreich erreicht.",
                            screenshot=_screenshot_jpeg_b64(page))
                history.append({"step": i+1, "action": "done", "result": reason})
                return

            elif act == "error":
                _write_step(run_id, f"❌ Agent meldet Problem (Schritt {i+1})",
                            reason or "Ziel nicht erreichbar.",
                            screenshot=_screenshot_jpeg_b64(page))
                history.append({"step": i+1, "action": "error", "result": reason})
                raise RuntimeError(f"Autonomer Agent: {reason}")

            elif act == "click":
                if dry_run:
                    _write_step(run_id, f"🧪 Schritt {i+1}: Würde klicken", target,
                                screenshot=_screenshot_jpeg_b64(page))
                else:
                    # Priority: button/link roles FIRST (avoids clicking headings),
                    # then text locator as fallback
                    try:
                        page.get_by_role("button", name=target).first.click(timeout=3000)
                    except Exception:
                        try:
                            page.get_by_role("link", name=target).first.click(timeout=3000)
                        except Exception:
                            try:
                                page.get_by_text(target, exact=True).first.click(timeout=3000)
                            except Exception:
                                page.get_by_text(target, exact=False).first.click(timeout=5000)
                    page.wait_for_timeout(2000)
                    _write_step(run_id, f"🤖 Schritt {i+1}: Klick", target,
                                screenshot=_screenshot_jpeg_b64(page))

            elif act == "type":
                # Mask sensitive values in logs
                _SENSITIVE_TARGETS = ("password", "passwort", "kennwort", "secret", "pin")
                display_val = "***" if any(s in target.lower() for s in _SENSITIVE_TARGETS) else value
                if dry_run:
                    _write_step(run_id, f"🧪 Schritt {i+1}: Würde eingeben",
                                f"{target} → {display_val}",
                                screenshot=_screenshot_jpeg_b64(page))
                else:
                    # Find the input element
                    el = None
                    for locator_fn in [
                        lambda: page.get_by_label(target, exact=False).first,
                        lambda: page.get_by_placeholder(target, exact=False).first,
                        lambda: page.locator(f"[name='{target}']").first,
                        lambda: page.locator(f"input[id*='{target}' i]").first,
                    ]:
                        try:
                            candidate = locator_fn()
                            candidate.wait_for(state="visible", timeout=2000)
                            el = candidate
                            break
                        except Exception:
                            continue

                    if el is None:
                        raise Exception(f"Feld '{target}' nicht gefunden")

                    # Human-like input: click → select all → type → Tab
                    # This triggers JavaScript change handlers that fill() misses
                    try:
                        el.click(timeout=3000)
                        page.wait_for_timeout(300)
                        # Select all existing text
                        page.keyboard.press("Control+A")  # Linux (Cloud Run)
                        page.wait_for_timeout(100)
                        # Type the new value (replaces selected text)
                        page.keyboard.type(value, delay=50)
                        page.wait_for_timeout(300)
                        # Press Tab to trigger blur/change events
                        page.keyboard.press("Tab")
                        page.wait_for_timeout(500)
                    except Exception:
                        # Fallback to Playwright fill() for standard inputs
                        el.fill(value, timeout=5000)

                    page.wait_for_timeout(1000)
                    _write_step(run_id, f"🤖 Schritt {i+1}: Eingabe",
                                f"{target} → {display_val}",
                                screenshot=_screenshot_jpeg_b64(page))

            elif act == "select":
                if dry_run:
                    _write_step(run_id, f"🧪 Schritt {i+1}: Würde auswählen",
                                f"{target} → {value}",
                                screenshot=_screenshot_jpeg_b64(page))
                else:
                    sel = None
                    for locator_fn in [
                        lambda: page.locator(f"select[name='{target}']").first,
                        lambda: page.locator(f"select#{target}").first,
                        lambda: page.get_by_label(target, exact=False).first,
                    ]:
                        try:
                            candidate = locator_fn()
                            candidate.wait_for(state="attached", timeout=3000)
                            sel = candidate
                            break
                        except Exception:
                            continue
                    if sel is None:
                        raise Exception(f"Select '{target}' nicht gefunden")
                    _smart_select_option(sel, value, run_id)
                    page.wait_for_timeout(500)
                    _write_step(run_id, f"🤖 Schritt {i+1}: Auswahl",
                                f"{target} → {value}",
                                screenshot=_screenshot_jpeg_b64(page))

            elif act == "scroll":
                px = int(value) if str(value).strip().lstrip('-').isdigit() else 300
                if dry_run:
                    _write_step(run_id, f"🧪 Schritt {i+1}: Würde scrollen", f"{px}px")
                else:
                    page.mouse.wheel(0, px)
                    page.wait_for_timeout(500)
                    _write_step(run_id, f"🤖 Schritt {i+1}: Scroll", f"{px}px",
                                screenshot=_screenshot_jpeg_b64(page))

            elif act == "wait":
                secs = int(value) if str(value).strip().isdigit() else 2
                page.wait_for_timeout(secs * 1000)
                _write_step(run_id, f"🤖 Schritt {i+1}: Warte", f"{secs}s")

            else:
                _write_step(run_id, f"⚠️ Schritt {i+1}: Unbekannte Aktion",
                            f"{act}: {target}")
                step_result = f"unknown action: {act}"

        except RuntimeError:
            raise
        except Exception as e:
            step_result = f"Fehler: {str(e)[:100]}"
            _write_step(run_id, f"⚠️ Schritt {i+1}: Aktion fehlgeschlagen",
                        f"{act}: {target} → {e}",
                        screenshot=_screenshot_jpeg_b64(page))

        history.append({
            "step": i+1,
            "action": act,
            "target": target,
            "value": value,
            "result": step_result,
        })

    # Max steps reached
    _write_step(run_id, f"⚠️ Maximale Schritte erreicht ({max_steps})",
                f"Ziel möglicherweise nicht vollständig erreicht: {goal}",
                screenshot=_screenshot_jpeg_b64(page))



def _looks_like_date(value: str) -> bool:
    """Return True if value already looks like a concrete date (DD.MM.YYYY, YYYY-MM-DD etc.)."""
    import re
    patterns = [
        r"^\d{1,2}\.\d{1,2}\.\d{4}$",   # 22.03.2026
        r"^\d{4}-\d{2}-\d{2}$",           # 2026-03-22
        r"^\d{1,2}/\d{1,2}/\d{4}$",       # 22/03/2026
    ]
    return any(re.match(p, value.strip()) for p in patterns)


def _resolve_date_expression_if_needed(value: str, run_id: str) -> str | None:
    """
    If value is a relative date expression (e.g. 'nächsten Montag', '+7', 'morgen'),
    ask Gemini to resolve it to a concrete date in DD.MM.YYYY format.
    Returns the resolved date string, or None if value doesn't look date-related.
    """
    import datetime
    date_keywords = [
        "montag", "dienstag", "mittwoch", "donnerstag", "freitag",
        "samstag", "sonntag", "morgen", "übermorgen", "nächste",
        "monday", "tuesday", "wednesday", "thursday", "friday",
        "saturday", "sunday", "tomorrow", "next", "heute", "today",
    ]
    v_lower = value.strip().lower()
    is_relative = (
        v_lower.startswith("+") or
        any(kw in v_lower for kw in date_keywords)
    )
    if not is_relative:
        return None

    today = datetime.date.today().strftime("%d.%m.%Y")
    prompt = (
        f"Heute ist {today}. "
        f"Welches konkrete Datum meint der Ausdruck \"{value}\"? "
        f"Antworte NUR mit dem Datum im Format TT.MM.JJJJ, ohne weiteren Text."
    )
    try:
        response = _genai().models.generate_content(
            model=GEMINI_MODEL,
            contents=[types.Part.from_text(text=prompt)],
        )
        resolved = response.text.strip()
        _write_step(run_id, "📅 Datum aufgelöst", f'"{value}" → {resolved}')
        return resolved
    except Exception as e:
        log.warning(f"Date resolution failed for '{value}': {e}")
        return None


def _check_precondition(
    precondition: str, input_values: dict, run_id: str
) -> str | None:
    """
    Evaluate the precondition before running the browser automation.
    Substitutes {placeholders} from input_values, then asks Gemini.
    Returns None if the condition is met (proceed), or a reason string if skipped.
    """
    # Substitute placeholders
    condition = precondition
    for key, val in input_values.items():
        condition = condition.replace(f"{{{key}}}", val)

    import datetime
    today = datetime.date.today().strftime("%d.%m.%Y")
    _write_step(run_id, "🔍 Vorbedingung prüfen", condition)

    prompt = (
        f"Heute ist {today}.\n"
        f"Entscheide ob die folgende Aufgabe ausgeführt werden soll:\n\n"
        f"Bedingung: {condition}\n\n"
        f"Antworte mit genau einem Wort: JA (ausführen) oder NEIN (überspringen), "
        f"gefolgt von einem Doppelpunkt und einer kurzen deutschen Begründung.\n"
        f"Beispiel: 'NEIN: Regen am 24.03.2026 laut Wettervorhersage'"
    )
    try:
        response = _genai().models.generate_content(
            model=GEMINI_MODEL,
            contents=[types.Part.from_text(text=prompt)],
        )
        answer = response.text.strip()
        log.info(f"[Precondition] Answer: {answer}")

        if answer.upper().startswith("NEIN"):
            reason = answer[4:].lstrip(": ").strip() or condition
            _write_step(run_id, "⏭️ Vorbedingung nicht erfüllt", reason)
            return reason  # → skip
        else:
            # Log the JA result with Gemini's reasoning
            reasoning = answer[3:].lstrip(": ").strip() if answer.upper().startswith("JA") else answer
            _write_step(run_id, "✅ Vorbedingung erfüllt", reasoning or "Ausführung fortgesetzt.")
            return None  # → proceed
    except Exception as e:
        log.warning(f"Precondition check failed (proceeding anyway): {e}")
        return None  # On error, proceed rather than skip



def _is_css_selector(s: str) -> bool:
    """Check if a string looks like a CSS selector (not a visual description)."""
    s = s.strip()
    if not s:
        return False
    # Patterns: #id, .class, tag[attr], input[type=...], a:has-text(...)
    if s.startswith(('#', '.', '[')) or ':' in s:
        return True
    if re.match(r'^[a-z]+\[', s):
        return True
    return False


def _css_to_readable(selector: str) -> str:
    """Convert a CSS selector to a human-readable description for Gemini Vision."""
    s = selector.strip()
    # #password → "password input field"
    if s.startswith('#'):
        name = s[1:].replace('-', ' ').replace('_', ' ')
        return f"the '{name}' input field"
    # .login-field → "login field element"  
    if s.startswith('.'):
        name = s[1:].replace('-', ' ').replace('_', ' ')
        return f"the '{name}' element"
    # input[type="password"] → "password input"
    m = re.search(r'type=["\']?(\w+)', s)
    if m:
        return f"the {m.group(1)} input field"
    # a:has-text('Login') → "Login link/button"
    m = re.search(r"has-text\(['\"]([^'\"]+)", s)
    if m:
        return f"the '{m.group(1)}' link or button"
    # Fallback: just clean up special chars
    clean = re.sub(r'[#.\[\]:="\']', ' ', s).strip()
    return f"the '{clean}' element"


def _vision_fill(page: Page, description: str, value: str, run_id: str) -> None:
    """Fill an input field using a multi-tier fallback strategy:
    1. Try Playwright CSS selector directly (if description looks like one)
    2. Try Gemini Vision with a human-readable description
    3. Try text-based Playwright search (get_by_label, get_by_placeholder)
    """
    _SENSITIVE_KEYWORDS = ("password", "passwort", "kennwort", "secret", "pin", "token")
    display_value = "***" if any(k in description.lower() for k in _SENSITIVE_KEYWORDS) else value

    # --- Tier 1: Direct Playwright CSS selector (iFrame-aware) ---
    if _is_css_selector(description):
        try:
            _resolve_wait(page, description, timeout=10000)
            loc = _resolve_locator(page, description)
            if loc.count() > 0:
                loc.first.click(timeout=3000)
                page.wait_for_timeout(200)
                page.keyboard.press("Control+a")
                page.keyboard.type(value, delay=30)
                _write_step(run_id, f"⌨️ Eingabe (CSS)", f"{description}: {display_value}")
                return
        except Exception as e:
            log.info(f"_vision_fill: CSS selector '{description}' failed: {e}")
            _write_step(run_id, "⚠️ CSS-Selector fehlgeschlagen, versuche Vision", description)

    # --- Tier 2: Gemini Vision locate ---
    # Convert CSS selectors to human-readable for Gemini
    vision_desc = _css_to_readable(description) if _is_css_selector(description) else description
    screenshot_b64 = _screenshot_b64(page)
    coords = _gemini_locate(screenshot_b64, vision_desc, run_id)

    if coords:
        x, y = coords["x"], coords["y"]
        _write_step(run_id, f"⌨️ Eingabe via Vision ({x},{y})", f"{vision_desc}: {display_value}")
        page.mouse.click(x, y)
        page.wait_for_timeout(300)
        page.keyboard.press("Control+a")
        page.keyboard.type(value, delay=30)
        return

    # --- Tier 3: Text-based Playwright search ---
    # Try common text-based locators as last resort
    label_text = re.sub(r'[#.\[\]:="\']', ' ', description).strip()
    for locator_fn in [
        lambda: page.get_by_label(label_text, exact=False).first,
        lambda: page.get_by_placeholder(label_text, exact=False).first,
        lambda: page.locator(f'input[name*="{label_text}" i]').first,
        lambda: page.locator(f'input[type="{label_text}"]').first,
    ]:
        try:
            el = locator_fn()
            el.click(timeout=3000)
            page.wait_for_timeout(200)
            page.keyboard.press("Control+a")
            page.keyboard.type(value, delay=30)
            _write_step(run_id, f"⌨️ Eingabe (Text-Suche)", f"{description}: {display_value}")
            return
        except Exception:
            continue

    raise RuntimeError(
        f"Eingabefeld nicht gefunden: '{description}'"
    )



def _smart_select_option(select_locator, value: str, run_id: str, timeout: int = 5000) -> None:
    """
    Smart select_option: tries exact label match first, then partial/substring match.
    This handles cases like PCCaddie where option labels are:
    'DIENSTAG, 10.03.2026 | 9 Löcher von Tee 1' but the value is just '10.03.2026'.
    """
    # 1) Try exact label match
    try:
        select_locator.select_option(label=value, timeout=timeout)
        return
    except Exception:
        pass

    # 2) Try exact value match (e.g. DAY|2026-03-10)
    try:
        select_locator.select_option(value=value, timeout=2000)
        return
    except Exception:
        pass

    # 3) Partial label match: read all options, find best match containing value
    try:
        options = select_locator.evaluate("""
            el => Array.from(el.options).map(o => ({value: o.value, text: o.text.trim()}))
        """)
        log.info(f"smart_select_option: searching '{value}' in {len(options)} options")

        # Find options whose text contains the value (case-insensitive)
        val_lower = value.lower()
        matches = [o for o in options if val_lower in o["text"].lower()]

        if len(matches) == 1:
            log.info(f"smart_select_option: partial match → '{matches[0]['text']}'")
            _write_step(run_id, "🔍 Partial Match", f"'{value}' → '{matches[0]['text']}'")
            select_locator.select_option(value=matches[0]["value"], timeout=2000)
            return
        elif len(matches) > 1:
            # Multiple matches — pick the shortest (most specific)
            matches.sort(key=lambda o: len(o["text"]))
            log.info(f"smart_select_option: best of {len(matches)} matches → '{matches[0]['text']}'")
            _write_step(run_id, "🔍 Partial Match (best)", f"'{value}' → '{matches[0]['text']}'")
            select_locator.select_option(value=matches[0]["value"], timeout=2000)
            return

        # 4a) ISO date fallback: try converting '2026-03-12' -> '12.03.2026'
        import re as _re
        iso_match = _re.match(r'^(\d{4})-(\d{2})-(\d{2})$', value)
        if iso_match:
            y, m, d = iso_match.groups()
            de_date = f"{d}.{m}.{y}"
            de_matches = [o for o in options if de_date in o["text"]]
            if de_matches:
                log.info(f"smart_select_option: ISO→DE date match '{de_date}' → '{de_matches[0]['text']}'")
                _write_step(run_id, "🔍 ISO-Datum Match", f"'{value}' → '{de_matches[0]['text']}'")
                select_locator.select_option(value=de_matches[0]["value"], timeout=2000)
                return

        # 4b) No text match — try matching against option values
        val_matches = [o for o in options if val_lower in o["value"].lower()]
        if val_matches:
            log.info(f"smart_select_option: value match → '{val_matches[0]['value']}'")
            _write_step(run_id, "🔍 Value Match", f"'{value}' → option value '{val_matches[0]['value']}'")
            select_locator.select_option(value=val_matches[0]["value"], timeout=2000)
            return

        # Nothing found
        opt_texts = [o["text"] for o in options[:5]]
        raise RuntimeError(
            f"Option '{value}' nicht gefunden. Verfügbare Optionen: {opt_texts}"
        )
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"select_option fehlgeschlagen für '{value}': {e}")


def _playwright_smart_select(page: Page, pw_code: str, run_id: str) -> None:
    """
    Fallback for failed playwright select_option calls.
    Parses the pw_code to extract the locator and option value,
    then uses _smart_select_option for partial label matching.
    
    Handles patterns like:
    - page.get_by_label("Datum").select_option("value")
    - page.locator("#id").select_option(label="value")
    - page.get_by_label("Datum").first.select_option("value")
    """
    import re as _re
    
    # Extract the option value from select_option(...) call
    # Match select_option("value"), select_option(label="value"), select_option(value="value")
    opt_match = _re.search(
        r'\.select_option\(\s*(?:label\s*=\s*|value\s*=\s*)?["\']([^"\']+)["\']', 
        pw_code
    )
    if not opt_match:
        raise RuntimeError(f"Konnte Option-Wert nicht aus Playwright-Code extrahieren: {pw_code[:100]}")
    
    option_value = opt_match.group(1)
    log.info(f"_playwright_smart_select: extracted option value '{option_value}'")
    
    # Extract everything before .select_option to reconstruct the locator
    locator_code = pw_code[:pw_code.index('.select_option')]
    # Remove .first if present (we'll add it back)
    locator_code = locator_code.rstrip()
    if locator_code.endswith('.first'):
        locator_code = locator_code[:-5]
    
    log.info(f"_playwright_smart_select: locator code = '{locator_code}'")
    
    # Execute just the locator part to get the element
    try:
        locator = eval(locator_code, {"page": page})  # noqa: S307
    except Exception as e:
        raise RuntimeError(f"Locator-Auswertung fehlgeschlagen: {e}")
    
    # If the locator is not a select element, find the select within it
    try:
        tag = locator.first.evaluate("el => el.tagName.toLowerCase()")
        if tag != "select":
            locator = locator.first.locator("select").first
    except Exception:
        pass
    
    _smart_select_option(locator.first, option_value, run_id)


def _vision_select(page: Page, description: str, value: str, run_id: str) -> None:
    """Select a dropdown option using a multi-tier fallback:
    1. Try Playwright CSS selector directly (if description looks like one)
    2. Try Gemini Vision to locate the dropdown
    3. Fall back to text-based search
    """
    # --- Tier 1: Direct Playwright CSS selector (iFrame-aware) ---
    if _is_css_selector(description):
        try:
            # Wait for the selector to appear (handles page navigation delays)
            _resolve_wait(page, description, timeout=20000)
            loc = _resolve_locator(page, description)
            if loc.count() > 0:
                _write_step(run_id, f"📋 Dropdown (CSS)", f"{description} → {value}")
                _smart_select_option(loc.first, value, run_id)
                return
        except Exception as e:
            log.info(f"_vision_select: CSS selector '{description}' failed: {e}")
            _write_step(run_id, "⚠️ CSS-Selector fehlgeschlagen, versuche Vision", description)

    # --- Tier 2: Gemini Vision locate ---
    vision_desc = _css_to_readable(description) if _is_css_selector(description) else description
    screenshot_b64 = _screenshot_b64(page)
    coords = _gemini_locate(screenshot_b64, vision_desc, run_id)

    if coords:
        x, y = coords["x"], coords["y"]
        _write_step(run_id, f"📋 Dropdown via Vision ({x},{y})", f"{vision_desc} → {value}")

        # Try native <select> first with smart matching
        # Use elementFromPoint to find the actual select at the click coordinates
        try:
            page.mouse.click(x, y)
            page.wait_for_timeout(400)

            # Find the select element at or near the click point
            select_loc = None

            # Strategy A: If original description is a CSS selector, retry it now
            # (the click may have triggered lazy rendering)
            if _is_css_selector(description):
                try:
                    retry_loc = page.locator(description)
                    if retry_loc.count() > 0:
                        tag = retry_loc.first.evaluate("el => el.tagName.toLowerCase()")
                        if tag == "select":
                            select_loc = retry_loc.first
                            log.info(f"_vision_select: CSS selector '{description}' now works after click")
                except Exception:
                    pass

            # Strategy B: Use elementFromPoint to find select at click coords
            if select_loc is None:
                try:
                    found_select = page.evaluate(f"""() => {{
                        let el = document.elementFromPoint({x}, {y});
                        // Walk up the DOM to find a <select> element
                        while (el && el.tagName !== 'SELECT' && el !== document.body) {{
                            el = el.parentElement;
                        }}
                        if (el && el.tagName === 'SELECT') {{
                            // Return a unique identifier to locate it
                            if (el.id) return '#' + el.id;
                            if (el.name) return 'select[name="' + el.name + '"]';
                            // Fallback: count preceding select siblings
                            const selects = Array.from(document.querySelectorAll('select'));
                            const idx = selects.indexOf(el);
                            return '__select_index__' + idx;
                        }}
                        return null;
                    }}""")
                    if found_select:
                        if found_select.startswith("__select_index__"):
                            idx = int(found_select.replace("__select_index__", ""))
                            select_loc = page.locator("select").nth(idx)
                        else:
                            select_loc = page.locator(found_select).first
                        log.info(f"_vision_select: elementFromPoint found select: {found_select}")
                except Exception as e:
                    log.info(f"_vision_select: elementFromPoint failed: {e}")

            # Strategy C: Fallback to first select (original behavior)
            if select_loc is None:
                select_loc = page.locator("select").first
                log.info("_vision_select: falling back to first select on page")

            _smart_select_option(select_loc, value, run_id)
            return
        except Exception:
            pass

        # Fallback: click to open dropdown, then click the matching option text
        page.mouse.click(x, y)
        page.wait_for_timeout(600)
        try:
            page.get_by_text(value, exact=False).first.click(timeout=5_000)
        except Exception:
            shot2 = _screenshot_b64(page)
            coords2 = _gemini_locate(shot2, value, run_id)
            if coords2:
                page.mouse.click(coords2["x"], coords2["y"])
            else:
                raise RuntimeError(f"Option '{value}' nicht gefunden im Dropdown")
        return

    # --- Tier 3: Text/label based search ---
    label_text = re.sub(r'[#.\[\]:="\']', ' ', description).strip()
    try:
        loc = page.get_by_label(label_text, exact=False).first
        _write_step(run_id, f"📋 Dropdown (Label-Suche)", f"{label_text} → {value}")
        _smart_select_option(loc, value, run_id)
        return
    except Exception:
        pass

    raise RuntimeError(f"Dropdown nicht gefunden: '{description}'")


def _vision_hover(page: Page, description: str, run_id: str) -> None:
    """Hover over an element located by Gemini Vision (to reveal sub-menus etc.)."""
    screenshot_b64 = _screenshot_b64(page)
    coords = _gemini_locate(screenshot_b64, description, run_id)
    if coords:
        x, y = coords["x"], coords["y"]
        _write_step(run_id, f"🖱️ Hover ({x},{y})", description)
        page.mouse.move(x, y)
        page.wait_for_timeout(600)
    else:
        raise RuntimeError(f"Element für Hover nicht gefunden: '{description}'")


def _vision_checkbox(page: Page, description: str, run_id: str) -> None:
    """Toggle a checkbox located by Gemini Vision."""
    screenshot_b64 = _screenshot_b64(page)
    coords = _gemini_locate(screenshot_b64, description, run_id)
    if coords:
        x, y = coords["x"], coords["y"]
        _write_step(run_id, f"☑️ Checkbox ({x},{y})", description)
        page.mouse.click(x, y)
        page.wait_for_timeout(300)
    else:
        try:
            page.get_by_label(description, exact=False).first.check(timeout=5_000)
        except Exception:
            raise RuntimeError(f"Checkbox nicht gefunden: '{description}'")


def _verify_success(screenshot_b64: str, indicator: str) -> str:
    """
    Ask Gemini to check if the success indicator is visible in the screenshot.
    Uses KB grounding to leverage known success indicators per website.
    Returns a human-readable result string.
    """
    prompt = (
        f'Look at this screenshot. '
        f'Is this text or element visible: "{indicator}"? '
        f'If you have knowledge base information about this website\'s success indicators, use it. '
        f'Answer with a short German summary of what you see on the screen. '
        f'Start with "✅ Erfolgreich:" if the success indicator is visible, '
        f'or "⚠️ Unklar:" if it is not clearly visible.'
    )

    client = _genai()
    try:
        kb_tool = get_kb_grounding_tool()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(
                    data=base64.b64decode(screenshot_b64),
                    mime_type="image/png",
                ),
                types.Part.from_text(text=prompt),
            ],
            config=types.GenerateContentConfig(tools=[kb_tool]),
        )
    except Exception as e:
        log.warning(f"verify_success with KB failed, retrying without: {e}")
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(
                    data=base64.b64decode(screenshot_b64),
                    mime_type="image/png",
                ),
                types.Part.from_text(text=prompt),
            ],
        )
    # Log KB grounding for admin transparency
    kb_info = extract_grounding_info(response)
    if kb_info:
        log.info(f"verify_success KB grounding: {kb_info}")
    return response.text.strip()


# ─── Firestore Helpers ──────────────────────────────────────────────────────────

def _screenshot_jpeg_b64(page) -> str:
    """Returns a compressed base64-encoded JPEG thumbnail (640px wide) for admin log."""
    try:
        import io
        from PIL import Image
        raw = page.screenshot(type="png")
        img = Image.open(io.BytesIO(raw))
        # Resize to max 640px wide
        w, h = img.size
        if w > 640:
            img = img.resize((640, int(h * 640 / w)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60, optimize=True)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        log.warning(f"Screenshot JPEG failed: {e}")
        return ""


def _write_step(run_id: str, action: str, detail: str,
                is_error: bool = False, screenshot: str = "") -> None:
    """Append a step to the agent run document in Firestore."""
    import datetime
    log.info(f"[Step] {action}: {detail[:100]}")
    try:
        db = _firestore()
        ref = db.collection("agent_runs").document(run_id)
        entry: dict = {
            "timestamp": datetime.datetime.utcnow(),  # SERVER_TIMESTAMP not allowed in ArrayUnion
            "action": action,
            "detail": detail,
            "isError": is_error,
        }
        if screenshot:
            entry["screenshot"] = screenshot
        ref.update({"steps": firestore.ArrayUnion([entry])})
    except Exception as e:
        log.warning(f"Failed to write step to Firestore: {e}")


def _set_run_status(
    run_id: str,
    status: str,
    result: str = "",
    error_msg: str = "",
) -> None:
    """Update the overall run status in Firestore."""
    log.info(f"[Status] runId={run_id} status={status}")
    try:
        db = _firestore()
        ref = db.collection("agent_runs").document(run_id)
        update: dict = {
            "status": status,
            "completedAt": firestore.SERVER_TIMESTAMP,
        }
        if result:
            update["result"] = result
        if error_msg:
            update["errorMsg"] = error_msg
        ref.update(update)
    except Exception as e:
        log.warning(f"Failed to update run status in Firestore: {e}")


# ─── KB Feedback Loop ──────────────────────────────────────────────────────────
# After each successful REAL run (not sandbox), write structured feedback to
# Firestore. This creates a learning loop: the agent can query past runs to
# know which selectors worked, which needed fallbacks, and where errors occurred.


# ─── Self-Healing KB Analysis ──────────────────────────────────────────────────


def _strip_markdown(text: str) -> str:
    """Strip markdown code fences (```json ... ```) from LLM output."""
    text = text.strip()
    if text.startswith("```"):
        # Remove first line (```json or ```)
        lines = text.split("\n")
        lines = lines[1:]  # skip opening fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]  # skip closing fence
        text = "\n".join(lines)
    return text.strip()


def _analyze_failure_and_suggest_fix(
    run_id: str,
    agent_id: str,
    target_url: str,
    failed_step: dict,
    error_msg: str,
    page_screenshot_b64: str,
    dom_snippet: str,
) -> None:
    """Analyze a failed step with Gemini Pro and write a fix suggestion to Firestore.

    Two-step process:
    1. Technical analysis → JSON with new_selector, confidence, analysis
    2. Klartext translation → problem_text + solution_text for non-technical admin
    """
    from urllib.parse import urlparse
    import datetime

    step_action = failed_step.get("action", "unknown")
    step_desc = failed_step.get("description", "") or failed_step.get("target_description", "")
    step_value = failed_step.get("value", "")

    # Load site knowledge for this domain (if available)
    site_knowledge_text = ""
    try:
        domain = urlparse(target_url).hostname or ""
        db = _firestore()
        # Query kb_sessions for site_knowledge documents matching this domain
        kb_docs = db.collection("kb_sessions").where(
            "type", "==", "site_knowledge"
        ).stream()
        for doc in kb_docs:
            sk = doc.to_dict()
            base_url = sk.get("baseUrl", "")
            if base_url and base_url in domain:
                site_name = sk.get("siteName", base_url)
                # Format UI patterns
                patterns = sk.get("uiPatterns", [])
                if patterns:
                    pattern_lines = []
                    for p in patterns:
                        pattern_lines.append(
                            f"- {p.get('pattern', '')}: {p.get('description', '')} "
                            f"LÖSUNG: {p.get('solution', '')}"
                        )
                    site_knowledge_text += f"\n\nBEKANNTES WISSEN ÜBER {site_name.upper()}:\n"
                    site_knowledge_text += "\n".join(pattern_lines)

                # Format common errors
                errors = sk.get("commonErrors", [])
                if errors:
                    error_lines = []
                    for e in errors:
                        error_lines.append(
                            f"- {e.get('error', '')}: Ursache: {e.get('cause', '')} → "
                            f"Fix: {e.get('fix', '')}"
                        )
                    site_knowledge_text += f"\n\nHÄUFIGE FEHLER BEI {site_name.upper()}:\n"
                    site_knowledge_text += "\n".join(error_lines)

                # Format learned patterns (auto-learned from approved fixes)
                learned = sk.get("learnedPatterns", [])
                if learned:
                    learned_lines = []
                    for lp in learned[-10:]:  # Last 10 to keep prompt manageable
                        learned_lines.append(
                            f"- Problem: {lp.get('problem', '')} → "
                            f"Lösung: {lp.get('solution', '')} "
                            f"(alt: {lp.get('oldSelector', '')[:50]} → "
                            f"neu: {lp.get('newSelector', '')[:50]})"
                        )
                    site_knowledge_text += f"\n\nGELERNTE FIXES ({len(learned)} gesamt, letzte {len(learned_lines)}):\n"
                    site_knowledge_text += "\n".join(learned_lines)

                log.info(f"[Self-Healing] Loaded site knowledge for {site_name} "
                         f"({len(patterns)} patterns, {len(errors)} errors, {len(learned)} learned)")
                break
    except Exception as e:
        log.warning(f"[Self-Healing] Site knowledge load failed: {e}")

    # 1. Technical analysis (Gemini Pro + Screenshot + DOM + Site Knowledge)
    analysis_prompt = f"""Du bist ein Analyse-Agent für Web-Automatisierung.
Eine Aktion ist fehlgeschlagen. Analysiere den Fehler und schlage eine Korrektur vor.

FEHLGESCHLAGENER SCHRITT:
- Aktion: {step_action}
- Ziel: {step_desc[:200]}
- Wert: {step_value[:100]}
- Fehlermeldung: {error_msg[:300]}

AKTUELLER DOM-AUSZUG (reduziert):
{dom_snippet[:3000]}
{site_knowledge_text}

ANWEISUNGEN:
1. Analysiere warum der Selektor/die Aktion nicht mehr funktioniert
2. Nutze das bekannte Wissen über diese Website (falls vorhanden) um bessere Vorschläge zu machen
3. Suche im DOM nach dem Element das der Intention am nächsten kommt
4. Generiere einen robusten neuen Selektor oder Aktionsänderung
5. Bewerte deine Sicherheit (0.0 - 1.0)

Antworte NUR als JSON:
{{
  "analysis": "Kurze technische Erklärung",
  "old_selector": "Der fehlgeschlagene Selektor/Beschreibung",
  "new_selector": "Vorgeschlagener neuer Selektor",
  "new_action": "click|input_text|select|playwright|...",
  "confidence": 0.85,
  "change_type": "selector_update|action_change|step_removal|step_addition"
}}"""

    try:
        contents = []
        if page_screenshot_b64:
            contents.append(
                types.Part.from_bytes(
                    data=base64.b64decode(page_screenshot_b64),
                    mime_type="image/jpeg",
                )
            )
        contents.append(types.Part.from_text(text=analysis_prompt))

        resp = _reasoning_genai().models.generate_content(
            model=REASONING_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(temperature=0.1),
        )
        fix_json = json.loads(_strip_markdown(resp.text))
    except Exception as e:
        log.warning(f"[Self-Healing] Technical analysis failed: {e}")
        return

    # 2. Klartext translation (Gemini Pro – for non-technical admins)
    translate_prompt = f"""Übersetze diese technische Analyse in einfache Sprache für einen Nicht-Techniker.
Der Admin muss verstehen: Was ist passiert? Was schlägt der Agent vor?

Technische Analyse: {fix_json.get('analysis', '')}
Alter Selektor: {fix_json.get('old_selector', '')}
Neuer Selektor: {fix_json.get('new_selector', '')}
Sicherheit: {fix_json.get('confidence', 0)}

Antworte als JSON:
{{
  "problem_text": "Ein Satz der das Problem erklärt (z.B. 'Der Login-Button heißt jetzt Einloggen statt Anmelden')",
  "solution_text": "Ein Satz der die Lösung erklärt (z.B. 'Der Agent hat den neuen Button gefunden und kann ihn ab jetzt nutzen')"
}}"""

    try:
        text_resp = _reasoning_genai().models.generate_content(
            model=REASONING_MODEL,
            contents=translate_prompt,
            config=types.GenerateContentConfig(temperature=0.3),
        )
        human_text = json.loads(_strip_markdown(text_resp.text))
    except Exception:
        human_text = {
            "problem_text": fix_json.get("analysis", "Unbekannter Fehler"),
            "solution_text": "Bitte manuell prüfen.",
        }

    # 3. Write fix suggestion to Firestore for admin review
    confidence = fix_json.get("confidence", 0.0)
    suggestion_doc = {
        "runId": run_id,
        "agentId": agent_id,
        "domain": urlparse(target_url).hostname or "unknown",
        "targetUrl": target_url,
        "createdAt": datetime.datetime.now(tz=datetime.timezone.utc),
        "status": "pending",  # pending | approved | rejected
        "failedStep": {
            "stepNum": failed_step.get("step", 0),
            "action": step_action,
            "description": step_desc[:100],
            "target_description": failed_step.get("target_description", "")[:100],
            "value": step_value[:100],
        },
        "error": error_msg[:300],
        "fix": {
            "oldSelector": fix_json.get("old_selector", ""),
            "newSelector": fix_json.get("new_selector", ""),
            "newAction": fix_json.get("new_action", ""),
            "changeType": fix_json.get("change_type", ""),
            "technicalAnalysis": fix_json.get("analysis", ""),
        },
        "confidence": confidence,
        "humanReadable": {
            "problemText": human_text.get("problem_text", ""),
            "solutionText": human_text.get("solution_text", ""),
        },
        # Store screenshot reference (truncated to stay under Firestore 1MB limit)
        "screenshot": page_screenshot_b64[:50000] if page_screenshot_b64 else None,
        "reviewedBy": None,
        "reviewedAt": None,
        "reviewNote": None,
    }

    try:
        db = _firestore()
        db.collection("kb_fix_suggestions").add(suggestion_doc)
        log.info(
            f"[Self-Healing] Fix suggestion written for run {run_id} "
            f"(confidence={confidence:.0%}, domain={suggestion_doc['domain']})"
        )
        _write_step(
            run_id,
            "🔧 Verbesserungsvorschlag erstellt",
            f"{human_text.get('problem_text', '')} (Sicherheit: {confidence:.0%})",
        )
    except Exception as e:
        log.warning(f"[Self-Healing] Firestore write failed: {e}")

def _write_run_feedback(
    run_id: str,
    agent_id: str,
    target_url: str,
    action_sequence: list,
    step_results: list,
    final_status: str,
    result: str = "",
) -> None:
    """Write run execution feedback to Firestore for KB learning.

    Only called for non-sandbox runs (quality gate: only verified data enters KB).
    The feedback document captures:
    - Which selectors succeeded/failed
    - Which steps needed fallbacks (vision → text search)
    - Execution timing per step
    - Error patterns for future avoidance
    """
    import datetime

    # Extract domain from target URL
    try:
        from urllib.parse import urlparse
        domain = urlparse(target_url).hostname or "unknown"
    except Exception:
        domain = "unknown"

    # Identify the skill from the agent run
    skill_id = ""
    for step in action_sequence:
        if step.get("action") == "navigate":
            break
    # Try to extract skill ID from agent_id pattern
    skill_id = agent_id  # Use agent_id as skill identifier

    # Analyze step results
    total = len(step_results)
    successful = sum(1 for s in step_results if s.get("status") == "success")
    failed = sum(1 for s in step_results if s.get("status") == "error")
    aborted = sum(1 for s in step_results if s.get("status") == "aborted")

    # Build selector feedback (which selectors worked/failed)
    selector_feedback = []
    for i, sr in enumerate(step_results):
        step_def = action_sequence[i] if i < len(action_sequence) else {}
        entry = {
            "step": sr.get("step", i + 1),
            "action": sr.get("action", ""),
            "status": sr.get("status", "unknown"),
            "executionMs": sr.get("executionMs", 0),
        }

        # Include selector info from action_sequence
        desc = step_def.get("description", "")
        target_desc = step_def.get("target_description", "")
        if desc:
            entry["selector"] = desc[:100]
        if target_desc:
            entry["targetDescription"] = target_desc[:100]

        # Include error info
        if sr.get("error"):
            entry["error"] = sr["error"][:200]
        if sr.get("reason"):
            entry["reason"] = sr["reason"][:100]

        selector_feedback.append(entry)

    # Build the feedback document
    feedback_doc = {
        "runId": run_id,
        "agentId": agent_id,
        "skillId": skill_id,
        "domain": domain,
        "targetUrl": target_url,
        "status": final_status,
        "result": (result or "")[:500],
        "timestamp": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
        "totalSteps": total,
        "successfulSteps": successful,
        "failedSteps": failed,
        "abortedSteps": aborted,
        "totalExecutionMs": sum(s.get("executionMs", 0) for s in step_results),
        "selectorFeedback": selector_feedback,
    }

    # Write to Firestore
    try:
        db = _firestore()
        db.collection("kb_run_feedback").document(run_id).set(feedback_doc)
        log.info(f"[KB Feedback] Written to kb_run_feedback/{run_id} "
                 f"({successful}/{total} steps OK, domain={domain})")
    except Exception as e:
        log.warning(f"[KB Feedback] Firestore write failed: {e}")

