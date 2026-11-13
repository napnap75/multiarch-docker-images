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

func TestRangeStartDateExpression(t *testing.T) {
	cfg, err := Load("../../tests/config.jsonc")
	if err != nil {
		t.Fatalf("load test config: %v", err)
	}

	fields, errs := cfg.Classify([]string{
		"821099447 QUITTANCE 2 avenue des Champs-Elysées Periode : du 10 au 30 septembre 2026",
	})
	if len(errs) != 0 {
		t.Fatalf("classify range date: %v", errs)
	}
	got, ok := fields["emissionDate"].(time.Time)
	if !ok || got.Format("02/01/2006") != "10/09/2026" {
		t.Fatalf("emissionDate = %v, want 10/09/2026", fields["emissionDate"])
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
	if _, err := FrenchDate("no date in this text 123"); err == nil {
		t.Fatal("frDate: expected an error when no French date is found")
	}
	if _, err := FrenchDate("le 31 février 2026"); err == nil {
		t.Fatal("frDate: expected an error on an invalid date")
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
