package account

import (
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"testing"
)

func TestLoginProtobufDecodesGoldenAndRejectsTruncation(t *testing.T) {
	data, _ := hex.DecodeString("0a05616c69636510071d0000803f")
	fields, e := readLoginPB(data)
	if e != nil || string(pbData(fields, 1, 2)) != "alice" || pbNumber(fields, 2) != 7 || len(pbData(fields, 3, 5)) != 4 {
		t.Fatalf("golden protocol error=%v fields=%v", e, fields)
	}
	if got := hex.EncodeToString(pbBytes(nil, 1, []byte("alice"))); got != "0a05616c696365" {
		t.Fatal(got)
	}
	for _, bad := range [][]byte{{0}, {9, 1}, {13, 1}, {10, 4, 1}, {8, 128}, {8, 255, 255, 255, 255, 255, 255, 255, 255, 255, 255, 255}, {15}} {
		if _, e := readLoginPB(bad); e == nil {
			t.Fatalf("malformed protobuf accepted: %x", bad)
		}
	}
}

func TestLoginRSAEncryptsPasswordWithStandardPadding(t *testing.T) {
	key, e := rsa.GenerateKey(rand.Reader, 2048)
	if e != nil {
		t.Fatal(e)
	}
	password := "fixture-密码-123"
	encoded, e := encryptLoginPassword(password, key.N.Text(16), fmt.Sprintf("%x", key.E))
	if e != nil {
		t.Fatal(e)
	}
	cipher, e := base64.StdEncoding.DecodeString(encoded)
	if e != nil {
		t.Fatal(e)
	}
	plain, e := rsa.DecryptPKCS1v15(rand.Reader, key, cipher)
	if e != nil || string(plain) != password {
		t.Fatalf("RSA round trip failed: %v", e)
	}
	if _, e = encryptLoginPassword(password, "invalid", "10001"); e == nil {
		t.Fatal("invalid RSA key accepted")
	}
}
