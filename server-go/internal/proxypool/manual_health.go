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
	var input struct {
		HealthAuto *bool `json:"healthAuto"`
	}
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&input); err != nil || input.HealthAuto == nil {
		runResponse(w, http.StatusUnprocessableEntity, map[string]any{"detail": "此入口当前只支持修改自动节点体检"})
		return
	}
	if err := s.Gate.WithWrite(r.Context(), store.PriorityInteractive, func(conn *sql.Conn) error {
		return settings.WriteValue(r.Context(), conn, manualHealthSetting, *input.HealthAuto)
	}); err != nil {
		runPayload(w, "", err)
		return
	}
	value, err := proxyStrategy(r.Context(), s.DB)
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
