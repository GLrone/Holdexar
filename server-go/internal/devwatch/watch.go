package devwatch

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

const (
	EnvWatch = "HOLDEXAR_DEV_WATCH"
	EnvChild = "HOLDEXAR_DEV_CHILD"
)

func fingerprint(root string) (string, error) {
	hash := sha256.New()
	add := func(path string) error {
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		fmt.Fprintf(hash, "%s\x00%d\x00", filepath.ToSlash(rel), len(data))
		_, err = hash.Write(data)
		return err
	}
	for _, name := range []string{"go.mod", "go.sum"} {
		if err := add(filepath.Join(root, name)); err != nil && !(name == "go.sum" && os.IsNotExist(err)) {
			return "", err
		}
	}
	for _, dir := range []string{"cmd", "internal"} {
		err := filepath.WalkDir(filepath.Join(root, dir), func(path string, entry fs.DirEntry, err error) error {
			if os.IsNotExist(err) {
				return nil
			}
			if err != nil {
				return err
			}
			if entry.IsDir() {
				return nil
			}
			if strings.HasSuffix(path, "_test.go") || (filepath.Ext(path) != ".go" && filepath.Ext(path) != ".sql") {
				return nil
			}
			return add(path)
		})
		if err != nil {
			return "", err
		}
	}
	return fmt.Sprintf("%x", hash.Sum(nil)), nil
}

type child struct {
	cmd   *exec.Cmd
	input io.WriteCloser
	done  chan struct{}
	err   error
}

func start(exe string, args []string, output io.Writer) (*child, error) {
	cmd := exec.Command(exe, args...)
	for _, value := range os.Environ() {
		key, _, _ := strings.Cut(value, "=")
		if !strings.EqualFold(key, EnvWatch) && !strings.EqualFold(key, EnvChild) {
			cmd.Env = append(cmd.Env, value)
		}
	}
	cmd.Env = append(cmd.Env, EnvChild+"=1")
	cmd.Stdout, cmd.Stderr = output, output
	input, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	if err = cmd.Start(); err != nil {
		input.Close()
		return nil, err
	}
	c := &child{cmd: cmd, input: input, done: make(chan struct{})}
	go func() { c.err = cmd.Wait(); close(c.done) }()
	return c, nil
}

func (c *child) stop() error {
	_ = c.input.Close()
	select {
	case <-c.done:
		return nil
	case <-time.After(15 * time.Second):
		if err := c.cmd.Process.Kill(); err != nil {
			return err
		}
		select {
		case <-c.done:
			return nil
		case <-time.After(5 * time.Second):
			return fmt.Errorf("旧服务尚未退出")
		}
	}
}

func Run(ctx context.Context, root, executable string, args []string, output io.Writer) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	current, err := fingerprint(root)
	if err != nil {
		return err
	}
	buildDir, err := os.MkdirTemp("", "holdexar-go-reload-")
	if err != nil {
		return err
	}
	defer func() {
		if err := os.RemoveAll(buildDir); err != nil {
			fmt.Fprintf(output, "[开发] 临时构建清理失败：%v\n", err)
		}
	}()
	active, err := start(executable, args, output)
	if err != nil {
		return err
	}
	defer func() {
		if err := active.stop(); err != nil {
			fmt.Fprintf(output, "[开发] 服务关闭失败：%v\n", err)
		}
	}()
	fmt.Fprintln(output, "[开发] 已监视后端源码，保存后自动编译并重启")
	ticker := time.NewTicker(400 * time.Millisecond)
	defer ticker.Stop()
	attempted, seen := current, current
	changedAt := time.Now()
	generation := 0
	childDone := active.done
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-childDone:
			if exitCodeOf(active.err) == restartExitCode {
				// 数据库恢复换库：原样重启子进程（不重编译），监视继续
				fmt.Fprintln(output, "[开发] 数据库恢复完成，正在重启后端")
				replacement, err := start(executable, args, output)
				if err != nil {
					return fmt.Errorf("恢复后重启开发服务失败：%w", err)
				}
				active = replacement
				childDone = active.done
				generation++
				continue
			}
			if generation == 0 {
				return fmt.Errorf("开发服务启动后退出：%v", active.err)
			}
			fmt.Fprintf(output, "[开发] 后端已退出：%v；修正并保存后自动重试\n", active.err)
			childDone = nil
		case <-ticker.C:
		}
		next, err := fingerprint(root)
		if err != nil {
			fmt.Fprintf(output, "[开发] 暂未读取到完整源码：%v\n", err)
			continue
		}
		if next != seen {
			seen = next
			changedAt = time.Now()
			continue
		}
		if next == attempted || time.Since(changedAt) < 300*time.Millisecond {
			continue
		}
		attempted = next
		generation++
		candidate := filepath.Join(buildDir, fmt.Sprintf("serverd-%d.exe", generation))
		fmt.Fprintln(output, "[开发] 检测到后端改动，正在编译；当前服务继续运行")
		build := exec.CommandContext(ctx, "go", "build", "-o", candidate, "./cmd/serverd")
		build.Dir = root
		build.Stdout, build.Stderr = output, output
		if err = build.Run(); err != nil {
			_ = os.Remove(candidate)
			if ctx.Err() != nil {
				return nil
			}
			fmt.Fprintln(output, "[开发] 编译失败，保留当前服务；修正并保存后自动重试")
			continue
		}
		latest, err := fingerprint(root)
		if err != nil || latest != next {
			_ = os.Remove(candidate)
			fmt.Fprintln(output, "[开发] 编译期间源码又有改动，继续合并后重建")
			continue
		}
		if ctx.Err() != nil {
			return nil
		}
		if err = active.stop(); err != nil {
			return fmt.Errorf("无法交接旧服务：%w", err)
		}
		replacement, err := start(candidate, args, output)
		if err != nil {
			return err
		}
		oldExecutable := active.cmd.Path
		active = replacement
		childDone = active.done
		if filepath.Dir(oldExecutable) == buildDir {
			_ = os.Remove(oldExecutable)
		}
		fmt.Fprintln(output, "[开发] 新后端已启动，窗口将自动重新连接")
	}
}

// restartExitCode 数据库恢复换库的重启请求退出码（跨进程契约，
// 与 internal/systemx.RestartExitCode、窗口宿主同值）。
const restartExitCode = 75

func exitCodeOf(err error) int {
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return exit.ExitCode()
	}
	return -1
}
