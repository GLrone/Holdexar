package pilot

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"
	"unicode"
)

const (
	sessionMaxFiles  = 200
	titleAutoChars   = 24
	titleMaxLen      = 60
	snippetChars     = 80
	stateCacheMax    = 64
	decisionFileName = "decisions.jsonl"
)

var sidRe = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

// ValidSID 会话 id 白名单（封死路径拼接面）。
func ValidSID(sid string) bool { return sidRe.MatchString(sid) }

var quoteMarkerRe = regexp.MustCompile(`【[^】\d]*\d+】`)

func DeriveTitle(q string) string {
	text := strings.Join(strings.Fields(quoteMarkerRe.ReplaceAllString(q, "")), " ")
	if text == "" {
		return ""
	}
	runes := []rune(text)
	if len(runes) > titleAutoChars {
		return string(runes[:titleAutoChars]) + "…"
	}
	return text
}

type SessionState struct {
	Sid             string
	Turns           []map[string]any
	Summary         string
	SummaryThrough  int
	Markers         []map[string]any
	Title           string
	LastGame        map[string]any
	UpdatedAt       string
	PendingProposal map[string]any
	Proposals       map[string]map[string]any
	Pinned          bool
	Archived        bool
	ForkedFrom      string
}

func (s *SessionState) ProposalStateOf(pid string) string {
	recorded, _ := s.Proposals[pid]["state"].(string)
	if recorded == "pending" {
		if pending, _ := s.PendingProposal["pid"].(string); pending != pid {
			return "withdrawn"
		}
	}
	return recorded
}

type SessionStore struct {
	base string
	mu   sync.Mutex
}

func NewSessionStore(base string) *SessionStore {
	_ = os.MkdirAll(base, 0o755)
	store := &SessionStore{base: base}
	store.sweep()
	return store
}

func (st *SessionStore) Base() string { return st.base }

func (st *SessionStore) path(sid string) string {
	return filepath.Join(st.base, sid+".jsonl")
}

func (st *SessionStore) Load(sid string) *SessionState {
	if !ValidSID(sid) {
		return nil
	}
	st.mu.Lock()
	defer st.mu.Unlock()
	raw, err := os.ReadFile(st.path(sid))
	if err != nil {
		return nil
	}
	state := st.fold(sid, readLines(raw))
	if info, err := os.Stat(st.path(sid)); err == nil {
		state.UpdatedAt = isoTS(info.ModTime())
	}
	return state
}

func (st *SessionStore) RawRecords(sid string) []map[string]any {
	if !ValidSID(sid) {
		return nil
	}
	st.mu.Lock()
	defer st.mu.Unlock()
	raw, err := os.ReadFile(st.path(sid))
	if err != nil {
		return nil
	}
	return readLines(raw)
}

func (st *SessionStore) LoadOrNew(sid string) *SessionState {
	if state := st.Load(sid); state != nil {
		return state
	}
	return &SessionState{Sid: sid}
}

func (st *SessionStore) AppendTurn(sid string, record map[string]any) bool {
	if !ValidSID(sid) {
		return false
	}
	state := st.Load(sid)
	if state == nil {
		state = &SessionState{Sid: sid}
	}
	if state.Title == "" {
		if q, _ := record["q"].(string); q != "" {
			st.SetTitle(sid, DeriveTitle(q))
		}
	}
	payload := map[string]any{"t": "turn"}
	for key, value := range record {
		payload[key] = value
	}
	return st.append(sid, payload)
}

func (st *SessionStore) SetTurnFeedback(sid, tid, label string) string {
	if !ValidSID(sid) || tid == "" || (label != "accepted" && label != "rejected") {
		return "bad_request"
	}
	st.mu.Lock()
	defer st.mu.Unlock()
	path := st.path(sid)
	raw, err := os.ReadFile(path)
	if err != nil {
		return "no_session"
	}
	state := st.fold(sid, readLines(raw))
	found := false
	for _, turn := range state.Turns {
		if t, _ := turn["tid"].(string); t == tid {
			found = true
			break
		}
	}
	if !found {
		return "turn_gone"
	}
	record := map[string]any{"t": "feedback", "tid": tid, "label": label, "ts": nowISO()}
	if st.appendLocked(path, record) {
		return ""
	}
	return "write_failed"
}

func (st *SessionStore) SetTitle(sid, text string) bool {
	text = strings.Join(strings.Fields(text), " ")
	if runes := []rune(text); len(runes) > titleMaxLen {
		text = string(runes[:titleMaxLen])
	}
	if text == "" {
		return false
	}
	return st.append(sid, map[string]any{"t": "title", "text": text})
}

func (st *SessionStore) AppendSummary(sid, text string, through int, trigger string, preTokens, postTokens any) bool {
	record := map[string]any{"t": "summary", "text": text, "through": through,
		"trigger": trigger, "after_turn": through}
	if preTokens != nil {
		record["pre_tokens"] = preTokens
	}
	if postTokens != nil {
		record["post_tokens"] = postTokens
	}
	return st.append(sid, record)
}

func (st *SessionStore) AppendProposal(sid string, record map[string]any) bool {
	payload := map[string]any{"t": "proposal"}
	for key, value := range record {
		payload[key] = value
	}
	return st.append(sid, payload)
}

// Truncate 编辑重问：追加软截断记录（历史行不动，折叠时截断）。
func (st *SessionStore) Truncate(sid string, keep int) bool {
	if !ValidSID(sid) {
		return false
	}
	return st.append(sid, map[string]any{"t": "truncate", "keep": keep})
}

func (st *SessionStore) SetPinned(sid string, on bool) bool {
	if !ValidSID(sid) {
		return false
	}
	return st.append(sid, map[string]any{"t": "pin", "on": on})
}

func (st *SessionStore) SetArchived(sid string, on bool) bool {
	if !ValidSID(sid) {
		return false
	}
	return st.append(sid, map[string]any{"t": "archive", "on": on})
}

// Fork 分叉：源前 keep 轮派生新会话（源账本零改动，目标已存在拒绝）。
func (st *SessionStore) Fork(srcSid, newSid string, keep int) bool {
	if !ValidSID(srcSid) || !ValidSID(newSid) || srcSid == newSid {
		return false
	}
	src := st.Load(srcSid)
	if src == nil {
		return false
	}
	if keep > len(src.Turns) {
		keep = len(src.Turns)
	}
	if keep < 0 {
		keep = 0
	}
	ts := nowISO()
	records := []map[string]any{{"t": "fork", "from": srcSid, "turn": keep, "ts": ts}}
	if title := st.TitleOf(src); title != "" {
		records = append(records, map[string]any{"t": "title", "text": title, "ts": ts})
	}
	if src.Summary != "" && src.SummaryThrough > 0 && src.SummaryThrough <= keep {
		records = append(records, map[string]any{"t": "summary", "text": src.Summary,
			"through": src.SummaryThrough, "trigger": "auto",
			"after_turn": src.SummaryThrough, "ts": ts})
	}
	for i := 0; i < keep; i++ {
		records = append(records, src.Turns[i])
	}
	st.mu.Lock()
	defer st.mu.Unlock()
	if _, err := os.Stat(st.path(newSid)); err == nil {
		return false
	}
	file, err := os.Create(st.path(newSid))
	if err != nil {
		return false
	}
	defer file.Close()
	writer := bufio.NewWriter(file)
	ok := true
	for _, record := range records {
		payload, err := json.Marshal(record)
		if err != nil {
			ok = false
			break
		}
		if _, err := writer.WriteString(string(payload) + "\n"); err != nil {
			ok = false
			break
		}
	}
	if err := writer.Flush(); err != nil {
		ok = false
	}
	return ok
}

func (st *SessionStore) DeleteWithReason(sid string) string {
	if !ValidSID(sid) {
		return "no_session"
	}
	st.mu.Lock()
	defer st.mu.Unlock()
	if _, err := os.Stat(st.path(sid)); err != nil {
		return "no_session"
	}
	purgeFlywheelSession(st.base, sid)
	if err := os.Remove(st.path(sid)); err != nil {
		return "delete_failed"
	}
	purgeAttachments(st.base, sid)
	return "deleted"
}

func (st *SessionStore) TitleOf(state *SessionState) string {
	if state.Title != "" {
		return state.Title
	}
	for _, turn := range state.Turns {
		if q, _ := turn["q"].(string); strings.TrimSpace(q) != "" {
			return DeriveTitle(q)
		}
	}
	return ""
}

func (st *SessionStore) ListRecent(limit int, q string) []map[string]any {
	st.mu.Lock()
	defer st.mu.Unlock()
	entries, err := os.ReadDir(st.base)
	if err != nil {
		return nil
	}
	type fileEntry struct {
		path  string
		sid   string
		mtime time.Time
	}
	var files []fileEntry
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".jsonl") {
			continue
		}
		sid := strings.TrimSuffix(entry.Name(), ".jsonl")
		if !ValidSID(sid) {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			continue
		}
		files = append(files, fileEntry{path: st.path(sid), sid: sid, mtime: info.ModTime()})
	}
	slices.SortFunc(files, func(a, b fileEntry) int { return b.mtime.Compare(a.mtime) })
	needle := strings.ToLower(strings.TrimSpace(q))
	if needle == "" && len(files) > limit {
		files = files[:limit]
	}
	out := []map[string]any{}
	for _, file := range files {
		raw, err := os.ReadFile(file.path)
		if err != nil {
			continue
		}
		state := st.fold(file.sid, readLines(raw))
		title := st.TitleOf(state)
		snippet := ""
		if needle != "" {
			var hit bool
			hit, snippet = matchState(state, title, needle)
			if !hit {
				continue
			}
		}
		out = append(out, map[string]any{
			"sid": file.sid, "title": title, "turn_total": len(state.Turns),
			"updated_at": isoTS(file.mtime), "pinned": state.Pinned,
			"archived": state.Archived, "forked_from": forkedFromOrEmpty(state),
			"snippet": snippet,
		})
	}
	slices.SortStableFunc(out, func(a, b map[string]any) int {
		pi, _ := a["pinned"].(bool)
		pj, _ := b["pinned"].(bool)
		if pi != pj {
			if pi {
				return -1
			}
			return 1
		}
		return 0
	})
	if needle != "" && len(out) > limit {
		out = out[:limit]
	}
	return out
}

func forkedFromOrEmpty(state *SessionState) any {
	if state.ForkedFrom == "" {
		return nil
	}
	return state.ForkedFrom
}

func indexRunesFold(text, lowerNeedle string) (int, int) {
	nRunes := []rune(lowerNeedle)
	if len(nRunes) == 0 {
		return 0, 0
	}
	tRunes := []rune(text)
	if len(tRunes) < len(nRunes) {
		return -1, 0
	}
	for i := 0; i <= len(tRunes)-len(nRunes); i++ {
		match := true
		for j, nr := range nRunes {
			if unicode.ToLower(tRunes[i+j]) != nr {
				match = false
				break
			}
		}
		if match {
			return i, len(nRunes)
		}
	}
	return -1, 0
}

func matchState(state *SessionState, title, needle string) (bool, string) {
	if pos, _ := indexRunesFold(title, needle); pos >= 0 {
		return true, ""
	}
	for _, turn := range state.Turns {
		q, _ := turn["q"].(string)
		if pos, span := indexRunesFold(q, needle); pos >= 0 {
			return true, clipSnippet(q, pos, span)
		}
		resp, _ := turn["resp"].(map[string]any)
		text, _ := resp["answer"].(string)
		if pos, span := indexRunesFold(text, needle); pos >= 0 {
			return true, clipSnippet(text, pos, span)
		}
	}
	return false, ""
}

func clipSnippet(text string, pos, span int) string {
	runes := []rune(text)
	if len(runes) <= snippetChars {
		return strings.Join(strings.Fields(text), " ")
	}
	lead := (snippetChars - span) / 2
	if lead < 0 {
		lead = 0
	}
	start := pos - lead
	if start < 0 {
		start = 0
	}
	end := start + snippetChars
	if end > len(runes) {
		end = len(runes)
	}
	start = end - snippetChars
	if start < 0 {
		start = 0
	}
	piece := strings.Join(strings.Fields(string(runes[start:end])), " ")
	out := ""
	if start > 0 {
		out = "…"
	}
	out += piece
	if end < len(runes) {
		out += "…"
	}
	return out
}

func (st *SessionStore) append(sid string, record map[string]any) bool {
	if !ValidSID(sid) {
		return false
	}
	st.mu.Lock()
	defer st.mu.Unlock()
	return st.appendLocked(st.path(sid), record)
}

func (st *SessionStore) appendLocked(path string, record map[string]any) bool {
	if _, ok := record["ts"]; !ok {
		record["ts"] = nowISO()
	}
	payload, err := json.Marshal(record)
	if err != nil {
		return false
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return false
	}
	if _, err := file.WriteString(string(payload) + "\n"); err != nil {
		file.Close()
		return false
	}
	return file.Close() == nil
}

func applyRecord(state *SessionState, record map[string]any) {
	switch t, _ := record["t"].(string); t {
	case "turn":
		state.Turns = append(state.Turns, record)
		if lastGame, ok := record["last_game"].(map[string]any); ok && lastGame != nil {
			state.LastGame = lastGame
		}
	case "summary":
		state.Summary, _ = record["text"].(string)
		through, _ := toInt(record["through"])
		if through < 0 {
			through = 0
		}
		if through > len(state.Turns) {
			through = len(state.Turns)
		}
		state.SummaryThrough = through
		state.Markers = append(state.Markers, map[string]any{
			"after_turn": state.SummaryThrough, "trigger": stringOr(record["trigger"], "auto"),
			"ts": record["ts"], "pre_tokens": record["pre_tokens"], "post_tokens": record["post_tokens"],
		})
	case "title":
		state.Title, _ = record["text"].(string)
	case "proposal":
		pid, _ := record["pid"].(string)
		if pid != "" {
			failedCount := 0
			if failed, ok := record["failed"].([]any); ok {
				failedCount = len(failed)
			}
			state.Proposals[pid] = map[string]any{
				"state": record["state"], "done": record["done"], "failedCount": failedCount,
			}
		}
		if stateStr, _ := record["state"].(string); stateStr == "pending" {
			state.PendingProposal = record
		} else {
			state.PendingProposal = nil
		}
	case "truncate":
		keep, _ := toInt(record["keep"])
		if keep < 0 {
			keep = 0
		}
		if keep < len(state.Turns) {
			state.Turns = state.Turns[:keep]
		}
		if state.SummaryThrough > len(state.Turns) {
			state.SummaryThrough = len(state.Turns)
		}
		kept := state.Markers[:0]
		for _, marker := range state.Markers {
			after, _ := toInt(marker["after_turn"])
			if after <= len(state.Turns) {
				kept = append(kept, marker)
			}
		}
		state.Markers = kept
		state.PendingProposal = nil
	case "pin":
		state.Pinned, _ = record["on"].(bool)
	case "archive":
		state.Archived, _ = record["on"].(bool)
	case "fork":
		state.ForkedFrom, _ = record["from"].(string)
	}
}

func stringOr(value any, fallback string) string {
	if text, ok := value.(string); ok && text != "" {
		return text
	}
	return fallback
}

func toInt(value any) (int, bool) {
	switch v := value.(type) {
	case float64:
		return int(v), true
	case json.Number:
		n, err := v.Int64()
		return int(n), err == nil
	case int:
		return v, true
	}
	return 0, false
}

func (st *SessionStore) fold(sid string, records []map[string]any) *SessionState {
	state := &SessionState{Sid: sid, Proposals: map[string]map[string]any{}}
	for _, record := range records {
		applyRecord(state, record)
	}
	return state
}

func readLines(raw []byte) []map[string]any {
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
		if _, ok := item["t"]; !ok {
			continue
		}
		out = append(out, item)
	}
	return out
}

func (st *SessionStore) sweep() {
	entries, err := os.ReadDir(st.base)
	if err != nil {
		return
	}
	type fileEntry struct {
		path  string
		sid   string
		mtime time.Time
	}
	var files []fileEntry
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".jsonl") {
			continue
		}
		sid := strings.TrimSuffix(entry.Name(), ".jsonl")
		if !ValidSID(sid) {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			continue
		}
		files = append(files, fileEntry{path: st.path(sid), sid: sid, mtime: info.ModTime()})
	}
	if len(files) <= sessionMaxFiles {
		return
	}
	slices.SortFunc(files, func(a, b fileEntry) int { return a.mtime.Compare(b.mtime) })
	expired := files[:len(files)-sessionMaxFiles]
	sids := make([]string, 0, len(expired))
	for _, file := range expired {
		sids = append(sids, file.sid)
	}
	purgeFlywheelSessions(st.base, sids)
	for _, file := range expired {
		_ = os.Remove(file.path)
		purgeAttachments(st.base, file.sid)
	}
}

func nowISO() string { return time.Now().Format("2006-01-02T15:04:05") }

func isoTS(t time.Time) string { return t.Format("2006-01-02T15:04:05") }

var _ = fmt.Sprintf
