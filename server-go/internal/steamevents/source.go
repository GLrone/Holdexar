package steamevents

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"

	"holdexar/server-go/internal/netenv"
)

const sourceHost = "partner.steamgames.com"
const maxSourceBytes = 8 << 20

type documentClient struct{ *http.Client }

func sourceClient() documentClient {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = func(r *http.Request) (*url.URL, error) {
		address, err := netenv.ResolveSystemProxy(r.Context())
		if err != nil || address == "" {
			return nil, err
		}
		return url.Parse(address)
	}
	return documentClient{&http.Client{
		Transport: transport,
		Timeout:   15 * time.Second,
		CheckRedirect: func(r *http.Request, via []*http.Request) error {
			if len(via) >= 10 || r.URL.Scheme != "https" || r.URL.Host != sourceHost || r.URL.User != nil {
				return http.ErrUseLastResponse
			}
			return nil
		},
	}}
}

func (c documentClient) Fetch(ctx context.Context, language string) (string, error) {
	locale := "english"
	if language == "zh" {
		locale = "schinese"
	}
	address := "https://" + sourceHost + "/doc/marketing/upcoming_events?l=" + locale
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, address, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
	resp, err := c.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return "", ctx.Err()
		}
		return "", errors.New("Steam 活动文档网络请求失败")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("Steam 活动文档返回 HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxSourceBytes+1))
	if err != nil {
		return "", err
	}
	if len(body) > maxSourceBytes {
		return "", errors.New("Steam 活动文档响应过大")
	}
	return string(body), nil
}
