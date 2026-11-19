package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"sync"
	"sync/atomic"
	"time"

	"holdexar/hostkit"
)

type WritePriority int

const (
	PriorityInteractive WritePriority = 0
	PriorityBackground  WritePriority = 10
)

func (p WritePriority) queueIndex() (int, error) {
	switch p {
	case PriorityInteractive:
		return 0, nil
	case PriorityBackground:
		return 1, nil
	}
	return 0, fmt.Errorf("未知的写优先级 %d", int(p))
}

func (p WritePriority) label() string {
	switch p {
	case PriorityInteractive:
		return "交互"
	case PriorityBackground:
		return "后台"
	}
	return fmt.Sprintf("优先级%d", int(p))
}

func (p WritePriority) pyName() string {
	if p == PriorityInteractive {
		return "INTERACTIVE"
	}
	return "BACKGROUND"
}

const (
	defaultWaitTimeout = 60 * time.Second
	defaultLockTimeout = 60 * time.Second
	defaultHoldWarn    = 5 * time.Second
	lockPollInterval   = 25 * time.Millisecond
)

type writeReq struct {
	seq      uint64
	priority WritePriority
	queue    int
	reply    chan *Lease
	reject   chan error
}

var ErrWritesPaused = errors.New("数据库正在整理，写入暂不可用")

// Lease 写者位凭证，持有人须显式调用 Done 释放。
type Lease struct {
	gate *WriteGate
	seq  uint64
	file *os.File
	done sync.Once
}

func (l *Lease) Done() {
	l.done.Do(func() {
		if l.file != nil {
			unlockFile(l.file)
			l.file = nil
		}
		l.gate.doneCh <- l.seq
	})
}

// WriteGate 单 Actor 单写者闸：交互队列优先于后台队列，持闸期间同步持有跨进程文件锁。
type WriteGate struct {
	db       *sql.DB
	lockPath string

	reqCh    chan *writeReq
	doneCh   chan uint64
	abortCh  chan uint64
	warnCh   chan uint64
	pauseCh  chan chan error
	resumeCh chan struct{}
	diagCh   chan chan map[string]any
	seq      atomic.Uint64

	waitTimeout   time.Duration
	lockTimeout   time.Duration
	holdWarnAfter time.Duration
	warnFn        func(format string, args ...any)
}

func NewWriteGate(db *sql.DB, dbPath string) *WriteGate {
	g := &WriteGate{
		db:            db,
		lockPath:      processWriteLockPath(dbPath),
		reqCh:         make(chan *writeReq),
		doneCh:        make(chan uint64),
		abortCh:       make(chan uint64),
		warnCh:        make(chan uint64, 1),
		pauseCh:       make(chan chan error),
		resumeCh:      make(chan struct{}, 1),
		diagCh:        make(chan chan map[string]any),
		waitTimeout:   defaultWaitTimeout,
		lockTimeout:   defaultLockTimeout,
		holdWarnAfter: defaultHoldWarn,
		warnFn:        func(format string, args ...any) { fmt.Fprintf(os.Stderr, format+"\n", args...) },
	}
	go g.run()
	return g
}

var _ = resolveFinalPath

// processWriteLockPath 在系统临时目录按规范化绝对路径 SHA-256 前缀生成跨进程锁路径。
func processWriteLockPath(dbPath string) string {
	return hostkit.ProcessWriteLockPath(dbPath)
}

func (g *WriteGate) run() {
	var queues [2][]*writeReq
	var owner *writeReq
	var holdStart time.Time
	var holdTimer *time.Timer
	var warned bool
	var paused bool

	grant := func(req *writeReq) {
		if holdTimer != nil {
			holdTimer.Stop()
		}
		owner = req
		holdStart = time.Now()
		warned = false
		holdTimer = time.AfterFunc(g.holdWarnAfter, func() { g.warnCh <- req.seq })
		req.reply <- &Lease{gate: g, seq: req.seq}
	}

	clearOwner := func() {
		if holdTimer != nil {
			holdTimer.Stop()
			holdTimer = nil
		}
		owner = nil
	}

	handoff := func() {
		for i := 0; i < len(queues); i++ {
			if len(queues[i]) == 0 {
				continue
			}
			req := queues[i][0]
			queues[i] = queues[i][1:]
			grant(req)
			return
		}
	}

	for {
		select {
		case reply := <-g.pauseCh:
			if owner == nil && len(queues[0]) == 0 && len(queues[1]) == 0 {
				paused = true
				reply <- nil
			} else {
				reply <- ErrWritesPaused
			}
		case <-g.resumeCh:
			paused = false
		case req := <-g.reqCh:
			if paused {
				req.reject <- ErrWritesPaused
				continue
			}
			// 写者位空出且无人排队才直接授予，后来者不得插队
			if owner == nil && len(queues[0]) == 0 && len(queues[1]) == 0 {
				grant(req)
			} else {
				queues[req.queue] = append(queues[req.queue], req)
			}
		case seq := <-g.doneCh:
			if owner != nil && owner.seq == seq {
				clearOwner()
				handoff()
			}
		case seq := <-g.abortCh:
			if owner != nil && owner.seq == seq {
				// 排队者放弃前已被授权：代为释放并移交，防写者位悬空
				clearOwner()
				handoff()
				continue
			}
			for i := 0; i < len(queues); i++ {
				for j, r := range queues[i] {
					if r.seq == seq {
						queues[i] = append(queues[i][:j], queues[i][j+1:]...)
						break
					}
				}
			}
		case seq := <-g.warnCh:
			if owner != nil && owner.seq == seq && !warned {
				warned = true
				g.warnFn("%s写事务占用写锁已超过 %v 秒仍未释放",
					owner.priority.label(), time.Since(holdStart).Round(100*time.Millisecond))
			}
		case reply := <-g.diagCh:
			snap := map[string]any{
				"busy":                owner != nil,
				"depth":               0,
				"owner_priority":      nil,
				"owner_label":         nil,
				"waiting_interactive": len(queues[0]),
				"waiting_background":  len(queues[1]),
			}
			if owner != nil {
				snap["depth"] = 1
				snap["owner_priority"] = owner.priority.pyName()
			}
			reply <- snap
		}
	}
}

// Acquire 依次取得进程内写者位与跨进程文件锁（不可重入，须显式 Done）。
func (g *WriteGate) Acquire(ctx context.Context, priority WritePriority) (*Lease, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	qi, err := priority.queueIndex()
	if err != nil {
		return nil, err
	}
	req := &writeReq{
		seq:      g.seq.Add(1),
		priority: priority,
		queue:    qi,
		reply:    make(chan *Lease, 1),
		reject:   make(chan error, 1),
	}
	select {
	case g.reqCh <- req:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	timer := time.NewTimer(g.waitTimeout)
	defer timer.Stop()
	select {
	case lease := <-req.reply:
		// 进程内授权在前、跨进程锁在后；锁取不得则交还写者位
		f, lerr := lockFile(g.lockPath, g.lockTimeout, ctx)
		if lerr != nil {
			lease.Done()
			return nil, lerr
		}
		lease.file = f
		return lease, nil
	case err := <-req.reject:
		return nil, err
	case <-timer.C:
		return nil, g.abandon(req, errors.New("等待数据库写入超时"))
	case <-ctx.Done():
		return nil, g.abandon(req, ctx.Err())
	}
}

// PauseWrites 仅在空闲且无排队时进入维护窗口拒绝新写（直到 ResumeWrites）；忙碌立即返回 ErrWritesPaused。
func (g *WriteGate) PauseWrites(ctx context.Context) error {
	reply := make(chan error, 1)
	select {
	case g.pauseCh <- reply:
	case <-ctx.Done():
		return ctx.Err()
	}
	select {
	case err := <-reply:
		return err
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (g *WriteGate) ResumeWrites() {
	select {
	case g.resumeCh <- struct{}{}:
	default:
	}
}

// Diagnostics 返回写闸与连接池诊断快照（owner_label 恒 nil；ctx 取消返回 nil）。
func (g *WriteGate) Diagnostics(ctx context.Context) map[string]any {
	if ctx == nil {
		ctx = context.Background()
	}
	reply := make(chan map[string]any, 1)
	select {
	case g.diagCh <- reply:
	case <-ctx.Done():
		return nil
	}
	select {
	case snap := <-reply:
		var stats sql.DBStats
		if g.db != nil {
			stats = g.db.Stats()
		}
		overflow := 0
		if stats.MaxOpenConnections > 0 {
			overflow = stats.OpenConnections - stats.MaxOpenConnections
		}
		snap["pool"] = map[string]any{
			"type":       "*sql.DB",
			"status":     fmt.Sprintf("Pool size: %d  Connections in pool: %d  Current Overflow: %d  Current Checked out connections: %d", stats.MaxOpenConnections, stats.Idle, overflow, stats.InUse),
			"size":       stats.MaxOpenConnections,
			"checkedOut": stats.InUse,
			"checkedIn":  stats.Idle,
			"overflow":   overflow,
		}
		return snap
	case <-ctx.Done():
		return nil
	}
}

// abandon 通知 Actor 按 seq 退队并回收竞态下已授予的凭证。
func (g *WriteGate) abandon(req *writeReq, cause error) error {
	g.abortCh <- req.seq
	select {
	case lease := <-req.reply:
		lease.Done()
	default:
	}
	return cause
}

// WithWrite 以 BEGIN IMMEDIATE 独占执行 fn（报错或 panic 自动回滚）；fn 不得自管事务。
func (g *WriteGate) WithWrite(ctx context.Context, priority WritePriority, fn func(*sql.Conn) error) error {
	lease, err := g.Acquire(ctx, priority)
	if err != nil {
		return err
	}
	defer lease.Done()
	conn, err := g.db.Conn(ctx)
	if err != nil {
		return fmt.Errorf("获取数据库写连接失败: %w", err)
	}
	defer conn.Close()
	if _, err := conn.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		return fmt.Errorf("开启写事务失败: %w", err)
	}
	open := true
	defer func() {
		if open {
			_, _ = conn.ExecContext(context.Background(), "ROLLBACK")
		}
	}()
	if err := fn(conn); err != nil {
		return err
	}
	if _, err := conn.ExecContext(ctx, "COMMIT"); err != nil {
		return fmt.Errorf("提交写事务失败: %w", err)
	}
	open = false
	return nil
}

// lockFile 在系统临时目录创建锁文件并按 25ms 轮询非阻塞独占锁（依赖临时目录本地盘支持文件锁）。
func lockFile(path string, timeout time.Duration, ctx context.Context) (*os.File, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, fmt.Errorf("跨进程写锁文件打开失败: %w", err)
	}
	if info, serr := f.Stat(); serr == nil && info.Size() == 0 {
		if _, werr := f.Write([]byte{0}); werr != nil {
			f.Close()
			return nil, fmt.Errorf("跨进程写锁文件初始化失败: %w", werr)
		}
	}
	deadline := time.Now().Add(timeout)
	for {
		if err := lockFileExclusive(f); err == nil {
			return f, nil
		}
		if time.Now().After(deadline) {
			f.Close()
			return nil, errors.New("等待数据库写入超时")
		}
		select {
		case <-ctx.Done():
			f.Close()
			return nil, ctx.Err()
		case <-time.After(lockPollInterval):
		}
	}
}

func unlockFile(f *os.File) {
	unlockFileExclusive(f)
	_ = f.Close()
}
