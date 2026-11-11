//go:build windows

// Holdexar 独立更新宿主（Go 原生 UpdateHost）：换装事务、数据保护协议、
// 健康验收与回滚。CLI 与恢复语义对齐原实现；退出码 0=成功 1=失败。
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const (
	healthTimeout     = 90 * time.Second
	waitPIDTimeout    = 15 * time.Second
	registrationWait  = 3 * time.Second
	probePollInterval = 500 * time.Millisecond
	probeHTTPTimeout  = 3 * time.Second
	answerHTTPTimeout = time.Second
	defaultServerPort = 28765
)

type options struct {
	staging         string
	target          string
	targetVersion   string
	sourceVersion   string
	attemptID       string
	dataDir         string
	waitPID         int
	skipHealthCheck bool
	noLaunch        bool
	recoverOnly     bool
}

func reportDataProtocol(argv []string) bool {
	for _, arg := range argv {
		if arg == "--update-protocol" {
			payload := []byte(`{"updateDataProtocol":1}`)
			if path := os.Getenv(envPrefix + "UPDATE_PROTOCOL_RESULT"); path != "" {
				_ = os.WriteFile(path, payload, 0o600)
			} else {
				_, _ = fmt.Fprintln(os.Stdout, string(payload))
			}
			return true
		}
	}
	return false
}

func versionFromTag(tag string) string {
	text := strings.TrimSpace(tag)
	if len(text) > 1 && (text[0] == 'v' || text[0] == 'V') && text[1] >= '0' && text[1] <= '9' {
		return text[1:]
	}
	return text
}

func versionGT(first, second string) bool {
	parse := func(v string) ([]int, string) {
		main_, pre := v, ""
		if idx := strings.IndexAny(v, "-+"); idx >= 0 {
			main_, pre = v[:idx], v[idx+1:]
		}
		var parts []int
		for _, piece := range strings.Split(main_, ".") {
			value := 0
			for _, r := range piece {
				if r < '0' || r > '9' {
					break
				}
				value = value*10 + int(r-'0')
			}
			parts = append(parts, value)
		}
		return parts, pre
	}
	left, leftPre := parse(first)
	right, rightPre := parse(second)
	for i := 0; i < len(left) || i < len(right); i++ {
		l, r := 0, 0
		if i < len(left) {
			l = left[i]
		}
		if i < len(right) {
			r = right[i]
		}
		if l != r {
			return l > r
		}
	}
	if leftPre == "" && rightPre != "" {
		return true
	}
	if leftPre != "" && rightPre != "" {
		return leftPre > rightPre
	}
	return false
}

func logUpdate(root, message string) {
	line := time.Now().Format("2006-01-02 15:04:05") + " [更新宿主] " + message
	fmt.Println(line)
	logs := filepath.Join(root, "logs")
	if err := os.MkdirAll(logs, 0o755); err == nil {
		file, err := os.OpenFile(filepath.Join(logs, "update.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err == nil {
			_, _ = file.WriteString(line + "\n")
			_ = file.Close()
		}
	}
}

type diagnosis struct {
	AttemptID       string  `json:"attemptId"`
	TargetVersion   string  `json:"targetVersion"`
	SourceVersion   string  `json:"sourceVersion"`
	PreviousVersion string  `json:"previousVersion,omitempty"`
	CurrentVersion  string  `json:"currentVersion,omitempty"`
	Notes           string  `json:"notes,omitempty"`
	DataDir         string  `json:"dataDir"`
	StartTime       float64 `json:"startTime"`
	Stage           string  `json:"stage"`
	Success         bool    `json:"success"`
	Error           *string `json:"error"`
	EndTime         float64 `json:"endTime,omitempty"`
	Acknowledged    bool    `json:"acknowledged,omitempty"`
	DataPreserved   bool    `json:"dataPreserved,omitempty"`
}

func writeDiagnosis(root string, record diagnosis) {
	payload, err := json.MarshalIndent(record, "", "  ")
	if err == nil {
		logs := filepath.Join(root, "logs")
		if os.MkdirAll(logs, 0o755) == nil {
			_ = os.WriteFile(filepath.Join(logs, "update-diagnosis.json"), payload, 0o644)
		}
	}
}

func finishDiagnosis(root string, currentVersion string, record *diagnosis) {
	if record == nil {
		tx := readTransaction(root)
		record = &diagnosis{
			AttemptID: txString(tx, "attemptId"), TargetVersion: txString(tx, "targetVersion"),
			SourceVersion: txString(tx, "sourceVersion"), DataDir: txString(tx, "dataDir"),
		}
	}
	record.CurrentVersion = currentVersion
	record.Stage = stageCommitted
	record.Success = true
	record.EndTime = float64(time.Now().UnixNano()) / 1e9
	if currentVersion != record.TargetVersion {
		record.Stage = "accepted_failure"
		record.Success = false
		message := "已保留已接受数据，当前使用相容版本"
		record.Error = &message
	}
	writeDiagnosis(root, *record)
}

func validateRoot(root string) error {
	if strings.EqualFold(filepath.Base(filepath.Dir(root)), versionsDir) {
		if _, err := os.Stat(filepath.Join(filepath.Dir(filepath.Dir(root)), stateDirName, currentStateFile)); err == nil {
			return errors.New("更新目标不能是版本目录内部")
		}
	}
	return nil
}

func waitProcessExit(pid int, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for {
		if !pidAlive(pid) {
			return true
		}
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func resolveDefaultRoot() string {
	exe, err := os.Executable()
	if err != nil {
		wd, _ := os.Getwd()
		return wd
	}
	dir := filepath.Dir(exe)
	if strings.EqualFold(filepath.Base(dir), "updater") {
		return filepath.Dir(dir)
	}
	return dir
}

func alert(message string) {
	messageBox(0, message, appName, 0x10)
}

func main() {
	argv := os.Args[1:]
	if reportDataProtocol(argv) {
		return
	}
	opts := &options{}
	flags := flag.NewFlagSet("UpdateHost", flag.ContinueOnError)
	flags.StringVar(&opts.staging, "staging", "", "更新载荷暂存目录")
	flags.BoolVar(&opts.recoverOnly, "recover-only", false, "恢复安装目录中断的更新事务")
	flags.StringVar(&opts.target, "target", "", "安装根目录")
	flags.StringVar(&opts.targetVersion, "target-version", "", "目标版本号")
	flags.StringVar(&opts.sourceVersion, "source-version", "", "源版本号")
	flags.StringVar(&opts.attemptID, "attempt-id", "", "更新尝试唯一 ID")
	flags.StringVar(&opts.dataDir, "data-dir", "", "用户数据目录")
	flags.IntVar(&opts.waitPID, "wait-pid", 0, "等待退出的进程 PID")
	flags.BoolVar(&opts.skipHealthCheck, "skip-health-check", false, "跳过健康检查")
	flags.BoolVar(&opts.noLaunch, "no-launch", false, "完成后不自动拉起")
	if err := flags.Parse(argv); err != nil {
		os.Exit(1)
	}
	root := opts.target
	if root == "" {
		if env := strings.TrimSpace(os.Getenv(envPrefix + "INSTALL_ROOT")); env != "" {
			root = env
		} else {
			root = resolveDefaultRoot()
		}
	}
	root = absPath(root)
	if opts.recoverOnly {
		if err := validateRoot(root); err != nil {
			fmt.Fprintf(os.Stderr, "更新恢复未完成，数据保护已保留：%v\n", err)
			os.Exit(1)
		}
		release, err := acquireSwapMutex()
		if err != nil {
			fmt.Fprintf(os.Stderr, "更新恢复未完成，数据保护已保留：%v\n", err)
			os.Exit(1)
		}
		err = recoverInterruptedUpdate(root)
		release()
		if err != nil {
			fmt.Fprintf(os.Stderr, "更新恢复未完成，数据保护已保留：%v\n", err)
			os.Exit(1)
		}
		os.Exit(0)
	}
	if opts.staging == "" {
		fmt.Fprintln(os.Stderr, "必须提供 --staging 或 --recover-only")
		os.Exit(1)
	}
	opts.staging = absPath(opts.staging)
	if opts.dataDir != "" {
		opts.dataDir = absPath(opts.dataDir)
	}

	release, err := acquireSwapMutex()
	if err != nil {
		logUpdate(root, "更新未开始："+err.Error())
		alert("[更新] 本次更新未完成。已有更新正在进行。\n\n详情见 " + filepath.Join(root, "logs", "update-diagnosis.json"))
		os.Exit(1)
	}
	ok := executeUpdate(root, opts)
	release()
	if !ok {
		alert("[更新] 本次更新未完成。数据恢复状态与处理方法见诊断说明。\n\n详情见 " + filepath.Join(root, "logs", "update-diagnosis.json"))
		os.Exit(1)
	}
	os.Exit(0)
}
