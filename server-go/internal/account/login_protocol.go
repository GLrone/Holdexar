package account

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"math/big"
	"mime/multipart"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

const loginUserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
const loginNetworkMessage = "连不上 Steam 登录服务，请检查网络或代理设置后重试"
const loginResponseMessage = "Steam 登录服务响应异常，请稍后重试"

type loginFailure struct {
	message   string
	temporary bool
}

func (e *loginFailure) Error() string { return e.message }
func loginResponseError() error       { return &loginFailure{message: loginResponseMessage} }

type loginPBField struct {
	number, wire int
	value        uint64
	data         []byte
}

func pbBytes(dst []byte, field int, value []byte) []byte {
	dst = binary.AppendUvarint(dst, uint64(field<<3|2))
	dst = binary.AppendUvarint(dst, uint64(len(value)))
	return append(dst, value...)
}
func pbVarint(dst []byte, field int, value uint64) []byte {
	dst = binary.AppendUvarint(dst, uint64(field<<3))
	return binary.AppendUvarint(dst, value)
}
func readLoginPB(data []byte) ([]loginPBField, error) {
	fields := []loginPBField{}
	for len(data) > 0 {
		key, n := binary.Uvarint(data)
		if n <= 0 || key>>3 == 0 || key>>3 > (1<<29)-1 {
			return nil, loginResponseError()
		}
		data = data[n:]
		field := loginPBField{number: int(key >> 3), wire: int(key & 7)}
		switch field.wire {
		case 0:
			field.value, n = binary.Uvarint(data)
			if n <= 0 {
				return nil, loginResponseError()
			}
			data = data[n:]
		case 1, 5:
			n = 8
			if field.wire == 5 {
				n = 4
			}
			if len(data) < n {
				return nil, loginResponseError()
			}
			field.data, data = data[:n], data[n:]
		case 2:
			length, n := binary.Uvarint(data)
			if n <= 0 || length > uint64(len(data)-n) {
				return nil, loginResponseError()
			}
			data = data[n:]
			field.data, data = data[:int(length)], data[int(length):]
		default:
			return nil, loginResponseError()
		}
		fields = append(fields, field)
	}
	return fields, nil
}
func pbData(fields []loginPBField, number, wire int) []byte {
	for _, field := range fields {
		if field.number == number && field.wire == wire {
			return field.data
		}
	}
	return nil
}
func pbNumber(fields []loginPBField, number int) uint64 {
	for _, field := range fields {
		if field.number == number && field.wire == 0 {
			return field.value
		}
	}
	return 0
}
func encryptLoginPassword(password, modulus, exponent string) (string, error) {
	n, ok := new(big.Int).SetString(modulus, 16)
	if !ok || n.Sign() <= 0 {
		return "", loginResponseError()
	}
	e, ok := new(big.Int).SetString(exponent, 16)
	if !ok || !e.IsInt64() || e.Sign() <= 0 || e.Int64() > int64(math.MaxInt) {
		return "", loginResponseError()
	}
	cipher, err := rsa.EncryptPKCS1v15(rand.Reader, &rsa.PublicKey{N: n, E: int(e.Int64())}, []byte(password))
	if err != nil {
		return "", loginResponseError()
	}
	return base64.StdEncoding.EncodeToString(cipher), nil
}

type loginAuth struct {
	client    *http.Client
	clientID  uint64
	requestID []byte
	steamID   string
	interval  time.Duration
	guards    []int
	codeGuard int
	deviceOK  bool
}
type loginReply struct {
	status, result int
	body           []byte
}

func loginResultError(code int) error {
	message := fmt.Sprintf("Steam 拒绝了登录请求：错误码 %d", code)
	switch code {
	case 5:
		message = "账号或密码不正确"
	case 65, 88:
		message = "验证码不正确，请重试"
	case 84:
		message = "尝试次数过多，请稍等几分钟再试"
	}
	return &loginFailure{message: message}
}
func (r loginReply) check() error {
	if r.status >= 400 {
		return &loginFailure{message: fmt.Sprintf("Steam 登录服务不可用（HTTP %d）", r.status)}
	}
	if r.result != 1 {
		return loginResultError(r.result)
	}
	return nil
}
func (c *SteamClient) authEndpoint(method string) string {
	return strings.TrimRight(c.opts.AuthBaseURL, "/") + "/" + method + "/v1"
}
func (c *SteamClient) authRequest(ctx context.Context, client *http.Client, method, endpoint string, fields map[string]string) (loginReply, error) {
	copy := *client
	copy.Timeout = 15 * time.Second
	origin, err := url.Parse(endpoint)
	if err != nil {
		return loginReply{}, loginResponseError()
	}
	copy.CheckRedirect = func(req *http.Request, via []*http.Request) error {
		if len(via) >= 10 || req.URL.Scheme != origin.Scheme || req.URL.Host != origin.Host {
			return loginResponseError()
		}
		return nil
	}
	for attempt := 0; attempt < 3; attempt++ {
		var body bytes.Buffer
		contentType := ""
		if method == http.MethodPost {
			writer := multipart.NewWriter(&body)
			for key, value := range fields {
				if err = writer.WriteField(key, value); err != nil {
					return loginReply{}, loginResponseError()
				}
			}
			if writer.Close() != nil {
				return loginReply{}, loginResponseError()
			}
			contentType = writer.FormDataContentType()
		}
		req, err := http.NewRequestWithContext(ctx, method, endpoint, &body)
		if err != nil {
			return loginReply{}, loginResponseError()
		}
		if method == http.MethodGet {
			query := req.URL.Query()
			for key, value := range fields {
				query.Set(key, value)
			}
			req.URL.RawQuery = query.Encode()
		} else {
			req.Header.Set("Content-Type", contentType)
		}
		req.Header.Set("User-Agent", loginUserAgent)
		req.Header.Set("Origin", "https://store.steampowered.com")
		req.Header.Set("Referer", "https://store.steampowered.com/login/")
		resp, err := copy.Do(req)
		if err == nil {
			data, readErr := io.ReadAll(io.LimitReader(resp.Body, (1<<20)+1))
			resp.Body.Close()
			if len(data) > 1<<20 {
				return loginReply{}, loginResponseError()
			}
			if readErr == nil {
				result := 1
				if value := resp.Header.Get("x-eresult"); value != "" {
					if result, err = strconv.Atoi(value); err != nil {
						return loginReply{}, loginResponseError()
					}
				}
				return loginReply{status: resp.StatusCode, result: result, body: data}, nil
			}
			err = readErr
		}
		if ctx.Err() != nil {
			return loginReply{}, ctx.Err()
		}
		var authority x509.UnknownAuthorityError
		var certificate x509.CertificateInvalidError
		var hostname x509.HostnameError
		if errors.As(err, &authority) || errors.As(err, &certificate) || errors.As(err, &hostname) {
			return loginReply{}, &loginFailure{message: "Steam 证书校验失败，登录已停止"}
		}
		if attempt < 2 {
			if err = c.sleep(ctx, time.Duration(attempt+1)*800*time.Millisecond); err != nil {
				return loginReply{}, err
			}
		}
	}
	return loginReply{}, &loginFailure{message: loginNetworkMessage, temporary: true}
}
func (c *SteamClient) beginLogin(ctx context.Context, account, password string) (*loginAuth, error) {
	client, err := c.client(ctx)
	if err != nil {
		return nil, &loginFailure{message: loginNetworkMessage, temporary: true}
	}
	reply, err := c.authRequest(ctx, client, http.MethodGet, c.authEndpoint("GetPasswordRSAPublicKey"), map[string]string{"origin": "https://store.steampowered.com", "input_protobuf_encoded": base64.StdEncoding.EncodeToString(pbBytes(nil, 1, []byte(account)))})
	if err != nil {
		return nil, err
	}
	if err = reply.check(); err != nil {
		return nil, err
	}
	key, err := readLoginPB(reply.body)
	if err != nil {
		return nil, err
	}
	timestampPresent := false
	for _, field := range key {
		timestampPresent = timestampPresent || field.number == 3 && field.wire == 0
	}
	if !timestampPresent {
		return nil, loginResponseError()
	}
	password64, err := encryptLoginPassword(password, string(pbData(key, 1, 2)), string(pbData(key, 2, 2)))
	if err != nil {
		return nil, err
	}
	device := pbVarint(pbBytes(nil, 1, []byte(loginUserAgent)), 2, 2)
	message := pbBytes(nil, 2, []byte(account))
	message = pbBytes(message, 3, []byte(password64))
	message = pbVarint(message, 4, pbNumber(key, 3))
	message = pbVarint(message, 5, 1)
	message = pbVarint(message, 7, 1)
	message = pbBytes(message, 8, []byte("Store"))
	message = pbBytes(message, 9, device)
	message = pbVarint(message, 11, 6)
	reply, err = c.authRequest(ctx, client, http.MethodPost, c.authEndpoint("BeginAuthSessionViaCredentials"), map[string]string{"input_protobuf_encoded": base64.StdEncoding.EncodeToString(message)})
	if err != nil {
		return nil, err
	}
	if err = reply.check(); err != nil {
		return nil, err
	}
	fields, err := readLoginPB(reply.body)
	if err != nil {
		return nil, err
	}
	auth := &loginAuth{client: client, clientID: pbNumber(fields, 1), requestID: pbData(fields, 2, 2), steamID: strconv.FormatUint(pbNumber(fields, 5), 10)}
	if auth.clientID == 0 || len(auth.requestID) == 0 || auth.steamID == "0" {
		return nil, loginResponseError()
	}
	interval := 5.0
	if value := pbData(fields, 3, 5); len(value) == 4 {
		interval = float64(math.Float32frombits(binary.LittleEndian.Uint32(value)))
		if math.IsNaN(interval) || math.IsInf(interval, 0) {
			interval = 5
		}
	}
	auth.interval = time.Duration(min(max(interval, 1), 15) * float64(time.Second))
	for _, field := range fields {
		if field.number == 4 && field.wire == 2 {
			guard, err := readLoginPB(field.data)
			if err != nil {
				return nil, err
			}
			auth.guards = append(auth.guards, int(pbNumber(guard, 1)))
		}
	}
	for _, wanted := range []int{3, 2} {
		for _, guard := range auth.guards {
			if guard == wanted && auth.codeGuard == 0 {
				auth.codeGuard = wanted
			}
		}
	}
	reply, err = c.authRequest(ctx, client, http.MethodPost, strings.TrimRight(c.opts.CheckDeviceURL, "/")+"/"+auth.steamID, map[string]string{"clientid": strconv.FormatUint(auth.clientID, 10), "steamid": auth.steamID})
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	if err == nil && reply.status == 200 {
		var body map[string]any
		if json.Unmarshal(reply.body, &body) == nil {
			auth.deviceOK = body["success"] == true || body["success"] == float64(1)
		}
	}
	return auth, nil
}
func (c *SteamClient) submitLoginGuard(ctx context.Context, auth *loginAuth, code string) error {
	message := pbVarint(nil, 1, auth.clientID)
	message = binary.AppendUvarint(message, 2<<3|1)
	sid, _ := strconv.ParseUint(auth.steamID, 10, 64)
	message = binary.LittleEndian.AppendUint64(message, sid)
	message = pbBytes(message, 3, []byte(strings.TrimSpace(code)))
	message = pbVarint(message, 4, uint64(auth.codeGuard))
	reply, err := c.authRequest(ctx, auth.client, http.MethodPost, c.authEndpoint("UpdateAuthSessionWithSteamGuardCode"), map[string]string{"input_protobuf_encoded": base64.StdEncoding.EncodeToString(message)})
	if err != nil {
		return err
	}
	if err = reply.check(); err != nil {
		return err
	}
	fields, err := readLoginPB(reply.body)
	if err != nil {
		return err
	}
	if next := pbNumber(fields, 5); next != 0 {
		auth.clientID = next
	}
	if next := pbData(fields, 6, 2); len(next) > 0 {
		auth.requestID = append([]byte(nil), next...)
	}
	return nil
}
func (c *SteamClient) pollLogin(ctx context.Context, auth *loginAuth) (bool, string, error) {
	message := pbBytes(pbVarint(nil, 1, auth.clientID), 2, auth.requestID)
	reply, err := c.authRequest(ctx, auth.client, http.MethodPost, c.authEndpoint("PollAuthSessionStatus"), map[string]string{"input_protobuf_encoded": base64.StdEncoding.EncodeToString(message)})
	if err != nil {
		var failure *loginFailure
		if errors.As(err, &failure) && failure.temporary {
			return false, "", nil
		}
		return false, "", err
	}
	if reply.status >= 500 {
		return false, "", nil
	}
	if reply.result != 1 {
		return true, "", nil
	}
	fields, err := readLoginPB(reply.body)
	return false, string(pbData(fields, 3, 2)), err
}
