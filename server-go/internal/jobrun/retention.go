package jobrun

import "context"

const RetentionJob = "agent_ledger_prune"

func (r *Recorder) Prune(ctx context.Context) (int64, bool, error) {
	return r.prune(ctx, 5000, 20)
}

func (r *Recorder) prune(ctx context.Context, chunk, maxChunks int) (int64, bool, error) {
	cutoff := stampAt(r.Now().AddDate(0, 0, -30))
	return r.Gate.PruneBatches(ctx, `DELETE FROM agent_runs WHERE id IN (
		SELECT id FROM agent_runs WHERE runner='job' AND created_at<? ORDER BY id LIMIT ?)`, chunk, maxChunks, cutoff)
}
