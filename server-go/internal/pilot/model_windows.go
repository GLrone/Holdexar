//go:build windows

// model_windows.go 模型上下文窗口自动识别（规则表对齐 model_windows.py，
// 顺序敏感：具体在前宽泛在后；识别不出回落宽裕口径并标 fallback）。
package pilot

import (
	"regexp"
)

type windowRule struct {
	re     *regexp.Regexp
	window int
}

var windowRules = compileWindows([][]any{
	{`deepseek-(v[0-9]|r[0-9]|chat|coder|reasoner)`, 131072},
	{`glm-4\.6`, 200000},
	{`glm-[45]`, 131072},
	{`moonshot-v1-8k`, 8192},
	{`moonshot-v1-32k`, 32768},
	{`moonshot-v1-128k`, 131072},
	{`kimi-k[0-9]`, 131072},
	{`qwen-long`, 1000000},
	{`qwen[0-9.]*-(max|plus|turbo|flash|coder)`, 131072},
	{`qwen2\.5`, 32768},
	{`qwen3[:\-]\d`, 32768},
	{`qwen[0-9]`, 131072},
	{`minimax-text-01`, 1000000},
	{`minimax`, 131072},
	{`claude-(opus|sonnet|haiku)-[34]|claude-3`, 200000},
	{`claude`, 200000},
	{`gpt-5`, 400000},
	{`gpt-4\.1`, 1047576},
	{`gpt-4\.5`, 131072},
	{`gpt-4o|gpt-4-turbo|\bo[13]\b`, 128000},
	{`gpt-4`, 8192},
	{`gemini-1\.5-pro`, 2097152},
	{`gemini-1\.5-flash`, 1048576},
	{`gemini-2`, 1048576},
	{`doubao-pro-32k|doubao-lite-32k`, 32768},
	{`doubao-(pro|lite)-128k`, 131072},
	{`doubao`, 32768},
	{`llama-?3\.[12]`, 131072},
	{`llama-?3\b`, 8192},
	{`command-r`, 128000},
	{`yi-large`, 32768},
})

// FallbackContextWindow 表外模型兜底窗口（宽裕口径，source=fallback）。
const FallbackContextWindow = 200000

func compileWindows(pairs [][]any) []windowRule {
	out := make([]windowRule, 0, len(pairs))
	for _, pair := range pairs {
		out = append(out, windowRule{re: regexp.MustCompile("(?i)" + pair[0].(string)), window: pair[1].(int)})
	}
	return out
}

// GuessContextWindow 模型名 → 窗口；表里没有返回 0（不猜）。
func GuessContextWindow(model string) int {
	name := trimSpace(model)
	if name == "" {
		return 0
	}
	for _, rule := range windowRules {
		if rule.re.MatchString(name) {
			return rule.window
		}
	}
	return 0
}

func trimSpace(s string) string {
	start, end := 0, len(s)
	for start < end && isSpaceByte(s[start]) {
		start++
	}
	for end > start && isSpaceByte(s[end-1]) {
		end--
	}
	return s[start:end]
}

func isSpaceByte(b byte) bool {
	return b == ' ' || b == '\t' || b == '\n' || b == '\r' || b == '\v' || b == '\f'
}
