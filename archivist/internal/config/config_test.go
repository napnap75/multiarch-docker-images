package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestDefaultConfigExpressionHelpers(t *testing.T) {
	cfg, err := Load("../../config.jsonc")
	if err != nil {
		t.Fatalf("load default config: %v", err)
	}
	if got := cfg.Fields["emissionDate"].French; got != "Date d'émission" {
		t.Fatalf("emissionDate french title = %q, want %q", got, "Date d'émission")
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

func TestLoadRequiresFrenchFieldTitle(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.jsonc")
	if err := os.WriteFile(path, []byte(`{"fields":[{"name":"company","type":"text"}]}`), 0600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	if _, err := Load(path); err == nil {
		t.Fatal("Load accepted a field without a French title")
	}
}

func TestLoadSourceKeepsExpressionFieldsIsolated(t *testing.T) {
	firstSource := []byte(`{
		// retained JSONC comment
		"fields": [{"name":"company","french":"Société","type":"text","mandatory":true}],
		"rules": [{"when":"pages[0] contains ` + "`Acme`" + `","set":[{"field":"company","value":"Acme"}]}],
		"templates": {"default":"{company}.pdf"}
	}`)
	secondSource := []byte(`{
		"fields": [{"name":"amount","french":"Montant","type":"number","mandatory":true}],
		"rules": [{"when":"pages[0] contains ` + "`Invoice`" + `","set":[{"field":"amount","value":42}]}],
		"templates": {"default":"{amount}.pdf"}
	}`)

	firstConfig, err := LoadSource(firstSource)
	if err != nil {
		t.Fatalf("load first source: %v", err)
	}
	if _, err := LoadSource(secondSource); err != nil {
		t.Fatalf("load second source: %v", err)
	}
	fields, rendered, errs := firstConfig.Evaluate([]string{"Acme invoice"})
	if len(errs) != 0 {
		t.Fatalf("evaluate first config after second load: %v", errs)
	}
	if fields["company"] != "Acme" || rendered != "Acme.pdf" {
		t.Fatalf("first config result = (%v, %q), want company Acme and Acme.pdf", fields, rendered)
	}
}

func TestFrenchDate(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"Facture en date du 19 janvier 2026", "19/01/2026"},
		{"le 1er févr. 2026", "01/02/2026"},
		{"Janvier 2026", "01/01/2026"}, // no day: first of the month
		{"Avis arrêté au 3 septembre 2026", "03/09/2026"},
		{"Situation au 31 decembre 2026", "31/12/2026"}, // unaccented
	}
	for _, c := range cases {
		v, err := FrenchDate(c.in)
		if err != nil {
			t.Fatalf("frDate(%q): %v", c.in, err)
		}
		got, ok := v.(time.Time)
		if !ok || got.Format("02/01/2006") != c.want {
			t.Fatalf("frDate(%q) = %v, want %s", c.in, v, c.want)
		}
	}
	for _, text := range []string{"no date in this text 123", "le 31 février 2026"} {
		got, err := FrenchDate(text)
		if err != nil || got != "" {
			t.Fatalf("frDate(%q) = (%v, %v), want empty string and no error", text, got, err)
		}
	}
}

func TestFrenchDateMissingAllowsLaterRule(t *testing.T) {
	source := []byte(strings.ReplaceAll(`
	{"fields":[{"name":"emissionDate","french":"Date","type":"date","mandatory":false}],
	"rules":[
	{"when":"frDate(pages[0]) != @@","set":[{"field":"emissionDate","eval":"frDate(pages[0])"}]},
	{"when":"firstMatch(@[0-9]{2}/[0-9]{2}/[0-9]{4}@, pages[0]) != @@","set":[{"field":"emissionDate","eval":"date(firstMatch(@[0-9]{2}/[0-9]{2}/[0-9]{4}@, pages[0]), @02/01/2006@)"}]}
	],"templates":{"default":"document.pdf"}}`, "@", "`"))
	cfg, err := LoadSource(source)
	if err != nil {
		t.Fatalf("load configuration: %v", err)
	}
	fields, _, errs := cfg.Evaluate([]string{"Date : 15/01/2026"})
	if len(errs) != 0 {
		t.Fatalf("evaluate fallback rule: %v", errs)
	}
	got, ok := fields["emissionDate"].(time.Time)
	if !ok || got.Format("02/01/2006") != "15/01/2026" {
		t.Fatalf("emissionDate = %v, want 15/01/2026", fields["emissionDate"])
	}
}

func TestFrPeriodUsesFrenchMonthNames(t *testing.T) {
	got, err := FrPeriod(time.Date(2026, time.September, 10, 0, 0, 0, 0, time.UTC), "month", 0)
	if err != nil {
		t.Fatalf("frPeriod: %v", err)
	}
	if got != "Septembre 2026" {
		t.Fatalf("frPeriod = %q, want %q", got, "Septembre 2026")
	}
}
