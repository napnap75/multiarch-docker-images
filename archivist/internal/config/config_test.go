package config

import (
	"testing"
	"time"
)

func TestDefaultConfigExpressionHelpers(t *testing.T) {
	cfg, err := Load("../../config.jsonc")
	if err != nil {
		t.Fatalf("load default config: %v", err)
	}

	fields, rendered, errs := cfg.Evaluate([]string{
		"Relevé de compte\nBRED\n12345678901\nDate : 15/01/2026",
	})
	if len(errs) != 0 {
		t.Fatalf("evaluate default config: %v", errs)
	}
	if got, ok := fields["emissionDate"].(time.Time); !ok || got.Format("02/01/2006") != "15/01/2026" {
		t.Fatalf("emissionDate = %v, want 15/01/2026", fields["emissionDate"])
	}
	if rendered == "" {
		t.Fatal("default config rendered an empty file name")
	}
}
