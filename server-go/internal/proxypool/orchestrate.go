package proxypool

// 池维护编排（消费端组装）：把订阅同步、准入晋升、健康波次、池签名比对、恢复
// 探测与出口聚合接成可运行的编排入口。各段零件自管落账（Syncer / HealthChecker /
// DeadProbeRunner / RecordRunExits），本文件只定步骤顺序与失败隔离——一步失败
// 不拖垮其余（对齐 Python 维护循环：探针异常只进收尾 end_reason，合格集比对照常
// 收尾；同步失败旧 Runtime 继续工作）。重建经回调注入：标准形态 KernelRebuildFunc
// （池文件重生成 → 运行配置重生成 → 内核热重载）；回调为 nil 时回落
// MaybeRequestPoolRebuild 既有全局钩子。serverd 进程接线不在此文件。

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"holdexar/server-go/internal/clashcfg"
	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/store"
)

// Orchestrator 编排依赖：零件经字段注入，nil 的段跳过。DB / Gate / DataDir 必需。
// Signature 与 Rebuild 是可替身的回调读点（测试注入假实现；缺省走包内默认）。
type Orchestrator struct {
	DB      *sql.DB
	Gate    *store.WriteGate
	DataDir string
	Syncer  *Syncer          // 订阅同步（nil = 跳过）
	Health  *HealthChecker   // 健康波次（nil = 跳过）
	Prober  *DeadProbeRunner // 恢复探测（nil = RunDeadProbeSweep 现场组装）
	Now     func() time.Time
	Lanes   int // 重建时的 lane 数（0 = 按池规模，LaneCountFor）
	// ProbeBatch 恢复探测批大小（0 = DeadProbeBatchSize）。
	ProbeBatch int
	// ProbeL1URL / ProbeL2URL 健康波次探针目标（空 = 生产默认；测试与 live 指向本地桩）。
	ProbeL1URL string
	ProbeL2URL string
	// Signature 池签名读点（nil = PoolSignature 默认实现，闸内只读）。
	Signature func(ctx context.Context) ([]string, error)
	// Rebuild 合格集变化后的重建回调（clashcfg 重生成 + clashkernel 热重载由
	// 接线方提供）；nil = MaybeRequestPoolRebuild 全局钩子（接线前 no-op）。
	Rebuild func(ctx context.Context) error
}

// StepReport 单步骤账目（Ran=false = 依赖未注入跳过；Error 空 = 成功或未触发）。
type StepReport struct {
	Ran   bool
	Error string
}

func (s StepReport) failed() bool { return s.Error != "" }

// MaintenanceReport 一轮池维护的账目（步骤失败不改变整体成败）。
type MaintenanceReport struct {
	Sync             StepReport
	SyncResult       *SyncResult
	Admission        StepReport
	Promoted         int
	Health           StepReport
	HealthRunID      int64
	HealthSummary    HealthRunSummary
	SignatureChanged bool
	Rebuild          StepReport
}

func (o *Orchestrator) now() time.Time {
	if o.Now != nil {
		return o.Now()
	}
	return time.Now()
}

func (o *Orchestrator) check() error {
	if o == nil || o.DB == nil || o.Gate == nil || o.DataDir == "" {
		return fmt.Errorf("编排依赖未注入（DB / Gate / DataDir 必填）")
	}
	return nil
}

// poolSignature 读点：注入的替身优先，缺省走 PoolSignature（闸内只读）。
func (o *Orchestrator) poolSignature(ctx context.Context) ([]string, error) {
	if o.Signature != nil {
		return o.Signature(ctx)
	}
	conn, err := o.DB.Conn(ctx)
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	return PoolSignature(ctx, conn, o.DataDir, o.now())
}

// requestRebuild 签名变化后的重建出口：注入回调优先，nil 回落全局钩子。
func (o *Orchestrator) requestRebuild(ctx context.Context, before, after []string) (StepReport, bool) {
	if !PoolSignatureChanged(before, after) {
		return StepReport{}, false
	}
	report := StepReport{Ran: true}
	if o.Rebuild != nil {
		if err := o.Rebuild(ctx); err != nil {
			report.Error = err.Error()
		}
		return report, true
	}
	MaybeRequestPoolRebuild(before, after)
	return report, true
}

// RunPoolMaintenance 一轮池维护：订阅同步 → 准入晋升检查 → 健康波次
// （StartHealthRun → L1 → L2 → FinishHealthRun 串联）→ 池签名比对 → 变化则重建。
// before 签名在同步前取，让同步 / 准入 / 健康任一步引起的合格集变化都收敛到
// 同一重建信号（Python handle_subscription_refresh 与 _maintenance_probe 两处
// before/after 比较的并集）。除依赖缺失外不返回错误——各步失败进报告字段。
func RunPoolMaintenance(ctx context.Context, o *Orchestrator) (*MaintenanceReport, error) {
	if err := o.check(); err != nil {
		return nil, err
	}
	report := &MaintenanceReport{}
	before, sigErr := o.poolSignature(ctx)

	// ① 订阅同步：失败留痕不中断（Python 同步失败 → 旧 Runtime 继续工作）
	if o.Syncer != nil {
		report.Sync.Ran = true
		res, err := o.Syncer.SyncSubscriptions(ctx)
		report.SyncResult = res
		if err != nil {
			report.Sync.Error = err.Error()
		}
	}

	// ② 准入晋升检查：候选订阅按体检事实转正（AutoAdmitHealthyCandidate 对
	//    晋升失败保持候选不抛出；只有查询级错误才整轮记账并回滚）。
	report.Admission.Ran = true
	now := o.now()
	err := o.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if err := EnsureSchema(ctx, conn); err != nil {
			return err
		}
		rows, err := conn.QueryContext(ctx,
			`SELECT id FROM proxy_subscriptions WHERE kind = ? AND admission_status = ? ORDER BY id`,
			"clash", AdmissionCandidate)
		if err != nil {
			return err
		}
		defer rows.Close()
		var ids []int64
		for rows.Next() {
			var id int64
			if err := rows.Scan(&id); err != nil {
				return err
			}
			ids = append(ids, id)
		}
		if err := rows.Err(); err != nil {
			return err
		}
		for _, id := range ids {
			result, err := AutoAdmitHealthyCandidate(ctx, conn, o.DataDir, id, now)
			if err != nil {
				return err
			}
			if result != nil && result.Promoted {
				report.Promoted++
			}
		}
		return nil
	})
	if err != nil {
		report.Admission.Error = err.Error()
	}

	// ③ 健康波次：run 行先落库（即刻提交），L1 → L2 分层探测，汇总收尾提交。
	//    探针层异常只进 end_reason（Python probe_errors 形态），不阻断收尾。
	if o.Health != nil {
		report.Health.Ran = true
		runID, err := StartHealthRun(ctx, o.Gate, DefaultScheduleChannel, nil, now)
		if err != nil {
			report.Health.Error = err.Error()
		} else {
			report.HealthRunID = runID
			summary, waveErr := o.runHealthWave(ctx, runID)
			report.HealthSummary = summary
			if waveErr != nil {
				report.Health.Error = waveErr.Error()
			}
		}
	}

	// ④ 池签名比对 → ⑤ 变化则重建。签名读不了时跳过重建判定（宁少重建一次，
	//    不在事实不全时扰动内核）。
	after, err := o.poolSignature(ctx)
	if err != nil {
		report.Rebuild.Error = "重建判定跳过：池签名读取失败（" + err.Error() + "）"
	} else if sigErr != nil {
		report.Rebuild.Error = "重建判定跳过：池签名读取失败（" + sigErr.Error() + "）"
	} else {
		rebuild, changed := o.requestRebuild(ctx, before, after)
		report.SignatureChanged = changed
		report.Rebuild = rebuild
	}
	return report, nil
}

// runHealthWave L1 出口身份 → L2 业务可用性两波（节点名单 = 池文件当前成员），
// 建行与收尾由调用方串联；探针层错误只进 end_reason。
func (o *Orchestrator) runHealthWave(ctx context.Context, runID int64) (HealthRunSummary, error) {
	summary := HealthRunSummary{}
	started := o.now()
	names := PoolFileNames(o.DataDir)
	var probeErrors []string
	l1, err := o.Health.ExitIPCheckPool(ctx, names, ExitProbeOptions{URL: o.ProbeL1URL})
	if err != nil {
		probeErrors = append(probeErrors, "l1_error")
	}
	l2, err := o.Health.BusinessCheckPool(ctx, names, BusinessProbeOptions{URL: o.ProbeL2URL})
	if err != nil {
		probeErrors = append(probeErrors, "l2_error")
	}
	for _, outcome := range l1 {
		if outcome.OK && outcome.ExitIP != "" {
			summary.IPKnown++
		}
	}
	summary.Total = int64(len(l2))
	for _, outcome := range l2 {
		if outcome.OK {
			summary.SteamOK++
		}
	}
	summary.Failed = summary.Total - summary.SteamOK
	summary.EndReason = strings.Join(probeErrors, ",")
	finished := o.now()
	if err := FinishHealthRun(ctx, o.Gate, runID, summary, finished,
		finished.Sub(started).Milliseconds()); err != nil {
		return summary, err
	}
	return summary, nil
}

// DeadProbeSweepReport 一轮恢复探测扫批的账目。
type DeadProbeSweepReport struct {
	Outcomes         []DeadProbeOutcome
	Recovered        int
	SignatureChanged bool
	Rebuild          StepReport
}

// RunDeadProbeSweep 恢复探测扫批：取批 → 临时内核探测 → 结果落账（RecoverDeadNodes
// 内含提交）→ 签名比对 → 恢复的节点重新合格则重建（Python run_scheduled_l0 的
// 恢复探测后 before/after 比较同款）。probeURL 空 = L2 业务探针目标。
func RunDeadProbeSweep(ctx context.Context, o *Orchestrator, probeURL string) (*DeadProbeSweepReport, error) {
	if err := o.check(); err != nil {
		return nil, err
	}
	report := &DeadProbeSweepReport{}
	before, sigErr := o.poolSignature(ctx)

	prober := o.Prober
	if prober == nil {
		prober = &DeadProbeRunner{DB: o.DB, Gate: o.Gate, DataDir: o.DataDir, Now: o.Now}
	}
	outcomes, err := prober.RecoverDeadNodes(ctx, probeURL, o.ProbeBatch)
	if err != nil {
		report.Rebuild.Error = "恢复探测失败：" + err.Error()
		return report, nil
	}
	report.Outcomes = outcomes
	for _, outcome := range outcomes {
		if outcome.OK {
			report.Recovered++
		}
	}

	after, err := o.poolSignature(ctx)
	if err != nil || sigErr != nil {
		report.Rebuild.Error = "重建判定跳过：池签名读取失败"
		return report, nil
	}
	rebuild, changed := o.requestRebuild(ctx, before, after)
	report.SignatureChanged = changed
	report.Rebuild = rebuild
	return report, nil
}

// IngestCrawlExits 爬取出口聚合消费入口：RecordRunExits 包装（落 proxy_run_exits
// 行 + 生产证据回写节点账本）。对齐 Python：出口聚合的对应物 write_run_exits →
// record_run_exits 没有重建 / 健康波次的后续动作——生产证据只落账，合格集变化
// 由下一拍维护统一收敛，本入口不触发重建。fail-soft：错误返回交调用方降级，
// 作业结果不受影响（Python 吞异常记日志返回 0 的 Go 侧形态）。
func IngestCrawlExits(ctx context.Context, o *Orchestrator, runID int64,
	rows []ExitStatsRow, nodeByExit map[string]string) (int, error) {
	if err := o.check(); err != nil {
		return 0, err
	}
	return RecordRunExits(ctx, o.Gate, runID, rows, nodeByExit, nil)
}

// KernelRebuildFunc 重建回调的标准形态（live 与 serverd 接线复用）：闸内
// BuildPool 重生成池文件 → PrepareRuntimeConfig 沿现役端点重生成运行配置 →
// EnsureRunning 热重载（内核进程不动；控制器通路故障由 EnsureRunning 回退重启）。
// 合格集已空且内核在跑时拒绝物化空池（保留现核等订阅恢复，Python
// _rebuild_skip_reason 同款守卫）；内核未跑时 EnsureRunning 冷启动拉起。
func KernelRebuildFunc(db *sql.DB, gate *store.WriteGate, dataDir, exePath string,
	runtime *clashkernel.Runtime) func(ctx context.Context) error {
	return func(ctx context.Context) error {
		written := 0
		err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
			if err := EnsureSchema(ctx, conn); err != nil {
				return err
			}
			eligible, err := EligibleNodes(ctx, conn, time.Now())
			if err != nil {
				return err
			}
			if len(eligible) == 0 {
				return nil
			}
			result, err := BuildPool(ctx, conn, dataDir, time.Now())
			written = result.WrittenCount
			return err
		})
		if err != nil {
			return err
		}
		if err := InvalidateCrawlLanes(dataDir); err != nil {
			return err
		}
		if written == 0 {
			return nil
		}
		lanes, err := CrawlLaneCount(ctx, db, dataDir)
		if err != nil {
			return err
		}
		wasRunning := runtime.Status().Running
		oldPID := runtime.ProcessPid()
		var previous []byte
		if wasRunning {
			previous, err = os.ReadFile(filepath.Join(dataDir, "proxypool", clashcfg.RuntimeConfigFilename))
			if err != nil {
				return err
			}
		}
		configPath, _, err := clashcfg.PrepareRuntimeConfig(dataDir, lanes, wasRunning)
		if err != nil {
			return err
		}
		if _, err = runtime.EnsureRunning(exePath, configPath, clashkernel.EnsureRunningOptions{}); err != nil {
			if wasRunning && runtime.ProcessPid() == oldPID {
				err = errors.Join(err, os.WriteFile(configPath, previous, 0o644))
			}
			return err
		}
		return BindCrawlLanes(ctx, db, dataDir, uint32(runtime.ProcessPid()))
	}
}
