package proxypool

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"sync"
	"time"

	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

const ManualHealthJob = "proxy_health"
const manualHealthSetting = "proxy.health_auto"

type ManualHealth struct {
	DB      *sql.DB
	Gate    *store.WriteGate
	DataDir string
	Target  string
	// Bus 事件总线（nil=不广播 settings.changed）；RestartKernelForPort 内核
	// 在跑时的端口切换重启（nil=按内核未运行处理，端口 KV 下次启动生效）。
	Bus                  *events.Bus
	RestartKernelForPort func(ctx context.Context, port int) error
}

// strategyKnown 路由策略白名单（与 Python STRATEGIES 同表）。
func strategyKnown(name string) bool {
	switch name {
	case "proxy_first", "direct_only", "direct_first", "proxy_only":
		return true
	}
	return false
}

// portError 端口参数类错误：以 400 形态回给调用方（与 Python ValueError→400 同径）。
type portError string

func (e portError) Error() string { return string(e) }

// kvBool 读布尔 KV（缺省返回 def；值非布尔形态按 def 处理）。
func kvBool(ctx context.Context, conn *sql.Conn, key string, def bool) (bool, error) {
	var raw any
	err := conn.QueryRowContext(ctx, `SELECT json_extract(value_json,'$') FROM app_settings WHERE key=?`, key).Scan(&raw)
	if err == sql.ErrNoRows {
		return def, nil
	}
	if err != nil {
		return def, err
	}
	if b, ok := raw.(bool); ok {
		return b, nil
	}
	if f, ok := raw.(float64); ok {
		return f != 0, nil
	}
	return def, nil
}

// kernelPortConfig 解析内核端口配置：模式缺省按有无存档端口判定，
// 非法模式/端口自愈（自愈写回由调用方的写事务承载时落库）。
func (s *ManualHealth) kernelPortConfig(ctx context.Context) (string, *int) {
	var mode *string
	var port *int
	_ = s.DB.QueryRowContext(ctx, `SELECT json_extract(value_json,'$') FROM app_settings WHERE key='proxy.clash_port_mode'`).Scan(&mode)
	_ = s.DB.QueryRowContext(ctx, `SELECT json_extract(value_json,'$') FROM app_settings WHERE key='proxy.clash_port'`).Scan(&port)
	m := "auto"
	if mode != nil && (*mode == "auto" || *mode == "fixed") {
		m = *mode
	} else if port != nil {
		m = "fixed"
	}
	if port != nil && (*port < 1 || *port > 65535) {
		port = nil
	}
	if m == "fixed" && port == nil {
		m = "auto"
	}
	return m, port
}

// publishChanged 广播 settings.changed（键清单载荷，与设置面同形；无总线静默跳过）。
func (s *ManualHealth) publishChanged(keys ...string) {
	if s.Bus == nil || len(keys) == 0 {
		return
	}
	s.Bus.Publish("settings.changed", events.KV{Key: "keys", Val: keys})
}

type manualProbe struct {
	ID                               int64
	Port                             int
	Scheme, Host, Username, Password string
	Enabled                          bool
	Checked, Label                   string
	Latency                          int64
	Error                            *string
}

func (s *ManualHealth) Enabled(ctx context.Context) (bool, error) {
	var enabled bool
	err := s.DB.QueryRowContext(ctx, `SELECT COALESCE((SELECT json_extract(value_json,'$') FROM app_settings WHERE key=?),1)`, manualHealthSetting).Scan(&enabled)
	return enabled, err
}

func (s *ManualHealth) Check(ctx context.Context, id int64, automatic bool) ([]json.RawMessage, error) {
	result := []json.RawMessage{}
	rows, err := s.DB.QueryContext(ctx, `SELECT id,scheme,host,port,COALESCE(username,''),COALESCE(password,''),enabled,
		COALESCE(CAST(last_checked_at AS TEXT),''),COALESCE(NULLIF(label,''),host||':'||port)
		FROM proxies WHERE (?=0 OR id=?) AND (?=0 OR EXISTS(SELECT 1 FROM proxies WHERE enabled)) ORDER BY id`, id, id, automatic)
	if err != nil {
		return nil, err
	}
	var probes []manualProbe
	for rows.Next() {
		var probe manualProbe
		if err := rows.Scan(&probe.ID, &probe.Scheme, &probe.Host, &probe.Port, &probe.Username, &probe.Password,
			&probe.Enabled, &probe.Checked, &probe.Label); err != nil {
			rows.Close()
			return nil, err
		}
		probes = append(probes, probe)
	}
	err = rows.Err()
	rows.Close()
	if err != nil || len(probes) == 0 {
		return result, err
	}
	var workers sync.WaitGroup
	for i := range probes {
		workers.Go(func() { s.probe(ctx, &probes[i]) })
	}
	workers.Wait()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	raw, err := json.Marshal(probes)
	if err != nil {
		return nil, err
	}
	now := pyTimeText(time.Now().In(sched.DefaultLoc))
	err = s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		ids, err := commitManualProbes(ctx, conn, string(raw), now)
		if err != nil || len(ids) == 0 {
			return err
		}
		idJSON, err := json.Marshal(ids)
		if err != nil {
			return err
		}
		const applied = ` FROM json_each(?) AS r WHERE json_extract(r.value,'$.ID') IN (SELECT value FROM json_each(?))`
		if _, err := conn.ExecContext(ctx, `INSERT INTO proxy_events(ts,kind,target,proxy_label,duration_ms,error)
			SELECT ?,'test','IStoreBrowseService/GetItems',json_extract(r.value,'$.Label'),
			json_extract(r.value,'$.Latency'),json_extract(r.value,'$.Error')`+applied, now, string(raw), string(idJSON)); err != nil {
			return err
		}
		views, err := conn.QueryContext(ctx, `SELECT json_set(`+proxyItemJSON+`, '$.testError',
			(SELECT json_extract(r.value,'$.Error') FROM json_each(?) AS r WHERE p.id=json_extract(r.value,'$.ID')))
			FROM proxies AS p WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id`, string(raw), string(idJSON))
		if err != nil {
			return err
		}
		defer views.Close()
		for views.Next() {
			var payload string
			if err := views.Scan(&payload); err != nil {
				return err
			}
			result = append(result, json.RawMessage(payload))
		}
		return views.Err()
	})
	return result, err
}

func commitManualProbes(ctx context.Context, conn *sql.Conn, raw, now string) ([]int64, error) {
	rows, err := conn.QueryContext(ctx, `WITH incoming AS MATERIALIZED (
		SELECT json_extract(value,'$.ID') proxyID,json_extract(value,'$.Scheme') scheme,json_extract(value,'$.Host') host,
		json_extract(value,'$.Port') port,json_extract(value,'$.Username') username,json_extract(value,'$.Password') password,
		json_extract(value,'$.Checked') checked,json_extract(value,'$.Enabled') enabled,
		json_extract(value,'$.Latency') latency,json_extract(value,'$.Error') error FROM json_each(?)
	) UPDATE proxies AS p SET status=CASE WHEN i.error IS NULL THEN 'ok' ELSE 'failed' END,
		latency_ms=i.latency,last_checked_at=?,
		consecutive_failures=CASE WHEN i.error IS NULL THEN 0 ELSE p.consecutive_failures+1 END,
		enabled=CASE WHEN i.error IS NOT NULL AND p.consecutive_failures>=4 THEN 0 ELSE p.enabled END
	FROM incoming i WHERE p.id=i.proxyID AND p.scheme=i.scheme AND p.host=i.host AND p.port=i.port
	AND COALESCE(p.username,'')=i.username AND COALESCE(p.password,'')=i.password AND p.enabled=i.enabled
	AND COALESCE(CAST(p.last_checked_at AS TEXT),'')=i.checked RETURNING id`, raw, now)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []int64
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

func (s *ManualHealth) probe(ctx context.Context, p *manualProbe) {
	start := time.Now()
	defer func() { p.Latency = time.Since(start).Milliseconds() }()
	proxy := &url.URL{Scheme: p.Scheme, Host: net.JoinHostPort(p.Host, strconv.Itoa(p.Port))}
	if p.Username != "" {
		user, pass := p.Username, p.Password
		for _, value := range []*string{&user, &pass} {
			if secretbox.IsEncrypted(*value) {
				plain, err := secretbox.DecryptSecretIn(s.DataDir, *value, "proxy-credential")
				if err != nil {
					message := "代理凭据无法在本机解密"
					p.Error = &message
					return
				}
				*value = plain
			}
		}
		proxy.User = url.UserPassword(user, pass)
	}
	target := s.Target
	if target == "" {
		target = BusinessProbeURL(220, "us")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = http.ProxyURL(proxy)
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 12 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	message := manualProbeError(ctx, client, target)
	if message != "" {
		p.Error = &message
	}
}

func manualProbeError(ctx context.Context, client *http.Client, target string) string {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return "体检地址无效"
	}
	resp, err := client.Do(req)
	if err != nil {
		return "连接代理失败"
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Sprintf("HTTP %d", resp.StatusCode)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, (1<<20)+1))
	var object map[string]json.RawMessage
	if err != nil || len(raw) > 1<<20 || json.Unmarshal(raw, &object) != nil || object == nil {
		return "非 JSON 响应（疑似风控拦截页）"
	}
	return ""
}

func (s *ManualHealth) Register(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/v1/proxies/test_all", func(w http.ResponseWriter, r *http.Request) {
		items, err := s.Check(r.Context(), 0, false)
		if err != nil {
			runPayload(w, "", err)
			return
		}
		runResponse(w, http.StatusOK, map[string]any{"items": items})
	})
	mux.HandleFunc("POST /api/v1/proxies/{id}/test", func(w http.ResponseWriter, r *http.Request) {
		id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
		if err != nil || id <= 0 {
			runResponse(w, http.StatusUnprocessableEntity, map[string]any{"detail": "代理编号无效"})
			return
		}
		items, err := s.Check(r.Context(), id, false)
		if err != nil {
			runPayload(w, "", err)
			return
		}
		if len(items) == 0 {
			runResponse(w, http.StatusNotFound, map[string]any{"detail": "代理不存在或已发生变化"})
			return
		}
		runResponse(w, http.StatusOK, items[0])
	})
	mux.HandleFunc("GET /api/v1/proxies/events", s.events)
	mux.HandleFunc("PUT /api/v1/proxies/strategy", s.healthSetting)
}

func (s *ManualHealth) healthSetting(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Strategy      *string `json:"strategy"`
		ClashPort     *int    `json:"clashPort"`
		Autostart     *bool   `json:"autostart"`
		HealthAuto    *bool   `json:"healthAuto"`
		ClashPortMode *string `json:"clashPortMode"`
	}
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
	if err := dec.Decode(&body); err != nil {
		runResponse(w, http.StatusUnprocessableEntity, map[string]any{"detail": "请求体无效"})
		return
	}
	ctx := r.Context()

	// ① 策略：白名单校验 + 用户显式标记（标记为真后订阅导入的自动切换不再覆盖）
	if body.Strategy != nil {
		if !strategyKnown(*body.Strategy) {
			runResponse(w, http.StatusBadRequest, map[string]any{"detail": "未知策略: " + *body.Strategy})
			return
		}
		if err := s.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
			if err := settings.WriteValue(ctx, conn, "proxy.strategy", *body.Strategy); err != nil {
				return err
			}
			explicit, err := kvBool(ctx, conn, "proxy.strategy_explicit", false)
			if err != nil {
				return err
			}
			if !explicit {
				return settings.WriteValue(ctx, conn, "proxy.strategy_explicit", true)
			}
			return nil
		}); err != nil {
			runPayload(w, "", err)
			return
		}
		s.publishChanged("proxy.strategy", "proxy.strategy_explicit")
	}

	// ② 端口模式/端口：fixed 校验端口并（内核在跑时）立即按新端口重启；auto 只改模式
	if body.ClashPortMode != nil || body.ClashPort != nil {
		oldMode, oldPort := s.kernelPortConfig(ctx)
		targetMode := oldMode
		if body.ClashPortMode != nil {
			targetMode = *body.ClashPortMode
		} else if body.ClashPort != nil {
			targetMode = "fixed"
		}
		if targetMode != "auto" && targetMode != "fixed" {
			runResponse(w, http.StatusBadRequest, map[string]any{"detail": "代理端口模式必须是 auto 或 fixed"})
			return
		}
		changed := []string{}
		if err := s.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
			if targetMode == "fixed" {
				targetPort := oldPort
				if body.ClashPort != nil {
					p := *body.ClashPort
					targetPort = &p
				}
				if targetPort == nil || *targetPort < 1 || *targetPort > 65535 {
					return portError("固定端口必须在 1 到 65535 之间")
				}
				if s.RestartKernelForPort != nil && (oldMode != "fixed" || oldPort == nil || *oldPort != *targetPort) {
					if err := s.RestartKernelForPort(ctx, *targetPort); err != nil {
						return fmt.Errorf("代理端口切换失败：%w", err)
					}
				}
				if err := settings.WriteValue(ctx, conn, "proxy.clash_port", *targetPort); err != nil {
					return err
				}
				if err := settings.WriteValue(ctx, conn, "proxy.clash_port_mode", "fixed"); err != nil {
					return err
				}
				changed = append(changed, "proxy.clash_port", "proxy.clash_port_mode")
				return nil
			}
			if err := settings.WriteValue(ctx, conn, "proxy.clash_port_mode", "auto"); err != nil {
				return err
			}
			changed = append(changed, "proxy.clash_port_mode")
			return nil
		}); err != nil {
			if pe, ok := err.(portError); ok {
				runResponse(w, http.StatusBadRequest, map[string]any{"detail": string(pe)})
				return
			}
			runPayload(w, "", err)
			return
		}
		s.publishChanged(changed...)
	}

	// ③ 自启 / 体检开关
	changed := []string{}
	for _, pair := range []struct {
		body *bool
		key  string
	}{{body.Autostart, "proxy.autostart"}, {body.HealthAuto, manualHealthSetting}} {
		if pair.body == nil {
			continue
		}
		if err := s.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
			return settings.WriteValue(ctx, conn, pair.key, *pair.body)
		}); err != nil {
			runPayload(w, "", err)
			return
		}
		changed = append(changed, pair.key)
	}
	if len(changed) > 0 {
		s.publishChanged(changed...)
	}

	value, err := proxyStrategy(ctx, s.DB)
	runPayload(w, value, err)
}

func (s *ManualHealth) events(w http.ResponseWriter, r *http.Request) {
	limit, ok := runPageValue(r, "limit", 200, 1, 1000)
	if !ok {
		runResponse(w, http.StatusUnprocessableEntity, map[string]any{"detail": "分页参数不正确"})
		return
	}
	var payload string
	err := s.DB.QueryRowContext(r.Context(), `SELECT json_group_array(json_object(
		'id',id,'ts',replace(ts,' ','T'),'kind',kind,'target',target,'proxyLabel',proxy_label,
		'statusCode',status_code,'durationMs',duration_ms,'error',error))
		FROM (SELECT * FROM proxy_events ORDER BY id DESC LIMIT ?)`, limit).Scan(&payload)
	runPayload(w, payload, err)
}
