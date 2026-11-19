package hostkit

import "testing"

func TestVersionFromTag(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"v0.3.8", "0.3.8"},
		{"V1.2.3-beta.1", "1.2.3-beta.1"},
		{"  v0.4.0  ", "0.4.0"},
		{"0.3.8", "0.3.8"},
		{"v", "v"},
		{"V", "V"},
		{"vivid", "vivid"},
		{"legacy_version", "legacy_version"},
		{"", ""},
	}
	for _, tc := range cases {
		if got := VersionFromTag(tc.in); got != tc.want {
			t.Fatalf("VersionFromTag(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestVersionGT(t *testing.T) {
	cases := []struct {
		a, b string
		want bool
	}{
		{"0.3.9", "0.3.8", true},
		{"0.3.8", "0.3.9", false},
		{"0.3.10", "0.3.9", true},
		{"0.4.0", "0.3.99", true},
		{"0.3.8", "0.3.8", false},
		{"0.2", "0.2.0", false},
		{"0.2.0", "0.2", false},
		{"0.2.1", "0.2", true},
		{"1.0.0-beta", "1.0.0-alpha", true},
		{"1.0.0-alpha", "1.0.0-beta", false},
		{"1.0.0", "1.0.0-beta", true},
		{"1.0.0-beta", "1.0.0", false},
		{"0.3.8-w5", "0.3.7-w4", true},
		{"v1.0.0", "0.9.9", true},
		{"V0.4.0", "v0.3.9", true},
		{"v0.3.8", "v0.3.8", false},
		{"0.3.7", "legacy_version", true},
	}
	for _, tc := range cases {
		if got := VersionGT(tc.a, tc.b); got != tc.want {
			t.Fatalf("VersionGT(%q, %q) = %v, want %v", tc.a, tc.b, got, tc.want)
		}
	}
}
