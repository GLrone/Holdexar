package proxypool

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
)

const proxyItemJSON = `json_object(
	'id',id,'label',label,'scheme',scheme,'host',host,'port',port,
	'hasAuth',json(CASE WHEN COALESCE(username,'')<>'' THEN 'true' ELSE 'false' END),
	'url',scheme||'://'||CASE WHEN COALESCE(username,'')<>'' THEN '***@' ELSE '' END||host||':'||port,
	'enabled',json(CASE WHEN enabled THEN 'true' ELSE 'false' END),'status',status,
	'latencyMs',latency_ms,'consecutiveFailures',consecutive_failures,'lastCheckedAt',replace(last_checked_at,' ','T'))`

func proxyStrategy(ctx context.Context, db *sql.DB) (string, error) {
	var strategy string
	err := db.QueryRowContext(ctx, `WITH cfg AS (
		SELECT COALESCE((SELECT json_extract(value_json,'$') FROM app_settings WHERE key='proxy.strategy'),'direct_only') strategy,
		(SELECT json_extract(value_json,'$') FROM app_settings WHERE key='proxy.clash_port_mode') mode,
		(SELECT json_extract(value_json,'$') FROM app_settings WHERE key='proxy.clash_port') port,
		COALESCE((SELECT json_extract(value_json,'$') FROM app_settings WHERE key='proxy.autostart'),1) autostart,
		COALESCE((SELECT json_extract(value_json,'$') FROM app_settings WHERE key='proxy.health_auto'),1) health
	), valid AS (
		SELECT *, (typeof(port)='integer' AND port BETWEEN 1 AND 65535 AND COALESCE(mode,'fixed')<>'auto') fixed FROM cfg
	) SELECT json_object(
		'strategy',CASE WHEN strategy IN ('pinned','clash') THEN 'proxy_first' ELSE strategy END,
		'clashPortMode',CASE WHEN fixed THEN 'fixed' ELSE 'auto' END,
		'clashPort',CASE WHEN fixed THEN port END,
		'autostart',json(CASE WHEN autostart THEN 'true' ELSE 'false' END),
		'healthAuto',json(CASE WHEN health THEN 'true' ELSE 'false' END)) FROM valid`).Scan(&strategy)
	return strategy, err
}

func RegisterProxyList(mux *http.ServeMux, db *sql.DB, dataDir string) {
	mux.HandleFunc("GET /api/v1/proxies", func(w http.ResponseWriter, r *http.Request) {
		ctx := r.Context()
		subscriptions, err := ListSubscriptionViews(ctx, db, dataDir, "")
		if err != nil {
			runPayload(w, "", err)
			return
		}
		var items string
		err = db.QueryRowContext(ctx, `SELECT json_group_array(`+proxyItemJSON+`) FROM (SELECT * FROM proxies ORDER BY id)`).Scan(&items)
		if err != nil {
			runPayload(w, "", err)
			return
		}
		strategy, err := proxyStrategy(ctx, db)
		if err != nil {
			runPayload(w, "", err)
			return
		}
		runResponse(w, http.StatusOK, map[string]any{
			"items": json.RawMessage(items), "strategy": json.RawMessage(strategy), "subscriptions": subscriptions,
		})
	})
}
