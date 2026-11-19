package notifications

import (
	"database/sql"
	"net/http"
	"regexp"
	"strconv"
	"strings"

	"holdexar/server-go/internal/httpx"
)

var retryableHints = []string{
	"smtpserverdisconnected", "smtpconnecterror", "smtpresponsenotready",
	"connectionrefusederror", "connectionreseterror", "timeouterror", "gaierror",
	"timeout", "timed out", "temporar", "network", "unreachable", "broken pipe",
	"closed", "421", "450", "451", "452",
	"拒绝", "无法连接", "超时", "网络", "重置", "断开",
}

var permanentHints = []string{
	"smtpauthenticationerror", "authentication", "535", "534", "530",
	"username and password", "login", "未配置",
	"smtpsenderrefused", "smtphelorefused", "smtpdataerror", "smtputf8error",
	"553", "550", "554", "552", "551", "no such",
}

const (
	unknownDeliveryPrefix = "SMTPDeliveryUnknown:"
	rejectedDeliveryExpr  = `^SMTPRejected: ([45][0-9]{2})(?: |$)`
)

var rejectedDeliveryRe = regexp.MustCompile(rejectedDeliveryExpr)

func deliveryUnknown(errText string) bool {
	return strings.HasPrefix(errText, unknownDeliveryPrefix) || strings.HasPrefix(errText, "PluginDeliveryUnknown:")
}

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

type statusStat struct {
	Count    int64 `json:"count"`
	Attempts int64 `json:"attempts"`
}

type statsResponse struct {
	Total       int64                 `json:"total"`
	Delivered   int64                 `json:"delivered"`
	Pending     int64                 `json:"pending"`
	Suppressed  int64                 `json:"suppressed"`
	Sending     int64                 `json:"sending"`
	Unknown     int64                 `json:"unknown"`
	Failed      int64                 `json:"failed"`
	Retryable   int64                 `json:"retryable"`
	Permanent   int64                 `json:"permanent"`
	Attempts    int64                 `json:"attempts"`
	MaxAttempts int                   `json:"maxAttempts"`
	ByStatus    map[string]statusStat `json:"byStatus"`
}

func (h *handler) getStats(w http.ResponseWriter, r *http.Request) {
	rows, err := h.db.QueryContext(r.Context(),
		"SELECT status, COUNT(id), COALESCE(SUM(attempts), 0)"+
			" FROM notification_candidates GROUP BY status")
	if err != nil {
		httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
		return
	}
	byStatus := map[string]statusStat{}
	for rows.Next() {
		var status string
		var count, attempts int64
		if err := rows.Scan(&status, &count, &attempts); err != nil {
			rows.Close()
			httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
			return
		}
		byStatus[status] = statusStat{Count: count, Attempts: attempts}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
		return
	}
	rows.Close()

	failedRows, err := h.db.QueryContext(r.Context(),
		"SELECT attempts, last_error FROM notification_candidates WHERE status = 'failed'")
	if err != nil {
		httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
		return
	}
	defer failedRows.Close()
	canRetry := int64(0)
	for failedRows.Next() {
		var attempts sql.NullInt64
		var lastError sql.NullString
		if err := failedRows.Scan(&attempts, &lastError); err != nil {
			httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
			return
		}
		if attemptsOrZero(attempts) < maxAttempts && isRetryable(lastError.String) {
			canRetry++
		}
	}
	if err := failedRows.Err(); err != nil {
		httpx.WriteJSON(w, http.StatusInternalServerError, httpx.DetailNull())
		return
	}

	failed := byStatus["failed"].Count
	total, totalAttempts := int64(0), int64(0)
	for _, b := range byStatus {
		total += b.Count
		totalAttempts += b.Attempts
	}
	httpx.WriteJSON(w, http.StatusOK, statsResponse{
		Total:       total,
		Delivered:   byStatus["delivered"].Count,
		Pending:     byStatus["pending"].Count,
		Suppressed:  byStatus["suppressed"].Count,
		Sending:     byStatus["sending"].Count,
		Unknown:     byStatus[statusUnknown].Count,
		Failed:      failed,
		Retryable:   canRetry,
		Permanent:   failed - canRetry,
		Attempts:    totalAttempts,
		MaxAttempts: maxAttempts,
		ByStatus:    byStatus,
	})
}
