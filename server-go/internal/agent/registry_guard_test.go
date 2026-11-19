package agent

import "testing"

// 注册表防回归：真实 specs+Register*Tools 装配路径下，工具总数固定且全部入参 schema 可解析。
func TestBuildRegistryToolCountAndSchemaHealth(t *testing.T) {
	reg, err := buildRegistry()
	if err != nil {
		t.Fatal(err)
	}
	if failures := reg.schemaFailures(); len(failures) > 0 {
		for name, e := range failures {
			t.Errorf("工具 %s schema 解析失败: %v", name, e)
		}
		t.Fatal("schemaFailures 应为空")
	}
	const wantTools = 80
	if got := len(reg.all()); got != wantTools {
		t.Fatalf("注册工具总数=%d want %d", got, wantTools)
	}
}
