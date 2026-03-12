"""
GolfStatus Browser Agent – Cloud Run Service (Gemini Live Agent Challenge)

Core of the autonomous UI Navigator: receives an HTTP request from the
Go orchestration layer (Cloud Functions), launches a headless Chromium
browser via Playwright, and executes a multi-step action sequence.

Hybrid Architecture:
  - DETERMINISTIC actions (navigate, js, playwright) use Playwright API directly
  - INTELLIGENT actions (click, find_click) use Gemini Vision to locate UI elements
    by analysing screenshots, then click at the returned (x, y) coordinates

Each step is logged in real-time to Firestore (agent_runs/{runId}), enabling
a live dashboard in the Flutter mobile app.

Deployed as: Cloud Run service (europe-west3, 2 vCPU, 2GB RAM)
"""
import base64
import datetime
import json
import logging
import os
import re
import time
import functions_framework
from flask import Request, jsonify
from google.cloud import firestore
from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("GCP_PROJECT", os.environ.get("PROJECT_ID", ""))
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

_db: firestore.Client | None = None
_genai_client: genai.Client | None = None


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
            location=os.environ.get("AI_LOCATION", "us-central1"),
        )
    return _genai_client


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

    if not run_id or not target_url:
        return jsonify({"error": "runId and targetUrl are required"}), 400

    log.info(f"[RunBrowserAgent] runId={run_id} url={target_url} steps={len(action_sequence)}")

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
# Users can enter dates as "nächsten Samstag", "morgen", "+6" etc.
# These are resolved to DD.MM.YYYY before the browser automation starts.
# Local Python resolution handles common cases; Gemini is the fallback.

from datetime import datetime as _datetime, timedelta as _timedelta
_re = re  # alias for backward compat with existing code

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
                model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
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

    with sync_playwright() as p:
        # ── Anti-Detection & Browser Stealth ──────────────────────────────
        # Many booking portals (e.g. PC Caddy) use bot-detection. These
        # flags prevent Chromium from being identified as automated:
        # - AutomationControlled: removes the "Chrome is being controlled by
        #   automated software" infobar and navigator.webdriver flag
        # - Custom User-Agent: mimics a real desktop browser
        # - German Accept-Language: matches the target portal locale
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
        # ── Cookie Consent Auto-Dismiss ───────────────────────────────────
        # Booking portals show cookie banners that block UI interaction.
        # We pre-set the CookieBot consent cookie AND poll for common
        # accept buttons to ensure the agent never gets stuck on a banner.
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
            for step_def in action_sequence:
                try:
                    _execute_step(page, step_def, input_values, run_id, dry_run=dry_run)
                except AbortStepError as e:
                    abort_reason = str(e)
                    break

            # Final result
            if dry_run:
                # Check if steps had sandboxOff (real execution despite sandbox mode)
                total = len(action_sequence)
                real_count = sum(1 for s in action_sequence if s.get("sandboxOff"))
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

        except Exception as e:
            try:
                _screenshot_b64(page)
            except Exception:
                pass
            raise
        finally:
            browser.close()


class AbortStepError(Exception):
    """Raised by abort_if action to stop the sequence gracefully."""


def _execute_step(page: Page, step_def: dict, input_values: dict, run_id: str, dry_run: bool = False) -> None:
    """
    DSL Command Router – dispatches a single skill step to the correct handler.

    Each step from the action sequence is routed based on its 'action' field:
    - Deterministic actions (navigate, js, playwright, press) use Playwright directly
    - Vision actions (click, find_click, input_text) use Gemini to locate elements
    - Control flow (abort_if, if_input, wait_for) use Gemini for visual verification

    In Sandbox mode (dry_run=True), actions are simulated: Gemini still locates
    elements but no real clicks or form submissions happen. Individual steps can
    override this with sandboxOff=True for hybrid test/real execution.
    """
    DRY = dry_run  # shorthand
    step_num = step_def.get("step", "?")
    action = step_def.get("action", "")
    target_desc = step_def.get("target_description", "")
    visual_anchor = step_def.get("visual_anchor", target_desc)
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
            coords = _gemini_locate(shot, visual_anchor)
            _write_step(run_id, f"🧪 Würde klicken: {visual_anchor}",
                        f"Position: ({coords['x']},{coords['y']})" if coords else "Element nicht eindeutig lokalisiert")
        else:
            _vision_click(page, visual_anchor, run_id)
    elif action == "input_text":
        if DRY:
            shot = _screenshot_b64(page)
            coords = _gemini_locate(shot, visual_anchor)
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
                    coords = _gemini_locate(shot, visual_anchor)
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
                page.evaluate(js_code)
                _write_step(run_id, "⚙️ JS ausgeführt", (target_desc or raw_value)[:80])
            except Exception as e:
                _write_step(run_id, "⚠️ JS Fehler", str(e)[:80])
    elif action == "playwright":
        # Execute raw Playwright API call (from codegen or KB segment recording).
        # {placeholder} values are substituted from user inputValues.
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
        _re = re  # use top-level import
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
            # SECURITY NOTE: pw_code is sourced exclusively from admin-defined skills
            # stored in Firestore (config/agent_skills), NOT from end-user input.
            # Only users with admin privileges can create or modify skills.
            try:
                exec(pw_code, {"page": page, "expect": None})  # noqa: S102
                _write_step(run_id, "🎭 Playwright", log_code[:120])
            except Exception as e:
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
    elif action == "find_click":
        # Smart visual search: finds FIRST available/matching element (e.g. first free tee slot)
        _vision_find_click(page, visual_anchor, run_id, dry_run=DRY)
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
    else:
        log.warning(f"Unknown action: {action}")


# ─── Gemini Vision Helpers ──────────────────────────────────────────────────────
# The vision layer is the core innovation: instead of fragile CSS selectors,
# we send screenshots to Gemini and ask it to locate UI elements by description.
# This makes the agent resilient to UI changes – if a button moves or changes
# color, Gemini still finds it. CSS selectors are used as a fast path when
# available (from KB segment recordings), with vision as the fallback.

def _screenshot_b64(page: Page) -> str:
    """Take a screenshot and return as base64."""
    raw = page.screenshot(type="png")
    return base64.b64encode(raw).decode()


def _gemini_locate(screenshot_b64: str, description: str) -> dict | None:
    """
    Ask Gemini Vision: where is the element described by `description`?
    Returns {"x": int, "y": int} or None.
    """
    prompt = (
        f"You are a browser automation assistant. Look at this screenshot and find the element: "
        f'"{description}". '
        f"Return ONLY a JSON object with the EXACT pixel coordinates at its center: "
        f'{{\"x\": <int>, \"y\": <int>}}. '
        f"Do NOT include any other text, explanation, or markdown. "
        f"If the element is not visible, return {{\"x\": -1, \"y\": -1}}."
    )

    client = _genai()
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


def _vision_click(page: Page, description: str, run_id: str) -> None:
    """Locate a specific, known element via Gemini Vision and click it.
    Falls back to Playwright text search if vision fails.
    Use this for elements with a clear, unique description (e.g. 'Login button')."""
    screenshot_b64 = _screenshot_b64(page)
    coords = _gemini_locate(screenshot_b64, description)

    if coords:
        x, y = coords["x"], coords["y"]
        _write_step(run_id, f"👆 Klick auf ({x},{y})", description)
        page.mouse.click(x, y)
        page.wait_for_timeout(1000)
    else:
        # Fallback: try to find by text content
        _write_step(run_id, "⚠️ Position nicht erkannt, versuche Text-Suche", description)
        try:
            page.get_by_text(description, exact=False).first.click(timeout=5000)
        except Exception:
            raise RuntimeError(
                f"Element nicht gefunden: '{description}'. Kein Klick möglich."
            )


def _vision_find_click(page: Page, description: str, run_id: str, dry_run: bool = False) -> None:
    """
    HYBRID APPROACH – the key differentiator of this agent.

    Unlike _vision_click (which locates a specific known element), find_click
    performs an intelligent visual SEARCH: it asks Gemini to find the BEST
    matching element on the page, even without an exact text match.

    This is critical for booking scenarios where the agent needs to find:
    - The first AVAILABLE time slot (ignoring grayed-out ones)
    - The nearest slot >= target time (e.g. 'first free slot from 11:00')
    - A specific option in a visual grid (not a standard <select>)

    The prompt is in German to match the booking portal language.
    """
    screenshot_b64 = _screenshot_b64(page)
    _write_step(run_id, f"🔎 Suche: {description}", "Analysiere Seite...")

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
        response = _genai().models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=base64.b64decode(screenshot_b64), mime_type="image/png"),
                types.Part.from_text(text=prompt),
            ],
        )
        _re = re  # use top-level import
        raw = response.text.strip()
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


def _check_if_input(condition: str, input_values: dict, run_id: str) -> None:
    """
    Evaluates a simple condition against input_values.
    Syntax: "{variable} == value"  or  "{variable} != value"
    If condition is NOT met, raises AbortStepError to skip remaining steps.
    """
    _re = re  # use top-level import
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



def _looks_like_date(value: str) -> bool:
    """Return True if value already looks like a concrete date (DD.MM.YYYY, YYYY-MM-DD etc.)."""
    # re is imported at module level
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
    # datetime imported at module level
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

    # datetime imported at module level
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



def _vision_fill(page: Page, description: str, value: str, run_id: str) -> None:
    """Take a screenshot, ask Gemini where the input is, then fill it."""
    screenshot_b64 = _screenshot_b64(page)
    coords = _gemini_locate(screenshot_b64, description)

    _SENSITIVE_KEYWORDS = ("password", "passwort", "kennwort", "secret", "pin", "token")
    display_value = "***" if any(k in description.lower() for k in _SENSITIVE_KEYWORDS) else value

    if coords:
        x, y = coords["x"], coords["y"]
        _write_step(run_id, f"⌨️ Eingabe in ({x},{y})", f"{description}: {display_value}")
        page.mouse.click(x, y)
        page.wait_for_timeout(300)
        # Select all and type
        page.keyboard.press("Control+a")
        page.keyboard.type(value, delay=30)
    else:
        raise RuntimeError(
            f"Eingabefeld nicht gefunden: '{description}'"
        )


def _vision_select(page: Page, description: str, value: str, run_id: str) -> None:
    """Click a dropdown via Gemini Vision, then select the option by value/label."""
    screenshot_b64 = _screenshot_b64(page)
    coords = _gemini_locate(screenshot_b64, description)
    if not coords:
        raise RuntimeError(f"Dropdown nicht gefunden: '{description}'")

    x, y = coords["x"], coords["y"]
    _write_step(run_id, f"📋 Dropdown ({x},{y})", f"{description} → {value}")

    # Try native <select> first (Playwright can select by label)
    try:
        page.mouse.click(x, y)
        page.wait_for_timeout(400)
        page.locator("select").first.select_option(label=value)
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
        coords2 = _gemini_locate(shot2, value)
        if coords2:
            page.mouse.click(coords2["x"], coords2["y"])
        else:
            raise RuntimeError(f"Option '{value}' nicht gefunden im Dropdown")


def _vision_hover(page: Page, description: str, run_id: str) -> None:
    """Hover over an element located by Gemini Vision (to reveal sub-menus etc.)."""
    screenshot_b64 = _screenshot_b64(page)
    coords = _gemini_locate(screenshot_b64, description)
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
    coords = _gemini_locate(screenshot_b64, description)
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
    Returns a human-readable result string.
    """
    prompt = (
        f'Look at this screenshot. '
        f'Is this text or element visible: "{indicator}"? '
        f'Answer with a short German summary of what you see on the screen. '
        f'Start with "✅ Erfolgreich:" if the success indicator is visible, '
        f'or "⚠️ Unklar:" if it is not clearly visible.'
    )

    client = _genai()
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
    # datetime imported at module level
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
