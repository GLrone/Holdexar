package proxypool

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

type Orchestrator struct {
	DB         *sql.DB
	Gate       *store.WriteGate
	DataDir    string
	Syncer     *Syncer
	Health     *HealthChecker
	Prober     *DeadProbeRunner
	Now        func() time.Time
	Lanes      int // 未使用；KernelRebuildFunc 由 CrawlLaneCount 决定 lane 数
	ProbeBatch int
	ProbeL1URL string
	ProbeL2URL string
	Signature  func(ctx context.Context) ([]string, error)
	Rebuild    func(ctx context.Context) error
}

type StepReport struct {
	Ran   bool
	Error string
}

func (s StepReport) failed() bool { return s.Error != "" }

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

func RunPoolMaintenance(ctx context.Context, o *Orchestrator) (*MaintenanceReport, error) {
	if err := o.check(); err != nil {
		return nil, err
	}
	report := &MaintenanceReport{}
	before, sigErr := o.poolSignature(ctx)

	if o.Syncer != nil {
		report.Sync.Ran = true
		res, err := o.Syncer.SyncSubscriptions(ctx)
		report.SyncResult = res
		if err != nil {
			report.Sync.Error = err.Error()
		}
	}

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

type DeadProbeSweepReport struct {
	Outcomes         []DeadProbeOutcome
	Recovered        int
	SignatureChanged bool
	Rebuild          StepReport
}

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

func IngestCrawlExits(ctx context.Context, o *Orchestrator, runID int64,
	rows []ExitStatsRow, nodeByExit map[string]string) (int, error) {
	if err := o.check(); err != nil {
		return 0, err
	}
	return RecordRunExits(ctx, o.Gate, runID, rows, nodeByExit, nil)
}

// KernelRebuildFunc 合格集为空时拒绝物化空池（保留现核）；非空且未跑时冷启动。
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
