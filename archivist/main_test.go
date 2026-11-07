package main

import "testing"

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
