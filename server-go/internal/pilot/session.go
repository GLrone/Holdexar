//go:build windows

// session.go 领航员会话账本：sid.jsonl 追加式记录 + 折叠投影（后者胜）。
// 语义对齐 pilot/session.py：turn/summary/title/proposal/truncate/pin/
// archive/fork/feedback 九类记录，坏行跳过，缓存带 stat 键失配即重折叠。
package pilot

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
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

// DeriveTitle 首问 → 标题：剥引用标号、压平空白、截断。
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

// SessionState 账本折叠态。
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

// ProposalStateOf 提议的有效状态：记录 pending 但已不是当前待定项 = withdrawn。
func (s *SessionState) ProposalStateOf(pid string) string {
	recorded, _ := s.Proposals[pid]["state"].(string)
	if recorded == "pending" {
		if pending, _ := s.PendingProposal["pid"].(string); pending != pid {
			return "withdrawn"
		}
	}
	return recorded
}

// SessionStore 会话账本存取（目录 = <data>/pilot/sessions）。
type SessionStore struct {
	base string
	mu   sync.Mutex
}

// NewSessionStore 构造存储并做文件数清扫（超上限删最旧）。
func NewSessionStore(base string) *SessionStore {
	_ = os.MkdirAll(base, 0o755)
	store := &SessionStore{base: base}
	store.sweep()
	return store
}

// Base 会话文件目录（flywheel 投影输入）。
func (st *SessionStore) Base() string { return st.base }

func (st *SessionStore) path(sid string) string {
	return filepath.Join(st.base, sid+".jsonl")
}

// Load 载入折叠态；无文件返回 nil。
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

// RawRecords 原始记录行（不折叠，坏行已滤）：决策飞轮反馈投影输入。
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

// LoadOrNew 载入或新建（内存态，首写落盘）。
func (st *SessionStore) LoadOrNew(sid string) *SessionState {
	if state := st.Load(sid); state != nil {
		return state
	}
	return &SessionState{Sid: sid}
}

// AppendTurn 落一轮问答；首问自动起名。返回是否落账成功。
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

// SetTurnFeedback 追加用户评价；返回错误码（nil=成功）。
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

// SetTitle 追加改名记录（后者胜）。
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

// AppendSummary 压缩落账：摘要 + 边界事实（对话流分隔线由此还原）。
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

// AppendProposal 批量提议落账（同 pid 后者胜）。
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

// SetPinned 置顶开/关。
func (st *SessionStore) SetPinned(sid string, on bool) bool {
	if !ValidSID(sid) {
		return false
	}
	return st.append(sid, map[string]any{"t": "pin", "on": on})
}

// SetArchived 归档开/关。
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

// DeleteWithReason 删除会话及飞轮影子记录、附件；返回终态机器码。
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

// TitleOf 生效标题：账本 title 行优先，无则首问兜底。
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

// ListRecent 最近会话投影：mtime 倒序、置顶在前；q 命中标题或问答正文。
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
	sort.Slice(files, func(i, j int) bool { return files[i].mtime.After(files[j].mtime) })
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
	sort.SliceStable(out, func(i, j int) bool {
		pi, _ := out[i]["pinned"].(bool)
		pj, _ := out[j]["pinned"].(bool)
		return !pi && pj
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

func matchState(state *SessionState, title, needle string) (bool, string) {
	if strings.Contains(strings.ToLower(title), needle) {
		return true, ""
	}
	for _, turn := range state.Turns {
		q, _ := turn["q"].(string)
		if pos := strings.Index(strings.ToLower(q), needle); pos >= 0 {
			return true, clipSnippet(q, pos, len([]rune(needle)))
		}
		resp, _ := turn["resp"].(map[string]any)
		text, _ := resp["answer"].(string)
		if pos := strings.Index(strings.ToLower(text), needle); pos >= 0 {
			return true, clipSnippet(text, pos, len([]rune(needle)))
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

// append 追加记录并应用折叠。
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

// apply 单条记录折叠（与 session._apply 逐支对齐）。
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

// sweep 文件数超上限时删最旧（确定性清扫）。
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
	sort.Slice(files, func(i, j int) bool { return files[i].mtime.Before(files[j].mtime) })
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
