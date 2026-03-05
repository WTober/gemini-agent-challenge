package function

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"

	"github.com/GoogleCloudPlatform/functions-framework-go/functions"
	"google.golang.org/genai"
)

func init() {
	functions.HTTP("CompileSkill", CompileSkill)
}

// ──────────────────────────────────────────────────────────────────────────────
// CompileSkill – Bidirectional NL ↔ Skill Command Compiler
//
// Mode "compile":   German natural language → Simple-Mode commands
// Mode "decompile": Simple-Mode commands → German natural language
// ──────────────────────────────────────────────────────────────────────────────

type compileSkillRequest struct {
	Data struct {
		Mode string `json:"mode"` // "compile" or "decompile"
		Text string `json:"text"` // Input text
	} `json:"data"`
}

type compileSkillResponse struct {
	Result   string   `json:"result"`
	Warnings []string `json:"warnings,omitempty"`
}

func CompileSkill(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	if r.Method == http.MethodOptions {
		w.Header().Set("Access-Control-Allow-Methods", "POST")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
		w.WriteHeader(http.StatusNoContent)
		return
	}

	ctx := r.Context()

	var req compileSkillRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, `{"error":"invalid JSON"}`, http.StatusBadRequest)
		return
	}

	mode := strings.ToLower(strings.TrimSpace(req.Data.Mode))
	text := strings.TrimSpace(req.Data.Text)

	if text == "" {
		http.Error(w, `{"error":"text is required"}`, http.StatusBadRequest)
		return
	}

	// Load model config from AppConfig (admin-configurable)
	model, location := getCompilerModelConfig(ctx)

	var result string
	var warnings []string
	var err error

	switch mode {
	case "compile":
		result, warnings, err = compileNLToCommands(ctx, text, model, location)
	case "decompile":
		result, err = decompileCommandsToNL(ctx, text, model, location)
	default:
		http.Error(w, `{"error":"mode must be 'compile' or 'decompile'"}`, http.StatusBadRequest)
		return
	}

	if err != nil {
		log.Printf("CompileSkill [%s]: Error: %v", mode, err)
		http.Error(w, fmt.Sprintf(`{"error":"%s"}`, err.Error()), http.StatusInternalServerError)
		return
	}

	resp := compileSkillResponse{Result: result, Warnings: warnings}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{"data": resp})
}

// getCompilerModelConfig reads model/location from AppConfig with fallback.
func getCompilerModelConfig(ctx context.Context) (string, string) {
	config := LoadAppConfig(ctx)
	return config.SkillCompilerModel, config.SkillCompilerLocation
}

// ─── Knowledge Base (System Prompt) ──────────────────────────────────────────

const skillKnowledgeBase = `Du bist ein Compiler für Browser-Automatisierungs-Skills.
Du übersetzt zwischen natürlicher deutscher Sprache und strukturierten Skill-Befehlen.

# Verfügbare Befehle (Simple-Mode-Format)

Jede Zeile hat das Format: aktion: parameter

## Klick-Aktionen
- click: <Beschreibung des Elements>
  Klickt auf ein visuell beschriebenes Element. Gemini Vision lokalisiert es auf dem Screenshot.
  Beispiel: click: Login-Button
  Beispiel: click: Menü-Eintrag "Startzeiten"

- find_click: <Beschreibung>
  Intelligente visuelle Suche: findet das ERSTE passende/verfügbare Element und klickt darauf.
  Ideal für: erste freie Startzeit, erster verfügbarer Slot.
  Beispiel: find_click: erster freier Zeitslot ab 10:00 Uhr mit mindestens 2 freien Plätzen

- try_click: <Beschreibung>
  Wie find_click, aber wenn das Element nicht gefunden wird, läuft der Skill weiter (kein Fehler).
  Ideal für optionale Elemente wie Cookie-Banner.
  Beispiel: try_click: Cookie-Banner akzeptieren

## Eingabe-Aktionen
- input: <Feld-Beschreibung> -> <Wert oder {platzhalter}>
  Gibt Text in ein Eingabefeld ein. {platzhalter} werden durch Benutzereingaben ersetzt.
  Beispiel: input: E-Mail-Feld -> {username}
  Beispiel: input: Passwort-Feld -> {password}
  Beispiel: input: Datumsfeld -> {target_date}

- select: <Dropdown-Beschreibung> -> <Option oder {platzhalter}>
  Wählt eine Option in einem Dropdown-Menü aus.
  Beispiel: select: Anzahl Spieler -> {player_count}

## Tastatur & Navigation
- press: <Taste>
  Drückt eine Taste. Verfügbare Tasten: Enter, Tab, Escape, ArrowDown, ArrowUp, etc.
  Beispiel: press: Enter

- navigate: <URL>
  Navigiert zu einer URL.
  Beispiel: navigate: https://www.example.com/login

- scroll: <Pixel>
  Scrollt die Seite. Positive Werte = nach unten, negative = nach oben.
  Beispiel: scroll: 300

- hover: <Element-Beschreibung>
  Bewegt die Maus über ein Element (ohne Klick).
  Beispiel: hover: Dropdown-Menü "Plätze"

## Warten & Prüfen
- wait: <Sekunden>
  Wartet eine feste Anzahl Sekunden.
  Beispiel: wait: 3

- wait_for: <Element-Beschreibung>
  Wartet bis ein bestimmtes Element auf der Seite sichtbar wird.
  Beispiel: wait_for: Tabelle mit Startzeiten

- abort_if: <Bedingung>
  Bricht den Skill ab, wenn eine visuelle Bedingung erfüllt ist (Gemini prüft Screenshot).
  Beispiel: abort_if: Meldung "Platz gesperrt" ist sichtbar

- if_input: {variable} == wert
  Bedingte Ausführung: Wenn die Bedingung NICHT erfüllt ist, wird der Rest übersprungen.
  Beispiel: if_input: {player_count} == 2

## Formular-Elemente
- checkbox: <Checkbox-Beschreibung>
  Schaltet eine Checkbox um.
  Beispiel: checkbox: AGBs akzeptieren

## Erweiterte Aktionen
- js: <JavaScript-Code>
  Führt beliebiges JavaScript auf der Seite aus. {platzhalter} werden ersetzt.
  Beispiel: js: document.querySelector('#date-picker').value = '{target_date}'

- playwright: <Playwright-API-Aufruf>
  Führt einen direkten Playwright-API-Aufruf aus (Python-Syntax).
  Beispiel: playwright: page.locator('#login-form input[name="email"]').fill('{username}')
  Beispiel: playwright: page.get_by_role("button", name="Anmelden").click()
  Beispiel: playwright: page.locator('[data-date="DAY|{target_date}"]').click()

- screenshot: <Label>
  Erstellt einen Debug-Screenshot mit optionalem Label.
  Beispiel: screenshot: Nach Login

# Spezielle Syntax

## Platzhalter
- {variablenname} wird durch Benutzereingaben ersetzt
- Häufige Platzhalter: {username}, {password}, {target_date}, {target_time}, {player_count}

## Kommentare
- Zeilen die mit # beginnen sind Kommentare und werden ignoriert
- Verwende Kommentare um den natürlichsprachigen Originaltext zu dokumentieren

## Sandbox-Bypass
- Zeilen die mit ! beginnen werden auch im Sandbox-Modus echt ausgeführt
- Beispiel: !click: Buchung bestätigen

# Konventionen
- Beschreibungen sollen VISUELL sein (was man auf dem Bildschirm sieht)
- Für Datumsfelder: Benutzer können "morgen", "nächsten Montag" etc. eingeben - wird automatisch aufgelöst
- Immer erst navigieren, dann interagieren
- Nach Klicks kurz warten (wird automatisch eingefügt, kein explizites wait nötig)
- Passwörter immer als {password} Platzhalter, nie als Klartext
`

// ─── Compile: NL → Commands ──────────────────────────────────────────────────

func compileNLToCommands(ctx context.Context, naturalText, model, location string) (string, []string, error) {
	prompt := fmt.Sprintf(`Übersetze den folgenden natürlichsprachigen deutschen Text in Browser-Skill-Befehle.

Regeln:
1. Setze den Originalsatz als # Kommentar VOR jeden übersetzten Befehl
2. Verwende nur die dokumentierten Befehle aus der Wissensbasis
3. Wenn ein Benutzer-Input nötig ist, verwende {platzhalter}
4. Gib NUR die übersetzten Befehle zurück, keinen einleitenden oder abschließenden Text
5. Wenn ein Schritt unklar ist, übersetze ihn trotzdem bestmöglich und füge einen Kommentar mit ⚠️ hinzu

Text:
%s`, naturalText)

	aiClient, err := genai.NewClient(ctx, &genai.ClientConfig{
		Project:  ProjectID,
		Location: location,
		Backend:  genai.BackendVertexAI,
	})
	if err != nil {
		return "", nil, fmt.Errorf("genai client: %w", err)
	}

	resp, err := aiClient.Models.GenerateContent(ctx, model, genai.Text(prompt), &genai.GenerateContentConfig{
		SystemInstruction: genai.NewContentFromText(skillKnowledgeBase, "user"),
		Temperature:       ptrFloat(0.2),
	})
	if err != nil {
		return "", nil, fmt.Errorf("generate: %w", err)
	}

	result := extractText(resp)
	// Strip markdown code fences if Gemini wrapped the output
	result = stripCodeFences(result)

	// Lint pass: validate each non-comment, non-empty line
	warnings := lintSkillCommands(result)

	return result, warnings, nil
}

// ─── Decompile: Commands → NL ────────────────────────────────────────────────

func decompileCommandsToNL(ctx context.Context, commands, model, location string) (string, error) {
	// Fast path: extract existing NL comments if available
	if nlText := extractNLComments(commands); nlText != "" {
		return nlText, nil
	}

	// Gemini path: generate readable German text
	prompt := fmt.Sprintf(`Übersetze die folgenden Browser-Skill-Befehle in natürlichen, verständlichen deutschen Text.

Regeln:
1. Schreibe flüssigen, zusammenhängenden Text (keine Aufzählung)
2. Beschreibe was der Skill tut, als würde man es einem Menschen erklären
3. Erwähne Platzhalter wie {username} als "der Benutzername" etc.
4. Gib NUR den deutschen Text zurück, keine Befehle oder Formatierung
5. Playwright-Befehle sollen ebenfalls in lesbare Sprache übersetzt werden

Befehle:
%s`, commands)

	aiClient, err := genai.NewClient(ctx, &genai.ClientConfig{
		Project:  ProjectID,
		Location: location,
		Backend:  genai.BackendVertexAI,
	})
	if err != nil {
		return "", fmt.Errorf("genai client: %w", err)
	}

	resp, err := aiClient.Models.GenerateContent(ctx, model, genai.Text(prompt), &genai.GenerateContentConfig{
		SystemInstruction: genai.NewContentFromText(skillKnowledgeBase, "user"),
		Temperature:       ptrFloat(0.3),
	})
	if err != nil {
		return "", fmt.Errorf("generate: %w", err)
	}

	return strings.TrimSpace(extractText(resp)), nil
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

func extractNLComments(commands string) string {
	var comments []string
	for _, line := range strings.Split(commands, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "#") {
			comment := strings.TrimSpace(strings.TrimPrefix(trimmed, "#"))
			// Skip tool-generated markers
			if comment == "" || strings.HasPrefix(comment, "⚠️") {
				continue
			}
			comments = append(comments, comment)
		}
	}
	if len(comments) == 0 {
		return ""
	}
	return strings.Join(comments, ". ") + "."
}

// lintSkillCommands validates each line against known actions.
func lintSkillCommands(text string) []string {
	known := map[string]bool{
		"click": true, "input": true, "eingabe": true, "select": true, "auswählen": true,
		"press": true, "taste": true, "scroll": true, "hover": true, "checkbox": true,
		"wait_for": true, "warte_auf": true, "wait": true, "warte": true,
		"navigate": true, "gehe": true, "abort_if": true, "abbruch_wenn": true,
		"find_click": true, "suche_klick": true, "try_click": true, "versuch_klick": true,
		"js": true, "playwright": true, "screenshot": true, "if_input": true, "wenn_eingabe": true,
	}

	var warnings []string
	for i, rawLine := range strings.Split(text, "\n") {
		line := strings.TrimSpace(rawLine)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		// Strip sandbox bypass prefix
		if strings.HasPrefix(line, "!") {
			line = strings.TrimSpace(line[1:])
		}
		colonIdx := strings.Index(line, ":")
		if colonIdx == -1 {
			warnings = append(warnings, fmt.Sprintf("Zeile %d: Kein Befehl erkannt (fehlt ':'): %s", i+1, line))
			continue
		}
		action := strings.ToLower(strings.TrimSpace(line[:colonIdx]))
		if !known[action] {
			warnings = append(warnings, fmt.Sprintf("Zeile %d: Unbekannter Befehl '%s'", i+1, action))
		}
	}
	return warnings
}

func stripCodeFences(s string) string {
	s = strings.TrimSpace(s)
	if strings.HasPrefix(s, "```") {
		lines := strings.Split(s, "\n")
		// Remove first line (```lang) and last line (```)
		if len(lines) > 2 {
			end := len(lines) - 1
			if strings.TrimSpace(lines[end]) == "```" {
				lines = lines[1:end]
			} else {
				lines = lines[1:]
			}
		}
		s = strings.Join(lines, "\n")
	}
	return strings.TrimSpace(s)
}

func extractText(resp *genai.GenerateContentResponse) string {
	if resp == nil || len(resp.Candidates) == 0 || resp.Candidates[0].Content == nil {
		return ""
	}
	var texts []string
	for _, part := range resp.Candidates[0].Content.Parts {
		if part.Text != "" {
			texts = append(texts, part.Text)
		}
	}
	return strings.Join(texts, "")
}

func ptrFloat(f float32) *float32 {
	return &f
}
