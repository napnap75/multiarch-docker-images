package main

import (
	"testing"

	"github.com/napnap75/archivist/internal/storage"
)

func TestConfigPathFromArgs(t *testing.T) {
	tests := []struct {
		name        string
		args        []string
		defaultPath string
		want        string
	}{
		{
			name:        "default path",
			args:        []string{"serve"},
			defaultPath: "config.jsonc",
			want:        "config.jsonc",
		},
		{
			name:        "config after serve",
			args:        []string{"serve", "--config", "/etc/archivist/custom.jsonc"},
			defaultPath: "config.jsonc",
			want:        "/etc/archivist/custom.jsonc",
		},
		{
			name:        "config among PocketBase flags",
			args:        []string{"serve", "--http=127.0.0.1:8090", "--config=/etc/archivist/custom.jsonc"},
			defaultPath: "config.jsonc",
			want:        "/etc/archivist/custom.jsonc",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := configPathFromArgs(tt.args, tt.defaultPath)
			if err != nil {
				t.Fatalf("configPathFromArgs() error = %v", err)
			}
			if got != tt.want {
				t.Errorf("configPathFromArgs() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestLoadEnvConfigPrefixes(t *testing.T) {
	t.Setenv("ARCHIVIST_S3_ENDPOINT", "s3:9000")
	t.Setenv("ARCHIVIST_S3_ACCESS_KEY", "key")
	t.Setenv("ARCHIVIST_S3_SECRET_KEY", "secret")
	t.Setenv("ARCHIVIST_S3_BUCKET", "bucket")
	t.Setenv("ARCHIVIST_ADMIN_EMAIL", "admin@example.net")
	t.Setenv("ARCHIVIST_ADMIN_PASSWORD", "password-123456")

	t.Run("defaults", func(t *testing.T) {
		c, err := LoadEnvConfig()
		if err != nil {
			t.Fatalf("LoadEnvConfig() error = %v", err)
		}
		want := storage.DefaultPrefixes
		if c.InboxPrefix != want.Inbox || c.PendingPrefix != want.Pending || c.FilesPrefix != want.Files {
			t.Errorf("prefixes = %q %q %q, want %q %q %q",
				c.InboxPrefix, c.PendingPrefix, c.FilesPrefix, want.Inbox, want.Pending, want.Files)
		}
	})

	t.Run("custom prefixes", func(t *testing.T) {
		t.Setenv("ARCHIVIST_S3_INBOX_PREFIX", "drop")
		t.Setenv("ARCHIVIST_S3_PENDING_PREFIX", "/review/")
		t.Setenv("ARCHIVIST_S3_FILES_PREFIX", "/")
		c, err := LoadEnvConfig()
		if err != nil {
			t.Fatalf("LoadEnvConfig() error = %v", err)
		}
		if c.InboxPrefix != "drop/" || c.PendingPrefix != "review/" || c.FilesPrefix != "" {
			t.Errorf("prefixes = %q %q %q, want drop/ review/ \"\"", c.InboxPrefix, c.PendingPrefix, c.FilesPrefix)
		}
	})

	t.Run("inbox prefix cannot be the bucket root", func(t *testing.T) {
		t.Setenv("ARCHIVIST_S3_INBOX_PREFIX", "/")
		if _, err := LoadEnvConfig(); err == nil {
			t.Fatal("LoadEnvConfig() error = nil, want non-nil")
		}
	})

	t.Run("identical prefixes are rejected", func(t *testing.T) {
		t.Setenv("ARCHIVIST_S3_INBOX_PREFIX", "same")
		t.Setenv("ARCHIVIST_S3_PENDING_PREFIX", "same")
		t.Setenv("ARCHIVIST_S3_FILES_PREFIX", "files")
		if _, err := LoadEnvConfig(); err == nil {
			t.Fatal("LoadEnvConfig() error = nil, want non-nil")
		}
	})
}
