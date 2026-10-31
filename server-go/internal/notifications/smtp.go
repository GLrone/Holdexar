package notifications

import (
	"context"
	"encoding/json"
	"math"
	"net/http"
	"strconv"
	"strings"

	"holdexar/server-go/internal/config"
	"holdexar/server-go/internal/secretbox"
)

// smtpPasswordKey SMTP 授权码的 KV 键（加密存储）。
const smtpPasswordKey = "smtp.password"

// dataDirResolver secretbox 密钥材料落点，读时解析（对齐 Python
// get_settings().data_dir 的取值时机）；测试可替换。
var dataDirResolver = config.ResolveDataDir

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
	pwdConfigured, err := h.passwordConfigured(ctx)
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
	v.set("hasPassword", pwdConfigured)
	v.set("useSsl", truthy(useSsl))
	return v, nil
}

// passwordConfigured SMTP 授权码是否已配置，判定对齐 Python get_secret_value：
// 键缺席/空值/NULL 回无凭据；存量明文（无 enc1: 前缀）原样算已配置；密文
// 解密失败（篡改、用途不符、keyring 锁定态、换钥窗口外）一律按无凭据降级。
func (h *handler) passwordConfigured(ctx context.Context) (bool, error) {
	stored, present, err := h.settingValue(ctx, smtpPasswordKey)
	if err != nil || !present {
		return false, err
	}
	if stored == nil {
		return false, nil
	}
	s, isStr := stored.(string)
	if !isStr {
		// 非字符串值：Python str() 后必非密文前缀 → 原样返回，按真值判定
		return truthy(stored), nil
	}
	if s == "" {
		return false, nil
	}
	if !secretbox.IsEncrypted(s) {
		return true, nil
	}
	dataDir, err := dataDirResolver()
	if err != nil {
		return false, err
	}
	plain, err := secretbox.DecryptSecretIn(dataDir, s, secretbox.SecretPurpose(smtpPasswordKey))
	if err != nil {
		return false, nil
	}
	return plain != "", nil
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
