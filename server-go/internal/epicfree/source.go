package epicfree

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"

	"holdexar/server-go/internal/netenv"
)

type Offer struct {
	Title         string `json:"title"`
	TitleCn       string `json:"titleCn"`
	AppID         *int64 `json:"appid"`
	Start         string `json:"start"`
	End           string `json:"end"`
	Upcoming      bool   `json:"upcoming"`
	Image         string `json:"image"`
	URL           string `json:"url"`
	PriceOriginal string `json:"priceOriginal"`
	Namespace     string `json:"-"`
	ElementID     string `json:"-"`
	OfferType     string `json:"-"`
}

type MobileOffer struct {
	Title  *string `json:"title"`
	Image  string  `json:"image"`
	URL    string  `json:"url"`
	End    *string `json:"end"`
	Worth  *string `json:"worth"`
	Source string  `json:"source"`
	Direct bool    `json:"direct"`
}

type publicStatusError int

func (e publicStatusError) Error() string { return fmt.Sprintf("公开促销来源返回 HTTP %d", e) }

func publicSource(u *url.URL) bool {
	if u.Scheme != "https" || u.User != nil || u.Port() != "" {
		return false
	}
	switch u.Host {
	case "store-site-backend-static-ipv4.ak.epicgames.com", "store-content-ipv4.ak.epicgames.com",
		"egs-platform-service.store.epicgames.com", "store.epicgames.com", "www.gamerpower.com", "store.steampowered.com":
		return true
	default:
		return false
	}
}

func newPublicClient() *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = func(r *http.Request) (*url.URL, error) {
		proxy, err := netenv.ResolveSystemProxy(r.Context())
		if err != nil || proxy == "" {
			return nil, err
		}
		return url.Parse(proxy)
	}
	return &http.Client{
		Timeout: 12 * time.Second, Transport: transport,
		CheckRedirect: func(r *http.Request, via []*http.Request) error {
			if len(via) >= 6 || !publicSource(r.URL) || r.URL.Host != via[0].URL.Host {
				return http.ErrUseLastResponse
			}
			return nil
		},
	}
}

func readPublicJSON(ctx context.Context, client *http.Client, address string, headers map[string]string, dest any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, address, nil)
	if err != nil || !publicSource(req.URL) {
		return errors.New("公开促销来源地址无效")
	}
	for key, value := range headers {
		req.Header.Set(key, value)
	}
	resp, err := client.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return errors.New("公开促销来源连接失败")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return publicStatusError(resp.StatusCode)
	}
	const limit = 16 << 20
	raw, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return err
	}
	if len(raw) > limit || json.Unmarshal(raw, dest) != nil {
		return errors.New("公开促销来源的数据格式无效")
	}
	return nil
}
