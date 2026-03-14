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
		Mode      string   `json:"mode"`       // "compile" or "decompile"
		Text      string   `json:"text"`       // Input text
		InputKeys []string `json:"inputKeys"` // Existing expectedInput keys to reuse
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
		result, warnings, err = compileNLToCommands(ctx, text, req.Data.InputKeys, model, location)
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

- find_slot: <Zielzeit> -> <Späteste Zeit>
  Findet und klickt den nächsten verfügbaren buchbaren Zeitslot in einer Timetable.
  Sucht die erste sichtbare, buchbare Zeit >= Zielzeit und <= Späteste Zeit.
  Verwendet data-time HTML-Attribute der Tabellen-Zeilen.
  Beispiel: find_slot: {target_time} -> {max_time}
  Beispiel: find_slot: 08:00 -> 10:00

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

## Autonomer Modus
- autonomous: <Natürlichsprachiges Ziel>
  Startet einen autonomen Modus: Gemini analysiert den Seiteninhalt als Text und entscheidet selbst,
  welche Aktionen (Klicks, Eingaben, Scrolling) nötig sind um das angegebene Ziel zu erreichen.
  Max 15 Schritte. Optional: Zahl nach dem Ziel begrenzt die Schritte.
  Ideal für: komplexe Workflows auf unbekannten Websites, die sich schlecht als feste Schrittfolge definieren lassen.
  Beispiel: autonomous: Buche eine Startzeit am {target_date} um {target_time}
  Beispiel: autonomous: Finde den günstigsten Flug von München nach Berlin

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

## Mehrzeiliger Code (>>> / <<<)
- Für mehrzeilige js: oder playwright: Schritte verwende den Block-Delimiter:
  playwright: >>>
  target = '{target_time}'
  page.locator(f'tr[data-time="{target}"]').click()
  <<<
- Alles zwischen >>> und <<< wird als ein einziger Schritt interpretiert.
- WICHTIG: Mehrzeilige Blöcke dürfen beim Übersetzen NICHT verändert werden!
  Sie enthalten exakten Code der 1:1 erhalten bleiben muss.
- Wenn du einen mehrzeiligen Block siehst, gib ihn exakt so zurück.

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

// ─── Code Block Preservation ─────────────────────────────────────────────────

const codeBlockSeparator = "───── Code-Blöcke (nicht editieren) ─────"

// extractCodeBlocks replaces >>> / <<< blocks in simple-mode text with ⚙️[N]
// markers and returns the cleaned text plus the extracted blocks.
// Each block includes its action prefix (e.g. "!playwright: >>>").
func extractCodeBlocks(text string) (cleaned string, blocks []string) {
	lines := strings.Split(text, "\n")
	var out []string
	var current []string
	inBlock := false
	blockIdx := 0

	for _, line := range lines {
		if inBlock {
			current = append(current, line)
			if strings.TrimSpace(line) == "<<<" {
				inBlock = false
				blocks = append(blocks, strings.Join(current, "\n"))
				current = nil
			}
			continue
		}
		// Check for >>> at end of line (start of multiline block)
		trimmed := strings.TrimSpace(line)
		if strings.HasSuffix(trimmed, ">>>") {
			inBlock = true
			blockIdx++
			// Keep the action prefix (e.g. "!playwright:") as part of the block
			current = []string{line}
			// Replace entire block start line with marker
			// Preserve ! prefix if present
			prefix := ""
			if strings.HasPrefix(trimmed, "!") {
				prefix = "! "
			}
			out = append(out, fmt.Sprintf("%s⚙️[%d]", prefix, blockIdx))
			continue
		}
		out = append(out, line)
	}

	cleaned = strings.Join(out, "\n")
	return
}

// formatCodeBlockSection creates the bottom section with code blocks.
func formatCodeBlockSection(blocks []string) string {
	if len(blocks) == 0 {
		return ""
	}
	var sb strings.Builder
	sb.WriteString("\n\n" + codeBlockSeparator + "\n")
	for i, block := range blocks {
		sb.WriteString(fmt.Sprintf("⚙️[%d]:\n", i+1))
		sb.WriteString(block)
		if i < len(blocks)-1 {
			sb.WriteString("\n")
		}
	}
	return sb.String()
}

// splitCodeBlockSection splits NL text into the main text and extracted code blocks.
// Returns the main text (without separator) and a map of block index → block content.
func splitCodeBlockSection(text string) (mainText string, blocks map[int]string) {
	blocks = make(map[int]string)
	sepIdx := strings.Index(text, codeBlockSeparator)
	if sepIdx == -1 {
		return text, blocks
	}
	mainText = strings.TrimSpace(text[:sepIdx])
	bottom := text[sepIdx+len(codeBlockSeparator):]

	// Parse ⚙️[N]: sections
	var currentIdx int
	var currentLines []string

	for _, line := range strings.Split(bottom, "\n") {
		trimmed := strings.TrimSpace(line)
		// Match ⚙️[N]:
		if strings.HasPrefix(trimmed, "⚙️[") && strings.HasSuffix(trimmed, ":") {
			// Save previous block
			if currentIdx > 0 && len(currentLines) > 0 {
				blocks[currentIdx] = strings.Join(currentLines, "\n")
			}
			// Parse index
			idxStr := trimmed[len("⚙️[") : len(trimmed)-2] // between [ and ]:
			fmt.Sscanf(idxStr, "%d", &currentIdx)
			currentLines = nil
			continue
		}
		if currentIdx > 0 {
			currentLines = append(currentLines, line)
		}
	}
	// Save last block
	if currentIdx > 0 && len(currentLines) > 0 {
		blocks[currentIdx] = strings.Join(currentLines, "\n")
	}
	return
}

// restoreCodeBlocks replaces ⚙️[N] markers in compiled output with original code blocks.
func restoreCodeBlocks(compiled string, blocks map[int]string) string {
	result := compiled
	for idx, block := range blocks {
		marker := fmt.Sprintf("⚙️[%d]", idx)
		// Also handle with # comment prefix and ! prefix
		for _, prefix := range []string{"", "# ", "! ", "!"} {
			full := prefix + marker
			if strings.Contains(result, full) {
				result = strings.Replace(result, full, block, 1)
				break
			}
		}
	}
	return result
}

// ─── Compile: NL → Commands ──────────────────────────────────────────────────

func compileNLToCommands(ctx context.Context, naturalText string, inputKeys []string, model, location string) (string, []string, error) {
	// Extract preserved code blocks from NL text (appended at bottom by decompile)
	mainText, codeBlocks := splitCodeBlockSection(naturalText)

	// Build instruction to reuse existing input keys
	inputKeysHint := ""
	if len(inputKeys) > 0 {
		inputKeysHint = fmt.Sprintf(`
12. WICHTIG – Bestehende Platzhalter-Namen: Der Skill hat bereits diese Input-Keys definiert:
    %s
    Du MUSST diese EXAKTEN Platzhalter-Namen verwenden (Groß-/Kleinschreibung und Unterstriche beachten)!
    Erfinde KEINE neuen Namen. Beispiel: Wenn {email} definiert ist, verwende {email} statt {username}.
    Wenn {datum} definiert ist, verwende {datum} statt {target_date}.
    Wenn {passwort} definiert ist, verwende {passwort} statt {password}.
    Nur wenn kein passender bestehender Platzhalter existiert, darfst du einen neuen erstellen.`,
			strings.Join(inputKeys, ", "))
	}

	prompt := fmt.Sprintf(`Übersetze den folgenden natürlichsprachigen deutschen Text in Browser-Skill-Befehle.

Regeln:
1. Setze den Originalsatz als # Kommentar VOR jeden übersetzten Befehl
2. Verwende nur die dokumentierten Befehle aus der Wissensbasis
3. Wenn ein Benutzer-Input nötig ist, verwende {platzhalter}
4. Gib NUR die übersetzten Befehle zurück, keinen einleitenden oder abschließenden Text
5. Wenn ein Schritt unklar ist, übersetze ihn trotzdem bestmöglich und füge einen Kommentar mit ⚠️ hinzu
6. PRIORITÄT: Verwende IMMER einfache Befehle (click, input, select) wenn möglich!
   - "Klicke auf das Login-Feld" → click: #login (NICHT js: document.querySelector...)
   - "Gib die E-Mail ein" → input: #login -> {email} (NICHT js: ...value = ...)
   - "Wähle das Datum aus" → select: #date -> {datum} (NICHT js: ...select_option...)
   Verwende js:/playwright: NUR wenn der Text EXPLIZIT "JavaScript", "querySelector" oder "Playwright" erwähnt.
7. Element-Referenzen wie '#login', '#password' sind CSS-Selektoren für click:/input:/select: Befehle.
   Beispiel: "Klicke auf '#login'" → click: #login
   Beispiel: "Gib {email} in '#login' ein" → input: #login -> {email}
8. SANDBOX-BYPASS: Es gibt zwei Notationen – verwende die jeweils passende:
   a) Sätze die mit ! beginnen → ALLE daraus generierten Befehle MÜSSEN mit ! beginnen.
      Beispiel: "! Klicke auf Buchung bestätigen" → "!click: Buchung bestätigen"
   b) [ECHTMODUS] am Anfang eines Absatzes → Gilt für den gesamten Absatz bis zur nächsten Leerzeile.
   c) Sätze OHNE ! bleiben normale Befehle (ohne ! Prefix).
   WICHTIG: Übernimm die ! Markierung EXAKT. Wenn ein Satz mit ! beginnt, MUSS jeder generierte Befehl auch mit ! beginnen.
9. Wartezeiten mit exakten Sekunden übernehmen
10. Wenn der Text EXPLIZIT von JavaScript oder querySelector spricht, verwende js: Befehle.
    Ansonsten verwende IMMER die einfachen Befehle (click, input, select, etc.).
11. Zeilen mit ⚙️[N] Markern sind Code-Block-Referenzen. Gib sie EXAKT als eigene Zeile zurück (mit ! Prefix falls vorhanden).
    Beispiel: "! ⚙️[1]" → "! ⚙️[1]" (unverändert übernehmen)%s

Text:
%s`, inputKeysHint, mainText)

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
	result = stripCodeFences(result)

	// Restore code blocks: replace ⚙️[N] markers with original code
	if len(codeBlocks) > 0 {
		result = restoreCodeBlocks(result, codeBlocks)
	}

	// Lint pass: validate each non-comment, non-empty line
	warnings := lintSkillCommands(result)

	return result, warnings, nil
}

// ─── Decompile: Commands → NL ────────────────────────────────────────────────

func decompileCommandsToNL(ctx context.Context, commands, model, location string) (string, error) {
	// Step 1: Extract >>> / <<< code blocks before Gemini processes them
	cleanedCommands, codeBlocks := extractCodeBlocks(commands)

	// Strip comment lines before sending to Gemini
	var filtered []string
	for _, line := range strings.Split(cleanedCommands, "\n") {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			continue
		}
		filtered = append(filtered, line)
	}
	cleanCommands := strings.Join(filtered, "\n")
	if cleanCommands == "" {
		return "", nil
	}

	// Build marker hint for the prompt if there are code blocks
	markerHint := ""
	if len(codeBlocks) > 0 {
		markerHint = fmt.Sprintf(`
9. CODE-BLOCK-MARKER: Zeilen mit ⚙️[N] sind Referenzen auf mehrzeilige Code-Blöcke.
   Beschreibe kurz was der Code-Block tut und behalte den Marker ⚙️[N] am Ende der Zeile.
   Beispiel: "! ⚙️[1]" → "! Per Playwright wird in einem mehrzeiligen Code-Block der passende Zeitslot gesucht. ⚙️[1]"
   WICHTIG: Der Marker ⚙️[N] MUSS in deiner Ausgabe vorkommen, exakt so wie im Input.`)
	}

	// Gemini path: generate readable German text
	prompt := fmt.Sprintf(`Übersetze die folgenden Browser-Skill-Befehle in natürlichen, verständlichen deutschen Text.

Regeln:
1. Schreibe EINEN Satz pro logischem Schritt, jeweils in einer neuen Zeile.
   Gruppiere zusammengehörige Schritte in Absätze (Leerzeile dazwischen).
2. Beschreibe was der Skill tut, als würde man es einem Menschen erklären
3. Erwähne Platzhalter wie {username} als "der Benutzername" etc.
4. Gib NUR den deutschen Text zurück, keine Befehle oder Formatierung
5. EINFACHE BEFEHLE einfach beschreiben – OHNE JavaScript/querySelector/Playwright zu erwähnen:
   - "click: #login" → "Klicke auf das Login-Feld."
   - "input: #login -> {email}" → "Gib die E-Mail-Adresse in das Login-Feld ein."
   - "select: #date -> {datum}" → "Wähle das Datum im Datumsfeld aus."
   - "click: button:has-text('Login')" → "Klicke auf den Login-Button."
   - "click: a:has-text('Startzeit')" → "Klicke auf den Link 'Startzeit'."
   CSS-Selektoren wie #login, #password sind Feld-IDs – beschreibe sie als "das Login-Feld", "das Passwort-Feld" etc.
   Erwähne KEINE querySelector, getElementById etc. für einfache click/input/select Befehle!
6. NUR für js: und playwright: Befehle technische Details bewahren:
   - Element-IDs, CSS-Selektoren, Playwright-Methoden, JavaScript-Code
7. SANDBOX-BYPASS: Befehle mit ! Prefix sind Echtmodus-Befehle.
   Beginne jeden Satz, der aus einem !-Befehl generiert wird, ebenfalls mit !
   Beispiel: "!click: Buchung bestätigen" → "! Klicke auf den Button 'Buchung bestätigen'."
   Befehle OHNE ! sind normale Befehle und beginnen normal ohne !.
8. Wartezeiten mit exakten Sekunden angeben%s

Beispiele:
- "click: #login" → "Klicke auf das Login-Feld."
- "input: #login -> {email}" → "Gib die E-Mail-Adresse in das Login-Feld ein."
- "input: #password -> {passwort}" → "Gib das Passwort in das Passwort-Feld ein."
- "click: button:has-text('Login')" → "Klicke auf den Login-Button."
- "select: #timetable_selection_date -> {datum}" → "Wähle das gewünschte Datum im Datum-Dropdown aus."
- "!playwright: page.get_by_role(\"link\", name=\"Startzeit buchen\").click()"
  → "! Per Playwright wird auf den Link 'Startzeit buchen' geklickt."

Befehle:
%s`, markerHint, cleanCommands)

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

	nlText := strings.TrimSpace(extractText(resp))

	// Append code blocks at the bottom so they survive the roundtrip
	if len(codeBlocks) > 0 {
		nlText += formatCodeBlockSection(codeBlocks)
	}

	return nlText, nil
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
// Skips lines inside multiline blocks (>>> / <<<).
func lintSkillCommands(text string) []string {
	known := map[string]bool{
		"click": true, "input": true, "eingabe": true, "select": true, "auswählen": true,
		"press": true, "taste": true, "scroll": true, "hover": true, "checkbox": true,
		"wait_for": true, "warte_auf": true, "wait": true, "warte": true,
		"navigate": true, "gehe": true, "abort_if": true, "abbruch_wenn": true,
		"find_click": true, "suche_klick": true, "try_click": true, "versuch_klick": true,
		"find_slot": true, "slot_suchen": true,
		"js": true, "playwright": true, "screenshot": true, "if_input": true, "wenn_eingabe": true,
		"autonomous": true, "autonom": true,
	}

	var warnings []string
	inBlock := false
	for i, rawLine := range strings.Split(text, "\n") {
		line := strings.TrimSpace(rawLine)
		// Handle multiline block delimiters
		if line == "<<<" {
			inBlock = false
			continue
		}
		if inBlock {
			continue // skip lines inside >>> / <<< blocks
		}
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
		// Check if the rest contains >>> (start of multiline block)
		rest := strings.TrimSpace(line[colonIdx+1:])
		if strings.HasSuffix(rest, ">>>") {
			inBlock = true
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
