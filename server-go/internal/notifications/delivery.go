package notifications

import (
	"context"
	"database/sql"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/config"
	"holdexar/server-go/internal/plugins"
	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/store"
)

type DeliveryOptions struct {
	Plugins    *plugins.Manager
	PluginHTTP *http.Client
	Now        func() time.Time
	DataDir    string
	Log        func(string, ...any)
	Watched    func(context.Context) ([]int64, error)
	Send       func(context.Context, SMTPConfig, string, string) (bool, string)
	Render     func(context.Context, []DeliveryCandidate, *int64, map[string]any) (string, string, error)
}
type DeliveryService struct {
	db      *sql.DB
	gate    *store.WriteGate
	options DeliveryOptions
	mu      sync.Mutex
	closing bool
	jobs    sync.WaitGroup
}

func NewDelivery(db *sql.DB, gate *store.WriteGate, options DeliveryOptions) *DeliveryService {
	if options.Now == nil {
		options.Now = time.Now
	}
	if options.Log == nil {
		options.Log = func(string, ...any) {}
	}
	if options.Send == nil {
		options.Send = SendMail
	}
	s := &DeliveryService{db: db, gate: gate, options: options}
	if s.options.Render == nil {
		s.options.Render = s.render
	}
	return s
}

type DispatchResult struct {
	Created   int64 `json:"created"`
	Sent      int64 `json:"sent"`
	Deferred  int   `json:"deferred"`
	Failed    int64 `json:"failed"`
	Retried   int   `json:"retried"`
	Retryable bool  `json:"retryable"`
	Unknown   int64 `json:"unknown"`
}

func (s *DeliveryService) Close(ctx context.Context) error {
	s.mu.Lock()
	s.closing = true
	s.mu.Unlock()
	done := make(chan struct{})
	go func() { s.jobs.Wait(); close(done) }()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
func (s *DeliveryService) ReadSMTP(ctx context.Context) (SMTPConfig, error) {
	rows, e := s.db.QueryContext(ctx, `SELECT key,value_json FROM app_settings WHERE key IN('smtp.host','smtp.port','smtp.user','smtp.password','smtp.to_addr','smtp.use_ssl')`)
	if e != nil {
		return SMTPConfig{}, e
	}
	values := map[string]any{}
	for rows.Next() {
		var key string
		var raw sql.NullString
		if e = rows.Scan(&key, &raw); e != nil {
			rows.Close()
			return SMTPConfig{}, e
		}
		if raw.Valid {
			values[key] = parseJSONNumber(raw.String)
		}
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return SMTPConfig{}, e
	}
	cfg := SMTPConfig{Host: stringValue(values["smtp.host"]), Port: int(pyInt(values["smtp.port"], 465)), User: stringValue(values["smtp.user"]), Password: stringValue(values["smtp.password"]), ToAddr: stringValue(values["smtp.to_addr"]), UseSSL: true}
	if v, ok := values["smtp.use_ssl"]; ok {
		cfg.UseSSL = truthy(v)
	}
	if secretbox.IsEncrypted(cfg.Password) {
		dataDir := s.options.DataDir
		if dataDir == "" {
			dataDir, e = config.ResolveDataDir()
			if e != nil {
				return SMTPConfig{}, e
			}
		}
		plain, err := secretbox.DecryptSecretIn(dataDir, cfg.Password, secretbox.SecretPurpose(smtpPasswordKey))
		if err != nil {
			s.options.Log("读取邮件授权信息失败，等待重新配置")
			cfg.Password = ""
		} else {
			cfg.Password = plain
		}
	}
	return cfg, nil
}
func stringValue(v any) string {
	if v == nil {
		return ""
	}
	return fmt.Sprint(v)
}
func (s *DeliveryService) Dispatch(ctx context.Context, cycleID *int64) (result DispatchResult, err error) {
	s.mu.Lock()
	if s.closing {
		s.mu.Unlock()
		return result, fmt.Errorf("提醒服务正在退出")
	}
	s.jobs.Add(1)
	s.mu.Unlock()
	defer s.jobs.Done()
	var claimed []DeliveryCandidate
	protocolStarted, settled := false, false
	cleanupCtx := func() (context.Context, context.CancelFunc) {
		return context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	}
	defer func() {
		if recovered := recover(); recovered != nil {
			err = fmt.Errorf("准备或发送提醒失败：%v", recovered)
		}
		if len(claimed) > 0 && !settled {
			terminal, cancel := cleanupCtx()
			defer cancel()
			if protocolStarted {
				message := "SMTPDeliveryUnknown: delivery interrupted"
				reason := "邮件发送结果无法确认，已停止自动重发"
				n, e := s.settle(terminal, claimed, "unknown", &reason, &message)
				result.Unknown += n
				if err == nil {
					err = e
				}
			} else if e := s.release(terminal, claimed); err == nil {
				err = e
			}
		}
		if err != nil {
			s.options.Log("通知投递未完成，已保留原价格结果：%v", err)
		}
	}()
	result.Unknown, err = s.Recover(ctx)
	if err != nil {
		return result, err
	}
	prefs, e := LoadPrefs(ctx, s.db)
	if e != nil {
		return result, e
	}
	recipient, e := savedRecipient(ctx, s.db)
	if e != nil {
		return result, e
	}
	result.Created, err = s.ResumeEventScans(ctx, cycleID)
	if err != nil {
		return result, err
	}
	if !truthy(prefs["enabled"]) || recipient == "" {
		return result, nil
	}
	if err = s.fillMissingDue(ctx); err != nil {
		return result, err
	}
	err = s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		recipient, e := savedRecipient(ctx, conn)
		if e != nil {
			return e
		}
		_, e = retargetTx(ctx, conn, recipient, s.options.Now())
		return e
	})
	if err != nil {
		return result, err
	}
	if InQuietWindow(s.options.Now(), prefs) {
		rows, e := readCandidates(ctx, s.db, `WHERE status IN('pending','suppressed','failed')`)
		if e != nil {
			return result, e
		}
		for _, c := range rows {
			if eligible(c, nil, s.options.Now()) {
				result.Deferred++
			}
		}
		return result, nil
	}
	claimed, err = s.claim(ctx)
	if err != nil || len(claimed) == 0 {
		return result, err
	}
	for _, c := range claimed {
		if c.Status == "failed" {
			result.Retried++
		}
		if cycleID == nil || c.CycleID != *cycleID {
			result.Deferred++
		}
	}
	subject, body, e := s.options.Render(ctx, claimed, cycleID, prefs)
	if e != nil {
		return result, e
	}
	if e = ctx.Err(); e != nil {
		return result, e
	}
	protocolStarted = true
	ok, problem := s.sendNotification(ctx, claimed[0].Recipient, subject, body)
	terminal, cancel := cleanupCtx()
	defer cancel()
	if strings.HasPrefix(problem, "DeliveryNotStarted:") {
		protocolStarted = false
		err = s.release(terminal, claimed)
	} else if ok {
		result.Sent, err = s.settle(terminal, claimed, "delivered", nil, nil)
	} else if deliveryUnknown(problem) {
		reason := "通知发送结果无法确认，已停止自动重发"
		var n int64
		n, err = s.settle(terminal, claimed, "unknown", &reason, &problem)
		result.Unknown += n
	} else {
		reason := "delivery: " + problem
		result.Failed, err = s.settle(terminal, claimed, "failed", &reason, &problem)
		result.Retryable = isRetryable(problem)
	}
	settled = true
	cycle := "无周期"
	if cycleID != nil {
		cycle = fmt.Sprint(*cycleID)
	}
	s.options.Log("通知本轮投递完成 │ 周期=%s，提醒记录=%d..%d，新增=%d，成功=%d，失败=%d，结果未知=%d", cycle, claimed[0].ID, claimed[len(claimed)-1].ID, result.Created, result.Sent, result.Failed, result.Unknown)
	return result, err
}

func (s *DeliveryService) SaveSMTP(ctx context.Context, cfg SMTPConfig) error {
	values := map[string]any{"smtp.host": cfg.Host, "smtp.port": cfg.Port, "smtp.user": cfg.User, "smtp.to_addr": cfg.ToAddr, "smtp.use_ssl": cfg.UseSSL}
	if cfg.Password != "" {
		dataDir := s.options.DataDir
		var e error
		if dataDir == "" {
			dataDir, e = config.ResolveDataDir()
			if e != nil {
				return e
			}
		}
		encrypted, e := secretbox.EncryptSecretIn(dataDir, cfg.Password, secretbox.SecretPurpose(smtpPasswordKey))
		if e != nil {
			return e
		}
		values[smtpPasswordKey] = encrypted
	}
	payload := []map[string]any{}
	for key, value := range values {
		payload = append(payload, map[string]any{"key": key, "value": value})
	}
	raw, e := deliveryJSON(payload)
	if e != nil {
		return e
	}
	return s.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		_, e := conn.ExecContext(ctx, `INSERT INTO app_settings(key,value_json,updated_at) SELECT value->>'key',value->'value',? FROM json_each(?) WHERE 1 ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at`, deliveryStamp(s.options.Now()), raw)
		if e != nil {
			return e
		}
		_, e = retargetTx(ctx, conn, cfg.ToAddr, s.options.Now())
		return e
	})
}
func (s *DeliveryService) SMTPView(ctx context.Context) (map[string]any, error) {
	cfg, e := s.ReadSMTP(ctx)
	if e != nil {
		return nil, e
	}
	password := []rune(cfg.Password)
	masked := ""
	if len(password) > 2 {
		masked = string(password[0]) + strings.Repeat("*", len(password)-2) + string(password[len(password)-1])
	} else {
		masked = strings.Repeat("*", len(password))
	}
	return map[string]any{"host": cfg.Host, "port": cfg.Port, "user": cfg.User, "password": masked, "hasPassword": cfg.Password != "", "toAddr": cfg.ToAddr, "useSsl": cfg.UseSSL}, nil
}
