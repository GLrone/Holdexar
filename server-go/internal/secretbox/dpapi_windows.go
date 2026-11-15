//go:build windows

// DPAPI 系统凭据封装（仅 Windows）：随机主密钥的用户级封装层。Linux/NAS
// 形态的对应保护层是口令封装模式（keyring.go passphrase）。
package secretbox

import (
	"bytes"
	"unsafe"

	"golang.org/x/sys/windows"
)

// dpapiEntropy DPAPI 附加熵（第二因素）：blob 被其他进程拿到也无法在白盒外解封。
var dpapiEntropy = []byte("holdexar:keyring:dpapi:v1")

func DpapiAvailable() bool { return true }

func dpapiProtect(data []byte) ([]byte, error) { return dpapiCall(data, true) }

func dpapiUnprotect(blob []byte) ([]byte, error) { return dpapiCall(blob, false) }

func dpapiCall(data []byte, protect bool) ([]byte, error) {
	if len(data) == 0 {
		return nil, keyringErr("系统凭据数据为空")
	}
	in := &windows.DataBlob{Size: uint32(len(data)), Data: &data[0]}
	ent := &windows.DataBlob{Size: uint32(len(dpapiEntropy)), Data: &dpapiEntropy[0]}
	var out windows.DataBlob
	var err error
	if protect {
		err = windows.CryptProtectData(in, nil, ent, 0, nil, windows.CRYPTPROTECT_UI_FORBIDDEN, &out)
	} else {
		err = windows.CryptUnprotectData(in, nil, ent, 0, nil, windows.CRYPTPROTECT_UI_FORBIDDEN, &out)
	}
	if err != nil {
		return nil, err
	}
	result := bytes.Clone(unsafe.Slice(out.Data, out.Size))
	windows.LocalFree(windows.Handle(unsafe.Pointer(out.Data)))
	return result, nil
}
