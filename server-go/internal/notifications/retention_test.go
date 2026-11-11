package notifications

import (
	"context"
	"testing"
)

func TestPruneCandidatesKeepsLiveRetryAndRecentRecovery(t *testing.T) {
	s, db := deliveryFixture(t, func(context.Context, SMTPConfig, string, string) (bool, string) {
		t.Error("retention attempted delivery")
		return false, "unexpected"
	})
	_, err := db.Exec(`DELETE FROM notification_candidates;
		WITH fixture(id,status,attempts,error,updated) AS (VALUES
		(1,'failed',1,'SMTPRejected: 421 busy','2025-09-01'),
		(2,'delivered',1,NULL,NULL),
		(3,'unknown',1,NULL,'2025-09-01'),
		(4,'failed',1,'SMTPAuthenticationError: rejected','2025-09-01'),
		(5,'failed',3,'SMTPRejected: 421 busy','2025-09-01'),
		(6,'pending',0,NULL,'2025-09-01'),
		(7,'suppressed',0,NULL,'2025-09-01'),
		(8,'sending',1,NULL,'2025-09-01'),
		(9,'sending',1,NULL,'2026-11-06 11:59:00'),
		(10,'delivered',1,NULL,'2026-11-01'))
		INSERT INTO notification_candidates(id,event_id,cycle_id,appid,event_type,category,recipient,status,attempts,last_error,created_at,updated_at)
		SELECT id,id,-1,123,'PRICE_DROP','price','fixture@example.invalid',status,attempts,error,'2025-09-01',updated FROM fixture`)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	first, err := s.pruneCandidates(ctx, 1, 1)
	if err != nil || first.Recovered != 1 || first.Deleted != 0 || !first.Truncated {
		t.Fatalf("first=%+v err=%v", first, err)
	}
	next, err := s.pruneCandidates(ctx, 2, 20)
	if err != nil || next.Recovered != 0 || next.Deleted != 4 || next.Truncated {
		t.Fatalf("next=%+v err=%v", next, err)
	}
	var remaining string
	if err := db.QueryRow(`SELECT group_concat(id||':'||status,',' ORDER BY id) FROM notification_candidates`).Scan(&remaining); err != nil || remaining != "1:failed,6:pending,7:suppressed,8:unknown,9:sending,10:delivered" {
		t.Fatalf("remaining=%s err=%v", remaining, err)
	}
}
