package pbapp

import (
	"testing"

	"ganigeorgiev.com/pocketbase/pocketbase"
	"github.com/napnap75/archivist/internal/config"
)

func TestEnsureDocumentsUsesTextForFreeFormEnums(t *testing.T) {
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
	if err := ensureDocuments(app, cfg); err != nil {
		t.Fatalf("ensure documents: %v", err)
	}

	coll, err := app.FindCollectionByNameOrId("documents")
	if err != nil {
		t.Fatalf("find documents collection: %v", err)
	}
	for _, name := range []string{"documentType", "bankAccount"} {
		field := coll.Fields.GetByName(name)
		if field == nil || field.Type() != "text" {
			t.Errorf("field %q = %v, want text field", name, field)
		}
	}
}
