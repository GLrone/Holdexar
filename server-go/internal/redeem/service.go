package redeem

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"holdexar/server-go/internal/account"
	"holdexar/server-go/internal/store"
)

// registerkey 错误码映射（53 表示 30 分钟 10 次上限，
// 配额耗尽时商店不查 key 直接返回 53）。
var errorCodes = map[int]string{
	9:  "已拥有",
	13: "地区限制",
	14: "无效激活码",
	15: "重复激活",
	24: "缺少主游戏",
	36: "需要PS3验证",
	50: "这是充值码",
	53: "次数上限（30 分钟内最多 10 次激活尝试，请稍后再试）",
}

const rawMax = 8000

const rawDisplayMax = 2000

// 批量激活节奏：同批 9 个并发、组间串行（防误操作一把梭 50 个 key 打爆限额）。
const batchChunkSize = 9

const ledgerStamp = "2006-01-02 15:04:05.000000"

type ServiceOptions struct {
	Accounts *account.Service
	Proxy    func(context.Context) (string, error)
	Log      func(string, ...any)
	Now      func() time.Time
}

type InputError struct{ Message string }

func (e *InputError) Error() string { return e.Message }

type Service struct {
	db      *sql.DB
	gate    *store.WriteGate
	opts    ServiceOptions
	counter *counter
}

func NewService(db *sql.DB, gate *store.WriteGate, opts ServiceOptions) *Service {
	if opts.Log == nil {
		opts.Log = func(string, ...any) {}
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	return &Service{db: db, gate: gate, opts: opts, counter: newCounter(ACTWindowSeconds*time.Second, opts.Now)}
}

func (s *Service) AccountUsage(steamID string) int { return s.counter.usage(steamID) }

type Result struct {
	Status  string `json:"status"`
	Detail  string `json:"detail"`
	SubID   string `json:"subId"`
	SubName string `json:"subName"`
	Raw     string `json:"raw"`
}

type BatchRow struct {
	Code string `json:"code"`
	Result
}

type BatchOutcome struct {
	Results []BatchRow `json:"results"`
	OK      int        `json:"ok"`
	Own     int        `json:"own"`
	Fail    int        `json:"fail"`
}

type Quota struct {
	HasCookie    bool   `json:"hasCookie"`
	HasSessionID bool   `json:"hasSessionId"`
	SteamID      string `json:"steamId"`
	Used         int    `json:"used"`
	Limit        int    `json:"limit"`
}

func (s *Service) ActivateKey(ctx context.Context, productKey string) (Result, error) {
	session, err := s.opts.Accounts.StoreSession(ctx, "", false)
	if err != nil {
		var wallet *account.WalletError
		if errors.As(err, &wallet) && wallet.Kind == "expired" {
			// 过期登录态不出网请求，直接返回失败且不计激活次数。
			steamID := session.SteamID
			if steamID == "" {
				steamID = "unknown"
			}
			result := Result{Status: "fail", Detail: "Cookie 已失效，请重新绑定"}
			if e := s.recordLedger(ctx, ledgerRow{Kind: "cdk", Status: result.Status, ProductKey: productKey, Detail: result.Detail, SteamID: steamID}); e != nil {
				return Result{}, e
			}
			return result, nil
		}
		if errors.Is(err, account.ErrAccountNotFound) || errors.Is(err, account.ErrNoStoreCredentials) {
			return Result{}, &InputError{"尚未绑定 Steam Cookie（请先在「我」页绑定）"}
		}
		return Result{}, err
	}
	sessionID := account.ParseCookies(session.Cookie)["sessionid"]
	if sessionID == "" {
		return Result{}, &InputError{"Cookie 中缺少 sessionid，请重新复制完整 Cookie"}
	}
	steamID := account.CookieSteamID(session.Cookie)
	if steamID == "" {
		steamID = "unknown"
	}
	var result Result
	if status, body, err := s.postRegisterKey(ctx, session.Cookie, sessionID, productKey); err != nil {
		// 传输层失败：Steam 未收到，不计激活尝试次数。
		result = Result{Status: "fail", Detail: fmt.Sprintf("激活请求失败：%s", err)}
	} else {
		s.counter.record(steamID)
		result = interpretRegisterKey(status, body)
	}
	row := ledgerRow{Kind: "cdk", Status: result.Status, ProductKey: productKey, SubID: result.SubID, SubName: result.SubName, Detail: result.Detail, Raw: result.Raw, SteamID: steamID}
	if err := s.recordLedger(ctx, row); err != nil {
		return Result{}, err
	}
	return result, nil
}

func (s *Service) ActivateBatch(ctx context.Context, keys []string) (BatchOutcome, error) {
	if len(keys) == 0 {
		return BatchOutcome{}, &InputError{"激活码列表为空"}
	}
	outcome := BatchOutcome{Results: []BatchRow{}}
	for start := 0; start < len(keys); start += batchChunkSize {
		end := min(start+batchChunkSize, len(keys))
		rows := make([]BatchRow, end-start)
		errs := make([]error, end-start)
		var wg sync.WaitGroup
		for i := start; i < end; i++ {
			wg.Add(1)
			go func(idx int, key string) {
				defer wg.Done()
				res, err := s.ActivateKey(ctx, key)
				if err != nil {
					errs[idx-start] = err
					return
				}
				rows[idx-start] = BatchRow{Code: key, Result: res}
			}(i, keys[i])
		}
		wg.Wait()
		for i := range errs {
			if errs[i] != nil {
				return BatchOutcome{}, errs[i]
			}
		}
		for _, row := range rows {
			outcome.Results = append(outcome.Results, row)
			switch row.Status {
			case "ok":
				outcome.OK++
			case "own":
				outcome.Own++
			default:
				outcome.Fail++
			}
		}
	}
	return outcome, nil
}

// QuotaStatus 返回当前账号维度 Cookie 与激活额度（禁止跨账号共享计数）。
func (s *Service) QuotaStatus(ctx context.Context) (Quota, error) {
	quota := Quota{Limit: ACTLimit}
	session, err := s.opts.Accounts.StoreSession(ctx, "", false)
	if err != nil {
		var wallet *account.WalletError
		if errors.As(err, &wallet) && wallet.Kind == "expired" {
			// StoreSession 拒绝放行过期 Cookie；按账号身份回填状态以引导重绑。
			quota.HasCookie = true
			quota.HasSessionID = true
			quota.SteamID = session.SteamID
			return quota, nil
		}
		if errors.Is(err, account.ErrAccountNotFound) || errors.Is(err, account.ErrNoStoreCredentials) {
			return quota, nil
		}
		return quota, err
	}
	jar := account.ParseCookies(session.Cookie)
	_, hasLogin := jar["steamLoginSecure"]
	_, hasSession := jar["sessionid"]
	quota.HasCookie = hasLogin
	quota.HasSessionID = hasLogin && hasSession
	if hasLogin {
		quota.SteamID = account.CookieSteamID(session.Cookie)
	}
	if quota.SteamID != "" {
		quota.Used = s.counter.usage(quota.SteamID)
	}
	return quota, nil
}

// activationsSearchWindow 检索上限。
// shortcut: 2000 条≈本地激活账本全量，超窗后将 status/q/key 筛选下推为参数化 SQL。
const activationsSearchWindow = 2000

func (s *Service) SearchActivations(ctx context.Context, status, q, key string, limit, offset int) (ActivationPage, error) {
	limit = max(1, min(limit, 200))
	offset = max(0, offset)
	status = strings.ToLower(strings.TrimSpace(status))
	switch status {
	case "", "ok", "own", "fail":
	default:
		return ActivationPage{}, &InputError{fmt.Sprintf("未知的结果筛选：%s", status)}
	}
	needle := strings.ToLower(strings.TrimSpace(q))
	code := strings.TrimSpace(key)
	var window []ActivationRow
	for len(window) < activationsSearchWindow {
		page, err := s.ListActivations(ctx, 200, len(window))
		if err != nil {
			return ActivationPage{}, err
		}
		if len(page.Rows) == 0 {
			break
		}
		window = append(window, page.Rows...)
	}
	page := ActivationPage{Rows: []ActivationRow{}}
	for _, r := range window {
		if status != "" && r.Status != status {
			continue
		}
		if code != "" && r.Key != code {
			continue
		}
		if needle != "" && !strings.Contains(strings.ToLower(r.Key), needle) &&
			!strings.Contains(strings.ToLower(r.SubName), needle) &&
			!strings.Contains(strings.ToLower(r.Detail), needle) &&
			!strings.Contains(strings.ToLower(r.SteamID), needle) {
			continue
		}
		page.Rows = append(page.Rows, r)
	}
	page.Total = int64(len(page.Rows))
	if offset >= len(page.Rows) {
		return page, nil
	}
	page.Rows = page.Rows[offset:min(offset+limit, len(page.Rows))]
	return page, nil
}

type ActivationRow struct {
	ID        int64  `json:"id"`
	CreatedAt string `json:"createdAt"`
	Kind      string `json:"kind"`
	Status    string `json:"status"`
	Key       string `json:"key"`
	SubID     string `json:"subId"`
	SubName   string `json:"subName"`
	Detail    string `json:"detail"`
	SteamID   string `json:"steamId"`
}

type ActivationPage struct {
	Total int64           `json:"total"`
	Rows  []ActivationRow `json:"rows"`
}

func (s *Service) ListActivations(ctx context.Context, limit, offset int) (ActivationPage, error) {
	limit = max(1, min(limit, 200))
	offset = max(0, offset)
	page := ActivationPage{Rows: []ActivationRow{}}
	if err := s.db.QueryRowContext(ctx, `SELECT count(*) FROM redeem_activations`).Scan(&page.Total); err != nil {
		return page, err
	}
	rows, err := s.db.QueryContext(ctx, `SELECT id,CAST(created_at AS TEXT),kind,status,product_key,subid,sub_name,detail,steamid FROM redeem_activations ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?`, limit, offset)
	if err != nil {
		return page, err
	}
	defer rows.Close()
	for rows.Next() {
		var row ActivationRow
		var created sql.NullString
		if err := rows.Scan(&row.ID, &created, &row.Kind, &row.Status, &row.Key, &row.SubID, &row.SubName, &row.Detail, &row.SteamID); err != nil {
			return page, err
		}
		row.CreatedAt = pyStamp(created)
		page.Rows = append(page.Rows, row)
	}
	return page, rows.Err()
}

type ledgerRow struct {
	Kind, Status, ProductKey, SubID, SubName, Detail, Raw, SteamID string
}

func (s *Service) recordLedger(ctx context.Context, row ledgerRow) error {
	values := []any{
		s.opts.Now().Format(ledgerStamp), row.Kind, row.Status,
		truncateRunes(row.ProductKey, 120),
		truncateRunes(row.SubID, 40),
		truncateRunes(row.SubName, 300),
		truncateRunes(row.Detail, 300),
		truncateRunes(row.Raw, rawMax),
		truncateRunes(row.SteamID, 32),
	}
	if err := s.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		_, e := conn.ExecContext(ctx, `INSERT INTO redeem_activations(created_at,kind,status,product_key,subid,sub_name,detail,raw,steamid) VALUES(?,?,?,?,?,?,?,?,?)`, values...)
		return e
	}); err != nil {
		return err
	}
	verdict, ok := map[string]string{"ok": "成功", "own": "已拥有"}[row.Status]
	if !ok {
		verdict = "失败"
	}
	mask := ""
	if row.ProductKey != "" {
		mask = maskKey(row.ProductKey)
	} else {
		mask = fmt.Sprintf("SubID %s", row.SubID)
	}
	s.opts.Log("激活记录：%s │ 类型=%s，激活码=%s，内容=%s，说明=%s，账号=%s",
		verdict, kindLabel(row.Kind), mask, truncateRunes(row.SubName, 80), truncateRunes(row.Detail, 80), row.SteamID)
	return nil
}

func kindLabel(kind string) string {
	if kind == "cdk" {
		return "激活"
	}
	return "免费领取"
}

func truncateRunes(s string, n int) string {
	if n < 0 || utf8.RuneCountInString(s) <= n {
		return s
	}
	runes := []rune(s)
	return string(runes[:n])
}

func maskKey(key string) string {
	runes := []rune(key)
	if len(runes) <= 7 {
		return strings.Repeat("*", len(runes))
	}
	return string(runes[:5]) + strings.Repeat("*", len(runes)-7) + string(runes[len(runes)-2:])
}

func pyStamp(raw sql.NullString) string {
	if !raw.Valid || raw.String == "" {
		return ""
	}
	if len(raw.String) >= 19 {
		if _, err := time.ParseInLocation("2006-01-02 15:04:05", raw.String[:19], time.Local); err == nil {
			return raw.String[:19]
		}
	}
	return raw.String
}
