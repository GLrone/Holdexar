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
	"syscall"
	"time"

	"holdexar/server-go/internal/config"
	"holdexar/server-go/internal/httpapi"
)

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

	server := &httpapi.Server{
		App:     config.AppName(),
		Version: config.Version(),
		DataDir: dataDir,
		DistDir: distDir,
	}
	httpServer := &http.Server{Addr: *addr, Handler: server.Handler()}

	fmt.Fprintf(os.Stderr, "数据目录：%s\n", dataDir)
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
