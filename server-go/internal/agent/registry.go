package agent

import (
	"encoding/json"
	"fmt"

	"github.com/google/jsonschema-go/jsonschema"
)

type ToolRegistry struct {
	specs      []*ToolSpec
	byName     map[string]*ToolSpec
	schemas    map[string]*jsonschema.Resolved
	schemaErrs map[string]error
}

func newRegistry() *ToolRegistry {
	return &ToolRegistry{
		byName:     map[string]*ToolSpec{},
		schemas:    map[string]*jsonschema.Resolved{},
		schemaErrs: map[string]error{},
	}
}

func parseRequiredParams(raw string) []string {
	var schema struct {
		Required []string `json:"required"`
	}
	if err := jsonDecoder([]byte(raw)).Decode(&schema); err != nil {
		return nil
	}
	return schema.Required
}

// resolveToolSchema 解析工具入参 schema 供调用期全量校验；解析失败回退必需键校验。
func resolveToolSchema(raw string) (*jsonschema.Resolved, error) {
	var sch jsonschema.Schema
	if err := json.Unmarshal([]byte(raw), &sch); err != nil {
		return nil, err
	}
	return sch.Resolve(nil)
}

func (r *ToolRegistry) register(spec *ToolSpec) error {
	if _, dup := r.byName[spec.Name]; dup {
		return fmt.Errorf("重复注册工具: %s", spec.Name)
	}
	spec.required = parseRequiredParams(spec.Parameters)
	spec.requiredParsed = true
	if res, err := resolveToolSchema(spec.Parameters); err == nil {
		r.schemas[spec.Name] = res
	} else {
		r.schemaErrs[spec.Name] = err
	}
	r.specs = append(r.specs, spec)
	r.byName[spec.Name] = spec
	return nil
}

func (r *ToolRegistry) get(name string) *ToolSpec {
	return r.byName[name]
}

func (r *ToolRegistry) all() []*ToolSpec {
	return r.specs
}

func (r *ToolRegistry) resolvedSchema(name string) *jsonschema.Resolved {
	return r.schemas[name]
}

// schemaFailures 返回入参 schema 解析失败的工具及原因（未失败为空）。
func (r *ToolRegistry) schemaFailures() map[string]error {
	return r.schemaErrs
}
