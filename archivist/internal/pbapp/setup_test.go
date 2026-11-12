package pbapp

import (
	"reflect"
	"testing"

	"ganigeorgiev.com/pocketbase/pocketbase"
	"ganigeorgiev.com/pocketbase/pocketbase/core"
	"github.com/napnap75/archivist/internal/config"
)

func enumTestCfg(t *testing.T) *config.Config {
	t.Helper()
	tpl, err := config.CompileTemplate("default", "Divers/{documentType}.pdf")
	if err != nil {
		t.Fatalf("compile template: %v", err)
	}
	fields := []config.Field{
		{Name: "template", Type: "enum"},
		{Name: "documentType", Type: "enum"},
	}
	return &config.Config{
		Fields: map[string]config.Field{
			"template":     fields[0],
			"documentType": fields[1],
		},
		FieldList: fields,
		Templates: map[string]*config.CompiledTemplate{"default": tpl},
	}
}

func enumTestApp(t *testing.T, cfg *config.Config) core.App {
	t.Helper()
	app := pocketbase.NewWithConfig(pocketbase.Config{DefaultDataDir: t.TempDir()})
	if err := app.Bootstrap(); err != nil {
		t.Fatalf("bootstrap PocketBase: %v", err)
	}
	if err := Setup(app, "admin@example.net", "password-123456", cfg); err != nil {
		t.Fatalf("setup: %v", err)
	}
	return app
}

// US-03: declared enums become official select fields; the "template"
// field starts with the configured template names, the others with the
// placeholder.
func TestEnsureDocumentsCreatesSelectEnums(t *testing.T) {
	cfg := enumTestCfg(t)
	app := enumTestApp(t, cfg)
	defer app.ClearBootstrap()

	coll, err := app.FindCollectionByNameOrId("documents")
	if err != nil {
		t.Fatalf("find documents collection: %v", err)
	}
	for name, want := range map[string][]string{
		"template":     {"default"},
		"documentType": {DummyEnumValue},
	} {
		field, ok := coll.Fields.GetByName(name).(*core.SelectField)
		if !ok || field == nil {
			t.Fatalf("field %q is not a select field", name)
		}
		if !reflect.DeepEqual(field.Values, want) {
			t.Errorf("field %q values = %v, want %v", name, field.Values, want)
		}
	}
}

// US-37: the startup cleanup keeps the values documents use, drops the
// others, keeps the placeholder only while alone, and forces the template
// list to the configuration.
func TestCleanupEnumValues(t *testing.T) {
	cfg := enumTestCfg(t)
	app := enumTestApp(t, cfg)
	defer app.ClearBootstrap()

	if err := AddEnumValue(app, "documentType", "Facture"); err != nil {
		t.Fatalf("add enum value: %v", err)
	}
	if err := AddEnumValue(app, "documentType", "obsolete"); err != nil {
		t.Fatalf("add enum value: %v", err)
	}
	coll, err := app.FindCollectionByNameOrId("documents")
	if err != nil {
		t.Fatalf("find documents collection: %v", err)
	}
	rec := core.NewRecord(coll)
	rec.Set("file_key", "pending/x.pdf")
	rec.Set("status", "pending")
	rec.Set("documentType", "Facture")
	if err := app.Save(rec); err != nil {
		t.Fatalf("save record: %v", err)
	}

	CleanupEnumValues(app, cfg)

	values, err := enumFieldValues(app, "documentType")
	if err != nil {
		t.Fatalf("enum field values: %v", err)
	}
	if !reflect.DeepEqual(values, []string{"Facture"}) {
		t.Errorf("documentType values = %v, want [Facture]", values)
	}
	tplValues, err := enumFieldValues(app, "template")
	if err != nil {
		t.Fatalf("enum field values: %v", err)
	}
	if !reflect.DeepEqual(tplValues, []string{"default"}) {
		t.Errorf("template values = %v, want [default]", tplValues)
	}
}
