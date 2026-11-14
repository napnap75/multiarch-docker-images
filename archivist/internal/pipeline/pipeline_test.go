package pipeline

import "testing"

// TestIsPDF covers the inbox rule: only content carrying the %PDF- header
// enters the pipeline; anything else is left in place (US-12).
func TestIsPDF(t *testing.T) {
	cases := []struct {
		name string
		data []byte
		want bool
	}{
		{"pdf header", []byte("%PDF-1.7\n..."), true},
		{"pdf header with leading whitespace", []byte("\n%PDF-1.4"), false},
		{"plain text", []byte("not a pdf at all\n"), false},
		{"image content", []byte("\x89PNG\r\n\x1a\n..."), false},
		{"empty", nil, false},
	}
	for _, c := range cases {
		if got := isPDF(c.data); got != c.want {
			t.Errorf("%s: isPDF = %v, want %v", c.name, got, c.want)
		}
	}
}
