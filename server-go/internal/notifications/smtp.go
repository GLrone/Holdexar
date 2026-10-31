package notifications

import (
	"encoding/json"
	"math"
	"net/http"
	"strconv"
	"strings"
)

// smtpView SMTP 出口的可公开投影：不含密码与授权码，只给「是否已配置」。
// 行缺席与行内 NULL 语义分开（Python get_value 缺席回默认、NULL 回 None）。
func (h *handler) smtpView(r *http.Request) (*orderedObj, error) {
	ctx := r.Context()
	host, hasHost, err := h.settingValue(ctx, "smtp.host")
	if err != nil {
		return nil, err
	}
	if !hasHost {
		host = ""
	}
	portRaw, _, err := h.settingValue(ctx, "smtp.port")
	if err != nil {
		return nil, err
	}
	user, _, err := h.settingValue(ctx, "smtp.user")
	if err != nil {
		return nil, err
	}
	pwdRaw, _, err := h.settingValue(ctx, "smtp.password")
	if err != nil {
		return nil, err
	}
	toAddr, _, err := h.settingValue(ctx, "smtp.to_addr")
	if err != nil {
		return nil, err
	}
	useSsl, hasSsl, err := h.settingValue(ctx, "smtp.use_ssl")
	if err != nil {
		return nil, err
	}
	if !hasSsl {
		useSsl = true
	}

	v := newOrderedObj()
	// configured = 收件地址与服务器都非空（Python bool() 语义）
	v.set("configured", truthy(toAddr) && truthy(host))
	v.set("host", host)
	v.set("port", pyInt(portRaw, 465))
	v.set("userMasked", maskAccount(user))
	// 密文解密链（keyring/secretbox）未迁移：非空即视为已配置。
	// 与 Python 的差异仅在锁定态 / 换钥崩溃窗口（Python 回无凭据）。
	v.set("hasPassword", truthy(pwdRaw))
	v.set("useSsl", truthy(useSsl))
	return v, nil
}

// maskAccount 邮箱账号脱敏：保留首尾各 1 位与域名（按 Unicode 字符切，对齐 Python 切片）。
func maskAccount(user any) string {
	s, ok := user.(string)
	if !ok || s == "" {
		return ""
	}
	name, domain, found := strings.Cut(s, "@")
	if !found {
		return ""
	}
	runes := []rune(name)
	if len(runes) <= 2 {
		return string(runes[:1]) + "***@" + domain
	}
	return string(runes[0]) + "***" + string(runes[len(runes)-1]) + "@" + domain
}

// pyInt Python int() 的窄化读取：数值/数字串 → 截断整数；零值类回缺省端口。
func pyInt(v any, def int64) int64 {
	if !truthy(v) {
		return def
	}
	switch x := v.(type) {
	case json.Number:
		if f, err := x.Float64(); err == nil {
			return int64(math.Trunc(f))
		}
	case string:
		if n, err := strconv.ParseInt(strings.TrimSpace(x), 10, 64); err == nil {
			return n
		}
		if f, err := strconv.ParseFloat(strings.TrimSpace(x), 64); err == nil {
			return int64(math.Trunc(f))
		}
	case bool:
		if x {
			return 1
		}
	}
	return def
}
