// Package pbapp sets up the PocketBase collections, superuser account and
// request hooks (US-03, US-04, US-22, US-23).
package pbapp

import (
	"errors"
	"fmt"
	"slices"

	"ganigeorgiev.com/pocketbase/pocketbase/core"
	"github.com/napnap75/archivist/internal/config"
)

const eventsName = "document_events"

// EventTypes is the official event_type vocabulary. It grows with the
// application: existing collections are migrated at startup (ensureEvents).
var EventTypes = []string{"imported", "corrected", "validated", "relocated"}

// Setup ensures the collections and the superuser account (US-03, US-04, US-01).
func Setup(app core.App, adminEmail, adminPassword string, cfg *config.Config) error {
	if err := ensureDocuments(app, cfg); err != nil {
		return err
	}
	if err := ensureEvents(app); err != nil {
		return err
	}
	if err := ensureSuperuser(app, adminEmail, adminPassword); err != nil {
		return err
	}
	// Startup cleanup of the official enum value lists (US-37, enums.go).
	CleanupEnumValues(app, cfg)
	return nil
}

func ensureDocuments(app core.App, cfg *config.Config) error {
	coll, err := app.FindCollectionByNameOrId("documents")
	if err != nil {
		// First start: create the collection with its fixed columns (US-03).
		coll = core.NewBaseCollection("documents")
	}

	// Fixed columns (re)declared on every start, ordering preserved, plus
	// the standard PocketBase created/updated autodate columns for
	// convenience in the admin UI (US-03).
	coll.Fields.RemoveByName("sha256")
	coll.Fields.RemoveByName("file_key")
	coll.Fields.RemoveByName("pages")
	coll.Fields.RemoveByName("status")
	coll.Fields.RemoveByName("created")
	coll.Fields.RemoveByName("updated")
	coll.Fields.Add(
		&core.TextField{Name: "sha256"},
		&core.TextField{Name: "file_key"},
		&core.JSONField{Name: "pages"},
		&core.SelectField{Name: "status", MaxSelect: 1, Values: []string{"pending", "processed", "validated"}},
		&core.AutodateField{Name: "created", OnCreate: true},
		&core.AutodateField{Name: "updated", OnCreate: true, OnUpdate: true},
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
			// Official select fields (US-37): the application manages the
			// value lists itself (enums.go) - creation with the dummy
			// placeholder, or with the configured template names for the
			// special "template" field; values added as documents are
			// processed or from the dashboard; unused values cleaned up
			// at startup.
			newField = &core.SelectField{Name: f.Name, MaxSelect: 1, Values: enumVocabulary(cfg, f.Name)}
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
		if _, declared := cfg.Fields[f.GetName()]; !declared && !isFixed(f.GetName()) {
			app.Logger().Warn("documents: column removed", "field", f.GetName(), "reason", "removed from config")
			coll.Fields.RemoveByName(f.GetName())
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
	for _, f := range []string{"sha256", "file_key", "pages", "status", "created", "updated"} {
		if f == name {
			return true
		}
	}
	return false
}

func ensureEvents(app core.App) error {
	coll, err := app.FindCollectionByNameOrId(eventsName)
	if err == nil {
		return migrateEventTypes(app, coll)
	}
	docColl, err := app.FindCollectionByNameOrId("documents")
	if err != nil {
		return err
	}
	coll = core.NewBaseCollection(eventsName)
	coll.Fields.Add(
		&core.RelationField{Name: "document", CollectionId: docColl.Id, MaxSelect: 1, CascadeDelete: true},
		&core.SelectField{Name: "event_type", MaxSelect: 1, Values: EventTypes},
		&core.JSONField{Name: "payload"},
		&core.DateField{Name: "timestamp"},
		&core.AutodateField{Name: "created", OnCreate: true},
		&core.AutodateField{Name: "updated", OnCreate: true, OnUpdate: true},
	)
	// Append-only (US-04): API rules locked to superusers are not enough,
	// the standard admin UI uses a superuser; the request hooks in
	// BindHooks reject updates and deletes for everyone.
	if err := app.Save(coll); err != nil {
		return fmt.Errorf("save %s collection: %w", eventsName, err)
	}
	return nil
}

// migrateEventTypes extends the event_type select of an existing collection
// with the event types the current version emits (e.g. "relocated", added
// with the consistency tools); the values already present are kept.
func migrateEventTypes(app core.App, coll *core.Collection) error {
	field, ok := coll.Fields.GetByName("event_type").(*core.SelectField)
	if !ok || field == nil {
		return fmt.Errorf("%s: event_type is not a select field", eventsName)
	}
	changed := false
	for _, v := range EventTypes {
		if !slices.Contains(field.Values, v) {
			field.Values = append(field.Values, v)
			changed = true
		}
	}
	if !changed {
		return nil
	}
	if err := app.Save(coll); err != nil {
		return fmt.Errorf("migrate %s event types: %w", eventsName, err)
	}
	app.Logger().Info("document_events: event types extended", "values", field.Values)
	return nil
}

func ensureSuperuser(app core.App, email, password string) error {
	superusers, err := app.FindCollectionByNameOrId(core.CollectionNameSuperusers)
	if err != nil {
		return err
	}
	rec, err := app.FindFirstRecordByFilter(superusers.Name, "email = {:e}", map[string]any{"e": email})
	if err != nil || rec == nil {
		rec = core.NewRecord(superusers)
		rec.SetEmail(email)
	} else if rec.ValidatePassword(password) {
		return nil
	}
	rec.SetPassword(password)
	return app.Save(rec)
}

var errAppendOnly = errors.New("document_events is append-only: update and delete are refused")
