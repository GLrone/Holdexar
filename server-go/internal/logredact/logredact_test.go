package logredact

import "testing"

func TestRedactSixRules(t *testing.T) {
	cases := []struct{ name, in, want string }{
		{"proxy-link", "import vless://a1b2c3d4-uuid@host:443?x=1 失败", "import vless://*** 失败"},
		{"proxy-link-ss", "订阅 ss://YWVzLTI1Ni1nY206cGFzcw==@1.2.3.4:8388 不可用", "订阅 ss://*** 不可用"},
		{"url-userinfo", "连不上 http://user:secret@example.com:8080/api", "连不上 http://***:***@example.com:8080/api"},
		{"query-secret", "GET https://api.steampowered.com/IStoreService/GetAppList/v1/?key=ABC123DEF456&x=1", "GET https://api.steampowered.com/IStoreService/GetAppList/v1/?key=***&x=1"},
		{"query-sessionid", "sessionid=abc123 已过期", "sessionid=*** 已过期"},
		{"json-secret", `配置读取失败 {"smtp.password": "p@ss w0rd"}`, `配置读取失败 {"smtp.password": "***"}`},
		{"json-cookie", `{"account.steam_cookies": "steamLoginSecure=1|2"}`, `{"account.steam_cookies": "***"}`},
		{"auth-header", "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.xxx 拒绝", "Authorization: Bearer *** 拒绝"},
		{"env-assign", "启动参数 STEAM_API_KEY=DEADBEEF123 生效", "启动参数 STEAM_API_KEY=*** 生效"},
		{"env-cookie", "HOLDEXAR_SMTP_PASSWORD=qwerty123 已读取", "HOLDEXAR_SMTP_PASSWORD=*** 已读取"},
		{"plain-key", `{"key": "games"}`, `{"key": "games"}`},
		{"plain-assign", "max_length=100 保持", "max_length=100 保持"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := Redact(c.in); got != c.want {
				t.Fatalf("Redact(%q) = %q, want %q", c.in, got, c.want)
			}
		})
	}
}

func TestRedactIdempotent(t *testing.T) {
	lines := []string{
		"import vless://a1b2c3d4-uuid@host:443?x=1 失败",
		"连不上 http://user:secret@example.com:8080/api",
		"GET https://api.steampowered.com/x/?key=ABC123DEF456&sid=abc123",
		`{"smtp.password": "p@ss"}`,
		"Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.xxx",
		"STEAM_API_KEY=DEADBEEF123",
		"https://example.com/api/v1/pq/Abcdef123456Ghij",
	}
	for _, line := range lines {
		once := Redact(line)
		if twice := Redact(once); twice != once {
			t.Fatalf("不幂等: %q → %q → %q", line, once, twice)
		}
	}
}

func TestMaskTokenURLs(t *testing.T) {
	cases := []struct{ name, in, want string }{
		{"token-path", "订阅校验 https://data.steamhl.com/api/v1/pq/Abcdef123456Ghij 完成", "订阅校验 https://data.steamhl.com/*** 完成"},
		{"word-path", "镜像 https://ghfast.top/https://github.com/x/y/releases/download/v1/app.zip 就绪", "镜像 https://ghfast.top/https://github.com/x/y/releases/download/v1/app.zip 就绪"},
		{"numeric-path", "玩家 https://store.steampowered.com/api/appdetails?appid=76561198000000000 正常", "玩家 https://store.steampowered.com/api/appdetails?appid=76561198000000000 正常"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := Redact(c.in); got != c.want {
				t.Fatalf("Redact(%q) = %q, want %q", c.in, got, c.want)
			}
		})
	}
}

func TestRedactURL(t *testing.T) {
	cases := []struct{ in, want string }{
		{"", ""},
		{"  https://example.com/sub/Abcdef123456Ghij?x=1  ", "https://example.com/***"},
		{"http://[::1", "***"},
		{"not a url", "***"},
		{"https://user:pass@host.example.com:8443/clash-sub-9", "https://user:pass@host.example.com:8443/***"},
	}
	for _, c := range cases {
		if got := RedactURL(c.in); got != c.want {
			t.Fatalf("RedactURL(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}
