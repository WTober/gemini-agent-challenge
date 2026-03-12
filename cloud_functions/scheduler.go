// scheduler.go – Scheduled Agent Execution (Gemini Live Agent Challenge)
//
// Triggered every 15 minutes by Cloud Scheduler. Scans all user agents with
// trigger="scheduled" and runs those whose scheduleConfig matches the current
// time window (in the user's local timezone).
//
// Key design decisions:
//   - Timezone-aware: each user has an IANA timezone, schedule matching happens
//     in local time (a user in Asia/Kuala_Lumpur gets their 21:00 trigger at
//     their local 21:00, not UTC 21:00)
//   - Double-execution guard: lastScheduledRun prevents re-triggering within 14 min
//   - sync.WaitGroup: goroutines MUST complete before the Cloud Function returns,
//     otherwise Cloud Functions kills them on exit (this was a critical bug fix)
package function

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"cloud.google.com/go/firestore"
	"github.com/GoogleCloudPlatform/functions-framework-go/functions"
	"google.golang.org/api/iterator"
)

func init() {
	functions.HTTP("RunScheduledAgents", RunScheduledAgents)
}

// RunScheduledAgents is triggered by Cloud Scheduler (every 15 minutes).
// It finds all user agents with trigger=scheduled and runs those whose
// scheduleConfig matches the current time window.
func RunScheduledAgents(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	ctx := context.Background()

	fsClient, err := firestore.NewClient(ctx, ProjectID)
	if err != nil {
		log.Printf("RunScheduledAgents: Firestore init error: %v", err)
		http.Error(w, "Internal Error", http.StatusInternalServerError)
		return
	}
	defer fsClient.Close()

	// Determine current time in UTC
	nowUTC := time.Now().UTC()

	// Calculate 15-minute bucket (0, 15, 30, 45)
	minuteBucket := (nowUTC.Minute() / 15) * 15

	// Support simulation parameters (for testing)
	if d := r.URL.Query().Get("sim_day"); d != "" {
		// Simulation handled below per-user via timezone
		_ = d
	}

	log.Printf("RunScheduledAgents: UTC %d:%02d (bucket=%d), Weekday=%d",
		nowUTC.Hour(), nowUTC.Minute(), minuteBucket, nowUTC.Weekday())

	// Load skill definitions once
	skillsDoc, err := fsClient.Collection("config").Doc("agent_skills").Get(ctx)
	if err != nil {
		log.Printf("RunScheduledAgents: Failed to load skills: %v", err)
		http.Error(w, "Skills not configured", http.StatusInternalServerError)
		return
	}
	skillsData := skillsDoc.Data()
	rawSkills, _ := skillsData["skills"].([]interface{})

	// Load retention config
	config := LoadAppConfig(ctx)
	retentionDays := getInt(config.AgentRunRetentionDays, 30)

	// Collection Group Query: all user_agents with trigger=scheduled
	iter := fsClient.CollectionGroup("user_agents").
		Where("trigger", "==", "scheduled").
		Documents(ctx)

	var wg sync.WaitGroup
	processed := 0
	triggered := 0
	skipped := 0

	for {
		doc, err := iter.Next()
		if err == iterator.Done {
			break
		}
		if err != nil {
			log.Printf("RunScheduledAgents: Iterator error: %v", err)
			break
		}
		processed++

		data := doc.Data()
		agentID := doc.Ref.ID
		// Extract userId from the document path: users/{uid}/user_agents/{agentId}
		uid := doc.Ref.Parent.Parent.ID

		agentName, _ := data["name"].(string)
		skillID, _ := data["skillId"].(string)
		goal, _ := data["goal"].(string)
		agentActive, _ := data["isActive"].(bool)

		// Skip disabled agents
		if !agentActive {
			log.Printf("RunScheduledAgents [%s/%s]: Agent not active, skipping", uid, agentID)
			skipped++
			continue
		}

		// Parse scheduleConfig
		rawConfig, _ := data["scheduleConfig"].(map[string]interface{})
		if len(rawConfig) == 0 {
			log.Printf("RunScheduledAgents [%s/%s]: No scheduleConfig, skipping", uid, agentID)
			skipped++
			continue
		}

		// Load user timezone, language, and admin status
		userTZ := "Europe/Berlin"
		userLang := "de"
		userIsAdmin := false
		userDoc, _ := fsClient.Collection("users").Doc(uid).Get(ctx)
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
			if admin, ok := userDoc.Data()["isAdmin"].(bool); ok {
				userIsAdmin = admin
			}
		}

		loc, err := time.LoadLocation(userTZ)
		if err != nil {
			loc, _ = time.LoadLocation("Europe/Berlin")
		}
		nowLocal := nowUTC.In(loc)

		// Check if schedule matches current time
		if !isScheduleDue(rawConfig, nowLocal, minuteBucket) {
			log.Printf("RunScheduledAgents [%s/%s]: Schedule not due (local=%s, config=%v), skipping",
				uid, agentID, nowLocal.Format("15:04"), rawConfig)
			continue
		}

		// Check double-execution guard: lastScheduledRun
		if lastRun, ok := data["lastScheduledRun"].(time.Time); ok {
			if time.Since(lastRun) < 14*time.Minute {
				log.Printf("RunScheduledAgents [%s/%s]: Already ran %v ago, skipping",
					uid, agentID, time.Since(lastRun).Round(time.Second))
				skipped++
				continue
			}
		}

		// Find skill definition
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
			log.Printf("RunScheduledAgents [%s/%s]: Skill '%s' not found, skipping", uid, agentID, skillID)
			skipped++
			continue
		}
		isActive, _ := skill["isActive"].(bool)
		if !isActive {
			log.Printf("RunScheduledAgents [%s/%s]: Skill '%s' not active, skipping", uid, agentID, skillID)
			skipped++
			continue
		}

		// Check if skill is released (non-released only allowed for admins)
		skillReleased, _ := skill["released"].(bool)
		if !skillReleased && !userIsAdmin {
			log.Printf("RunScheduledAgents [%s/%s]: Skill '%s' not released and user is not admin, skipping", uid, agentID, skillID)
			skipped++
			continue
		}

		skillName, _ := skill["name"].(string)
		skillType, _ := skill["skillType"].(string)
		if skillType == "" {
			skillType = "prompt"
		}
		creditCost := int64(getInt(skill["creditCost"], 500))

		// Load inputValues
		rawInputValues, _ := data["inputValues"].(map[string]interface{})
		inputValues := make(map[string]string)
		for k, v := range rawInputValues {
			if sv, ok := v.(string); ok {
				inputValues[k] = sv
			}
		}

		// Deduct credits (skip agent if insufficient)
		if err := checkAndDeductAgentCredits(ctx, uid, skillID, creditCost); err != nil {
			log.Printf("RunScheduledAgents [%s/%s]: Credit check failed: %v", uid, agentID, err)
			skipped++
			continue
		}

		// Log credit usage
		go func(uid, skillID, skillName, agentName string, cost int64) {
			desc := fmt.Sprintf("Scheduled Agent: %s – %s (%d Credits)", skillName, agentName, cost)
			_ = logCreditHistory(context.Background(), uid, CreditActionUsage, "agent_"+skillID, -cost, desc)
		}(uid, skillID, skillName, agentName, creditCost)

		// Create agent_runs document
		runRef := fsClient.Collection("agent_runs").NewDoc()
		runID := runRef.ID
		now := time.Now()

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
			"trigger":   "scheduled",
		})
		if err != nil {
			log.Printf("RunScheduledAgents [%s/%s]: Failed to create run: %v", uid, agentID, err)
			continue
		}

		// Execute concurrently but wait before returning
		wg.Add(1)
		go func(runID, uid, agentID, goal, skillType string, inputValues map[string]string, skill map[string]interface{}, userTZ, userLang string) {
			defer wg.Done()
			executeAgentRun(runID, uid, agentID, goal, skillType, inputValues, skill, false, userTZ, userLang)
		}(runID, uid, agentID, goal, skillType, inputValues, skill, userTZ, userLang)

		// Update lastScheduledRun + lastRunAt
		_, _ = doc.Ref.Update(ctx, []firestore.Update{
			{Path: "lastScheduledRun", Value: now},
			{Path: "lastRunAt", Value: now},
		})

		log.Printf("RunScheduledAgents: Triggered run %s (user=%s, agent=%s '%s', skill=%s)",
			runID, uid, agentID, agentName, skillID)
		triggered++
	}

	// Wait for all triggered agent runs to complete before returning.
	// CRITICAL: Without this WaitGroup, Cloud Functions kills goroutines when
	// the HTTP handler returns. This caused scheduled runs to show 0 steps
	// because executeAgentRun was terminated mid-execution. Fixed March 2026.
	log.Printf("RunScheduledAgents: Waiting for %d agent runs to complete...", triggered)
	wg.Wait()

	elapsed := time.Since(start)
	log.Printf("RunScheduledAgents: Done in %v – processed=%d, triggered=%d, skipped=%d",
		elapsed.Round(time.Millisecond), processed, triggered, skipped)

	fmt.Fprintf(w, "OK: processed=%d, triggered=%d, skipped=%d (%.1fs)\n",
		processed, triggered, skipped, elapsed.Seconds())
}

// isScheduleDue checks if the given scheduleConfig matches the current local time.
// minuteBucket is the 15-minute aligned UTC minute (0, 15, 30, 45).
func isScheduleDue(config map[string]interface{}, nowLocal time.Time, minuteBucket int) bool {
	schedType, _ := config["type"].(string)
	if schedType == "" {
		schedType = "weekly"
	}

	// Parse target time (e.g. "08:00")
	timeStr, _ := config["time"].(string)
	if timeStr == "" {
		return false
	}
	parts := strings.Split(timeStr, ":")
	if len(parts) != 2 {
		return false
	}
	targetHour, err1 := strconv.Atoi(parts[0])
	targetMinute, err2 := strconv.Atoi(parts[1])
	if err1 != nil || err2 != nil {
		return false
	}

	// Align target minute to 15-min bucket
	targetBucket := (targetMinute / 15) * 15

	// Check hour + minute bucket match in user's local time
	localHour := nowLocal.Hour()
	localMinuteBucket := (nowLocal.Minute() / 15) * 15

	if localHour != targetHour || localMinuteBucket != targetBucket {
		return false
	}

	// Apply offsetDays
	offset := 0
	if v, ok := config["offsetDays"]; ok {
		switch o := v.(type) {
		case int64:
			offset = int(o)
		case float64:
			offset = int(o)
		}
	}
	checkDay := nowLocal.AddDate(0, 0, -offset)

	// Check day-of-week / day-of-month based on type
	switch schedType {
	case "daily":
		return true

	case "weekly":
		rawDays, ok := config["weekdays"].([]interface{})
		if !ok || len(rawDays) == 0 {
			return false
		}
		// weekdays: 1=Monday, 7=Sunday (ISO)
		localWeekday := int(checkDay.Weekday())
		if localWeekday == 0 {
			localWeekday = 7 // Sunday = 7
		}
		for _, d := range rawDays {
			var dayNum int
			switch v := d.(type) {
			case int64:
				dayNum = int(v)
			case float64:
				dayNum = int(v)
			case map[string]interface{}:
				// Protobuf Int64Value wrapper: {@type: ..., value: 2}
				if val, ok := v["value"]; ok {
					switch vv := val.(type) {
					case int64:
						dayNum = int(vv)
					case float64:
						dayNum = int(vv)
					case string:
						dayNum, _ = strconv.Atoi(vv)
					}
				}
			}
			if dayNum == localWeekday {
				return true
			}
		}
		return false

	case "monthly":
		// Option 1: specific day of month
		if monthDay, ok := config["monthDay"]; ok {
			var md int
			switch v := monthDay.(type) {
			case int64:
				md = int(v)
			case float64:
				md = int(v)
			}
			if md > 0 && checkDay.Day() == md {
				return true
			}
		}
		// Option 2: nth weekday (e.g. 2nd Tuesday)
		if nth, ok := config["nthWeekday"]; ok {
			var nthNum, nthTypeNum int
			switch v := nth.(type) {
			case int64:
				nthNum = int(v)
			case float64:
				nthNum = int(v)
			}
			if nthType, ok := config["nthWeekdayType"]; ok {
				switch v := nthType.(type) {
				case int64:
					nthTypeNum = int(v)
				case float64:
					nthTypeNum = int(v)
				}
			}
			if nthNum > 0 && nthTypeNum > 0 {
				// Check if today is the nth occurrence of weekday nthTypeNum in this month
				isoWeekday := int(checkDay.Weekday())
				if isoWeekday == 0 {
					isoWeekday = 7
				}
				if isoWeekday == nthTypeNum {
					// Count which occurrence this is
					occurrence := (checkDay.Day()-1)/7 + 1
					if occurrence == nthNum {
						return true
					}
				}
			}
		}
		return false
	}

	return false
}
