// Package pbapp sets up the PocketBase collections, superuser account and
// request hooks (US-03, US-04, US-22, US-23).
package pbapp

import (
	"errors"
	"fmt"

	"ganigeorgiev.com/pocketbase/pocketbase/core"
	"github.com/napnap75/archivist/internal/config"
)

const eventsName = "document_events"

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
	// Startup cleanup of the official enum value lists (enums.go).
	CleanupEnumValues(app, cfg)
	return nil
}

// ensureDocuments creates or checks the documents collection: status,
// file_key, then one column per declared field (US-03). The migration
// policy is explicit refusal: any deviation from the startup shape makes
// the application refuse to start rather than guess.
func ensureDocuments(app core.App, cfg *config.Config) error {
	coll, err := app.FindCollectionByNameOrId("documents")
	created := false
	if err != nil {
		coll = core.NewBaseCollection("documents")
		created = true
	}

	// Fixed schema: status and file_key (US-03).
	status := coll.Fields.GetByName("status")
	if status == nil {
		status = &core.SelectField{Name: "status", MaxSelect: 1, Values: []string{"pending", "processed", "validated"}}
		coll.Fields.Add(status)
	} else if status, ok := status.(*core.SelectField); !ok {
		return errors.New("field status exists with a different type; refusing to start")
	}

	fileKey := coll.Fields.GetByName("file_key")
	if fileKey == nil {
		fileKey = &core.TextField{Name: "file_key"}
		coll.Fields.Add(fileKey)
	} else if _, ok := fileKey.(*core.TextField); !ok {
		return errors.New("field file_key exists with a different type; refusing to start")
	}

	// One column per declared field, with the type mapping US-03.
	for _, f := range cfg.FieldList {
		var newField core.Field
		switch f.Type {
		case "string":
			newField = &core.TextField{Name: f.Name}
		case "date":
			newField = &core.DateField{Name: f.Name}
		case "boolean":
			newField = &core.BoolField{Name: f.Name}
		case "enum":
			// Official select fields: the application manages the value
			// lists itself (enums.go) - creation with the placeholder,
			// values added as documents are processed or from the
			// dashboard, cleanup at startup.
			newField = &core.SelectField{Name: f.Name, MaxSelect: 1, Values: enumVocabulary(cfg, f.Name)}
		default:
			return fmt.Errorf("field %s has unsupported type %s", f.Name, f.Type)
		}
		existing := coll.Fields.GetByName(f.Name)
		if existing == nil {
			coll.Fields.Add(newField)
		} else if existing.Type() != newField.Type() {
			return fmt.Errorf("field %s changed type (%s to %s); refusing to start", f.Name, existing.Type(), newField.Type())
		}
	}

	if created {
		return app.Save(coll)
	}
	return nil
}

// ensureEvents creates the append-only events collection (US-04).
func ensureEvents(app core.App) error {
	if _, err := app.FindCollectionByNameOrId(eventsName); err == nil {
		return nil
	}
	coll := core.NewBaseCollection(eventsName)
	for _, field := range []core.Field{
		&core.TextField{Name: "document"},
		&core.TextField{Name: "event_type"},
		&core.JSONField{Name: "payload"},
		&core.AutodateField{Name: "timestamp", OnCreate: true},
	} {
		coll.Fields.Add(field)
	}
	return app.Save(coll)
}

// ensureSuperuser upserts the administrator account (US-01).
func ensureSuperuser(app core.App, email, password string) error {
	superusers, err := app.FindCollectionByNameOrId(core.CollectionNameSuperusers)
	if err != nil {
		return err
	}
	existing, err := app.FindAuthRecordByEmail(superusers, email)
	if err == nil && existing != nil {
		return nil
	}
	record := core.NewRecord(superusers)
	record.Email = email
	if err := record.SetPassword(password); err != nil {
		return err
	}
	return app.Save(record)
}
