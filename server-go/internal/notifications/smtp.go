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

const smtpPasswordKey = "smtp.password"

var dataDirResolver = config.ResolveDataDir

type smtpSummaryView struct {
	Configured  bool   `json:"configured"`
	Host        any    `json:"host"`
	Port        int64  `json:"port"`
	UserMasked  string `json:"userMasked"`
	HasPassword bool   `json:"hasPassword"`
	UseSSL      bool   `json:"useSsl"`
}

func (h *handler) smtpView(r *http.Request) (*smtpSummaryView, error) {
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

	return &smtpSummaryView{
		Configured:  truthy(toAddr) && truthy(host),
		Host:        host,
		Port:        pyInt(portRaw, 465),
		UserMasked:  maskAccount(user),
		HasPassword: pwdConfigured,
		UseSSL:      truthy(useSsl),
	}, nil
}

// passwordConfigured 存量明文算已配置；密文解密失败一律按未配置降级。
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
