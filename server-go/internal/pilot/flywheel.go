//go:build windows

// flywheel.go 决策飞轮：decisions.jsonl 追加（shadow，无读取依赖）+
// 意图×反馈只读投影 + 可训数据集导出。语义对齐 pilot/flywheel.py。
package pilot

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

var flywheelMu sync.Mutex

func decisionsPath(sessionsBase string) string {
	return filepath.Join(sessionsBase, decisionFileName)
}

func datasetPath(sessionsBase string) string {
	return filepath.Join(sessionsBase, "dataset.jsonl")
}

func preferencePath(sessionsBase string) string {
	return filepath.Join(sessionsBase, "preference_pairs.jsonl")
}

// AppendDecision 决策日志追加（失败静默：飞轮不依赖日志，日志不阻塞问答）。
func AppendDecision(sessionsBase string, entry map[string]any) bool {
	flywheelMu.Lock()
	defer flywheelMu.Unlock()
	file, err := os.OpenFile(decisionsPath(sessionsBase), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return false
	}
	defer file.Close()
	payload, err := json.Marshal(entry)
	if err != nil {
		return false
	}
	_, err = file.WriteString(string(payload) + "\n")
	return err == nil
}

func purgeFlywheelSession(sessionsBase, sid string) bool {
	return purgeFlywheelSessions(sessionsBase, []string{sid})
}

func purgeFlywheelSessions(sessionsBase string, sids []string) bool {
	if len(sids) == 0 {
		return true
	}
	flywheelMu.Lock()
	defer flywheelMu.Unlock()
	path := decisionsPath(sessionsBase)
	drop := map[string]bool{}
	for _, sid := range sids {
		drop[sid] = true
	}
	kept := []map[string]any{}
	changed := false
	for _, record := range readJSONL(path) {
		sid, _ := record["sid"].(string)
		if drop[sid] {
			changed = true
			continue
		}
		kept = append(kept, record)
	}
	if !changed {
		return true
	}
	return writeJSONLAtomic(path, kept)
}

func writeJSONLAtomic(path string, rows []map[string]any) bool {
	tmp := path + ".tmp"
	file, err := os.Create(tmp)
	if err != nil {
		return false
	}
	writer := bufio.NewWriter(file)
	for _, row := range rows {
		payload, err := json.Marshal(row)
		if err != nil {
			continue
		}
		if _, err := writer.WriteString(string(payload) + "\n"); err != nil {
			file.Close()
			return false
		}
	}
	if err := writer.Flush(); err != nil {
		file.Close()
		return false
	}
	if err := file.Close(); err != nil {
		return false
	}
	// Windows 下 os.Rename 不能覆盖已存在目标：先删旧文件再改名
	if _, err := os.Stat(path); err == nil {
		if err := os.Remove(path); err != nil {
			return false
		}
	}
	return os.Rename(tmp, path) == nil
}

// turnOutcomes 记录 → tid 结果标签（rejected/failed/empty/accepted/unlabeled）。
func turnOutcomes(records []map[string]any) map[string]string {
	feedback := map[string]string{}
	for _, record := range records {
		if t, _ := record["t"].(string); t != "feedback" {
			continue
		}
		tid, _ := record["tid"].(string)
		label, _ := record["label"].(string)
		if tid != "" && (label == "accepted" || label == "rejected") {
			feedback[tid] = label
		}
	}
	outcomes := map[string]string{}
	for _, record := range records {
		if t, _ := record["t"].(string); t != "turn" {
			continue
		}
		tid, _ := record["tid"].(string)
		if tid == "" {
			continue
		}
		if label, ok := feedback[tid]; ok {
			outcomes[tid] = label
			continue
		}
		resp, _ := record["resp"].(map[string]any)
		source, _ := resp["source"].(string)
		reason, _ := resp["reason"].(string)
		switch {
		case source != "llm":
			outcomes[tid] = source
		case reason == "llm_failed" || reason == "max_steps":
			outcomes[tid] = "failed"
		default:
			outcomes[tid] = "unlabeled"
		}
	}
	return outcomes
}

// FlywheelStats 决策飞轮画像（意图×反馈折叠）。
func FlywheelStats(sessionsBase string, rawRecords func(string) []map[string]any) map[string]any {
	records := readJSONL(decisionsPath(sessionsBase))
	outcomes := map[string]string{}
	if rawRecords != nil {
		entries, _ := os.ReadDir(sessionsBase)
		for _, entry := range entries {
			if !strings.HasSuffix(entry.Name(), ".jsonl") || entry.Name() == decisionFileName {
				continue
			}
			sid := strings.TrimSuffix(entry.Name(), ".jsonl")
			for tid, label := range turnOutcomes(rawRecords(sid)) {
				outcomes[tid] = label
			}
		}
	}
	total := len(records)
	labeled, unlabeled := 0, 0
	byFeedback := map[string]int{}
	byMode := map[string]int{}
	bySource := map[string]int{}
	type intentStat struct {
		total, accepted, rejected, failed, empty, unlabeled int
	}
	byIntent := map[string]*intentStat{}
	for _, record := range records {
		intent, _ := record["intent"].(string)
		mode, _ := record["mode"].(string)
		source, _ := record["source"].(string)
		byMode[mode]++
		bySource[source]++
		stat := byIntent[intent]
		if stat == nil {
			stat = &intentStat{}
			byIntent[intent] = stat
		}
		stat.total++
		tid, _ := record["tid"].(string)
		outcome, known := outcomes[tid]
		if !known {
			// 无会话态问答计入 unlabeled
			stat.unlabeled++
			unlabeled++
			continue
		}
		byFeedback[outcome]++
		switch outcome {
		case "accepted":
			stat.accepted++
		case "rejected":
			stat.rejected++
		case "failed":
			stat.failed++
		case "empty", "facts", "guide", "none":
			stat.empty++
		default:
			stat.unlabeled++
		}
		if outcome != "unlabeled" {
			labeled++
		} else {
			unlabeled++
		}
	}
	rejectedRate := any(nil)
	if n := byFeedback["accepted"] + byFeedback["rejected"]; n > 0 {
		rejectedRate = float64(byFeedback["rejected"]) / float64(n)
	}
	intentRows := []map[string]any{}
	keys := make([]string, 0, len(byIntent))
	for key := range byIntent {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		stat := byIntent[key]
		intentRows = append(intentRows, map[string]any{
			"intent": key, "total": stat.total, "accepted": stat.accepted,
			"rejected": stat.rejected, "failed": stat.failed, "empty": stat.empty,
			"unlabeled": stat.unlabeled,
		})
	}
	return map[string]any{
		"total": total, "labeled": labeled, "unlabeled": unlabeled,
		"rejected_rate": rejectedRate, "preference_pairs": 0,
		"by_feedback": byFeedback, "by_intent": intentRows,
		"by_mode": byMode, "by_source": bySource,
	}
}

// readJSONL 决策日志读取（无 t 分型过滤——决策条目不是会话记录）。
func readJSONL(path string) []map[string]any {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	out := []map[string]any{}
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		var item map[string]any
		if json.Unmarshal([]byte(line), &item) != nil || item == nil {
			continue
		}
		out = append(out, item)
	}
	return out
}

var _ = time.Now

// PreferencePair 真实回答偏好对（同会话同问先否定后肯定且内容不同）。
type PreferencePair struct {
	Sid      string `json:"sid"`
	Question string `json:"question"`
	Rejected string `json:"rejected"`
	Accepted string `json:"accepted"`
}

// PreferencePairs 导出真实回答偏好对（同会话同问先否定后肯定且内容不同）。
func PreferencePairs(sessionsBase string, rawRecords func(string) []map[string]any) []PreferencePair {
	if rawRecords == nil {
		return nil
	}
	entries, err := os.ReadDir(sessionsBase)
	if err != nil {
		return nil
	}
	var pairs []PreferencePair
	for _, entry := range entries {
		if !strings.HasSuffix(entry.Name(), ".jsonl") || entry.Name() == decisionFileName {
			continue
		}
		sid := strings.TrimSuffix(entry.Name(), ".jsonl")
		records := rawRecords(sid)
		outcomes := turnOutcomes(records)
		// 按问句分组，找先 rejected 后 accepted 的对
		type turnEntry struct {
			question string
			answer   string
		}
		var turnOrder []string
		turnMap := map[string]turnEntry{}
		for _, record := range records {
			if t, _ := record["t"].(string); t != "turn" {
				continue
			}
			tid, _ := record["tid"].(string)
			q, _ := record["q"].(string)
			resp, _ := record["resp"].(map[string]any)
			answer, _ := resp["answer"].(string)
			turnOrder = append(turnOrder, tid)
			turnMap[tid] = turnEntry{question: q, answer: answer}
		}
		// 找 rejected → accepted 的对
		for i, tidA := range turnOrder {
			if outcomes[tidA] != "rejected" {
				continue
			}
			for j := i + 1; j < len(turnOrder); j++ {
				tidB := turnOrder[j]
				if outcomes[tidB] != "accepted" {
					continue
				}
				da := turnMap[tidA]
				db := turnMap[tidB]
				if da.question == db.question && da.answer != db.answer {
					pairs = append(pairs, PreferencePair{
						Sid: sid, Question: da.question,
						Rejected: da.answer, Accepted: db.answer,
					})
					break
				}
			}
		}
	}
	return pairs
}

// ExportPreferencePairs 导出偏好对到文件。
func ExportPreferencePairs(sessionsBase string, rawRecords func(string) []map[string]any) (int, string, bool) {
	pairs := PreferencePairs(sessionsBase, rawRecords)
	path := preferencePath(sessionsBase)
	rows := make([]map[string]any, 0, len(pairs))
	for _, pair := range pairs {
		rows = append(rows, map[string]any{
			"sid": pair.Sid, "question": pair.Question,
			"rejected": pair.Rejected, "accepted": pair.Accepted,
		})
	}
	if writeJSONLAtomic(path, rows) {
		return len(pairs), path, true
	}
	return 0, path, false
}
