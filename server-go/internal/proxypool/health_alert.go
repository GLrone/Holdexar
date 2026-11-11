package proxypool

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"
)

func ProxyHealthAlertSnapshot(ctx context.Context, conn *sql.Conn, running bool, names []string, now time.Time) (map[string]any, error) {
	var poolTotal, poolOK, nodes, okNodes int
	if err := conn.QueryRowContext(ctx, `SELECT COUNT(*),COUNT(CASE WHEN enabled AND status='ok' THEN 1 END) FROM proxies`).Scan(&poolTotal, &poolOK); err != nil {
		return nil, err
	}
	exits, okExits := map[string]bool{}, map[string]bool{}
	if running && len(names) > 0 {
		raw, err := json.Marshal(names)
		if err != nil {
			return nil, err
		}
		rows, err := conn.QueryContext(ctx, `SELECT COALESCE(NULLIF(n.exit_ip,''),'node:'||n.node_id),n.state,
			n.last_l2_at IS NOT NULL,s.id IS NOT NULL AND NOT s.deprecated,COALESCE(CAST(s.last_stats AS TEXT),'')
			FROM proxy_nodes n LEFT JOIN proxy_subscriptions s ON s.id=n.subscription_id
			WHERE n.runtime_name IN (SELECT value FROM json_each(?))`, string(raw))
		if err != nil {
			return nil, err
		}
		defer rows.Close()
		for rows.Next() {
			var exit, state, stats string
			var l2, subReady bool
			if err := rows.Scan(&exit, &state, &l2, &subReady, &stats); err != nil {
				return nil, err
			}
			nodes++
			exits[exit] = true
			if l2 && subReady && (state == NodeActive || state == NodeStale) && SubscriptionBlockReason(stats, now) == "" {
				okNodes++
				okExits[exit] = true
			}
		}
		if err := rows.Err(); err != nil {
			return nil, err
		}
	}
	total, available := poolTotal+len(exits), poolOK+len(okExits)
	if total == 0 || available*5 >= total {
		return nil, nil
	}
	reason := "全部代理通道不可用"
	if available > 0 {
		reason = fmt.Sprintf("可用通道不足 20%%（%d/%d）", available, total)
	}
	subs, err := proxyAlertSubscriptions(ctx, conn)
	if err != nil {
		return nil, err
	}
	return map[string]any{"reason": reason, "available": available, "total": total, "subscriptions": subs,
		"pool_total": poolTotal, "pool_ok": poolOK, "clash_running": running, "clash_nodes": nodes,
		"clash_ok_nodes": okNodes, "clash_exit_ips": len(exits), "clash_ok_exit_ips": len(okExits)}, nil
}

func proxyAlertSubscriptions(ctx context.Context, conn *sql.Conn) ([]map[string]any, error) {
	rows, err := conn.QueryContext(ctx, `SELECT id,COALESCE(NULLIF(label,''),'订阅 '||id),kind,COALESCE(CAST(last_stats AS TEXT),'')
		FROM proxy_subscriptions ORDER BY id LIMIT 5`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []map[string]any{}
	for rows.Next() {
		var id int64
		var label, kind, raw string
		if err := rows.Scan(&id, &label, &kind, &raw); err != nil {
			return nil, err
		}
		var stats map[string]any
		_ = json.Unmarshal([]byte(raw), &stats)
		var traffic map[string]any
		if values, ok := stats["traffic"].(map[string]any); ok {
			traffic = map[string]any{}
			for _, key := range []string{"upload", "download", "total", "expire"} {
				if n, ok := values[key].(float64); ok {
					traffic[key] = n
				}
			}
		}
		if text, ok := stats["traffic"].(string); ok {
			traffic = map[string]any{}
			for _, part := range strings.Split(text, ";") {
				key, value, found := strings.Cut(strings.TrimSpace(part), "=")
				if found && (key == "upload" || key == "download" || key == "total" || key == "expire") {
					if n, err := strconv.ParseInt(strings.TrimSpace(value), 10, 64); err == nil {
						traffic[key] = n
					}
				}
			}
		}
		out = append(out, map[string]any{"id": id, "label": safeSubscriptionText(label), "kind": kind, "traffic": traffic})
	}
	return out, rows.Err()
}
