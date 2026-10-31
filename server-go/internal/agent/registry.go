package agent

import "fmt"

// ToolRegistry 工具登记表：重名即错（与 Python 注册表同语义），登记序即投影序。
type ToolRegistry struct {
	specs  []*ToolSpec
	byName map[string]*ToolSpec
}

func newRegistry() *ToolRegistry {
	return &ToolRegistry{byName: map[string]*ToolSpec{}}
}

// register 登记一个工具；重名返回错误（登记序保持声明序）。
func (r *ToolRegistry) register(spec *ToolSpec) error {
	if _, dup := r.byName[spec.Name]; dup {
		return fmt.Errorf("重复注册工具: %s", spec.Name)
	}
	r.specs = append(r.specs, spec)
	r.byName[spec.Name] = spec
	return nil
}

// get 按名取工具；未登记回 nil。
func (r *ToolRegistry) get(name string) *ToolSpec {
	return r.byName[name]
}

// all 全部登记工具（声明序）。
func (r *ToolRegistry) all() []*ToolSpec {
	return r.specs
}
