package storage

import "testing"

func TestNormalizePrefix(t *testing.T) {
	cases := []struct {
		value string
		want  string
	}{
		{"inbox", "inbox/"},
		{"inbox/", "inbox/"},
		{"/docs/inbox", "docs/inbox/"},
		{"/", ""},
		{"", ""},
	}
	for _, c := range cases {
		if got := NormalizePrefix(c.value); got != c.want {
			t.Errorf("NormalizePrefix(%q) = %q, want %q", c.value, got, c.want)
		}
	}
}

func TestIsReconciliationKey(t *testing.T) {
	cases := []struct {
		name     string
		prefixes Prefixes
		key      string
		want     bool
	}{
		{"file under files", DefaultPrefixes, "files/BRED/a.pdf", true},
		{"file under pending", DefaultPrefixes, "pending/a.PDF", true},
		{"file under inbox", DefaultPrefixes, "inbox/a.pdf", false},
		{"file elsewhere", DefaultPrefixes, "other/a.pdf", false},
		{"not a pdf", DefaultPrefixes, "files/readme.txt", false},
		{"file at the root", Prefixes{Inbox: "inbox/", Pending: "pending/", Files: ""}, "a.pdf", true},
		{"file in a root folder", Prefixes{Inbox: "inbox/", Pending: "pending/", Files: ""}, "BRED/a.pdf", true},
		{"inbox object at the root", Prefixes{Inbox: "inbox/", Pending: "pending/", Files: ""}, "inbox/a.pdf", false},
		{"pending object at the root", Prefixes{Inbox: "inbox/", Pending: "pending/", Files: ""}, "pending/a.pdf", true},
	}
	for _, c := range cases {
		if got := c.prefixes.IsReconciliationKey(c.key); got != c.want {
			t.Errorf("%s: IsReconciliationKey(%q) = %v, want %v", c.name, c.key, got, c.want)
		}
	}
}
