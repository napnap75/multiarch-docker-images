package pbapp

import (
	"encoding/json"
	"fmt"
	"log"
	"time"

	"github.com/napnap75/archivist/internal/config"
	"github.com/napnap75/archivist/internal/storage"
	"github.com/pocketbase/pocketbase/core"
)

// BindHooks registers the request hooks (US-04, US-22, US-23).
func BindHooks(app *core.App, cfg *config.Config, s3 *storage.Client) {
	// US-04: document_events is append-only for everyone, superusers included.
	app.OnRecordUpdateRequest(eventsName).PreBindFunc(func(e *core.RecordRequestEvent) error {
		return errAppendOnly
	})
	app.OnRecordDeleteRequest(eventsName).PreBindFunc(func(e *core.RecordRequestEvent) error {
		return errAppendOnly
	})

	// US-22 / US-23: corrections and validations through the standard UI.
	app.OnRecordUpdateRequest("documents").PreBindFunc(func(e *core.RecordRequestEvent) error {
		return handleDocumentUpdate(app, cfg, s3, e)
	})
}

func handleDocumentUpdate(app *core.App, cfg *config.Config, s3 *storage.Client, e *core.RecordRequestEvent) error {
	newRec := e.Record

	// The original state, before the submitted data is persisted.
	orig, err := app.FindRecordById("documents", newRec.Id)
	if err != nil {
		return err
	}
	oldStatus := orig.GetString("status")
	newStatus := newRec.GetString("status")
	fieldsChanged := false
	for _, f := range cfg.FieldList {
		if fmt.Sprintf("%v", orig.Get(f.Name)) != fmt.Sprintf("%v", newRec.Get(f.Name)) {
			fieldsChanged = true
			break
		}
	}

	if !fieldsChanged {
		// US-23: direct validation, processed -> validated, no field change.
		if newStatus == "validated" && oldStatus == "processed" {
			before := Snapshot{"status": oldStatus}
			after := Snapshot{"status": "validated"}
			if err := AppendEvent(app, newRec.Id, "validated", before, after, nil); err != nil {
				return err
			}
			return e.Next()
		}
		if newStatus != oldStatus {
			return fmt.Errorf("refused: the only direct status change is processed -> validated; other changes go through a field correction")
		}
		return e.Next()
	}

	// US-22: a field correction. Whatever the previous status, the document
	// becomes validated after a successful re-filing.
	fields := FieldsFromRecord(cfg, newRec)
	tplName := "default"
	if v, ok := fields["template"].(string); ok && v != "" {
		tplName = v
	}
	tpl, ok := cfg.Templates[tplName]
	if !ok {
		return fmt.Errorf("refused: unknown template %q", tplName)
	}
	rendered, err := tpl.Render(fields, nil)
	if err != nil {
		return fmt.Errorf("refused: %v", err)
	}

	oldKey := orig.GetString("file_key")
	target := "files/" + rendered
	finalKey := oldKey
	if target != oldKey {
		finalKey, err = s3.ResolveConflict(e.Request.Context(), target)
		if err != nil {
			return err
		}
		if err := s3.Copy(e.Request.Context(), oldKey, finalKey); err != nil {
			return fmt.Errorf("refused: re-filing failed: %v", err)
		}
		if err := s3.Remove(e.Request.Context(), oldKey); err != nil {
			// Roll back the partial copy so the S3 state matches the record.
			_ = s3.Remove(e.Request.Context(), finalKey)
			return fmt.Errorf("refused: re-filing failed: %v", err)
		}
	}
	newRec.Set("file_key", finalKey)
	newRec.Set("status", "validated")

	before := SnapshotFromRecord(cfg, orig, true)
	after := SnapshotFromRecord(cfg, newRec, true)
	if err := AppendEvent(app, newRec.Id, "corrected", before, after, nil); err != nil {
		log.Printf("corrected event for %s: %v", newRec.Id, err)
	}
	return e.Next()
}

// FieldsFromRecord reads the declared field values with their native types.
func FieldsFromRecord(cfg *config.Config, record *core.Record) map[string]any {
	out := map[string]any{}
	for _, f := range cfg.FieldList {
		switch f.Type {
		case "date":
			if !record.GetDateTime(f.Name).IsZero() {
				out[f.Name] = record.GetDateTime(f.Name).Time()
			}
		case "number":
			if record.GetFloat(f.Name) != 0 {
				out[f.Name] = record.GetFloat(f.Name)
			}
		case "boolean":
			if record.GetBool(f.Name) {
				out[f.Name] = true
			}
		default:
			if s := record.GetString(f.Name); s != "" {
				out[f.Name] = s
			}
		}
	}
	return out
}

// Snapshot is one before/after state of a document (arch.md §4).
type Snapshot map[string]any

// SnapshotFromRecord builds a snapshot: file_key always, status when it is
// set, and the recognized declared fields.
func SnapshotFromRecord(cfg *config.Config, record *core.Record, withStatus bool) Snapshot {
	s := Snapshot{"file_key": record.GetString("file_key")}
	if withStatus {
		if st := record.GetString("status"); st != "" {
			s["status"] = st
		}
	}
	for name, v := range FieldsFromRecord(cfg, record) {
		s[name] = v
	}
	return s
}

// Payload is the self-contained event payload (arch.md §4).
type Payload struct {
	EventType string   `json:"event_type"`
	Timestamp string   `json:"timestamp"`
	Errors    []string `json:"errors,omitempty"`
	Before    Snapshot `json:"before"`
	After     Snapshot `json:"after"`
}

// AppendEvent appends one event to document_events.
func AppendEvent(app *core.App, documentId, eventType string, before, after Snapshot, errs []string) error {
	coll, err := app.FindCollectionByNameOrId(eventsName)
	if err != nil {
		return err
	}
	now := time.Now().UTC()
	p := Payload{
		EventType: eventType,
		Timestamp: now.Format(time.RFC3339),
		Errors:    errs,
		Before:    before,
		After:     after,
	}
	data, err := json.Marshal(p)
	if err != nil {
		return err
	}
	rec := core.NewRecord(coll)
	rec.Set("document", documentId)
	rec.Set("event_type", eventType)
	rec.Set("payload", string(data))
	rec.Set("timestamp", now)
	return app.Save(rec)
}

// AppendImportedEvent builds the imported event of US-16.
func AppendImportedEvent(app *core.App, documentId, fromKey, toKey string, valid bool, fields map[string]any, errs []string) error {
	before := Snapshot{"file_key": fromKey}
	after := Snapshot{"file_key": toKey}
	if valid {
		after["status"] = "processed"
	} else {
		after["status"] = "pending"
	}
	for k, v := range fields {
		after[k] = v
	}
	return AppendEvent(app, documentId, "imported", before, after, errs)
}
