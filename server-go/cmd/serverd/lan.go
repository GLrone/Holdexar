package main

import (
	"context"
	"database/sql"
	"time"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

// resolveLANPasswordVerifier 按用户哈希 KV → 环境口令 → 旧密文一次性迁移解析；用户口令优先，无凭据返回空串。
func resolveLANPasswordVerifier(ctx context.Context, db *sql.DB, gate *store.WriteGate, dataDir string) (string, error) {
	phc, err := settings.ReadValue(ctx, db, settings.KeyLANPasswordHash)
	if err != nil {
		return "", err
	}
	if phc != "" {
		return phc, nil
	}
	if env := access.EnvPassword(); env != "" {
		return access.HashPassword(env), nil
	}
	legacy, err := settings.ReadSecret(ctx, db, dataDir, settings.KeyLANPassword)
	if err != nil {
		return "", err
	}
	if legacy == "" {
		return "", nil
	}
	migrated := access.HashPassword(legacy)
	err = gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		if err := settings.WriteValue(ctx, conn, settings.KeyLANPasswordHash, migrated); err != nil {
			return err
		}
		return settings.DeleteValue(ctx, conn, settings.KeyLANPassword)
	})
	if err != nil {
		return "", err
	}
	return migrated, nil
}

// initLANAccess 初始化局域网访问开关并刷新 Host 白名单（HOLDEXAR_ACCESS=lan 强制开启；环境口令仅内存哈希不落库）；失败按关闭处理。
func initLANAccess(ctx context.Context, db *sql.DB, gate *store.WriteGate, dataDir string, log, logFail func(string, ...any)) {
	var initErr error
	kvEnabled, err := settings.ReadBool(ctx, db, settings.KeyLANEnabled, false)
	if err != nil {
		initErr = err
	} else {
		access.SetLANEnabled(kvEnabled)
		access.RefreshExtraHosts()
		if access.LANEnabled() {
			log("局域网访问模式已开启，本机地址已加入访问白名单")
		}
	}
	if initErr != nil {
		access.SetLANEnabled(false)
		access.RefreshExtraHosts()
		logFail("局域网访问模式初始化失败，按关闭处理：%v", initErr)
	}
}

func unlockVerifierResolver(db *sql.DB, gate *store.WriteGate, dataDir string) func() (string, error) {
	return func() (string, error) {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return resolveLANPasswordVerifier(ctx, db, gate, dataDir)
	}
}
