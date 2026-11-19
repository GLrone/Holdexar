package clashcfg

import (
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"

	"gopkg.in/yaml.v3"
)

const (
	quotedOrBlockStyle     = yaml.DoubleQuotedStyle | yaml.SingleQuotedStyle | yaml.LiteralStyle | yaml.FoldedStyle
	yamlMaxDepth           = 128
	yamlMinExpansionBudget = 100_000
)

type yamlWalkBudget struct {
	active   map[*yaml.Node]bool
	steps    int
	maxSteps int
}

func newYAMLWalkBudget(inputLen int) *yamlWalkBudget {
	maxSteps := yamlMinExpansionBudget
	if scaled := inputLen * 2; scaled > maxSteps {
		maxSteps = scaled
	}
	return &yamlWalkBudget{
		active:   map[*yaml.Node]bool{},
		maxSteps: maxSteps,
	}
}

func (b *yamlWalkBudget) enter(depth int) error {
	if depth > yamlMaxDepth {
		return fmt.Errorf("YAML 嵌套层级超过上限（%d）", yamlMaxDepth)
	}
	b.steps++
	if b.steps > b.maxSteps {
		return fmt.Errorf("YAML 展开节点数超过安全上限（%d）", b.maxSteps)
	}
	return nil
}

func LoadYAML(text string) (map[string]any, error) {
	doc, err := loadYAMLDoc(text)
	if err != nil {
		return nil, err
	}
	m, ok := doc.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("配置不是 YAML 映射")
	}
	return m, nil
}

func loadYAMLDoc(text string) (any, error) {
	dec := yaml.NewDecoder(strings.NewReader(text))
	var root yaml.Node
	if err := dec.Decode(&root); err != nil {
		if errors.Is(err, io.EOF) {
			return nil, nil
		}
		return nil, err
	}
	var extra yaml.Node
	if err := dec.Decode(&extra); !errors.Is(err, io.EOF) {
		if err == nil {
			return nil, fmt.Errorf("不支持多文档 YAML")
		}
		return nil, err
	}
	return walkYAMLNode(&root, 0, newYAMLWalkBudget(len(text)))
}

func walkYAMLNode(n *yaml.Node, depth int, budget *yamlWalkBudget) (any, error) {
	if n == nil {
		return nil, nil
	}
	if err := budget.enter(depth); err != nil {
		return nil, err
	}
	if budget.active[n] {
		return nil, fmt.Errorf("YAML 存在循环引用")
	}
	switch n.Kind {
	case yaml.DocumentNode:
		if len(n.Content) == 0 {
			return nil, nil
		}
		budget.active[n] = true
		defer delete(budget.active, n)
		return walkYAMLNode(n.Content[0], depth, budget)
	case yaml.AliasNode:
		if n.Alias == nil {
			return nil, fmt.Errorf("YAML 别名未定义：%s", n.Value)
		}
		if budget.active[n.Alias] {
			return nil, fmt.Errorf("YAML 存在循环引用")
		}
		budget.active[n] = true
		defer delete(budget.active, n)
		v, err := walkYAMLNode(n.Alias, depth+1, budget)
		if err != nil {
			return nil, err
		}
		return copyYAMLValueWithLimit(v, depth+1, budget)
	case yaml.SequenceNode:
		budget.active[n] = true
		defer delete(budget.active, n)
		out := make([]any, len(n.Content))
		for i, item := range n.Content {
			v, err := walkYAMLNode(item, depth+1, budget)
			if err != nil {
				return nil, err
			}
			out[i] = v
		}
		return out, nil
	case yaml.MappingNode:
		budget.active[n] = true
		defer delete(budget.active, n)
		if len(n.Content)%2 != 0 {
			return nil, fmt.Errorf("YAML 映射键值不配对")
		}
		out := make(map[string]any, len(n.Content)/2)
		var merged []map[string]any
		for i := 0; i < len(n.Content); i += 2 {
			kNode, vNode := n.Content[i], n.Content[i+1]
			if isMergeKeyNode(kNode) {
				mv, err := walkYAMLNode(vNode, depth+1, budget)
				if err != nil {
					return nil, err
				}
				switch m := mv.(type) {
				case map[string]any:
					merged = append(merged, m)
				case []any:
					for _, elem := range m {
						em, ok := elem.(map[string]any)
						if !ok {
							return nil, fmt.Errorf("YAML merge 序列项必须是映射")
						}
						merged = append(merged, em)
					}
				default:
					return nil, fmt.Errorf("YAML merge 目标必须是映射或映射序列")
				}
				continue
			}
			keyStr, err := resolveMapKey(kNode, depth+1, budget)
			if err != nil {
				return nil, err
			}
			val, err := walkYAMLNode(vNode, depth+1, budget)
			if err != nil {
				return nil, err
			}
			out[keyStr] = val
		}
		for _, m := range merged {
			for mk, mv := range m {
				if _, exists := out[mk]; !exists {
					copied, err := copyYAMLValueWithLimit(mv, depth+1, budget)
					if err != nil {
						return nil, err
					}
					out[mk] = copied
				}
			}
		}
		return out, nil
	case yaml.ScalarNode:
		return resolveScalarNode(n), nil
	default:
		return nil, fmt.Errorf("不支持的 YAML 节点类型：%v", n.Kind)
	}
}

func isMergeKeyNode(n *yaml.Node) bool {
	if n == nil || n.Kind != yaml.ScalarNode {
		return false
	}
	if n.Style&quotedOrBlockStyle != 0 {
		return false
	}
	if n.Style&yaml.TaggedStyle != 0 {
		return n.Tag == "!!merge"
	}
	return n.Value == "<<"
}

func resolveScalarNode(n *yaml.Node) any {
	if n.Style&quotedOrBlockStyle != 0 {
		if n.Style&yaml.TaggedStyle != 0 {
			switch n.Tag {
			case "!!null":
				return nil
			case "!!bool", "!!int", "!!float":
				return resolveScalarText(n.Value)
			}
		}
		return n.Value
	}
	if n.Style&yaml.TaggedStyle != 0 {
		switch n.Tag {
		case "!!str":
			return n.Value
		case "!!null":
			return nil
		}
	}
	return resolveScalarText(n.Value)
}

func resolveMapKey(n *yaml.Node, depth int, budget *yamlWalkBudget) (string, error) {
	seen := map[*yaml.Node]bool{}
	for {
		if err := budget.enter(depth); err != nil {
			return "", err
		}
		if n == nil || n.Kind != yaml.AliasNode {
			break
		}
		if n.Alias == nil {
			return "", fmt.Errorf("YAML 别名未定义：%s", n.Value)
		}
		if budget.active[n.Alias] || seen[n] {
			return "", fmt.Errorf("YAML 存在循环引用")
		}
		seen[n] = true
		depth++
		n = n.Alias
	}
	if n == nil || n.Kind != yaml.ScalarNode {
		return "", fmt.Errorf("不支持非标量映射键")
	}
	switch v := resolveScalarNode(n).(type) {
	case nil:
		return "null", nil
	case string:
		return v, nil
	case bool:
		return strconv.FormatBool(v), nil
	case int64:
		return strconv.FormatInt(v, 10), nil
	case float64:
		return formatYAMLFloat(v), nil
	default:
		return fmt.Sprint(v), nil
	}
}
