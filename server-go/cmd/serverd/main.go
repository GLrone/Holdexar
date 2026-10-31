// serverd：服务骨架入口。数据目录判定见 internal/config。
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/agent"
	"holdexar/server-go/internal/config"
	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/eventstream"
	"holdexar/server-go/internal/games"
	"holdexar/server-go/internal/httpapi"
	"holdexar/server-go/internal/notifications"
	"holdexar/server-go/internal/rates"
	"holdexar/server-go/internal/regions"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
	"holdexar/server-go/internal/systemx"
)

// schemaVersion 与数据层迁移链的当前版本一致；迁移链 Go 化后由该链合成提供。
const schemaVersion = 34

func main() {
	addr := flag.String("addr", "127.0.0.1:28765", "监听地址")
	dataDirFlag := flag.String("data-dir", "", "显式数据目录，跳过自动判定")
	devSession := flag.Bool("dev-session", false, "开放开发态会话发放通道（发行版无此入口）")
	mcpEnabled := flag.Bool("mcp", false, "启用 MCP 面（等价环境变量 HOLDEXAR_MCP=1）")
	flag.Parse()

	// 日志接线点：stderr 之外同步写入环形缓冲（日志页 SSE 的回放事实源）；
	// 完整日志设施迁移前，行格式为时刻前缀 + 原文案。
	bus := events.NewBus()
	ring := eventstream.NewRing(2000)
	logLine := func(format string, args ...any) {
		msg := fmt.Sprintf(format, args...)
		fmt.Fprintln(os.Stderr, msg)
		ring.Write(time.Now().Format("15:04:05") + " " + msg)
	}

	if *dataDirFlag != "" {
		os.Setenv(config.EnvPrefix+"DATA_DIR", *dataDirFlag)
	}
	dataDir, err := config.ResolveDataDir()
	if err != nil {
		logLine("%v", err)
		os.Exit(1)
	}
	distDir, err := config.WebDistDir()
	if err != nil {
		logLine("%v", err)
		os.Exit(1)
	}

	dbPath := filepath.Join(dataDir, "holdexar.db")
	if _, err := os.Stat(dbPath); err != nil {
		logLine("数据库不存在：%s（首次建库由初始化链负责，服务端不代建）", dbPath)
		os.Exit(1)
	}
	if err := store.EnsureSupportedVersion(dbPath, schemaVersion); err != nil {
		logLine("%v", err)
		os.Exit(1)
	}
	db, err := store.Open(dbPath)
	if err != nil {
		logLine("%v", err)
		os.Exit(1)
	}
	defer db.Close()
	writeGate := store.NewWriteGate(db, dbPath)

	gate := access.New()
	server := &httpapi.Server{
		App:     config.AppName(),
		Version: config.Version(),
		DataDir: dataDir,
		DistDir: distDir,
		Register: func(mux *http.ServeMux) {
			regions.Register(mux, db)
			rates.Register(mux, db)
			games.Register(mux, db)
			systemx.Register(mux, db)
			systemx.RegisterSession(mux, gate, *devSession)
			settings.Register(mux, db, writeGate)
			notifications.Register(mux, db)
			eventstream.Register(mux, bus, ring)
			if err := agent.Register(mux, db, gate, agent.Options{
				DataDir:       dataDir,
				EnabledByFlag: *mcpEnabled,
				Log:           logLine,
			}); err != nil {
				logLine("%v", err)
				os.Exit(1)
			}
		},
	}
	httpServer := &http.Server{Addr: *addr, Handler: gate.Middleware(server.Handler())}

	logLine("数据目录：%s", dataDir)
	logLine("数据库：%s", dbPath)
	logLine("监听地址：%s", *addr)

	serveErr := make(chan error, 1)
	go func() {
		serveErr <- httpServer.ListenAndServe()
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	select {
	case err := <-serveErr:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			logLine("服务启动失败：%v", err)
			os.Exit(1)
		}
	case <-stop:
		logLine("收到退出信号，正在关闭")
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := httpServer.Shutdown(ctx); err != nil {
			logLine("服务关闭未完成：%v", err)
			os.Exit(1)
		}
	}
}
