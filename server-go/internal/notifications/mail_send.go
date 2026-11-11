package notifications

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"mime"
	"mime/multipart"
	"net"
	"net/mail"
	"net/smtp"
	"net/textproto"
	"os"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/config"
)

type SMTPConfig struct {
	Host     string `json:"host"`
	Port     int    `json:"port"`
	User     string `json:"user"`
	Password string `json:"password"`
	ToAddr   string `json:"toAddr"`
	UseSSL   bool   `json:"useSsl"`
}

func SendMail(ctx context.Context, cfg SMTPConfig, subject, body string) (bool, string) {
	if cfg.Host == "" || cfg.User == "" || cfg.Password == "" || cfg.ToAddr == "" {
		return false, "SMTP 未配置"
	}
	var trust *tls.Config
	if file := os.Getenv("SSL_CERT_FILE"); file != "" {
		raw, e := os.ReadFile(file)
		if e != nil {
			return false, "TLSCertificateError: " + e.Error()
		}
		pool, e := x509.SystemCertPool()
		if e != nil {
			pool = x509.NewCertPool()
		}
		if !pool.AppendCertsFromPEM(raw) {
			return false, "TLSCertificateError: certificate file contains no certificates"
		}
		trust = &tls.Config{RootCAs: pool}
	}
	return sendSMTP(ctx, cfg, subject, body, trust)
}
func mailMessage(cfg SMTPConfig, subject, body string) ([]byte, error) {
	from, e := mail.ParseAddress(cfg.User)
	if e != nil {
		return nil, e
	}
	to, e := mail.ParseAddress(cfg.ToAddr)
	if e != nil {
		return nil, e
	}
	var alternative bytes.Buffer
	parts := multipart.NewWriter(&alternative)
	for _, part := range []struct{ kind, text string }{{"plain", MailText(body)}, {"html", body}} {
		header := textproto.MIMEHeader{}
		header.Set("Content-Type", "text/"+part.kind+"; charset=utf-8")
		header.Set("Content-Transfer-Encoding", "base64")
		writer, e := parts.CreatePart(header)
		if e != nil {
			return nil, e
		}
		encoded := base64.StdEncoding.EncodeToString([]byte(part.text))
		for len(encoded) > 76 {
			if _, e = io.WriteString(writer, encoded[:76]+"\r\n"); e != nil {
				return nil, e
			}
			encoded = encoded[76:]
		}
		if _, e = io.WriteString(writer, encoded+"\r\n"); e != nil {
			return nil, e
		}
	}
	if e = parts.Close(); e != nil {
		return nil, e
	}
	from.Name = config.AppName()
	var message bytes.Buffer
	fmt.Fprintf(&message, "MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary=%q\r\nSubject: %s\r\nFrom: %s\r\nTo: %s\r\n\r\n", parts.Boundary(), mime.QEncoding.Encode("utf-8", subject), from.String(), to.String())
	message.Write(alternative.Bytes())
	return message.Bytes(), nil
}
func sendSMTP(ctx context.Context, cfg SMTPConfig, subject, body string, trust *tls.Config) (accepted bool, problem string) {
	if cfg.Host == "" || cfg.User == "" || cfg.Password == "" || cfg.ToAddr == "" {
		return false, "SMTP 未配置"
	}
	message, e := mailMessage(cfg, subject, body)
	if e != nil {
		return false, "SMTPAddressError: " + e.Error()
	}
	if strings.ContainsAny(subject, "\r\n") || strings.ContainsAny(cfg.User+cfg.ToAddr, "\r\n") {
		return false, "SMTPAddressError: invalid mail header"
	}
	dataStarted := false
	phase := "SMTPConnectError"
	defer func() {
		if problem != "" {
			problem = strings.ReplaceAll(problem, cfg.Password, "***")
		}
	}()
	fail := func(e error) (bool, string) {
		var response *textproto.Error
		if errors.As(e, &response) && response.Code >= 400 && response.Code < 600 {
			return false, fmt.Sprintf("SMTPRejected: %d %s: %s", response.Code, phase, response.Msg)
		}
		if dataStarted {
			return false, "SMTPDeliveryUnknown: " + e.Error()
		}
		var n net.Error
		if errors.As(e, &n) && n.Timeout() {
			return false, "TimeoutError: " + e.Error()
		}
		if errors.Is(e, io.EOF) || errors.Is(e, io.ErrUnexpectedEOF) {
			return false, "SMTPServerDisconnected: " + e.Error()
		}
		return false, phase + ": " + e.Error()
	}
	conn, e := (&net.Dialer{Timeout: 20 * time.Second}).DialContext(ctx, "tcp", net.JoinHostPort(cfg.Host, strconv.Itoa(cfg.Port)))
	if e != nil {
		return fail(e)
	}
	defer conn.Close()
	rawConn := conn
	stop := context.AfterFunc(ctx, func() { _ = rawConn.Close() })
	defer stop()
	deadline := time.Now().Add(20 * time.Second)
	if d, ok := ctx.Deadline(); ok && d.Before(deadline) {
		deadline = d
	}
	_ = conn.SetDeadline(deadline)
	tlsConfig := &tls.Config{ServerName: cfg.Host, MinVersion: tls.VersionTLS12}
	if trust != nil {
		tlsConfig = trust.Clone()
		tlsConfig.ServerName = cfg.Host
	}
	phase = "TLSCertificateError"
	if cfg.UseSSL {
		secure := tls.Client(conn, tlsConfig)
		if e = secure.HandshakeContext(ctx); e != nil {
			return fail(e)
		}
		conn = secure
	}
	phase = "SMTPConnectError"
	client, e := smtp.NewClient(conn, cfg.Host)
	if e != nil {
		return fail(e)
	}
	defer func() { _ = client.Quit(); _ = client.Close() }()
	phase = "SMTPHeloError"
	if e = client.Hello("localhost"); e != nil {
		return fail(e)
	}
	if !cfg.UseSSL {
		if e = client.StartTLS(tlsConfig); e != nil {
			return fail(e)
		}
	}
	phase = "SMTPAuthenticationError"
	if e = authenticate(client, cfg); e != nil {
		var response *textproto.Error
		if errors.As(e, &response) {
			return false, fmt.Sprintf("SMTPAuthenticationError: 邮箱拒绝登录（%d）：请核对授权码是否为当前有效值，并确认邮箱设置里已开启 IMAP/SMTP 服务；若刚连续失败过，几分钟后再试。（服务器回复：%s）", response.Code, response.Msg)
		}
		phase = "SMTPException"
		return fail(e)
	}
	from, _ := mail.ParseAddress(cfg.User)
	to, _ := mail.ParseAddress(cfg.ToAddr)
	phase = "SMTPSenderRefused"
	if e = client.Mail(from.Address); e != nil {
		return fail(e)
	}
	phase = "SMTPRecipientsRefused"
	if e = client.Rcpt(to.Address); e != nil {
		return fail(e)
	}
	dataStarted = true
	phase = "SMTPDataError"
	writer, e := client.Data()
	if e != nil {
		return fail(e)
	}
	if _, e = writer.Write(message); e != nil {
		return fail(e)
	}
	if e = writer.Close(); e != nil {
		return fail(e)
	}
	return true, ""
}

type loginAuth struct {
	user, password string
	challenge      int
}

func (a *loginAuth) Start(*smtp.ServerInfo) (string, []byte, error) { return "LOGIN", nil, nil }
func (a *loginAuth) Next(_ []byte, more bool) ([]byte, error) {
	if !more {
		return nil, nil
	}
	a.challenge++
	if a.challenge == 1 {
		return []byte(a.user), nil
	}
	if a.challenge == 2 {
		return []byte(a.password), nil
	}
	return nil, fmt.Errorf("too many authentication challenges")
}
func authenticate(client *smtp.Client, cfg SMTPConfig) error {
	available, mechanisms := client.Extension("AUTH")
	if !available {
		return fmt.Errorf("SMTP AUTH extension not supported by server.")
	}
	advertised := strings.Fields(mechanisms)
	has := func(wanted string) bool {
		for _, v := range advertised {
			if v == wanted {
				return true
			}
		}
		return false
	}
	var auth smtp.Auth
	switch {
	case has("CRAM-MD5"):
		auth = smtp.CRAMMD5Auth(cfg.User, cfg.Password)
	case has("PLAIN"):
		auth = smtp.PlainAuth("", cfg.User, cfg.Password, cfg.Host)
	case has("LOGIN"):
		auth = &loginAuth{user: cfg.User, password: cfg.Password}
	default:
		return fmt.Errorf("No suitable authentication method found.")
	}
	mechanism, response, e := auth.Start(&smtp.ServerInfo{Name: cfg.Host, TLS: true, Auth: advertised})
	if e != nil {
		return e
	}
	command := "AUTH " + mechanism
	if response != nil {
		command += " " + base64.StdEncoding.EncodeToString(response)
	}
	for rounds := 0; rounds < 6; rounds++ {
		id, e := client.Text.Cmd("%s", command)
		if e != nil {
			return e
		}
		client.Text.StartResponse(id)
		code, message, e := client.Text.ReadResponse(0)
		client.Text.EndResponse(id)
		if e != nil {
			return e
		}
		if code == 235 || code == 503 {
			_, e = auth.Next([]byte(message), false)
			return e
		}
		if code != 334 {
			return &textproto.Error{Code: code, Msg: message}
		}
		challenge, e := base64.StdEncoding.DecodeString(message)
		if e != nil {
			return e
		}
		response, e = auth.Next(challenge, true)
		if e != nil {
			return e
		}
		command = base64.StdEncoding.EncodeToString(response)
	}
	return fmt.Errorf("too many authentication challenges")
}
