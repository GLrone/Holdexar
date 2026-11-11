package notifications

import (
	"bufio"
	"context"
	"crypto/hmac"
	"crypto/md5"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"fmt"
	"net"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

func smtpCapture(t *testing.T, mode string) (SMTPConfig, *tls.Config, *atomic.Int32, <-chan string) {
	t.Helper()
	https := httptest.NewTLSServer(nil)
	certificate := https.TLS.Certificates[0]
	cert := https.Certificate()
	https.Close()
	var listener net.Listener
	var e error
	if mode == "starttls" {
		listener, e = net.Listen("tcp", "127.0.0.1:0")
	} else {
		listener, e = tls.Listen("tcp", "127.0.0.1:0", &tls.Config{Certificates: []tls.Certificate{certificate}})
	}
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { listener.Close() })
	pool := x509.NewCertPool()
	pool.AddCert(cert)
	trusted := &tls.Config{RootCAs: pool, ServerName: "127.0.0.1"}
	count := &atomic.Int32{}
	messages := make(chan string, 10)
	go func() {
		for {
			conn, e := listener.Accept()
			if e != nil {
				return
			}
			go func() {
				defer conn.Close()
				reader := bufio.NewReader(conn)
				fmt.Fprint(conn, "220 capture ESMTP\r\n")
				data := false
				var body strings.Builder
				authStep := 0
				for {
					line, e := reader.ReadString('\n')
					if e != nil {
						return
					}
					line = strings.TrimRight(line, "\r\n")
					if authStep > 0 {
						decoded, err := base64.StdEncoding.DecodeString(line)
						if err != nil {
							return
						}
						switch authStep {
						case 1:
							if string(decoded) != "sender@example.test" {
								return
							}
							authStep = 2
							fmt.Fprint(conn, "334 UGFzc3dvcmQ6\r\n")
							continue
						case 2:
							if string(decoded) != "capture-only" {
								return
							}
						case 3:
							h := hmac.New(md5.New, []byte("capture-only"))
							h.Write([]byte("challenge@capture"))
							if string(decoded) != fmt.Sprintf("sender@example.test %x", h.Sum(nil)) {
								return
							}
						}
						authStep = 0
						fmt.Fprint(conn, "235 authenticated\r\n")
						continue
					}
					if data {
						if line == "." {
							data = false
							count.Add(1)
							messages <- body.String()
							if mode == "lost-reply" {
								return
							}
							if mode == "temporary" {
								fmt.Fprint(conn, "451 temporary refusal\r\n")
							} else if mode == "permanent" {
								fmt.Fprint(conn, "554 permanent refusal\r\n")
							} else {
								fmt.Fprint(conn, "250 accepted\r\n")
							}
							continue
						}
						body.WriteString(line + "\r\n")
						continue
					}
					switch {
					case strings.HasPrefix(line, "EHLO"):
						if mode == "starttls" {
							fmt.Fprint(conn, "250-capture\r\n250-STARTTLS\r\n250 AUTH PLAIN LOGIN\r\n")
						} else if mode == "login" {
							fmt.Fprint(conn, "250-capture\r\n250 AUTH LOGIN\r\n")
						} else if mode == "cram" {
							fmt.Fprint(conn, "250-capture\r\n250 AUTH CRAM-MD5 PLAIN LOGIN\r\n")
						} else {
							fmt.Fprint(conn, "250-capture\r\n250 AUTH PLAIN LOGIN\r\n")
						}
					case line == "STARTTLS":
						fmt.Fprint(conn, "220 start TLS\r\n")
						secure := tls.Server(conn, &tls.Config{Certificates: []tls.Certificate{certificate}})
						if e := secure.Handshake(); e != nil {
							return
						}
						conn = secure
						reader = bufio.NewReader(conn)
					case strings.HasPrefix(line, "AUTH"):
						if mode == "auth-disconnect" {
							return
						} else if mode == "login" {
							authStep = 1
							fmt.Fprint(conn, "334 VXNlcm5hbWU6\r\n")
						} else if mode == "cram" {
							authStep = 3
							fmt.Fprint(conn, "334 "+base64.StdEncoding.EncodeToString([]byte("challenge@capture"))+"\r\n")
						} else if mode == "already-authenticated" {
							fmt.Fprint(conn, "503 already authenticated\r\n")
						} else if mode == "auth-rejected" {
							fmt.Fprint(conn, "535 invalid credential\r\n")
						} else {
							fmt.Fprint(conn, "235 authenticated\r\n")
						}
					case strings.HasPrefix(line, "MAIL FROM"), strings.HasPrefix(line, "RCPT TO"):
						fmt.Fprint(conn, "250 ok\r\n")
					case line == "DATA":
						data = true
						fmt.Fprint(conn, "354 submit data\r\n")
					case line == "QUIT":
						if mode != "quit-disconnect" {
							fmt.Fprint(conn, "221 goodbye\r\n")
						}
						return
					default:
						fmt.Fprint(conn, "500 unsupported\r\n")
					}
				}
			}()
		}
	}()
	addr := listener.Addr().(*net.TCPAddr)
	return SMTPConfig{Host: "127.0.0.1", Port: addr.Port, User: "sender@example.test", Password: "capture-only", ToAddr: "capture@example.test", UseSSL: mode != "starttls"}, trusted, count, messages
}
func TestSMTPAcceptedDataSurvivesQuitDisconnect(t *testing.T) {
	cfg, trusted, count, messages := smtpCapture(t, "quit-disconnect")
	ok, problem := sendSMTP(context.Background(), cfg, "价格变化", "<html><body><p>Portal 2 ¥35</p></body></html>", trusted)
	if !ok || problem != "" || count.Load() != 1 {
		t.Fatalf("%v %s %d", ok, problem, count.Load())
	}
	message := <-messages
	if !strings.Contains(message, "multipart/alternative") || !strings.Contains(message, "text/plain") || !strings.Contains(message, "text/html") {
		t.Fatal(message)
	}
}
func TestSMTPClassifiesReplyLossAndExplicitRefusals(t *testing.T) {
	for _, mode := range []string{"lost-reply", "temporary", "permanent", "auth-rejected"} {
		t.Run(mode, func(t *testing.T) {
			cfg, trusted, _, _ := smtpCapture(t, mode)
			ok, problem := sendSMTP(context.Background(), cfg, "test", "<p>notice</p>", trusted)
			if ok {
				t.Fatal("refusal accepted")
			}
			switch mode {
			case "lost-reply":
				if !deliveryUnknown(problem) || isRetryable(problem) {
					t.Fatal(problem)
				}
			case "temporary":
				if !isRetryable(problem) {
					t.Fatal(problem)
				}
			default:
				if deliveryUnknown(problem) || isRetryable(problem) {
					t.Fatal(problem)
				}
			}
		})
	}
}
func TestSMTPRejectsUntrustedTLSAndMissingConfiguration(t *testing.T) {
	cfg, _, count, _ := smtpCapture(t, "ok")
	ok, problem := sendSMTP(context.Background(), cfg, "test", "<p>x</p>", nil)
	if ok || problem == "" || count.Load() != 0 {
		t.Fatalf("%v %s", ok, problem)
	}
	cfg.Password = ""
	ok, problem = sendSMTP(context.Background(), cfg, "test", "<p>x</p>", nil)
	if ok || problem != "SMTP 未配置" {
		t.Fatalf("%v %s", ok, problem)
	}
}
func TestMailTextOmitsCodeAndKeepsAllowedLinks(t *testing.T) {
	got := MailText(`<html><head><title>hidden</title></head><body><p>A&amp;B</p><script>bad()</script><a href="https://store.steampowered.com/app/620/">Portal</a><a href="javascript:bad()">ignore</a></body></html>`)
	if strings.Contains(got, "hidden") || strings.Contains(got, "bad()") || !strings.Contains(got, "A&B") || !strings.Contains(got, "https://store.steampowered.com/app/620/") {
		t.Fatal(got)
	}
}

func TestSMTPStartTLSAndAdvertisedAuthenticationMechanisms(t *testing.T) {
	for _, mode := range []string{"starttls", "login", "cram", "already-authenticated"} {
		t.Run(mode, func(t *testing.T) {
			cfg, trusted, count, _ := smtpCapture(t, mode)
			ok, problem := sendSMTP(context.Background(), cfg, "test", "<p>notice</p>", trusted)
			if !ok || count.Load() != 1 {
				t.Fatalf("%v %s", ok, problem)
			}
		})
	}
}
func TestSMTPDisconnectDuringAuthenticationCanRetry(t *testing.T) {
	cfg, trusted, _, _ := smtpCapture(t, "auth-disconnect")
	ok, problem := sendSMTP(context.Background(), cfg, "test", "<p>notice</p>", trusted)
	if ok || !isRetryable(problem) || deliveryUnknown(problem) {
		t.Fatalf("%v %s", ok, problem)
	}
}

func TestMailTextHandlesMalformedTagsAndRejectsUntrustedLinkTargets(t *testing.T) {
	if got := MailText(`<p>before</p>< ><p>after</p>`); !strings.Contains(got, "after") {
		t.Fatal(got)
	}
	if safeMailURL("https://store.steampowered.com:99999/app/620/") != "" {
		t.Fatal("invalid port accepted")
	}
	if safeMailURL("HTTPS://store.steampowered.com/app/620/") == "" {
		t.Fatal("safe case-insensitive scheme rejected")
	}
}

func TestMissingSMTPConfigurationSkipsCertificateLoading(t *testing.T) {
	t.Setenv("SSL_CERT_FILE", t.TempDir()+"/missing.pem")
	if ok, problem := SendMail(context.Background(), SMTPConfig{}, "test", "<p>test</p>"); ok || problem != "SMTP 未配置" {
		t.Fatalf("%v %s", ok, problem)
	}
}
