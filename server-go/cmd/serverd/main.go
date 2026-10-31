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

	"holdexar/server-go/internal/config"
	"holdexar/server-go/internal/games"
	"holdexar/server-go/internal/httpapi"
	"holdexar/server-go/internal/rates"
	"holdexar/server-go/internal/regions"
	"holdexar/server-go/internal/store"
	"holdexar/server-go/internal/systemx"
)

// schemaVersion 与数据层迁移链的当前版本一致；迁移链 Go 化后由该链合成提供。
const schemaVersion = 34

func main() {
	addr := flag.String("addr", "127.0.0.1:28765", "监听地址")
	dataDirFlag := flag.String("data-dir", "", "显式数据目录，跳过自动判定")
	flag.Parse()

	if *dataDirFlag != "" {
		os.Setenv(config.EnvPrefix+"DATA_DIR", *dataDirFlag)
	}
	dataDir, err := config.ResolveDataDir()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	distDir, err := config.WebDistDir()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	dbPath := filepath.Join(dataDir, "holdexar.db")
	if _, err := os.Stat(dbPath); err != nil {
		fmt.Fprintf(os.Stderr, "数据库不存在：%s（首次建库由初始化链负责，服务端不代建）\n", dbPath)
		os.Exit(1)
	}
	if err := store.EnsureSupportedVersion(dbPath, schemaVersion); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	db, err := store.Open(dbPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer db.Close()

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
		},
	}
	httpServer := &http.Server{Addr: *addr, Handler: server.Handler()}

	fmt.Fprintf(os.Stderr, "数据目录：%s\n", dataDir)
	fmt.Fprintf(os.Stderr, "数据库：%s\n", dbPath)
	fmt.Fprintf(os.Stderr, "监听地址：%s\n", *addr)

	serveErr := make(chan error, 1)
	go func() {
		serveErr <- httpServer.ListenAndServe()
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	select {
	case err := <-serveErr:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			fmt.Fprintf(os.Stderr, "服务启动失败：%v\n", err)
			os.Exit(1)
		}
	case <-stop:
		fmt.Fprintln(os.Stderr, "收到退出信号，正在关闭")
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := httpServer.Shutdown(ctx); err != nil {
			fmt.Fprintf(os.Stderr, "服务关闭未完成：%v\n", err)
			os.Exit(1)
		}
	}
}
