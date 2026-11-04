package pbapp

import (
	"encoding/json"
	"fmt"
	"time"

	"ganigeorgiev.com/pocketbase/pocketbase/core"
	"github.com/napnap75/archivist/internal/config"
	"github.com/napnap75/archivist/internal/storage"
)

// BindHooks registers the request hooks (US-04, US-22, US-23, US-24).
func BindHooks(app core.App, cfg *config.Config, s3 *storage.Client) {
	// US-04: document_events is append-only for everyone, superusers included.
	app.OnRecordUpdateRequest(eventsName).BindFunc(func(e *core.RecordRequestEvent) error {
		return errAppendOnly
	})
	app.OnRecordDeleteRequest(eventsName).BindFunc(func(e *core.RecordRequestEvent) error {
		return errAppendOnly
	})

	// US-24: deleting a documents record deletes the underlying S3 object
	// first; on failure the deletion is refused and both the record and
	// the object are left in place. Like US-04, the hook binds to
	// superusers as well: API rules alone cannot enforce it.
	app.OnRecordDeleteRequest("documents").BindFunc(func(e *core.RecordRequestEvent) error {
		key := e.Record.GetString("file_key")
		if key == "" {
			return e.Next()
		}
		if err := s3.Remove(e.Request.Context(), key); err != nil {
			return fmt.Errorf("refused: S3 deletion of %s failed: %v", key, err)
		}
		return e.Next()
	})

	// US-22 / US-23: draft corrections and validations through the standard UI.
	app.OnRecordUpdateRequest("documents").BindFunc(func(e *core.RecordRequestEvent) error {
		return handleDocumentUpdate(app, cfg, e)
	})
}

// handleDocumentUpdate implements US-22 and US-23. A field edit from the
// standard UI is a draft correction: the values persist as they are, no
// template evaluation, no re-filing, no status change — the UI edits one
// field at a time and shows no preview of the resulting file name. The full
// correction (all fields, dry-run, re-filing, validation) is the custom
// dashboard's job (US-3x). The only direct status change is processed ->
// validated (US-23).
func handleDocumentUpdate(app core.App, cfg *config.Config, e *core.RecordRequestEvent) error {
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

	// US-23: the only direct status change is processed -> validated.
	if newStatus != oldStatus && (newStatus != "validated" || oldStatus != "processed") {
		return fmt.Errorf("refused: the only direct status change is processed -> validated; full corrections belong to the review dashboard")
	}

	// US-22: append the corrected event carrying the field diff alone;
	// file_key and status are absent since they did not change.
	if fieldsChanged {
		before, after := DiffSnapshots(SnapshotFromRecord(cfg, orig, false), SnapshotFromRecord(cfg, newRec, false))
		if len(before) > 0 || len(after) > 0 {
			if err := AppendEvent(app, newRec.Id, "corrected", before, after, nil); err != nil {
				app.Logger().Error("corrected event append failed", "document", newRec.Id, "error", err)
			}
		}
	}

	// US-23: the validated event, snapshots carrying the status change alone.
	if newStatus != oldStatus {
		before := Snapshot{"status": oldStatus}
		after := Snapshot{"status": "validated"}
		if err := AppendEvent(app, newRec.Id, "validated", before, after, nil); err != nil {
			return err
		}
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

// DiffSnapshots reduces two snapshots to the keys whose value differs
// between them: a key with the same value on both sides is omitted, a
// key present on one side only is kept on that side (added or deleted
// field). It is the section-4 payload rule: before/after carry only what
// the event changes.
func DiffSnapshots(before, after Snapshot) (Snapshot, Snapshot) {
	keys := map[string]bool{}
	for k := range before {
		keys[k] = true
	}
	for k := range after {
		keys[k] = true
	}
	db, da := Snapshot{}, Snapshot{}
	for k := range keys {
		bv, bok := before[k]
		av, aok := after[k]
		if bok && aok && fmt.Sprintf("%v", bv) == fmt.Sprintf("%v", av) {
			continue
		}
		if bok {
			db[k] = bv
		}
		if aok {
			da[k] = av
		}
	}
	return db, da
}

// Snapshot is one before/after state of a document (arch.md §4).
type Snapshot map[string]any

// SnapshotFromRecord builds the full snapshot of a record: file_key, status
// when set, and the recognized declared fields. AppendEvent reduces pairs of
// such snapshots to what actually changes.
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
func AppendEvent(app core.App, documentId, eventType string, before, after Snapshot, errs []string) error {
	coll, err := app.FindCollectionByNameOrId(eventsName)
	if err != nil {
		return err
	}
	now := time.Now().UTC()
	// The stored payload carries the event's diff only: unchanged keys
	// are dropped from both sides (arch.md section 4).
	before, after = DiffSnapshots(before, after)
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
func AppendImportedEvent(app core.App, documentId, fromKey, toKey string, valid bool, fields map[string]any, errs []string) error {
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
