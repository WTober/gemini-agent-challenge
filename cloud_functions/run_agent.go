package function

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"cloud.google.com/go/firestore"
	"github.com/GoogleCloudPlatform/functions-framework-go/functions"
	"google.golang.org/api/iterator"
)

func init() {
	functions.HTTP("RunUserAgent", RunUserAgent)
	functions.HTTP("GetUserAgents", GetUserAgents)
	functions.HTTP("SaveUserAgent", SaveUserAgent)
	functions.HTTP("DeleteUserAgent", DeleteUserAgent)
	functions.HTTP("GetAgentRunLog", GetAgentRunLog)
}

// AgentRunStep is a single step in the live execution log.
type AgentRunStep struct {
	Timestamp time.Time `firestore:"timestamp" json:"timestamp"`
	Action    string    `firestore:"action" json:"action"`
	Detail    string    `firestore:"detail" json:"detail"`
	IsError   bool      `firestore:"isError" json:"isError"`
}

// appendStep appends a step to an agent_runs document atomically.
func appendStep(ctx context.Context, fsClient *firestore.Client, runID string, step AgentRunStep) {
	_, err := fsClient.Collection("agent_runs").Doc(runID).Update(ctx, []firestore.Update{
		{Path: "steps", Value: firestore.ArrayUnion(map[string]interface{}{
			"timestamp": step.Timestamp,
			"action":    step.Action,
			"detail":    step.Detail,
			"isError":   step.IsError,
		})},
	})
	if err != nil {
		log.Printf("appendStep [%s]: %v", runID, err)
	}
}

// setRunStatus finalises a run in Firestore.
func setRunStatus(ctx context.Context, fsClient *firestore.Client, runID, status, errMsg string) {
	updates := []firestore.Update{
		{Path: "status", Value: status},
		{Path: "completedAt", Value: time.Now()},
	}
	if errMsg != "" {
		updates = append(updates, firestore.Update{Path: "errorMsg", Value: errMsg})
	}
	_, _ = fsClient.Collection("agent_runs").Doc(runID).Update(ctx, updates)
}

// checkAndDeductAgentCredits uses a dynamic credit cost per skill.
func checkAndDeductAgentCredits(ctx context.Context, uid string, skillID string, creditCost int64) error {
	config := LoadAppConfig(ctx)

	fsClient, err := firestore.NewClient(ctx, ProjectID)
	if err != nil {
		return err
	}
	defer fsClient.Close()

	userRef := fsClient.Collection("users").Doc(uid)

	return fsClient.RunTransaction(ctx, func(ctx context.Context, tx *firestore.Transaction) error {
		doc, err := tx.Get(userRef)
		if err != nil {
			return fmt.Errorf("SUBSCRIPTION_REQUIRED")
		}
		data := doc.Data()

		subData, ok := data["subscription"].(map[string]interface{})
		if !ok {
			return fmt.Errorf("SUBSCRIPTION_REQUIRED")
		}

		isVip, _ := subData["isVip"].(bool)
		if isVip {
			log.Printf("checkAndDeductAgentCredits [%s - %s]: VIP bypass", uid, skillID)
			return nil
		}

		subStatus, _ := subData["status"].(string)
		if subStatus != "PRO" && subStatus != "BASIC" {
			return fmt.Errorf("SUBSCRIPTION_REQUIRED")
		}

		balance := int64(0)
		if b, ok := data["creditBalance"].(int64); ok {
			balance = b
		} else if b, ok := data["creditBalance"].(float64); ok {
			balance = int64(b)
		}

		isDryRun := config.CreditSystemDryRun
		log.Printf("checkAndDeductAgentCredits [%s - %s]: Balance=%d, Cost=%d, DryRun=%v", uid, skillID, balance, creditCost, isDryRun)

		if balance < creditCost {
			if isDryRun {
				return nil // Shadow mode: allow but log
			}
			return fmt.Errorf("QUOTA_EXCEEDED")
		}

		if isDryRun {
			return nil
		}

		updates := []firestore.Update{
			{Path: "creditBalance", Value: firestore.Increment(-creditCost)},
			{Path: "creditLastUsed", Value: time.Now()},
		}
		return tx.Update(userRef, updates)
	})
}

// RunUserAgent triggers an agent run for the authenticated user.
// POST body: { "data": { "agentId": "..." } }
func RunUserAgent(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	if r.Method == http.MethodOptions {
		w.Header().Set("Access-Control-Allow-Methods", "POST")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
		w.WriteHeader(http.StatusNoContent)
		return
	}

	uid, err := VerifyUser(w, r)
	if err != nil {
		http.Error(w, "Unauthorized", http.StatusUnauthorized)
		return
	}

	var wrapper struct {
		Data struct {
			AgentID string `json:"agentId"`
			DryRun  bool   `json:"dryRun"` // optional per-run sandbox override
		} `json:"data"`
	}
	if err := json.NewDecoder(r.Body).Decode(&wrapper); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}
	agentID := wrapper.Data.AgentID
	requestDryRun := wrapper.Data.DryRun
	if agentID == "" {
		http.Error(w, "agentId is required", http.StatusBadRequest)
		return
	}

	ctx := context.Background()
	fsClient, err := firestore.NewClient(ctx, ProjectID)
	if err != nil {
		log.Printf("RunUserAgent: Firestore init error: %v", err)
		http.Error(w, "Internal Server Error", http.StatusInternalServerError)
		return
	}
	defer fsClient.Close()

	// 1. Load user agent
	agentDoc, err := fsClient.Collection("users").Doc(uid).Collection("user_agents").Doc(agentID).Get(ctx)
	if err != nil {
		http.Error(w, "Agent not found", http.StatusNotFound)
		return
	}
	agentData := agentDoc.Data()
	agentName, _ := agentData["name"].(string)
	skillID, _ := agentData["skillId"].(string)
	goal, _ := agentData["goal"].(string)

	// 2. Load skill definition from config/agent_skills
	skillsDoc, err := fsClient.Collection("config").Doc("agent_skills").Get(ctx)
	if err != nil {
		http.Error(w, "Skills not configured", http.StatusInternalServerError)
		return
	}
	skillsData := skillsDoc.Data()
	rawSkills, _ := skillsData["skills"].([]interface{})

	var skill map[string]interface{}
	for _, s := range rawSkills {
		if sm, ok := s.(map[string]interface{}); ok {
			if sm["id"] == skillID {
				skill = sm
				break
			}
		}
	}
	if skill == nil {
		http.Error(w, fmt.Sprintf("Skill '%s' not found or inactive", skillID), http.StatusBadRequest)
		return
	}
	isActive, _ := skill["isActive"].(bool)
	if !isActive {
		http.Error(w, "Skill is not active", http.StatusBadRequest)
		return
	}

	skillName, _ := skill["name"].(string)
	skillType, _ := skill["skillType"].(string)
	if skillType == "" {
		skillType = "prompt"
	}
	creditCost := int64(getInt(skill["creditCost"], 500))

	// Load inputValues from the user agent document
	rawInputValues, _ := agentData["inputValues"].(map[string]interface{})
	inputValues := make(map[string]string)
	for k, v := range rawInputValues {
		if sv, ok := v.(string); ok {
			inputValues[k] = sv
		}
	}

	// Load user timezone (IANA ID, e.g. "Europe/Berlin", "Asia/Kuala_Lumpur")
	userDoc, _ := fsClient.Collection("users").Doc(uid).Get(ctx)
	userTZ := "Europe/Berlin" // Fallback
	userLang := "de"          // Fallback
	if userDoc != nil {
		if tz, ok := userDoc.Data()["timezone"].(string); ok && tz != "" {
			userTZ = tz
		}
		if isGerman, ok := userDoc.Data()["isGerman"].(bool); ok && !isGerman {
			userLang = "en"
		}
		if lang, ok := userDoc.Data()["language"].(string); ok && lang != "" {
			userLang = lang
		}
	}

	// 3. Deduct credits
	if err := checkAndDeductAgentCredits(ctx, uid, skillID, creditCost); err != nil {
		switch err.Error() {
		case "QUOTA_EXCEEDED":
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusPaymentRequired)
			w.Write([]byte(`{"error":{"message":"QUOTA_EXCEEDED"}}`))
		case "SUBSCRIPTION_REQUIRED":
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusForbidden)
			w.Write([]byte(`{"error":{"message":"SUBSCRIPTION_REQUIRED"}}`))
		default:
			http.Error(w, "Internal Server Error", http.StatusInternalServerError)
		}
		return
	}

	// Log credit usage asynchronously
	go func() {
		desc := fmt.Sprintf("Agent-Skill: %s – %s (%d Credits)", skillName, agentName, creditCost)
		_ = logCreditHistory(context.Background(), uid, CreditActionUsage, "agent_"+skillID, -creditCost, desc)
	}()

	// 4. Create agent_runs document
	runRef := fsClient.Collection("agent_runs").NewDoc()
	runID := runRef.ID
	now := time.Now()

	// Load retention config for expiresAt
	config := LoadAppConfig(ctx)
	retentionDays := getInt(config.AgentRunRetentionDays, 30)

	_, err = runRef.Set(ctx, map[string]interface{}{
		"agentId":   agentID,
		"agentName": agentName,
		"userId":    uid,
		"skillId":   skillID,
		"skillName": skillName,
		"skillType": skillType,
		"status":    "pending",
		"steps":     []interface{}{},
		"result":    "",
		"startedAt": now,
		"errorMsg":  "",
		"expiresAt": now.Add(time.Duration(retentionDays) * 24 * time.Hour),
	})
	if err != nil {
		log.Printf("RunUserAgent: Failed to create run doc: %v", err)
		http.Error(w, "Internal Server Error", http.StatusInternalServerError)
		return
	}

	// 5. Execute asynchronously
	go executeAgentRun(runID, uid, agentID, goal, skillType, inputValues, skill, requestDryRun, userTZ, userLang)

	// 6. Update lastRunAt
	_, _ = fsClient.Collection("users").Doc(uid).Collection("user_agents").Doc(agentID).Update(ctx, []firestore.Update{
		{Path: "lastRunAt", Value: now},
	})

	log.Printf("RunUserAgent: Started run %s (user=%s, agent=%s, skill=%s)", runID, uid, agentID, skillID)
	writeResponse(w, map[string]interface{}{"data": map[string]interface{}{"runId": runID, "status": "pending"}})
}

// executeAgentRun runs the agent asynchronously, routing to the appropriate executor.
func executeAgentRun(runID, uid, agentID, goal, skillType string, inputValues map[string]string, skill map[string]interface{}, requestDryRun bool, userTZ, userLang string) {
	ctx := context.Background()

	fsClient, err := firestore.NewClient(ctx, ProjectID)
	if err != nil {
		log.Printf("executeAgentRun [%s]: Firestore init error: %v", runID, err)
		return
	}
	defer fsClient.Close()

	defer func() {
		if r := recover(); r != nil {
			log.Printf("executeAgentRun [%s]: PANIC: %v", runID, r)
			setRunStatus(ctx, fsClient, runID, "error", fmt.Sprintf("Unexpected error: %v", r))
		}
	}()

	// Mark running
	_, _ = fsClient.Collection("agent_runs").Doc(runID).Update(ctx, []firestore.Update{
		{Path: "status", Value: "running"},
	})

	switch skillType {
	case "browser_automation":
		// dryRun: either set by admin on the skill OR per-request (user initiated sandbox run)
		skillDryRun, _ := skill["dryRun"].(bool)
		executeBrowserAutomationRun(ctx, fsClient, runID, uid, agentID, inputValues, skill, skillDryRun || requestDryRun)
	default:
		executePromptRun(ctx, fsClient, runID, goal, inputValues, skill, userTZ, userLang)
	}
}

// resolveInputDates replaces relative date keywords in inputValues
// with actual dates in the user's timezone. Supports: heute/today, morgen/tomorrow, übermorgen.
// Uses ISO 8601 + weekday format consistent with {TODAY}/{TOMORROW} (see Lessons Learned #19).
func resolveInputDates(inputValues map[string]string, loc *time.Location) map[string]string {
	now := time.Now().In(loc)

	dateKeywords := map[string]time.Time{
		"heute":      now,
		"today":      now,
		"morgen":     now.AddDate(0, 0, 1),
		"tomorrow":   now.AddDate(0, 0, 1),
		"übermorgen": now.AddDate(0, 0, 2),
	}

	for k, v := range inputValues {
		lower := strings.ToLower(strings.TrimSpace(v))
		if resolved, ok := dateKeywords[lower]; ok {
			inputValues[k] = formatDateForAI(resolved)
		}
	}
	return inputValues
}

// formatDateForAI returns a date in DD.MM.YYYY format with English weekday
// for unambiguous AI consumption. Example: "27.02.2026 (Friday)"
func formatDateForAI(t time.Time) string {
	return t.Format("02.01.2006") + " (" + t.Weekday().String() + ")"
}

// formatDateTimeForAI returns a datetime in DD.MM.YYYY HH:MM format with English weekday
// for unambiguous AI consumption. Example: "27.02.2026 20:01 (Friday)"
func formatDateTimeForAI(t time.Time) string {
	return t.Format("02.01.2006 15:04") + " (" + t.Weekday().String() + ")"
}

// executePromptRun calls Gemini and updates Firestore with the text result.
func executePromptRun(ctx context.Context, fsClient *firestore.Client, runID, goal string, inputValues map[string]string, skill map[string]interface{}, userTZ, userLang string) {
	appendStep(ctx, fsClient, runID, AgentRunStep{
		Timestamp: time.Now(),
		Action:    "Skill geladen",
		Detail:    fmt.Sprintf("Skill: %s", skill["name"]),
	})

	systemPrompt, _ := skill["systemPrompt"].(string)
	prompt := strings.ReplaceAll(systemPrompt, "{userGoal}", goal)

	// Load user timezone (fallback to Europe/Berlin)
	loc, err := time.LoadLocation(userTZ)
	if err != nil {
		log.Printf("executePromptRun [%s]: Invalid timezone %q, falling back to Europe/Berlin", runID, userTZ)
		loc, _ = time.LoadLocation("Europe/Berlin")
	}

	// Resolve relative date keywords in inputValues (heute → 2026-02-25 (Dienstag))
	inputValues = resolveInputDates(inputValues, loc)

	// Inject system variables available to all prompts (in user's timezone)
	// Uses ISO 8601 + weekday for unambiguous AI interpretation (see Lessons Learned #19)
	nowLocal := time.Now().In(loc)
	prompt = strings.ReplaceAll(prompt, "{TODAY}", formatDateForAI(nowLocal))
	prompt = strings.ReplaceAll(prompt, "{TOMORROW}", formatDateForAI(nowLocal.AddDate(0, 0, 1)))
	prompt = strings.ReplaceAll(prompt, "{NOW}", formatDateTimeForAI(nowLocal))

	// Prepend date context so the model knows what "today" is.
	// All tested models (gemini-2.5-flash, gemini-3-flash-preview) think it's 2024
	// due to training cutoff. Without this context, they refuse weather forecasts
	// for 2026 dates claiming "this date is far in the future".
	dateContext := fmt.Sprintf(
		"[SYSTEM CONTEXT: Today is %s. All mentioned dates are relative to today and are NOT in the far future. "+
			"STRICT RULES: When using search results, ONLY use exact data (numbers, temperatures, percentages) found in the search results. "+
			"NEVER invent or estimate data. If you cannot find exact data, say so honestly.]\n\n",
		formatDateTimeForAI(nowLocal),
	)
	prompt = dateContext + prompt

	// Replace all {key} placeholders from inputValues (e.g. {password}, {username})
	for k, v := range inputValues {
		prompt = strings.ReplaceAll(prompt, "{"+k+"}", v)
	}

	// Append output language instruction (system prompt stays German,
	// but the AI responds in the user's language)
	if userLang != "" && userLang != "de" {
		langName := "English"
		switch userLang {
		case "en":
			langName = "English"
		case "es":
			langName = "Spanish"
		case "fr":
			langName = "French"
		case "it":
			langName = "Italian"
		default:
			langName = userLang
		}
		prompt += fmt.Sprintf("\n\n[OUTPUT LANGUAGE: You MUST respond in %s. The instructions above are in German, but your entire response must be written in %s.]", langName, langName)
	}

	model, _ := skill["model"].(string)
	location, _ := skill["location"].(string)
	temp := float64(0.7)
	if t, ok := skill["temperature"].(float64); ok {
		temp = t
	}
	if model == "" {
		model = "gemini-2.0-flash-001"
	}
	if location == "" {
		location = "europe-west3"
	}

	// Check if skill needs Google Search Grounding for live data (e.g. weather, prices)
	useGrounding, _ := skill["useGrounding"].(bool)

	appendStep(ctx, fsClient, runID, AgentRunStep{
		Timestamp: time.Now(),
		Action:    "KI-Anfrage gesendet",
		Detail:    fmt.Sprintf("Modell: %s @ %s, Grounding: %v", model, location, useGrounding),
	})

	result, genErr := runGenAIAgentPrompt(ctx, prompt, model, location, float32(temp), useGrounding)
	if genErr != nil {
		log.Printf("executePromptRun [%s]: Gemini error: %v", runID, genErr)
		appendStep(ctx, fsClient, runID, AgentRunStep{
			Timestamp: time.Now(),
			Action:    "Fehler bei KI-Anfrage",
			Detail:    genErr.Error(),
			IsError:   true,
		})
		setRunStatus(ctx, fsClient, runID, "error", genErr.Error())
		return
	}

	appendStep(ctx, fsClient, runID, AgentRunStep{
		Timestamp: time.Now(),
		Action:    "Antwort erhalten",
		Detail:    fmt.Sprintf("%d Zeichen Ergebnis generiert", len(result)),
	})

	completedAt := time.Now()
	_, _ = fsClient.Collection("agent_runs").Doc(runID).Update(ctx, []firestore.Update{
		{Path: "status", Value: "done"},
		{Path: "result", Value: result},
		{Path: "completedAt", Value: completedAt},
	})
	log.Printf("executePromptRun [%s]: Completed (%d chars)", runID, len(result))
}

// executeBrowserAutomationRun delegates to the Python RunBrowserAgent Cloud Function via HTTP.
func executeBrowserAutomationRun(
	ctx context.Context,
	fsClient *firestore.Client,
	runID, uid, agentID string,
	inputValues map[string]string,
	skill map[string]interface{},
	dryRun bool,
) {
	// URL of the Python browser agent Cloud Function (set via env var)
	runnerURL := os.Getenv("BROWSER_AGENT_URL")
	if runnerURL == "" {
		runnerURL = fmt.Sprintf(
			"https://europe-west3-%s.cloudfunctions.net/RunBrowserAgent",
			ProjectID,
		)
	}

	payload := map[string]interface{}{
		"runId":            runID,
		"agentId":          agentID,
		"userId":           uid,
		"targetUrl":        skill["targetUrl"],
		"inputValues":      inputValues,
		"actionSequence":   skill["actionSequence"],
		"successCondition": skill["successCondition"],
		"precondition":     skill["precondition"],
		"dryRun":           dryRun,
	}

	body, err := json.Marshal(payload)
	if err != nil {
		log.Printf("executeBrowserAutomationRun [%s]: marshal error: %v", runID, err)
		setRunStatus(ctx, fsClient, runID, "error", "Internal: payload marshal failed")
		return
	}

	resp, err := http.Post(runnerURL, "application/json", bytes.NewReader(body)) //nolint:noctx
	if err != nil {
		log.Printf("executeBrowserAutomationRun [%s]: HTTP error: %v", runID, err)
		summary := fmt.Sprintf("Browser-Agent nicht erreichbar: %v", err)
		appendStep(ctx, fsClient, runID, AgentRunStep{
			Timestamp: time.Now(),
			Action:    "Fehler",
			Detail:    summary,
			IsError:   true,
		})
		setRunStatus(ctx, fsClient, runID, "error", summary)
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		summary := fmt.Sprintf("Browser-Agent HTTP %d", resp.StatusCode)
		log.Printf("executeBrowserAutomationRun [%s]: %s", runID, summary)
		// Status + result are written by the Python service directly to Firestore
	}
	log.Printf("executeBrowserAutomationRun [%s]: Python runner responded %d", runID, resp.StatusCode)
}

// GetUserAgents returns all agents for the authenticated user.
func GetUserAgents(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	if r.Method == http.MethodOptions {
		w.Header().Set("Access-Control-Allow-Methods", "POST")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
		w.WriteHeader(http.StatusNoContent)
		return
	}

	uid, err := VerifyUser(w, r)
	if err != nil {
		http.Error(w, "Unauthorized", http.StatusUnauthorized)
		return
	}

	ctx := context.Background()
	fsClient, err := firestore.NewClient(ctx, ProjectID)
	if err != nil {
		http.Error(w, "Internal Server Error", http.StatusInternalServerError)
		return
	}
	defer fsClient.Close()

	iter := fsClient.Collection("users").Doc(uid).Collection("user_agents").
		OrderBy("createdAt", firestore.Asc).Documents(ctx)
	defer iter.Stop()

	agents := []map[string]interface{}{}
	for {
		doc, iterErr := iter.Next()
		if iterErr == iterator.Done {
			break
		}
		if iterErr != nil {
			log.Printf("GetUserAgents [%s]: Error: %v", uid, iterErr)
			break
		}
		d := doc.Data()
		d["id"] = doc.Ref.ID
		agents = append(agents, d)
	}

	writeResponse(w, map[string]interface{}{"data": map[string]interface{}{"agents": agents}})
}

// SaveUserAgent creates or updates a user agent.
// POST body: { "data": { "agent": { ... } } }
func SaveUserAgent(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	if r.Method == http.MethodOptions {
		w.Header().Set("Access-Control-Allow-Methods", "POST")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
		w.WriteHeader(http.StatusNoContent)
		return
	}

	uid, err := VerifyUser(w, r)
	if err != nil {
		http.Error(w, "Unauthorized", http.StatusUnauthorized)
		return
	}

	var wrapper struct {
		Data struct {
			Agent map[string]interface{} `json:"agent"`
		} `json:"data"`
	}
	if err := json.NewDecoder(r.Body).Decode(&wrapper); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}
	agent := wrapper.Data.Agent
	if agent == nil {
		http.Error(w, "agent data required", http.StatusBadRequest)
		return
	}

	now := time.Now()
	agent["updatedAt"] = now

	ctx := context.Background()
	fsClient, err := firestore.NewClient(ctx, ProjectID)
	if err != nil {
		http.Error(w, "Internal Server Error", http.StatusInternalServerError)
		return
	}
	defer fsClient.Close()

	agentsCol := fsClient.Collection("users").Doc(uid).Collection("user_agents")
	var agentID string

	if id, hasID := agent["id"].(string); hasID && id != "" {
		agentID = id
		delete(agent, "id") // Don't store id field inside doc
		docRef := agentsCol.Doc(agentID)
		// Preserve fields from existing document that aren't sent by client
		if snap, snapErr := docRef.Get(ctx); snapErr == nil {
			data := snap.Data()
			if createdAt, ok := data["createdAt"]; ok {
				agent["createdAt"] = createdAt
			}
			if lastRunAt, ok := data["lastRunAt"]; ok {
				agent["lastRunAt"] = lastRunAt
			}
			if lastRunId, ok := data["lastRunId"]; ok {
				agent["lastRunId"] = lastRunId
			}
		}
		// Full replace (not MergeAll) so removed inputValues keys are actually deleted
		_, err = docRef.Set(ctx, agent)
	} else {
		delete(agent, "id")
		agent["createdAt"] = now
		agent["isActive"] = true
		var docRef *firestore.DocumentRef
		docRef, _, err = agentsCol.Add(ctx, agent)
		if err == nil {
			agentID = docRef.ID
		}
	}

	if err != nil {
		log.Printf("SaveUserAgent [%s]: Firestore error: %v", uid, err)
		http.Error(w, "Internal Server Error", http.StatusInternalServerError)
		return
	}

	writeResponse(w, map[string]interface{}{"data": map[string]interface{}{"status": "ok", "agentId": agentID}})
}

// DeleteUserAgent removes a user agent by ID.
func DeleteUserAgent(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	if r.Method == http.MethodOptions {
		w.Header().Set("Access-Control-Allow-Methods", "POST")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
		w.WriteHeader(http.StatusNoContent)
		return
	}

	uid, err := VerifyUser(w, r)
	if err != nil {
		http.Error(w, "Unauthorized", http.StatusUnauthorized)
		return
	}

	var wrapper struct {
		Data struct {
			AgentID string `json:"agentId"`
		} `json:"data"`
	}
	if err := json.NewDecoder(r.Body).Decode(&wrapper); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}
	if wrapper.Data.AgentID == "" {
		http.Error(w, "agentId required", http.StatusBadRequest)
		return
	}

	ctx := context.Background()
	fsClient, err := firestore.NewClient(ctx, ProjectID)
	if err != nil {
		http.Error(w, "Internal Server Error", http.StatusInternalServerError)
		return
	}
	defer fsClient.Close()

	_, err = fsClient.Collection("users").Doc(uid).Collection("user_agents").Doc(wrapper.Data.AgentID).Delete(ctx)
	if err != nil {
		log.Printf("DeleteUserAgent [%s]: Error: %v", uid, err)
		http.Error(w, "Internal Server Error", http.StatusInternalServerError)
		return
	}

	writeResponse(w, map[string]interface{}{"data": map[string]interface{}{"status": "ok"}})
}

// GetAgentRunLog returns the run log for a specific agent run.
// Security: verifies that the run belongs to the requesting user.
func GetAgentRunLog(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	if r.Method == http.MethodOptions {
		w.Header().Set("Access-Control-Allow-Methods", "POST")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
		w.WriteHeader(http.StatusNoContent)
		return
	}

	uid, err := VerifyUser(w, r)
	if err != nil {
		http.Error(w, "Unauthorized", http.StatusUnauthorized)
		return
	}

	var wrapper struct {
		Data struct {
			RunID string `json:"runId"`
		} `json:"data"`
	}
	if err := json.NewDecoder(r.Body).Decode(&wrapper); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}
	if wrapper.Data.RunID == "" {
		http.Error(w, "runId required", http.StatusBadRequest)
		return
	}

	ctx := context.Background()
	fsClient, err := firestore.NewClient(ctx, ProjectID)
	if err != nil {
		http.Error(w, "Internal Server Error", http.StatusInternalServerError)
		return
	}
	defer fsClient.Close()

	doc, err := fsClient.Collection("agent_runs").Doc(wrapper.Data.RunID).Get(ctx)
	if err != nil {
		http.Error(w, "Run not found", http.StatusNotFound)
		return
	}

	runData := doc.Data()
	// Security: run must belong to the requesting user
	if runData["userId"] != uid {
		http.Error(w, "Forbidden", http.StatusForbidden)
		return
	}

	writeResponse(w, map[string]interface{}{"data": runData})
}
