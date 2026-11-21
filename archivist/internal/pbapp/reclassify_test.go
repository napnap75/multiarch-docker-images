package pbapp

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"ganigeorgiev.com/pocketbase/pocketbase"
	"ganigeorgiev.com/pocketbase/pocketbase/core"
	"github.com/napnap75/archivist/internal/config"
	"github.com/napnap75/archivist/internal/storage"
)

var reclassifyConfigSource = `
{
  "fields": [
	{ "name": "template", "french": "Modèle de nommage", "type": "enum" },
	{ "name": "documentType", "french": "Type de document", "type": "text", "mandatory": true },
	{ "name": "extra", "french": "Champ optionnel", "type": "text" }
  ],
  "rules": [
    {
      "when": "pages[0] contains ~BON 2026~",
      "set": [
        { "field": "template", "value": "ticket" },
        { "field": "documentType", "value": "Ticket" }
      ]
    }
  ],
  "templates": {
	"ticket": "{documentType}/{documentType}.pdf",
	"default": "Divers/{documentType}.pdf"
  }
}`

var reclassifyConfig = strings.ReplaceAll(reclassifyConfigSource, "~", "`")

// reclassifyStore is an in-memory RefileStore for the tests.
type reclassifyStore struct {
	objects  map[string][]byte
	prefixes storage.Prefixes
}

func (f *reclassifyStore) Prefixes() storage.Prefixes {
	if f.prefixes == (storage.Prefixes{}) {
		return storage.DefaultPrefixes
	}
	return f.prefixes
}

func (f *reclassifyStore) Exists(ctx context.Context, key string) bool {
	_, ok := f.objects[key]
	return ok
}

func (f *reclassifyStore) Copy(ctx context.Context, srcKey, dstKey string) error {
	data, ok := f.objects[srcKey]
	if !ok {
		return fmt.Errorf("no such key %q", srcKey)
	}
	f.objects[dstKey] = data
	return nil
}

func (f *reclassifyStore) Remove(ctx context.Context, key string) error {
	delete(f.objects, key)
	return nil
}

func (f *reclassifyStore) ResolveConflict(ctx context.Context, desired string) (string, error) {
	if !f.Exists(ctx, desired) {
		return desired, nil
	}
	return "", fmt.Errorf("no conflict resolution in the test store")
}

// testReclassifyApp compiles the test configuration and bootstraps a real
// PocketBase app around it.
func testReclassifyApp(t *testing.T) (core.App, *config.Config) {
	t.Helper()
	cfg, err := config.LoadSource([]byte(reclassifyConfig))
	if err != nil {
		t.Fatalf("load config: %v", err)
	}
	app := pocketbase.NewWithConfig(pocketbase.Config{DefaultDataDir: t.TempDir()})
	if err := app.Bootstrap(); err != nil {
		t.Fatalf("bootstrap PocketBase: %v", err)
	}
	t.Cleanup(func() { app.ClearBootstrap() })
	if err := Setup(app, "admin@example.net", "password-123456", cfg); err != nil {
		t.Fatalf("setup: %v", err)
	}
	return app, cfg
}

// reclassifyDocument creates a documents record straight through the app
// (no request hooks involved, like every internal caller of the data model).
func reclassifyDocument(t *testing.T, app core.App, cfg *config.Config, fileKey, status string, fields map[string]any, pages []string) *core.Record {
	t.Helper()
	coll, err := app.FindCollectionByNameOrId("documents")
	if err != nil {
		t.Fatalf("find documents: %v", err)
	}
	rec := core.NewRecord(coll)
	rec.Set("sha256", fileKey)
	rec.Set("file_key", fileKey)
	rec.Set("status", status)
	pagesJSON := "[]"
	if pages != nil {
		pagesJSON = `["` + strings.Join(pages, `","`) + `"]`
	}
	rec.Set("pages", pagesJSON)
	for name, v := range fields {
		rec.Set(name, v)
	}
	if err := app.Save(rec); err != nil {
		t.Fatalf("save document: %v", err)
	}
	return rec
}

func reclassifyEvents(t *testing.T, app core.App, documentId string) []map[string]any {
	t.Helper()
	recs, err := app.FindRecordsByFilter("document_events", "document = {:d}", "", 200, 0, map[string]any{"d": documentId})
	if err != nil {
		t.Fatalf("find events: %v", err)
	}
	out := make([]map[string]any, 0, len(recs))
	for _, r := range recs {
		var p map[string]any
		if err := json.Unmarshal([]byte(r.GetString("payload")), &p); err != nil {
			t.Fatalf("decode payload: %v", err)
		}
		out = append(out, p)
	}
	return out
}

func TestReclassifyOneUpdatesFieldsMovesAndBackToProcessed(t *testing.T) {
	app, cfg := testReclassifyApp(t)
	store := &reclassifyStore{objects: map[string][]byte{"files/old/Vieux.pdf": []byte("content")}}
	rec := reclassifyDocument(t, app, cfg, "files/old/Vieux.pdf", "validated",
		map[string]any{"documentType": "Carnet", "extra": "Banane"}, []string{"BON 2026_shop"})

	res, err := ReclassifyOne(context.Background(), app, store, cfg, rec)
	if err != nil {
		t.Fatalf("reclassify: %v", err)
	}
	if res.Outcome != "updated" {
		t.Fatalf("outcome = %q, want updated", res.Outcome)
	}
	if res.FinalKey != "files/Ticket/Ticket.pdf" {
		t.Fatalf("FinalKey = %q, want files/Ticket/Ticket.pdf", res.FinalKey)
	}
	fresh, err := app.FindRecordById("documents", rec.Id)
	if err != nil {
		t.Fatalf("find record: %v", err)
	}
	if got := fresh.GetString("file_key"); got != "files/Ticket/Ticket.pdf" {
		t.Fatalf("file_key = %q, want files/Ticket/Ticket.pdf", got)
	}
	if got := fresh.GetString("status"); got != "processed" {
		t.Fatalf("status = %q, want processed", got)
	}
	if got := fresh.GetString("documentType"); got != "Ticket" {
		t.Fatalf("documentType = %q, want Ticket", got)
	}
	if got := fresh.GetString("template"); got != "ticket" {
		t.Fatalf("template = %q, want ticket", got)
	}
	// A declared field the rules no longer produce is unset.
	if got := fresh.GetString("extra"); got != "" {
		t.Fatalf("extra not cleared: %q", got)
	}
	if !store.Exists(context.Background(), "files/Ticket/Ticket.pdf") ||
		store.Exists(context.Background(), "files/old/Vieux.pdf") {
		t.Fatalf("objects = %v, want the move performed", mapKeys(store.objects))
	}

	events := reclassifyEvents(t, app, rec.Id)
	if len(events) != 1 {
		t.Fatalf("want exactly 1 corrected event, got %d", len(events))
	}
	ev := events[0]
	if ev["event_type"] != "corrected" {
		t.Fatalf("event_type = %v, want corrected", ev["event_type"])
	}
	before, _ := ev["before"].(map[string]any)
	after, _ := ev["after"].(map[string]any)
	if before["status"] != "validated" {
		t.Fatalf("before.status = %v, want validated", before["status"])
	}
	if after["status"] != "processed" {
		t.Fatalf("after.status = %v, want processed", after["status"])
	}
	if after["file_key"] != "files/Ticket/Ticket.pdf" || before["file_key"] != "files/old/Vieux.pdf" {
		t.Fatalf("file_key diff = %v -> %v", before["file_key"], after["file_key"])
	}
	if after["documentType"] != "Ticket" {
		t.Fatalf("after.documentType = %v, want Ticket", after["documentType"])
	}
	if _, cleared := before["extra"]; !cleared {
		t.Fatalf("extra not carried in the before diff: %v", before)
	}
	if _, set := after["extra"]; set {
		t.Fatalf("extra must be absent from the after diff: %v", after)
	}
}

func TestReclassifyOneSkipsOnErrorAndLeavesEverythingInPlace(t *testing.T) {
	app, cfg := testReclassifyApp(t)
	store := &reclassifyStore{objects: map[string][]byte{"files/old/Entre.pdf": []byte("content")}}
	rec := reclassifyDocument(t, app, cfg, "files/old/Entre.pdf", "validated",
		map[string]any{"documentType": "Carnet", "extra": "Banane"}, []string{"aucune regle ici"})

	res, err := ReclassifyOne(context.Background(), app, store, cfg, rec)
	if err != nil {
		t.Fatalf("reclassify: %v", err)
	}
	if res.Outcome != "skipped" || res.Reason == "" {
		t.Fatalf("outcome = %+v, want skipped with a reason", res)
	}
	fresh, err := app.FindRecordById("documents", rec.Id)
	if err != nil {
		t.Fatalf("find record: %v", err)
	}
	if got := fresh.GetString("file_key"); got != "files/old/Entre.pdf" {
		t.Fatalf("file_key changed: %q", got)
	}
	if got := fresh.GetString("status"); got != "validated" {
		t.Fatalf("status changed: %q", got)
	}
	if got := fresh.GetString("documentType"); got != "Carnet" {
		t.Fatalf("documentType changed: %q", got)
	}
	if got := fresh.GetString("extra"); got != "Banane" {
		t.Fatalf("extra changed: %q", got)
	}
	if !store.Exists(context.Background(), "files/old/Entre.pdf") || len(store.objects) != 1 {
		t.Fatalf("objects changed: %v", mapKeys(store.objects))
	}
	if events := reclassifyEvents(t, app, rec.Id); len(events) != 0 {
		t.Fatalf("a skipped re-classification must record no event, got %d", len(events))
	}
}

func TestReclassifyOneSecondRunIsUnchanged(t *testing.T) {
	app, cfg := testReclassifyApp(t)
	store := &reclassifyStore{objects: map[string][]byte{"files/old/Vieux.pdf": []byte("content")}}
	rec := reclassifyDocument(t, app, cfg, "files/old/Vieux.pdf", "processed",
		map[string]any{"documentType": "Carnet"}, []string{"BON 2026_shop"})

	if res, err := ReclassifyOne(context.Background(), app, store, cfg, rec); err != nil || res.Outcome != "updated" {
		t.Fatalf("first reclassify = %+v, %v", res, err)
	}
	events := reclassifyEvents(t, app, rec.Id)

	res, err := ReclassifyOne(context.Background(), app, store, cfg, rec)
	if err != nil {
		t.Fatalf("second reclassify: %v", err)
	}
	if res.Outcome != "unchanged" {
		t.Fatalf("outcome = %q, want unchanged", res.Outcome)
	}
	if still := reclassifyEvents(t, app, rec.Id); len(still) != len(events) {
		t.Fatalf("an unchanged re-classification must record no event, got %d new", len(still)-len(events))
	}
}

func mapKeys(m map[string][]byte) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
