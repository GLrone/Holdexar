package alerts

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strconv"
	"time"

	"holdexar/server-go/internal/notifications"
	"holdexar/server-go/internal/proxypool"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

const proxyAlertTimes = "alerts.sys_alert_at"

func (s *Service) RecordProxyHealth(ctx context.Context, running bool, runtimeNames []string, now time.Time) (bool, error) {
	now = now.In(beijing)
	created := false
	err := s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		data, err := proxypool.ProxyHealthAlertSnapshot(ctx, conn, running, runtimeNames, now)
		if err != nil || data == nil {
			return err
		}
		data["appid"], data["labels"] = 0, []string{"连接通道提醒"}
		data["title"] = fmt.Sprintf("代理通道告警：%s", data["reason"])
		created, err = recordSystemNoticeTx(ctx, conn, systemNotice{"proxy", "proxy_health", "proxy_alert", "PROXY_ALERT", 12 * time.Hour}, data, now)
		return err
	})
	return created && err == nil, err
}

type systemNotice struct {
	category, kind, prefix, eventType string
	cooldown                          time.Duration
}

func recordSystemNoticeTx(ctx context.Context, conn *sql.Conn, notice systemNotice, data map[string]any, now time.Time) (bool, error) {
	var raw string
	if err := conn.QueryRowContext(ctx, `SELECT COALESCE((SELECT value_json FROM app_settings WHERE key=?),'{}')`, proxyAlertTimes).Scan(&raw); err != nil {
		return false, err
	}
	var cooldowns map[string]json.RawMessage
	if json.Unmarshal([]byte(raw), &cooldowns) != nil || cooldowns == nil {
		cooldowns = map[string]json.RawMessage{}
	}
	var last float64
	current := float64(now.UnixMilli()) / 1000
	if json.Unmarshal(cooldowns[notice.category], &last) == nil && current-last < notice.cooldown.Seconds() {
		return false, nil
	}
	encoded, err := json.Marshal(data)
	if err != nil {
		return false, err
	}
	var id int64
	if err := conn.QueryRowContext(ctx, `INSERT INTO fact_notices(source,kind,fact_key,data,occurred_at)
		VALUES ('alerts',?,?,?,?) RETURNING id`, notice.kind, fmt.Sprintf("%s:%d", notice.prefix, now.UnixNano()), string(encoded), stamp(now)).Scan(&id); err != nil {
		return false, err
	}
	data["eventType"], data["source"], data["kind"] = notice.eventType, "alerts", notice.kind
	data["observedAt"] = now.Format(time.RFC3339)
	if _, err := notifications.EnqueueFactsTx(ctx, conn, "fact_notice", []notifications.Fact{{ID: id, EventType: notice.eventType, Payload: data, OccurredAt: now}}, now); err != nil {
		return false, err
	}
	cooldowns[notice.category] = json.RawMessage(strconv.FormatFloat(current, 'f', -1, 64))
	err = settings.WriteValue(ctx, conn, proxyAlertTimes, cooldowns)
	return err == nil, err
}

func (s *Service) RecordBackupFailure(ctx context.Context, cause error) error {
	now := s.now()
	reason := []rune(cause.Error())
	if len(reason) > 180 {
		reason = reason[:180]
	}
	data := map[string]any{
		"appid": 0, "title": "自动备份失败", "labels": []string{"系统运行提醒"},
		"summary": "本轮自动备份未能完成。", "level": "danger",
		"rows": [][]string{{"失败原因", string(reason)}, {"发生时间", now.Format("2006-01-02 15:04:05")}},
		"hint": "可在设置的数据页检查备份目录剩余空间与写入权限。",
	}
	var created bool
	err := s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		var err error
		created, err = recordSystemNoticeTx(ctx, conn, systemNotice{"backup", "system_alert", "system_alert:backup", "SYSTEM_ALERT", 24 * time.Hour}, data, now)
		return err
	})
	if err == nil && created && s.opts.Dispatch != nil {
		return s.opts.Dispatch(ctx)
	}
	return err
}
