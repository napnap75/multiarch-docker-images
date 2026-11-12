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
	objects map[string][]byte
}

func (f *fakeS3) ListAll(ctx context.Context) ([]storage.InboxObject, error) {
	var keys []string
	for k := range f.objects {
		if strings.HasPrefix(k, "inbox/") {
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
		},
		FieldList: []config.Field{{Name: "documentType", Type: "text"}},
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
	t.Helper()
	job := &Job{
		Tool:      "find-lost-files",
		Status:    StatusRunning,
		StartedAt: time.Now().UTC(),
		Summary:   map[string]int{"scanned": 0, "ok": 0, "corrected": 0, "moved": 0, "duplicates": 0, "errors": 0},
	}
	if err := runFindLostFiles(context.Background(), app, s3, job); err != nil {
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
