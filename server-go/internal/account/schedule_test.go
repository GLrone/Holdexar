// schedule_test.go 自动轮换跳过语义：冻结只在会话已死时停摆，会话有效必须
// 放行按退避重试（否则续期成功的账号被永久冻结死锁）。
package account

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"testing"
	"time"
)

func TestRotationSkipFrozenOnlyWhileSessionDead(t *testing.T) {
	const deadSid = "76561198000000003"
	const liveSid = "76561198000000004"
	s, db := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{"persona_name": "fixture", "avatar_url": "", "online": false})
	})
	accessJWT := func(sid string, expSeconds int64) string {
		payload, _ := json.Marshal(map[string]any{"exp": expSeconds, "sub": sid})
		return "aaa." + base64.RawURLEncoding.EncodeToString(payload) + ".bbb"
	}
	insert := func(t *testing.T, sid, cookies string) {
		t.Helper()
		_, e := db.Exec(`INSERT INTO steam_accounts(steam_id,cookies,persona_name,avatar_url,is_active,is_online,in_game,bound_at,wallet_error,wallet_checked_at,wallet_backoff_level,wallet_fail_streak,wallet_frozen,wallet_json) VALUES(?,?,'','',1,0,'',?,'会话过期，正在续期',?,4,10,1,'{"check_ok":false}')`,
			sid, cookies, accountStamp(s.now()), accountStamp(s.now().Add(-2*time.Hour)))
		if e != nil {
			t.Fatal(e)
		}
	}
	// 会话已死 + 冻结 → 维持冻结跳过（死会话不值得发请求）
	dead := "sessionid=x; steamLoginSecure=" + deadSid + "||" + accessJWT(deadSid, time.Now().Add(-time.Hour).Unix()) + "; steamRefresh_steam=" + deadSid + "||refresh"
	insert(t, deadSid, dead)
	row, ok, e := s.row(context.Background(), deadSid)
	if e != nil || !ok {
		t.Fatalf("row: %v %v", ok, e)
	}
	if skip := s.rotationSkip(row, 30); skip == nil || skip["status"] != "frozen" {
		t.Fatalf("死会话冻结应跳过：%v", skip)
	}
	// 会话有效 + 冻结（续期已成功的历史冻结）→ 必须放行按退避重试
	live := "sessionid=x; steamLoginSecure=" + liveSid + "||" + accessJWT(liveSid, time.Now().Add(24*time.Hour).Unix()) + "; steamRefresh_steam=" + liveSid + "||refresh"
	insert(t, liveSid, live)
	row, ok, e = s.row(context.Background(), liveSid)
	if e != nil || !ok {
		t.Fatalf("row: %v %v", ok, e)
	}
	if skip := s.rotationSkip(row, 30); skip != nil {
		t.Fatalf("活会话冻结应放行重试：%v", skip)
	}
}
