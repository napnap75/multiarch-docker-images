// Package pbapp sets up the PocketBase collections, superuser account and
// request hooks (US-03, US-04, US-22, US-23).
package pbapp

import (
	"errors"
	"fmt"
	"log"

	"github.com/napnap75/archivist/internal/config"
	"github.com/pocketbase/pocketbase/core"
)

const eventsName = "document_events"

// Setup ensures the collections and the superuser account (US-03, US-04, US-01).
func Setup(app *core.App, adminEmail, adminPassword string, cfg *config.Config) error {
	if err := ensureDocuments(app, cfg); err != nil {
		return err
	}
	if err := ensureEvents(app); err != nil {
		return err
	}
	if err := ensureSuperuser(app, adminEmail, adminPassword); err != nil {
		return err
	}
	config.SetKnownFields(cfg.FieldList)
	return nil
}

func ensureDocuments(app *core.App, cfg *config.Config) error {
	coll, err := app.FindCollectionByNameOrId("documents")
	if err != nil {
		// First start: create the collection with its fixed columns (US-03).
		coll = core.NewBaseCollection("documents")
	}

	// Fixed columns (re)declared on every start, ordering preserved.
	coll.Fields.RemoveByName("sha256")
	coll.Fields.RemoveByName("file_key")
	coll.Fields.RemoveByName("pages")
	coll.Fields.RemoveByName("status")
	coll.Fields.Add(
		&core.TextField{Name: "sha256"},
		&core.TextField{Name: "file_key"},
		&core.JSONField{Name: "pages"},
		&core.SelectField{Name: "status", MaxSelect: 1, Values: []string{"pending", "processed", "validated"}},
	)

	// One column per declared field, mapped from the field type (US-03).
	for _, f := range cfg.FieldList {
		var newField core.Field
		switch f.Type {
		case "text":
			newField = &core.TextField{Name: f.Name}
		case "number":
			newField = &core.NumberField{Name: f.Name}
		case "date":
			newField = &core.DateField{Name: f.Name}
		case "boolean":
			newField = &core.BoolField{Name: f.Name}
		case "enum":
			// The vocabulary is an application notion (dashboard dropdown with
			// free entry), not a schema constraint: empty allowed values.
			newField = &core.SelectField{Name: f.Name, MaxSelect: 1}
		}
		if existing := coll.Fields.GetByName(f.Name); existing != nil {
			if existing.Type() != newField.Type() {
				return fmt.Errorf("field %s changed type from %s to %s: refusing to start",
					f.Name, existing.Type(), newField.Type())
			}
			continue
		}
		coll.Fields.Add(newField)
	}

	// Fields removed from the config are removed from the schema; values are
	// lost by design (they remain in the event snapshots).
	for _, f := range coll.Fields {
		if f.GetSystem() {
			continue
		}
		if _, declared := cfg.Fields[f.Name]; !declared && !isFixed(f.Name) {
			log.Printf("documents: removing column %q (removed from config)", f.Name)
			coll.Fields.RemoveByName(f.Name)
		}
	}

	if err := app.Save(coll); err != nil {
		return fmt.Errorf("save documents collection: %w", err)
	}
	if _, err := app.DB().NewQuery(
		"CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_sha256 ON documents (sha256)").Execute(); err != nil {
		return fmt.Errorf("sha256 unique index: %w", err)
	}
	return nil
}

func isFixed(name string) bool {
	for _, f := range []string{"sha256", "file_key", "pages", "status"} {
		if f == name {
			return true
		}
	}
	return false
}

func ensureEvents(app *core.App) error {
	if _, err := app.FindCollectionByNameOrId(eventsName); err == nil {
		return nil
	}
	docColl, err := app.FindCollectionByNameOrId("documents")
	if err != nil {
		return err
	}
	coll := core.NewBaseCollection(eventsName)
	coll.Fields.Add(
		&core.RelationField{Name: "document", CollectionId: docColl.Id, MaxSelect: 1, CascadeDelete: true},
		&core.SelectField{Name: "event_type", MaxSelect: 1, Values: []string{"imported", "corrected", "validated"}},
		&core.JSONField{Name: "payload"},
		&core.DateField{Name: "timestamp"},
	)
	// Append-only (US-04): API rules locked to superusers are not enough,
	// the standard admin UI uses a superuser; the request hooks in
	// BindHooks reject updates and deletes for everyone.
	if err := app.Save(coll); err != nil {
		return fmt.Errorf("save %s collection: %w", eventsName, err)
	}
	return nil
}

func ensureSuperuser(app *core.App, email, password string) error {
	superusers, err := app.FindCollectionByNameOrId(core.CollectionNameSuperusers)
	if err != nil {
		return err
	}
	rec, err := app.FindFirstRecordByFilter(superusers.Name, "email = {:e}", map[string]any{"e": email})
	if err != nil || rec == nil {
		rec = core.NewRecord(superusers)
		rec.SetEmail(email)
	}
	rec.SetPassword(password)
	return app.Save(rec)
}

var errAppendOnly = errors.New("document_events is append-only: update and delete are refused")
