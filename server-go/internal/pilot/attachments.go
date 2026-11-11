//go:build windows

// attachments.go 领航员附件：长文本落盘为外部资源，消息体只留引用。
// 布局 <data>/pilot/attachments/<sid>/<aid>.txt|.json；sid/aid 白名单封死
// 路径拼接面。语义对齐 pilot/attachments.py。
package pilot

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

const (
	// InlineLimitChars 内联阈值：超过即转附件（粘贴落文件口径）。
	InlineLimitChars = 15360
	previewLines     = 30
	previewChars     = 1200
	// ReadBlockChars 单次读取块预算：超出收窄区间并显式告知截断。
	ReadBlockChars = 24000
	// MaxAttachChars 单份附件上限（防御性封顶）。
	MaxAttachChars = 2000000
	maxFilesPerSid = 60
	defaultName    = "粘贴文本"
)

var aidRe = regexp.MustCompile(`^[0-9a-f]{12}$`)

// ValidAID 附件 id 白名单。
func ValidAID(aid string) bool { return aidRe.MatchString(aid) }

func attachmentsBase(dataDir string) string {
	return filepath.Join(dataDir, "pilot", "attachments")
}

func attachmentsDir(dataDir, sid string) string {
	if !ValidSID(sid) {
		return ""
	}
	return filepath.Join(attachmentsBase(dataDir), sid)
}

// ShouldInline 文本能否直接进提问字段。
func ShouldInline(text string) bool { return len([]rune(text)) <= InlineLimitChars }

func countLines(text string) int { return strings.Count(text, "\n") + 1 }

// PreviewOf 附件开头预览（行与字符双上限，先到者为准）。
func PreviewOf(text string) string {
	used := 0
	out := []string{}
	lines := strings.Split(text, "\n")
	if len(lines) > previewLines {
		lines = lines[:previewLines]
	}
	for _, line := range lines {
		if used+len([]rune(line))+1 > previewChars {
			r := []rune(line)
			remain := previewChars - used
			if remain < 0 {
				remain = 0
			}
			if remain > len(r) {
				remain = len(r)
			}
			out = append(out, string(r[:remain]))
			break
		}
		out = append(out, line)
		used += len([]rune(line)) + 1
	}
	return strings.Join(out, "\n")
}

// AttachmentRef 附件引用（不含磁盘路径与全文）。
type AttachmentRef struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	Chars     int    `json:"chars"`
	Lines     int    `json:"lines"`
	CreatedAt string `json:"created_at"`
}

func refOf(meta map[string]any) AttachmentRef {
	ref := AttachmentRef{
		ID:    stringOf(meta["id"]),
		Name:  stringOf(meta["name"]),
		Chars: jsonInt(meta["chars"]),
		Lines: jsonInt(meta["lines"]),
	}
	if ref.Name == "" {
		ref.Name = defaultName
	}
	if ts, ok := meta["created_at"].(string); ok {
		ref.CreatedAt = ts
	}
	return ref
}

// SaveAttachment 长文本落盘；sid 非法/内容空/超上限返回 ok=false。
func SaveAttachment(dataDir, sid, text, name string) (AttachmentRef, bool) {
	ref := AttachmentRef{}
	directory := attachmentsDir(dataDir, sid)
	body := text
	if directory == "" || strings.TrimSpace(body) == "" || len([]rune(body)) > MaxAttachChars {
		return ref, false
	}
	aid := newAID()
	meta := map[string]any{
		"id": aid, "name": attachmentName(name), "chars": len([]rune(body)),
		"lines": countLines(body), "created_at": time.Now().Format("2006-01-02T15:04:05"),
	}
	if err := os.MkdirAll(directory, 0o755); err != nil {
		return ref, false
	}
	metaPayload, _ := json.Marshal(meta)
	if err := os.WriteFile(filepath.Join(directory, aid+".txt"), []byte(body), 0o644); err != nil {
		return ref, false
	}
	// 写入顺序：先全文后元数据——元数据存在即代表附件可读。
	if err := os.WriteFile(filepath.Join(directory, aid+".json"), metaPayload, 0o644); err != nil {
		return ref, false
	}
	sweepAttachments(directory)
	return refOf(meta), true
}

func attachmentName(name string) string {
	trimmed := strings.TrimSpace(name)
	if trimmed == "" {
		return defaultName
	}
	if runes := []rune(trimmed); len(runes) > 80 {
		return string(runes[:80])
	}
	return trimmed
}

func newAID() string {
	buf := make([]byte, 6)
	_, _ = rand.Read(buf)
	return hex.EncodeToString(buf)
}

func attachmentMeta(dataDir, sid, aid string) map[string]any {
	directory := attachmentsDir(dataDir, sid)
	if directory == "" || !ValidAID(aid) {
		return nil
	}
	raw, err := os.ReadFile(filepath.Join(directory, aid+".json"))
	if err != nil {
		return nil
	}
	var meta map[string]any
	if json.Unmarshal(raw, &meta) != nil {
		return nil
	}
	return meta
}

func attachmentText(dataDir, sid, aid string) string {
	directory := attachmentsDir(dataDir, sid)
	if directory == "" || !ValidAID(aid) {
		return ""
	}
	raw, err := os.ReadFile(filepath.Join(directory, aid+".txt"))
	if err != nil {
		return ""
	}
	return string(raw)
}

// ListAttachments 会话附件引用清单。
func ListAttachments(dataDir, sid string) []AttachmentRef {
	directory := attachmentsDir(dataDir, sid)
	out := []AttachmentRef{}
	if directory == "" {
		return out
	}
	entries, err := os.ReadDir(directory)
	if err != nil {
		return out
	}
	type metaEntry struct {
		meta AttachmentRef
		path string
	}
	var metas []metaEntry
	for _, entry := range entries {
		if !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		aid := strings.TrimSuffix(entry.Name(), ".json")
		if !ValidAID(aid) {
			continue
		}
		if meta := attachmentMeta(dataDir, sid, aid); meta != nil {
			metas = append(metas, metaEntry{meta: refOf(meta),
				path: filepath.Join(directory, aid+".json")})
		}
	}
	sort.Slice(metas, func(i, j int) bool { return metas[i].path < metas[j].path })
	for _, meta := range metas {
		out = append(out, meta.meta)
	}
	return out
}

// PurgeAttachments 会话删除时清理附件（含目录）。
func PurgeAttachments(dataDir, sid string) {
	if directory := attachmentsDir(dataDir, sid); directory != "" {
		_ = os.RemoveAll(directory)
	}
}

// purgeAttachments 供 session 存储调用的清理钩子（路径由会话基址推导）。
func purgeAttachments(sessionsBase, sid string) {
	if !ValidSID(sid) {
		return
	}
	// sessionsBase = <data>/pilot/sessions → 附件目录是其兄弟目录
	dataDir := filepath.Dir(sessionsBase)
	PurgeAttachments(dataDir, sid)
}

// sweepAttachments 单会话附件数超上限删最旧。
func sweepAttachments(directory string) {
	entries, err := os.ReadDir(directory)
	if err != nil || len(entries) <= maxFilesPerSid*2 {
		return
	}
	type metaEntry struct {
		aid   string
		mtime time.Time
	}
	var metas []metaEntry
	for _, entry := range entries {
		if !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			continue
		}
		metas = append(metas, metaEntry{aid: strings.TrimSuffix(entry.Name(), ".json"),
			mtime: info.ModTime()})
	}
	if len(metas) <= maxFilesPerSid {
		return
	}
	sort.Slice(metas, func(i, j int) bool { return metas[i].mtime.Before(metas[j].mtime) })
	for _, meta := range metas[:len(metas)-maxFilesPerSid] {
		_ = os.Remove(filepath.Join(directory, meta.aid+".txt"))
		_ = os.Remove(filepath.Join(directory, meta.aid+".json"))
	}
}

// ReadRange 按行号区间读附件（1-based；与模型侧读取同一实现与预算）。
func ReadRange(dataDir, sid, aid string, startLine, endLine int) map[string]any {
	meta := attachmentMeta(dataDir, sid, aid)
	if meta == nil {
		return nil
	}
	text := attachmentText(dataDir, sid, aid)
	lines := strings.Split(text, "\n")
	totalLines := len(lines)
	if startLine < 1 {
		startLine = 1
	}
	if endLine == 0 || endLine > totalLines {
		endLine = totalLines
	}
	truncated := false
	if (endLine-startLine+1) > ReadBlockChars/80 || endLine-startLine+1 > 2000 {
		endLine = startLine + ReadBlockChars/80 - 1
		if endLine > totalLines {
			endLine = totalLines
		}
		truncated = true
	}
	if startLine > endLine {
		return map[string]any{"id": aid, "lines": "", "total_lines": totalLines,
			"start_line": startLine, "end_line": startLine - 1, "truncated": false}
	}
	selected := lines[startLine-1 : endLine]
	content := strings.Join(selected, "\n")
	if len([]rune(content)) > ReadBlockChars {
		content = string([]rune(content)[:ReadBlockChars])
		truncated = true
	}
	return map[string]any{
		"id": aid, "name": stringOf(meta["name"]), "lines": content,
		"total_lines": totalLines, "start_line": startLine, "end_line": endLine,
		"truncated": truncated,
	}
}

// ResolveRefsWithText 提问携带的附件 id → 引用列表（含全文注入用文本）；
// 不存在/非法 id 静默丢弃。
func ResolveRefsWithText(dataDir, sid string, ids []string, question string) []struct {
	Ref  AttachmentRef
	Text string
} {
	out := []struct {
		Ref  AttachmentRef
		Text string
	}{}
	for _, id := range ids {
		meta := attachmentMeta(dataDir, sid, id)
		if meta == nil {
			continue
		}
		out = append(out, struct {
			Ref  AttachmentRef
			Text string
		}{Ref: refOf(meta), Text: attachmentText(dataDir, sid, id)})
	}
	return out
}
