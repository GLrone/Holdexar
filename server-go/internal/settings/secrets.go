package settings

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"

	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/store"
)

func SealSecrets(ctx context.Context, db *sql.DB, gate *store.WriteGate, dataDir string) (int, error) {
	rows, e := db.QueryContext(ctx, `SELECT key,value_json FROM app_settings WHERE key IN('account.steam_api_key','pilot.llm.api_key','pilot.llm.provider_key.legacy-v1')`)
	if e != nil {
		return 0, e
	}
	updates := [][3]string{}
	for rows.Next() {
		var key string
		var raw sql.NullString
		if e = rows.Scan(&key, &raw); e != nil {
			rows.Close()
			return 0, e
		}
		if !raw.Valid || raw.String == "null" {
			continue
		}
		var value string
		if e = json.Unmarshal([]byte(raw.String), &value); e != nil {
			rows.Close()
			return 0, fmt.Errorf("凭据设置格式无效：%s", key)
		}
		if value != "" && !strings.HasPrefix(value, secretbox.Prefix) {
			updates = append(updates, [3]string{key, raw.String, value})
		}
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return 0, e
	}
	for i := range updates {
		token, e := secretbox.EncryptSecretIn(dataDir, updates[i][2], secretbox.SecretPurpose(updates[i][0]))
		if e != nil {
			return 0, e
		}
		value, _ := json.Marshal(token)
		updates[i][2] = string(value)
	}
	if len(updates) == 0 {
		return 0, nil
	}
	raw, _ := json.Marshal(updates)
	sealed := 0
	e = gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		result, e := conn.ExecContext(ctx, `UPDATE app_settings AS s SET value_json=json_extract(u.value,'$[2]'),updated_at=? FROM json_each(?) u WHERE s.key=json_extract(u.value,'$[0]') AND s.value_json=json_extract(u.value,'$[1]')`, beijingNow(), string(raw))
		if e != nil {
			return e
		}
		n, e := result.RowsAffected()
		sealed = int(n)
		return e
	})
	if e != nil {
		return 0, e
	}
	return sealed, nil
}
