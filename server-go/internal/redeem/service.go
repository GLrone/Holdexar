// service.go redeem 域：Steam CDK 批量激活（store 登录态通道）。
//
// 对齐 Python domains/redeem/service.py：单码激活 → Steam 商店
// ajaxregisterkey 端点；错误码 9=已拥有单列 own；每次收到的响应都计一次
// 激活尝试（传输失败不计——Steam 未收到）；结果无论成败落激活账本
// redeem_activations 一行；额度按账号维度滑动窗口计数（10 次/30 分钟，
// Steam 官方限制，本端计数仅作前端节奏指引）。
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

// registerkey 错误码 → 中文语义。该表按社区语义，勿按「官方文档」纠正——
// 53=次数上限（30 分钟 10 次尝试窗口）不是锁区；配额用尽时 Steam 不查 key
// 直接返 53，任何码都一样。
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

// Steam 回执原文入库上限（超出截断，防单行膨胀；回执主体信息在前列字段）。
const rawMax = 8000

// 结果原文展示上限（前端「展开原文」核对口径；入库另有 rawMax 兜底）。
const rawDisplayMax = 2000

// 批量激活节奏：同批 9 个并发、组间串行（防误操作一把梭 50 个 key 打爆限额）。
const batchChunkSize = 9

const ledgerStamp = "2006-01-02 15:04:05.000000"

type ServiceOptions struct {
	// Accounts 登录态来源（当前 active 账号 Cookie，临期自动换发）。
	Accounts *account.Service
	// Proxy 每次出网解析代理 URL；解析失败按直连处理（后台链路语义）。
	Proxy func(context.Context) (string, error)
	Log   func(string, ...any)
	Now   func() time.Time
}

// InputError 用户可纠正的输入问题（对齐 Python ValueError → 路由 400）。
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

// AccountUsage 该账号当前 30 分钟窗口内已用激活次数（注入 account.RedeemUsed）。
func (s *Service) AccountUsage(steamID string) int { return s.counter.usage(steamID) }

// Result 单码激活结果（ok|own|fail）。
type Result struct {
	Status  string `json:"status"`
	Detail  string `json:"detail"`
	SubID   string `json:"subId"`
	SubName string `json:"subName"`
	Raw     string `json:"raw"`
}

// BatchRow 批量结果行：code 为原始激活码。
type BatchRow struct {
	Code string `json:"code"`
	Result
}

// BatchOutcome 批量激活汇总（ok/own/fail 计数 + 逐码结果）。
type BatchOutcome struct {
	Results []BatchRow `json:"results"`
	OK      int        `json:"ok"`
	Own     int        `json:"own"`
	Fail    int        `json:"fail"`
}

// Quota 激活前提状态（Cookie 可用性 + 账号维度节奏计数）。
type Quota struct {
	HasCookie    bool   `json:"hasCookie"`
	HasSessionID bool   `json:"hasSessionId"`
	SteamID      string `json:"steamId"`
	Used         int    `json:"used"`
	Limit        int    `json:"limit"`
}

// ActivateKey 激活单个 CDK。登录态缺失/缺 sessionid 以 *InputError 返回（路由 400），
// 其余异常原样上抛（路由 502 兜底）；传输层失败与各失败分支都返回 fail 结果。
func (s *Service) ActivateKey(ctx context.Context, productKey string) (Result, error) {
	session, err := s.opts.Accounts.StoreSession(ctx, "", false)
	if err != nil {
		var wallet *account.WalletError
		if errors.As(err, &wallet) && wallet.Kind == "expired" {
			// 过期登录态：Go 不放行过期 Cookie 出网（账单同步同款），直接给
			// 激活端点 401 分支同款文案；Steam 未收到，不计激活次数。
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

// ActivateBatch 批量激活：每 9 个一组并发、组间串行。任一异常上抛（与
// asyncio.gather 语义一致，路由层统一兜底）。
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

// QuotaStatus 激活前提状态：Cookie 三件套可用性 + 账号维度激活计数
// （切换绑定 Cookie 即换号重计；不能用全局计数——跨账号共享计数是错误语义）。
func (s *Service) QuotaStatus(ctx context.Context) (Quota, error) {
	quota := Quota{Limit: ACTLimit}
	session, err := s.opts.Accounts.StoreSession(ctx, "", false)
	if err != nil {
		var wallet *account.WalletError
		if errors.As(err, &wallet) && wallet.Kind == "expired" {
			// 过期登录态：Python 原样返回旧 Cookie 解析（hasCookie 仍为真，
			// 供前端引导重绑）；Go StoreSession 不放行过期 Cookie，按已有
			// 账号行身份回答，sessionid 视为在（绑定 Cookie 常态）。
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

// activationsSearchWindow 检索窗：SearchActivations 经既有分页读取聚合的全量检索窗。
// shortcut: 2000 条≈本地激活账本全量（账本增速约数十条/日）；账本超窗后把
// 筛选下推为参数化 SQL（status/q/key），本函数契约不变。
const activationsSearchWindow = 2000

// SearchActivations 激活历史检索：筛选+搜索+按码回查，覆盖检索窗内全部库内记录。
// 复用既有 ListActivations 分页读取聚合检索窗（零 SQL 构建）：status=ok/own/fail
// 按结果过滤；key=激活码精确匹配（结果表失败码回查历史成功记录）；q 不分
// 大小写子串匹配激活码/版本名/说明/账号。返回过滤后总数与分页行。
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

// ActivationRow 激活历史行（不含 raw 回执原文，控制载荷）。
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

// ActivationPage 激活历史分页（时间倒序，同秒按 id 倒序）。
type ActivationPage struct {
	Total int64           `json:"total"`
	Rows  []ActivationRow `json:"rows"`
}

// ListActivations 激活历史：limit 夹取 1..200，offset 不为负；无过滤参数仅分页。
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

// ── 激活账本（redeem_activations，成功/已拥有/失败都记） ──────────────

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

// ── 文本工具（对齐 Python str 切片语义：按字符截断） ─────────────────

func truncateRunes(s string, n int) string {
	if n < 0 || utf8.RuneCountInString(s) <= n {
		return s
	}
	runes := []rune(s)
	return string(runes[:n])
}

// maskKey 激活码日志掩码：留首段与尾 2 位，中段以 * 代替。
func maskKey(key string) string {
	runes := []rune(key)
	if len(runes) <= 7 {
		return strings.Repeat("*", len(runes))
	}
	return string(runes[:5]) + strings.Repeat("*", len(runes)-7) + string(runes[len(runes)-2:])
}

// pyStamp 激活账本时间戳（本地 naive 微秒）转展示格式 "YYYY-MM-DD HH:MM:SS"。
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
