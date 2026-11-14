package pbapp

import (
	"reflect"
	"testing"

	"ganigeorgiev.com/pocketbase/pocketbase"
	"ganigeorgiev.com/pocketbase/pocketbase/core"
	"github.com/napnap75/archivist/internal/config"
)

// TestEnsureDocumentsCreatesSelectEnums replaces the former text-storage
// workaround: declared enums are now official select fields (US-03, US-37),
// created with the single dummy placeholder.
func TestEnsureDocumentsCreatesSelectEnums(t *testing.T) {
	app := pocketbase.NewWithConfig(pocketbase.Config{DefaultDataDir: t.TempDir()})
	if err := app.Bootstrap(); err != nil {
		t.Fatalf("bootstrap PocketBase: %v", err)
	}
	defer app.ClearBootstrap()

	fields := []config.Field{
		{Name: "documentType", Type: "enum"},
		{Name: "bankAccount", Type: "enum"},
	}
	cfg := &config.Config{
		Fields: map[string]config.Field{
			"documentType": fields[0],
			"bankAccount":  fields[1],
		},
		FieldList: fields,
	}
	if err := Setup(app, "admin@example.net", "password-123456", cfg); err != nil {
		t.Fatalf("setup: %v", err)
	}

	coll, err := app.FindCollectionByNameOrId("documents")
	if err != nil {
		t.Fatalf("find documents collection: %v", err)
	}
	for _, name := range []string{"documentType", "bankAccount"} {
		field, ok := coll.Fields.GetByName(name).(*core.SelectField)
		if !ok || field == nil {
			t.Fatalf("field %q is not a select field", name)
		}
		if !reflect.DeepEqual(field.Values, []string{DummyEnumValue}) {
			t.Errorf("field %q values = %v, want [%s]", name, field.Values, DummyEnumValue)
		}
	}
}

func TestEnsureSuperuserPreservesTokenKeyWhenPasswordIsUnchanged(t *testing.T) {
	app := pocketbase.NewWithConfig(pocketbase.Config{DefaultDataDir: t.TempDir()})
	if err := app.Bootstrap(); err != nil {
		t.Fatalf("bootstrap PocketBase: %v", err)
	}
	defer app.ClearBootstrap()

	cfg := &config.Config{}
	if err := Setup(app, "admin@example.net", "password-123456", cfg); err != nil {
		t.Fatalf("initial setup: %v", err)
	}
	record, err := app.FindFirstRecordByFilter(core.CollectionNameSuperusers, "email = {:e}", map[string]any{"e": "admin@example.net"})
	if err != nil {
		t.Fatalf("find initial superuser: %v", err)
	}
	originalTokenKey := record.TokenKey()

	if err := Setup(app, "admin@example.net", "password-123456", cfg); err != nil {
		t.Fatalf("repeated setup: %v", err)
	}
	record, err = app.FindRecordById(core.CollectionNameSuperusers, record.Id)
	if err != nil {
		t.Fatalf("find superuser after repeated setup: %v", err)
	}
	if record.TokenKey() != originalTokenKey {
		t.Fatal("superuser token key changed despite an unchanged password")
	}
}

// TestCleanupEnumValues covers the startup cleanup (US-37): a value kept
// because a document uses it, an unused value dropped, the dummy
// placeholder kept only while alone, and the template field forced to the
// configured template names.
func TestCleanupEnumValues(t *testing.T) {
	app := pocketbase.NewWithConfig(pocketbase.Config{DefaultDataDir: t.TempDir()})
	if err := app.Bootstrap(); err != nil {
		t.Fatalf("bootstrap PocketBase: %v", err)
	}
	defer app.ClearBootstrap()

	fields := []config.Field{
		{Name: "documentType", Type: "enum"},
		{Name: "template", Type: "enum"},
	}
	cfg := &config.Config{
		Fields: map[string]config.Field{
			"documentType": fields[0],
			"template":     fields[1],
		},
		FieldList: fields,
		Templates: map[string]*config.CompiledTemplate{
			"default": {},
		},
	}
	if err := Setup(app, "admin@example.net", "password-123456", cfg); err != nil {
		t.Fatalf("setup: %v", err)
	}

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
