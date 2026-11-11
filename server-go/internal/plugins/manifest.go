package plugins

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"regexp"
	"strings"
)

const maxManifest = 64 << 10

type Manifest struct {
	ID           string            `json:"id"`
	Name         string            `json:"name"`
	Version      string            `json:"version"`
	Description  string            `json:"description,omitempty"`
	Author       string            `json:"author,omitempty"`
	Homepage     string            `json:"homepage,omitempty"`
	APIVersion   int               `json:"apiVersion"`
	Kind         string            `json:"kind"`
	Domains      []string          `json:"domains"`
	Endpoint     string            `json:"endpoint"`
	Headers      map[string]string `json:"headers,omitempty"`
	Body         any               `json:"body"`
	ConfigSchema Schema            `json:"configSchema"`
}

type Schema struct {
	Type                 string           `json:"type"`
	Title                string           `json:"title,omitempty"`
	Properties           map[string]Field `json:"properties"`
	Required             []string         `json:"required,omitempty"`
	AdditionalProperties bool             `json:"additionalProperties,omitempty"`
}

type Field struct {
	Type        string `json:"type"`
	Title       string `json:"title,omitempty"`
	Description string `json:"description,omitempty"`
	Format      string `json:"format,omitempty"`
	Enum        []any  `json:"enum,omitempty"`
	Default     any    `json:"default,omitempty"`
}

var idPattern = regexp.MustCompile(`^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$`)
var versionPattern = regexp.MustCompile(`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$`)
var fieldPattern = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_]{0,47}$`)
var placeholders = regexp.MustCompile(`\{\{([^{}]+)\}\}`)

func strictJSON(data []byte, out any) error {
	d := json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if e := d.Decode(out); e != nil {
		return errors.New("invalid plugin document")
	}
	if d.Decode(new(any)) != io.EOF {
		return errors.New("invalid trailing plugin document")
	}
	return nil
}

func (m Manifest) Validate() error {
	raw, e := json.Marshal(m)
	if e != nil || len(raw) > maxManifest {
		return errors.New("plugin manifest is too large")
	}
	if !idPattern.MatchString(m.ID) || len(m.ID) > 80 || !versionPattern.MatchString(m.Version) || len(m.Version) > 64 {
		return errors.New("invalid plugin identity or version")
	}
	if strings.TrimSpace(m.Name) == "" || len(m.Name) > 120 || len(m.Description) > 2000 || len(m.Author) > 200 {
		return errors.New("invalid plugin description")
	}
	if m.APIVersion != 1 || m.Kind != "notification.webhook" {
		return errors.New("unsupported plugin API or capability")
	}
	if len(m.Domains) == 0 || len(m.Domains) > 8 {
		return errors.New("plugin must declare its HTTPS domains")
	}
	for _, d := range m.Domains {
		if d == "" || strings.ToLower(d) != d || strings.ContainsAny(d, "/:* @\\\t\r\n") {
			return errors.New("invalid plugin domain")
		}
	}
	if e = ValidateHTTPS(m.Endpoint, m.Domains); e != nil {
		return e
	}
	if m.Homepage != "" {
		if e = ValidateHTTPS(m.Homepage, nil); e != nil {
			return errors.New("invalid plugin homepage")
		}
	}
	if m.ConfigSchema.Type != "object" || m.ConfigSchema.AdditionalProperties || len(m.ConfigSchema.Properties) > 24 {
		return errors.New("unsupported configuration schema")
	}
	for key, field := range m.ConfigSchema.Properties {
		if !fieldPattern.MatchString(key) || !oneOf(field.Type, "string", "number", "boolean") || field.Format != "" && field.Format != "password" || field.Format == "password" && field.Type != "string" || len(field.Enum) > 32 {
			return errors.New("unsupported configuration field")
		}
		if len(field.Title) > 120 || len(field.Description) > 1000 {
			return errors.New("configuration description is too large")
		}
		if field.Default != nil {
			if e = validateValue(field, field.Default); e != nil {
				return e
			}
		}
		for _, v := range field.Enum {
			copy := field
			copy.Enum = nil
			if e = validateValue(copy, v); e != nil {
				return e
			}
		}
	}
	for _, key := range m.ConfigSchema.Required {
		if _, ok := m.ConfigSchema.Properties[key]; !ok {
			return errors.New("unknown required configuration field")
		}
	}
	if len(m.Headers) > 16 {
		return errors.New("too many plugin headers")
	}
	names := map[string]bool{}
	for name, value := range m.Headers {
		canonical := http.CanonicalHeaderKey(name)
		if !validHeaderName(name) || names[strings.ToLower(name)] || canonical != "Authorization" && !strings.HasPrefix(canonical, "X-") || len(value) > 4096 || strings.ContainsAny(value, "\r\n") {
			return errors.New("unsupported plugin header")
		}
		names[strings.ToLower(name)] = true
		if e = m.validateTemplate(value); e != nil {
			return e
		}
	}
	return walkStrings(m.Body, 0, func(s string) (string, error) { return s, m.validateTemplate(s) })
}

func validHeaderName(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || strings.ContainsRune("!#$%&'*+-.^_`|~", r)) {
			return false
		}
	}
	return true
}
func oneOf(s string, values ...string) bool {
	for _, v := range values {
		if s == v {
			return true
		}
	}
	return false
}

func (m Manifest) validateTemplate(s string) error {
	for _, match := range placeholders.FindAllStringSubmatch(s, -1) {
		key := match[1]
		if key == "subject" || key == "body" {
			continue
		}
		if !strings.HasPrefix(key, "config.") {
			return errors.New("unsupported plugin template")
		}
		if _, ok := m.ConfigSchema.Properties[strings.TrimPrefix(key, "config.")]; !ok {
			return errors.New("unknown plugin template field")
		}
	}
	rest := placeholders.ReplaceAllString(s, "")
	if strings.Contains(rest, "{{") || strings.Contains(rest, "}}") {
		return errors.New("invalid plugin template")
	}
	return nil
}

func validateValue(f Field, v any) error {
	good := false
	switch f.Type {
	case "string":
		s, ok := v.(string)
		good = ok && len(s) <= 8192
	case "boolean":
		_, good = v.(bool)
	case "number":
		n, ok := v.(float64)
		good = ok && !math.IsInf(n, 0) && !math.IsNaN(n)
	}
	if !good {
		return errors.New("configuration value has the wrong type or size")
	}
	if len(f.Enum) > 0 {
		got, _ := json.Marshal(v)
		for _, value := range f.Enum {
			b, _ := json.Marshal(value)
			if bytes.Equal(b, got) {
				return nil
			}
		}
		return errors.New("configuration value is outside its choices")
	}
	return nil
}

func ValidateConfig(s Schema, c map[string]any, required bool) error {
	b, e := json.Marshal(c)
	if e != nil || len(b) > maxManifest {
		return errors.New("configuration is too large")
	}
	for k, v := range c {
		f, ok := s.Properties[k]
		if !ok {
			return errors.New("unknown configuration field")
		}
		if e = validateValue(f, v); e != nil {
			return e
		}
	}
	if required {
		for _, k := range s.Required {
			v, ok := c[k]
			if !ok || v == nil || v == "" {
				return fmt.Errorf("configuration field %s is required", k)
			}
		}
	}
	return nil
}

func walkStrings(v any, depth int, f func(string) (string, error)) error {
	_, e := mapStrings(v, depth, f)
	return e
}
func mapStrings(v any, depth int, f func(string) (string, error)) (any, error) {
	if depth > 12 {
		return nil, errors.New("plugin template is too deeply nested")
	}
	switch x := v.(type) {
	case string:
		return f(x)
	case map[string]any:
		out := map[string]any{}
		for k, item := range x {
			if strings.Contains(k, "{{") {
				return nil, errors.New("template keys must be literal")
			}
			n, e := mapStrings(item, depth+1, f)
			if e != nil {
				return nil, e
			}
			out[k] = n
		}
		return out, nil
	case []any:
		out := make([]any, len(x))
		for i, item := range x {
			n, e := mapStrings(item, depth+1, f)
			if e != nil {
				return nil, e
			}
			out[i] = n
		}
		return out, nil
	case nil, bool, float64:
		return v, nil
	default:
		return nil, errors.New("unsupported plugin JSON value")
	}
}

func Render(m Manifest, config map[string]any, subject, body string) ([]byte, map[string]string, error) {
	if e := m.Validate(); e != nil {
		return nil, nil, e
	}
	if e := ValidateConfig(m.ConfigSchema, config, true); e != nil {
		return nil, nil, e
	}
	replace := func(s string) (string, error) {
		return placeholders.ReplaceAllStringFunc(s, func(token string) string {
			key := token[2 : len(token)-2]
			if key == "subject" {
				return subject
			}
			if key == "body" {
				return body
			}
			v := config[strings.TrimPrefix(key, "config.")]
			if v == nil {
				return ""
			}
			return fmt.Sprint(v)
		}), nil
	}
	value, e := mapStrings(m.Body, 0, replace)
	if e != nil {
		return nil, nil, e
	}
	payload, e := json.Marshal(value)
	if e != nil || len(payload) > 1<<20 {
		return nil, nil, errors.New("rendered notification is too large")
	}
	headers := map[string]string{}
	for k, v := range m.Headers {
		value, _ := replace(v)
		if len(value) > 8192 || strings.ContainsAny(value, "\r\n") {
			return nil, nil, errors.New("invalid rendered notification header")
		}
		headers[k] = value
	}
	return payload, headers, nil
}

func parsedURL(raw string) (*url.URL, error) {
	u, e := url.Parse(raw)
	if e != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.Fragment != "" || len(raw) > 4096 || u.Opaque != "" {
		return nil, errors.New("plugin address must be an HTTPS URL without credentials")
	}
	return u, nil
}
