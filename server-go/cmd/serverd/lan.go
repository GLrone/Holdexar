package main

// 局域网访问模式启动接线（Python _init_lan_access / ensure_lan_password 等价）：
// 开关与密码事实源在设置 KV 与环境变量，这里只做装配。

import (
	"context"
	"database/sql"
	"time"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

const (
	lanEnabledKey  = "access.lan_enabled"
	lanPasswordKey = "access.lan_password"
)

// ensureLANPassword 访问密码解析：环境变量优先，其次设置密文 KV；都没有则
// 生成并落库（下次解锁即用此密码）。
func ensureLANPassword(ctx context.Context, db *sql.DB, gate *store.WriteGate, dataDir string) (string, error) {
	if env := access.EnvPassword(); env != "" {
		return env, nil
	}
	stored, err := settings.ReadSecret(ctx, db, dataDir, lanPasswordKey)
	if err != nil {
		return "", err
	}
	if stored != "" {
		return stored, nil
	}
	generated := access.GeneratePassword()
	sealed, err := secretbox.EncryptSecretIn(dataDir, generated, secretbox.SecretPurpose(lanPasswordKey))
	if err != nil {
		return "", err
	}
	err = gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		return settings.WriteValue(ctx, conn, lanPasswordKey, sealed)
	})
	if err != nil {
		return "", err
	}
	return generated, nil
}

// initLANAccess 局域网访问模式初始化：读设置开关并同步 Host 闸白名单。
// 环境变量 HOLDEXAR_ACCESS=lan 强制开启（先于设置页存在的 NAS 无窗口场景）；
// 任何失败按关闭处理（fail-closed），不影响正常启动。
func initLANAccess(ctx context.Context, db *sql.DB, gate *store.WriteGate, dataDir string, log, logFail func(string, ...any)) {
	var initErr error
	kvEnabled, err := settings.ReadBool(ctx, db, lanEnabledKey, false)
	if err != nil {
		initErr = err
	} else {
		access.SetLANEnabled(kvEnabled)
		access.RefreshExtraHosts()
		if access.LANEnabled() {
			if _, err := ensureLANPassword(ctx, db, gate, dataDir); err != nil {
				initErr = err
			} else {
				log("局域网访问模式已开启，本机地址已加入访问白名单")
			}
		}
	}
	if initErr != nil {
		access.SetLANEnabled(false)
		access.RefreshExtraHosts()
		logFail("局域网访问模式初始化失败，按关闭处理：%v", initErr)
	}
}

// unlockPasswordResolver 解锁端点的密码解析（带短超时，隔离请求生命周期）。
func unlockPasswordResolver(db *sql.DB, gate *store.WriteGate, dataDir string) func() (string, error) {
	return func() (string, error) {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return ensureLANPassword(ctx, db, gate, dataDir)
	}
}
