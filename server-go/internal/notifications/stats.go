package notifications

import (
	"database/sql"
	"net/http"
	"regexp"
	"strconv"
	"strings"
)

// 临时故障判据：异常类型名优先、英文措辞其次、中文措辞兜底（源
// notifications/service.py _RETRYABLE_HINTS / _PERMANENT_HINTS，两清单须同步）。
var retryableHints = []string{
	"smtpserverdisconnected", "smtpconnecterror", "smtpresponsenotready",
	"connectionrefusederror", "connectionreseterror", "timeouterror", "gaierror",
	"timeout", "timed out", "temporar", "network", "unreachable", "broken pipe",
	"closed", "421", "450", "451", "452",
	"拒绝", "无法连接", "超时", "网络", "重置", "断开",
}

// 永久失败判据：凭据/地址/配置类，重试只会重复发出同一封失败邮件。
var permanentHints = []string{
	"smtpauthenticationerror", "authentication", "535", "534", "530",
	"username and password", "login", "未配置",
	"smtpsenderrefused", "smtphelorefused", "smtpdataerror", "smtputf8error",
	"553", "550", "554", "552", "551", "no such",
}

// 投递出口的结构化错误前缀（源 alerts/notify.py，前缀改动须两处同步）。
const (
	unknownDeliveryPrefix = "SMTPDeliveryUnknown:"
	rejectedDeliveryExpr  = `^SMTPRejected: ([45][0-9]{2})(?: |$)`
)

var rejectedDeliveryRe = regexp.MustCompile(rejectedDeliveryExpr)

// deliveryUnknown 结果是否无法确认（进程中断时可能已发出）。
func deliveryUnknown(errText string) bool {
	return errText != "" && strings.HasPrefix(errText, unknownDeliveryPrefix)
}

// deliveryRejectionCode 服务器拒绝码；无结构化码回 (0, false)。
func deliveryRejectionCode(errText string) (int, bool) {
	m := rejectedDeliveryRe.FindStringSubmatch(errText)
	if m == nil {
		return 0, false
	}
	code, _ := strconv.Atoi(m[1])
	return code, true
}

// isRetryable 这次失败是否值得重试：未知错误一律不可重试（宁可丢一封不盲发）。
func isRetryable(errText string) bool {
	if errText == "" || deliveryUnknown(errText) {
		return false
	}
	if code, ok := deliveryRejectionCode(errText); ok {
		return code < 500
	}
	text := strings.ToLower(errText)
	for _, hint := range permanentHints {
		if strings.Contains(text, hint) {
			return false
		}
	}
	for _, hint := range retryableHints {
		if strings.Contains(text, hint) {
			return true
		}
	}
	return false
}

// getStats 投递生产指标：按候选状态聚合计数与尝试次数。
func (h *handler) getStats(w http.ResponseWriter, r *http.Request) {
	rows, err := h.db.QueryContext(r.Context(),
		"SELECT status, COUNT(id), COALESCE(SUM(attempts), 0)"+
			" FROM notification_candidates GROUP BY status")
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
		return
	}
	type bucket struct {
		count    int64
		attempts int64
	}
	counts := map[string]*bucket{}
	byStatus := newOrderedObj()
	for rows.Next() {
		var status string
		var count, attempts int64
		if err := rows.Scan(&status, &count, &attempts); err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
			return
		}
		counts[status] = &bucket{count: count, attempts: attempts}
		byStatus.set(status, kvObj("count", count, "attempts", attempts))
	}
	if err := rows.Err(); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
		return
	}
	rows.Close()

	failedRows, err := h.db.QueryContext(r.Context(),
		"SELECT attempts, last_error FROM notification_candidates WHERE status = 'failed'")
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
		return
	}
	defer failedRows.Close()
	canRetry := int64(0)
	for failedRows.Next() {
		var attempts sql.NullInt64
		var lastError sql.NullString
		if err := failedRows.Scan(&attempts, &lastError); err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
			return
		}
		if attemptsOrZero(attempts) < maxAttempts && isRetryable(lastError.String) {
			canRetry++
		}
	}
	if err := failedRows.Err(); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
		return
	}

	failed := int64(0)
	if b, ok := counts["failed"]; ok {
		failed = b.count
	}
	total, totalAttempts := int64(0), int64(0)
	for _, b := range counts {
		total += b.count
		totalAttempts += b.attempts
	}
	statusCount := func(status string) int64 {
		if b, ok := counts[status]; ok {
			return b.count
		}
		return 0
	}
	writeJSON(w, http.StatusOK, kvObj(
		"total", total,
		"delivered", statusCount("delivered"),
		"pending", statusCount("pending"),
		"suppressed", statusCount("suppressed"),
		"sending", statusCount("sending"),
		"unknown", statusCount(statusUnknown),
		"failed", failed,
		// 失败里还能再试的（临时故障且未到次数上限）与已经永久放弃的
		"retryable", canRetry,
		"permanent", failed-canRetry,
		"attempts", totalAttempts,
		"maxAttempts", maxAttempts,
		"byStatus", byStatus,
	))
}
