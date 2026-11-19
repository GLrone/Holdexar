package agent

import (
	"bytes"
	"encoding/json"
)

// Obj 按插入序序列化 JSON 对象，避免 map 键重排。
type Obj struct {
	keys []string
	vals []any
}

func newObj() *Obj { return &Obj{} }

func (o *Obj) set(key string, val any) {
	o.keys = append(o.keys, key)
	o.vals = append(o.vals, val)
}

func (o *Obj) get(key string) any {
	for i, k := range o.keys {
		if k == key {
			return o.vals[i]
		}
	}
	return nil
}

// MarshalJSON 按插入序输出成员，值统一走 encoding/json（SetEscapeHTML(false)）。
func (o *Obj) MarshalJSON() ([]byte, error) {
	if o == nil {
		return []byte("null"), nil
	}
	var buf bytes.Buffer
	buf.WriteByte('{')
	for i, k := range o.keys {
		if i > 0 {
			buf.WriteByte(',')
		}
		kb, err := marshalPlain(k)
		if err != nil {
			return nil, err
		}
		buf.Write(kb)
		buf.WriteByte(':')
		vb, err := marshalPlain(o.vals[i])
		if err != nil {
			vb = []byte("null")
		}
		buf.Write(vb)
	}
	buf.WriteByte('}')
	return buf.Bytes(), nil
}

func marshalPlain(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(buf.Bytes(), []byte{'\n'}), nil
}

func encodeJSON(v any) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return "null"
	}
	return string(bytes.TrimSuffix(buf.Bytes(), []byte{'\n'}))
}
