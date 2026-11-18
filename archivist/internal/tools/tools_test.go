package tools

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
	"strings"
	"testing"
	"time"

	"ganigeorgiev.com/pocketbase/pocketbase"
	"ganigeorgiev.com/pocketbase/pocketbase/core"
	"github.com/napnap75/archivist/internal/config"
	"github.com/napnap75/archivist/internal/pbapp"
	"github.com/napnap75/archivist/internal/storage"
)

// fakeS3 is an in-memory bucket for the tool tests.
type fakeS3 struct {
	objects  map[string][]byte
	prefixes storage.Prefixes
}

func (f *fakeS3) Prefixes() storage.Prefixes {
	if f.prefixes == (storage.Prefixes{}) {
		return storage.DefaultPrefixes
	}
	return f.prefixes
}

func (f *fakeS3) ListReconciliationPDFs(ctx context.Context) ([]storage.InboxObject, error) {
	var keys []string
	for k := range f.objects {
		if !f.Prefixes().IsReconciliationKey(k) {
			continue
		}
		keys = append(keys, k)
	}
	sort.Strings(keys)
	out := make([]storage.InboxObject, 0, len(keys))
	for _, k := range keys {
		out = append(out, storage.InboxObject{Key: k, Size: int64(len(f.objects[k]))})
	}
	return out, nil
}

func (f *fakeS3) Hash(ctx context.Context, key string) (string, error) {
	data, ok := f.objects[key]
	if !ok {
		return "", fmt.Errorf("no such key %q", key)
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:]), nil
}

func (f *fakeS3) Exists(ctx context.Context, key string) bool {
	_, ok := f.objects[key]
	return ok
}

func (f *fakeS3) Copy(ctx context.Context, srcKey, dstKey string) error {
	data, ok := f.objects[srcKey]
	if !ok {
		return fmt.Errorf("no such key %q", srcKey)
	}
	f.objects[dstKey] = data
	return nil
}

func (f *fakeS3) Remove(ctx context.Context, key string) error {
	delete(f.objects, key)
	return nil
}

func (f *fakeS3) ResolveConflict(ctx context.Context, desired string) (string, error) {
	if !f.Exists(ctx, desired) {
		return desired, nil
	}
	for n := 1; n < 1000; n++ {
		ext := ""
		if i := strings.LastIndex(desired, "."); i >= 0 {
			ext = desired[i:]
		}
		candidate := fmt.Sprintf("%s (%d)%s", strings.TrimSuffix(desired, ext), n, ext)
		if !f.Exists(ctx, candidate) {
			return candidate, nil
		}
	}
	return "", fmt.Errorf("cannot resolve conflict on %q", desired)
}

func newTestApp(t *testing.T) core.App {
	t.Helper()
	app := pocketbase.NewWithConfig(pocketbase.Config{DefaultDataDir: t.TempDir()})
	if err := app.Bootstrap(); err != nil {
		t.Fatalf("bootstrap PocketBase: %v", err)
	}
	t.Cleanup(func() { app.ClearBootstrap() })
	cfg := &config.Config{
		Fields: map[string]config.Field{
			"documentType": {Name: "documentType", Type: "text"},
			"template":     {Name: "template", Type: "text"},
		},
		FieldList: []config.Field{
			{Name: "documentType", Type: "text"},
			{Name: "template", Type: "text"},
		},
	}
	if err := pbapp.Setup(app, "admin@example.net", "password-123456", cfg); err != nil {
		t.Fatalf("setup: %v", err)
	}
	return app
}

func addDocument(t *testing.T, app core.App, sha, fileKey string) *core.Record {
	t.Helper()
	coll, err := app.FindCollectionByNameOrId("documents")
	if err != nil {
		t.Fatalf("find documents: %v", err)
	}
	rec := core.NewRecord(coll)
	rec.Set("sha256", sha)
	rec.Set("file_key", fileKey)
	rec.Set("status", "processed")
	if err := app.Save(rec); err != nil {
		t.Fatalf("save document: %v", err)
	}
	return rec
}

func runScan(t *testing.T, app core.App, s3 S3Store) *Job {
	return runScanMode(t, app, s3, false)
}

func runScanMode(t *testing.T, app core.App, s3 S3Store, dryRun bool) *Job {
	t.Helper()
	job := &Job{
		Tool:      "find-lost-files",
		Status:    StatusRunning,
		StartedAt: time.Now().UTC(),
		Summary:   map[string]int{"scanned": 0, "ok": 0, "corrected": 0, "moved": 0, "duplicates": 0, "errors": 0},
		DryRun:    dryRun,
	}
	if err := runFindLostFiles(context.Background(), app, s3, job, dryRun); err != nil {
		t.Fatalf("runFindLostFiles: %v", err)
	}
	return job
}

func TestFindLostFilesInPlace(t *testing.T) {
	app := newTestApp(t)
	s3 := &fakeS3{objects: map[string][]byte{"files/a.pdf": []byte("content-a")}}
	addDocument(t, app, mustHash("content-a"), "files/a.pdf")

	job := runScan(t, app, s3)
	if job.Summary["ok"] != 1 || job.Summary["corrected"] != 0 || job.Summary["moved"] != 0 {
		t.Fatalf("summary = %v, want ok=1", job.Summary)
	}
	if len(job.Missing) != 0 {
		t.Fatalf("missing = %v, want empty", job.Missing)
	}
	if _, ok := s3.objects["files/a.pdf"]; !ok {
		t.Fatalf("object moved: %v", s3.objects)
	}
}

func TestFindLostFilesRelocated(t *testing.T) {
	app := newTestApp(t)
	s3 := &fakeS3{objects: map[string][]byte{"files/new/a.pdf": []byte("content-a")}}
	rec := addDocument(t, app, mustHash("content-a"), "files/old/a.pdf")

	job := runScan(t, app, s3)
	if job.Summary["corrected"] != 1 {
		t.Fatalf("summary = %v, want corrected=1", job.Summary)
	}
	fresh, err := app.FindRecordById("documents", rec.Id)
	if err != nil {
		t.Fatal(err)
	}
	if got := fresh.GetString("file_key"); got != "files/new/a.pdf" {
		t.Fatalf("file_key = %q, want files/new/a.pdf", got)
	}
	events, err := app.FindAllRecords("document_events")
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 1 {
		t.Fatalf("events = %d, want 1", len(events))
	}
	if events[0].GetString("event_type") != "relocated" {
		t.Fatalf("event_type = %q, want relocated", events[0].GetString("event_type"))
	}
	if _, ok := s3.objects["files/new/a.pdf"]; !ok {
		t.Fatalf("object should not move: %v", s3.objects)
	}
}

func TestFindLostFilesUnknownGoesToInbox(t *testing.T) {
	app := newTestApp(t)
	s3 := &fakeS3{objects: map[string][]byte{"files/unknown.pdf": []byte("content-x")}}

	job := runScan(t, app, s3)
	if job.Summary["moved"] != 1 {
		t.Fatalf("summary = %v, want moved=1", job.Summary)
	}
	if _, ok := s3.objects["inbox/unknown.pdf"]; !ok {
		t.Fatalf("object not in inbox: %v", s3.objects)
	}
}

func TestFindLostFilesStrayDuplicateLeftInPlace(t *testing.T) {
	app := newTestApp(t)
	s3 := &fakeS3{objects: map[string][]byte{
		"files/orig.pdf":  []byte("content-a"),
		"files/stray.pdf": []byte("content-a"),
	}}
	addDocument(t, app, mustHash("content-a"), "files/orig.pdf")

	job := runScan(t, app, s3)
	if job.Summary["duplicates"] != 1 || job.Summary["ok"] != 1 {
		t.Fatalf("summary = %v, want duplicates=1 ok=1", job.Summary)
	}
	if _, ok := s3.objects["files/stray.pdf"]; !ok {
		t.Fatalf("stray copy should stay: %v", s3.objects)
	}
}

func TestFindLostFilesMissingReported(t *testing.T) {
	app := newTestApp(t)
	s3 := &fakeS3{objects: map[string][]byte{}}
	addDocument(t, app, mustHash("content-a"), "files/gone.pdf")

	job := runScan(t, app, s3)
	if len(job.Missing) != 1 || job.Missing[0] != "files/gone.pdf" {
		t.Fatalf("missing = %v, want [files/gone.pdf]", job.Missing)
	}
	if job.Summary["missing"] != 1 {
		t.Fatalf("summary = %v, want missing=1", job.Summary)
	}
}

func TestFindLostFilesInboxExcluded(t *testing.T) {
	app := newTestApp(t)
	s3 := &fakeS3{objects: map[string][]byte{
		"inbox/upload.pdf": []byte("content-inbox"),
		"files/a.pdf":      []byte("content-a"),
	}}
	addDocument(t, app, mustHash("content-a"), "files/a.pdf")
	addDocument(t, app, mustHash("content-inbox"), "inbox/upload.pdf")

	job := runScan(t, app, s3)
	if job.Summary["scanned"] != 1 {
		t.Fatalf("summary = %v, want scanned=1 (inbox excluded)", job.Summary)
	}
	if len(job.Missing) != 0 {
		t.Fatalf("missing = %v, want empty (inbox records are the poller's business)", job.Missing)
	}
	if _, ok := s3.objects["inbox/upload.pdf"]; !ok {
		t.Fatalf("inbox object should stay: %v", s3.objects)
	}
}

func TestFindLostFilesScopesToPDFsInPendingAndFiles(t *testing.T) {
	app := newTestApp(t)
	s3 := &fakeS3{objects: map[string][]byte{
		"pending/subdir/review.PDF": []byte("pending-content"),
		"files/subdir/archived.pdf": []byte("files-content"),
		"archive/other.pdf":         []byte("outside-content"),
		"files/readme.txt":          []byte("text-content"),
		"inbox/upload.pdf":          []byte("inbox-content"),
	}}
	addDocument(t, app, mustHash("pending-content"), "pending/subdir/review.PDF")
	addDocument(t, app, mustHash("files-content"), "files/subdir/archived.pdf")
	addDocument(t, app, mustHash("outside-content"), "archive/other.pdf")
	addDocument(t, app, mustHash("text-content"), "files/readme.txt")
	addDocument(t, app, mustHash("inbox-content"), "inbox/upload.pdf")

	job := runScan(t, app, s3)
	if job.Total != 2 || job.Processed != 2 || job.Summary["scanned"] != 2 || job.Summary["ok"] != 2 {
		t.Fatalf("total=%d processed=%d summary=%v, want only two eligible PDFs scanned", job.Total, job.Processed, job.Summary)
	}
	if len(job.Missing) != 0 {
		t.Fatalf("missing = %v, want empty", job.Missing)
	}
	for _, key := range []string{"archive/other.pdf", "files/readme.txt", "inbox/upload.pdf"} {
		if _, ok := s3.objects[key]; !ok {
			t.Errorf("ignored object %q was changed or removed", key)
		}
	}
}

func TestEventTypesIncludeRelocated(t *testing.T) {
	app := newTestApp(t)
	coll, err := app.FindCollectionByNameOrId("document_events")
	if err != nil {
		t.Fatal(err)
	}
	field, ok := coll.Fields.GetByName("event_type").(*core.SelectField)
	if !ok {
		t.Fatalf("event_type is not a select field")
	}
	for _, v := range []string{"imported", "corrected", "validated", "relocated"} {
		found := false
		for _, value := range field.Values {
			if value == v {
				found = true
			}
		}
		if !found {
			t.Errorf("event_type values %v miss %q", field.Values, v)
		}
	}
}

func mustHash(content string) string {
	sum := sha256.Sum256([]byte(content))
	return hex.EncodeToString(sum[:])
}

func TestFindLostFilesCustomPrefixes(t *testing.T) {
	app := newTestApp(t)
	prefixes := storage.Prefixes{Inbox: "in/", Pending: "wait/", Files: ""}
	s3 := &fakeS3{
		objects:  map[string][]byte{"stray.pdf": []byte("content-x")},
		prefixes: prefixes,
	}

	job := runScan(t, app, s3)

	if _, ok := s3.objects["in/stray.pdf"]; !ok {
		t.Fatalf("unknown object should move to in/stray.pdf: %v", s3.objects)
	}
	if _, ok := s3.objects["stray.pdf"]; ok {
		t.Fatal("original object should be gone")
	}
	if job.Summary["moved"] != 1 {
		t.Fatalf("moved = %d, want 1", job.Summary["moved"])
	}
}

// refileConfig compiles the configuration the refile tests run against:
// a text documentType field and a default template placing files under
// Divers/.
const refileConfigSource = `{
  "fields": [
    { "name": "documentType", "french": "Type", "type": "text", "mandatory": false },
    { "name": "template", "french": "Modèle", "type": "text", "mandatory": false }
  ],
  "rules": [],
  "templates": { "default": "Divers/{documentType}.pdf" }
}`

func refileManager(t *testing.T) *config.Manager {
	t.Helper()
	cfg, err := config.LoadSource([]byte(refileConfigSource))
	if err != nil {
		t.Fatalf("load refile config: %v", err)
	}
	return config.NewManager(cfg, []byte(refileConfigSource))
}

func addFiledDocument(t *testing.T, app core.App, objectKey string, documentType string, status string, template string) *core.Record {
	t.Helper()
	rec := addDocument(t, app, mustHash(documentType+"@"+objectKey), objectKey)
	rec.Set("documentType", documentType)
	if template != "" {
		rec.Set("template", template)
	}
	rec.Set("status", status)
	if err := app.Save(rec); err != nil {
		t.Fatalf("save document %s: %v", objectKey, err)
	}
	return rec
}

func runRefile(t *testing.T, app core.App, s3 S3Store, manager ConfigStore) *Job {
	return runRefileMode(t, app, s3, manager, false)
}

func runRefileMode(t *testing.T, app core.App, s3 S3Store, manager ConfigStore, dryRun bool) *Job {
	t.Helper()
	job := &Job{
		Tool:      "refile-all",
		Status:    StatusRunning,
		StartedAt: time.Now().UTC(),
		Summary:   map[string]int{"scanned": 0, "unchanged": 0, "moved": 0, "skipped": 0, "errors": 0},
		DryRun:    dryRun,
	}
	if err := runRefileAll(context.Background(), app, s3, manager, job, dryRun); err != nil {
		t.Fatalf("runRefileAll: %v", err)
	}
	return job
}

func freshRecord(t *testing.T, app core.App, id string) *core.Record {
	t.Helper()
	rec, err := app.FindRecordById("documents", id)
	if err != nil {
		t.Fatalf("find record %s: %v", id, err)
	}
	return rec
}

func TestRefileAllRenamesDocuments(t *testing.T) {
	app := newTestApp(t)
	manager := refileManager(t)
	s3 := &fakeS3{objects: map[string][]byte{"files/old/a.pdf": []byte("content-a")}}
	rec := addFiledDocument(t, app, "files/old/a.pdf", "Facture", "processed", "")

	job := runRefile(t, app, s3, manager)

	want := "files/Divers/Facture.pdf"
	if _, ok := s3.objects[want]; !ok {
		t.Fatalf("object should move to %s: %v", want, s3.objects)
	}
	if _, ok := s3.objects["files/old/a.pdf"]; ok {
		t.Fatal("old object should be gone")
	}
	got := freshRecord(t, app, rec.Id)
	if got.GetString("file_key") != want {
		t.Fatalf("file_key = %q, want %q", got.GetString("file_key"), want)
	}
	if job.Summary["moved"] != 1 || job.Summary["unchanged"] != 0 {
		t.Fatalf("summary = %v, want moved=1 unchanged=0", job.Summary)
	}
	events, _ := app.FindAllRecords("document_events")
	if len(events) != 1 || events[0].GetString("event_type") != "relocated" {
		t.Fatalf("want one relocated event, got %d", len(events))
	}
}

func TestRefileAllFollowsTheConfiguredFilesPrefix(t *testing.T) {
	app := newTestApp(t)
	manager := refileManager(t)
	s3 := &fakeS3{
		objects:  map[string][]byte{"files/old/a.pdf": []byte("content-a")},
		prefixes: storage.Prefixes{Inbox: "in/", Pending: "wait/", Files: "archive/"},
	}
	addFiledDocument(t, app, "files/old/a.pdf", "Facture", "validated", "")

	job := runRefile(t, app, s3, manager)

	if _, ok := s3.objects["archive/Divers/Facture.pdf"]; !ok {
		t.Fatalf("object should move to archive/Divers/Facture.pdf: %v", s3.objects)
	}
	if job.Summary["moved"] != 1 {
		t.Fatalf("summary = %v, want moved=1", job.Summary)
	}
}

func TestRefileAllSkipsUnknownTemplate(t *testing.T) {
	app := newTestApp(t)
	manager := refileManager(t)
	s3 := &fakeS3{objects: map[string][]byte{"files/old/a.pdf": []byte("content-a")}}
	addFiledDocument(t, app, "files/old/a.pdf", "Facture", "processed", "gone")

	job := runRefile(t, app, s3, manager)

	if _, ok := s3.objects["files/old/a.pdf"]; !ok {
		t.Fatal("object with an unknown template must stay in place")
	}
	if job.Summary["skipped"] != 1 || job.Summary["moved"] != 0 {
		t.Fatalf("summary = %v, want skipped=1 moved=0", job.Summary)
	}
	if len(job.Skipped) != 1 || !strings.Contains(job.Skipped[0], "gone") {
		t.Fatalf("skipped = %v, want one entry naming the template", job.Skipped)
	}
}

func TestRefileAllLeavesPendingAlone(t *testing.T) {
	app := newTestApp(t)
	manager := refileManager(t)
	s3 := &fakeS3{objects: map[string][]byte{"files/old/a.pdf": []byte("content-a")}}
	addFiledDocument(t, app, "files/old/a.pdf", "Facture", "pending", "")

	job := runRefile(t, app, s3, manager)

	if _, ok := s3.objects["files/Divers/Facture.pdf"]; ok {
		t.Fatal("a pending document must never be re-filed")
	}
	if job.Total != 0 || job.Summary["scanned"] != 0 {
		t.Fatalf("total = %d scanned = %d, want 0/0", job.Total, job.Summary["scanned"])
	}
}

func TestRefileAllCountsUnchanged(t *testing.T) {
	app := newTestApp(t)
	manager := refileManager(t)
	s3 := &fakeS3{objects: map[string][]byte{"files/Divers/Facture.pdf": []byte("content-a")}}
	addFiledDocument(t, app, "files/Divers/Facture.pdf", "Facture", "processed", "")

	job := runRefile(t, app, s3, manager)

	if job.Summary["unchanged"] != 1 || job.Summary["moved"] != 0 {
		t.Fatalf("summary = %v, want unchanged=1 moved=0", job.Summary)
	}
	events, _ := app.FindAllRecords("document_events")
	if len(events) != 0 {
		t.Fatalf("no event expected, got %d", len(events))
	}
}

func TestRefileAllDryRunPlansWithoutTouchingAnything(t *testing.T) {
	app := newTestApp(t)
	manager := refileManager(t)
	s3 := &fakeS3{objects: map[string][]byte{"files/old/a.pdf": []byte("a")}}
	rec := addFiledDocument(t, app, "files/old/a.pdf", "Facture", "processed", "")

	job := runRefileMode(t, app, s3, manager, true)

	if _, ok := s3.objects["files/old/a.pdf"]; !ok {
		t.Fatalf("dry-run must leave the object in place: %v", s3.objects)
	}
	if _, ok := s3.objects["files/Divers/Facture.pdf"]; ok {
		t.Fatalf("dry-run must not move anything: %v", s3.objects)
	}
	if got := freshRecord(t, app, rec.Id).GetString("file_key"); got != "files/old/a.pdf" {
		t.Fatalf("file_key changed by dry-run: %q", got)
	}
	if events, _ := app.FindAllRecords("document_events"); len(events) != 0 {
		t.Fatalf("no event expected from a dry-run, got %d", len(events))
	}
	if job.Summary["moved"] != 1 || job.Summary["unchanged"] != 0 || job.Summary["scanned"] != 1 {
		t.Fatalf("summary = %v, want scanned=1 moved=1 unchanged=0", job.Summary)
	}
	want := "files/old/a.pdf → files/Divers/Facture.pdf"
	if len(job.Planned) != 1 || job.Planned[0] != want {
		t.Fatalf("planned = %v, want [%q]", job.Planned, want)
	}
}

func TestRefileAllDryRunReportsCollision(t *testing.T) {
	app := newTestApp(t)
	manager := refileManager(t)
	s3 := &fakeS3{objects: map[string][]byte{
		"files/old/a.pdf":          []byte("a"),
		"files/Divers/Facture.pdf": []byte("already there"),
	}}
	rec := addFiledDocument(t, app, "files/old/a.pdf", "Facture", "validated", "")

	job := runRefileMode(t, app, s3, manager, true)

	if _, ok := s3.objects["files/Divers/Facture (1).pdf"]; ok {
		t.Fatal("dry-run must not resolve the collision by writing")
	}
	if _, ok := s3.objects["files/old/a.pdf"]; !ok {
		t.Fatal("dry-run must leave the object in place")
	}
	if got := freshRecord(t, app, rec.Id).GetString("file_key"); got != "files/old/a.pdf" {
		t.Fatalf("file_key changed by dry-run: %q", got)
	}
	if job.Summary["moved"] != 1 {
		t.Fatalf("summary = %v, want moved=1", job.Summary)
	}
	want := "files/old/a.pdf → files/Divers/Facture (1).pdf (collision : suffixe de conflit appliqué)"
	if len(job.Planned) != 1 || job.Planned[0] != want {
		t.Fatalf("planned = %v, want [%q]", job.Planned, want)
	}
}

func TestFindLostFilesDryRunPlansWithoutTouchingAnything(t *testing.T) {
	app := newTestApp(t)
	s3 := &fakeS3{objects: map[string][]byte{
		"files/new/a.pdf":   []byte("content-a"),
		"files/unknown.pdf": []byte("content-x"),
	}}
	rec := addDocument(t, app, mustHash("content-a"), "files/old/a.pdf")

	job := runScanMode(t, app, s3, true)

	if len(s3.objects) != 2 {
		t.Fatalf("dry-run must not move any object: %v", s3.objects)
	}
	if _, ok := s3.objects["inbox/unknown.pdf"]; ok {
		t.Fatal("unknown object must stay in place in dry-run")
	}
	if got := freshRecord(t, app, rec.Id).GetString("file_key"); got != "files/old/a.pdf" {
		t.Fatalf("file_key changed by dry-run: %q", got)
	}
	if events, _ := app.FindAllRecords("document_events"); len(events) != 0 {
		t.Fatalf("no event expected from a dry-run, got %d", len(events))
	}
	if job.Summary["corrected"] != 1 || job.Summary["moved"] != 1 {
		t.Fatalf("summary = %v, want corrected=1 moved=1", job.Summary)
	}
	if len(job.Missing) != 1 || job.Missing[0] != "files/old/a.pdf" {
		t.Fatalf("missing = %v, want [files/old/a.pdf]", job.Missing)
	}
}
