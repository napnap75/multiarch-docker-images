// Package tools implements the dashboard's tools page: long-running
// maintenance tasks started by hand, each executed as a background job with
// live progress and a final summary.
package tools

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"ganigeorgiev.com/pocketbase/dbx"
	"ganigeorgiev.com/pocketbase/pocketbase/core"
	"github.com/napnap75/archivist/internal/pbapp"
	"github.com/napnap75/archivist/internal/storage"
)

// Descriptor is one tool as the tools page displays it: identity, title,
// description. The list is static; new tools are added here.
type Descriptor struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Description string `json:"description"`
}

var registry = []Descriptor{
	{
		ID:   "find-lost-files",
		Name: "Recherche des fichiers perdus",
		Description: "Parcourt tout le bucket (hors inbox/), recalcule l'empreinte sha256 de chaque objet et la compare à la base. " +
			"Un objet à sa place connue ne bouge pas ; un objet déplacé hors de l'application voit son file_key corrigé avec un événement relocated dans l'historique ; " +
			"un objet inconnu de la base est déplacé vers inbox/ pour être traité automatiquement ; " +
			"les enregistrements dont le fichier a disparu du bucket sont signalés dans le résumé, sans action automatique.",
	},
}

// Descriptors returns the tool list served to the dashboard.
func Descriptors() []Descriptor {
	return registry
}

func descriptor(id string) (Descriptor, bool) {
	for _, d := range registry {
		if d.ID == id {
			return d, true
		}
	}
	return Descriptor{}, false
}

// S3Store is the surface of the storage client the tools use; a seam kept
// for unit tests.
type S3Store interface {
	ListAll(ctx context.Context) ([]storage.InboxObject, error)
	Hash(ctx context.Context, key string) (string, error)
	Exists(ctx context.Context, key string) bool
	Copy(ctx context.Context, srcKey, dstKey string) error
	Remove(ctx context.Context, key string) error
	ResolveConflict(ctx context.Context, desired string) (string, error)
}

// Job is one tool run: identity, live progress, final summary.
type Job struct {
	ID         string
	Tool       string
	Status     string // running | done | failed | cancelled
	StartedAt  time.Time
	FinishedAt time.Time
	Total      int
	Processed  int
	Current    string
	Summary    map[string]int
	Missing    []string
	Errors     []string

	mu     sync.Mutex
	cancel context.CancelFunc
}

const (
	StatusRunning   = "running"
	StatusDone      = "done"
	StatusFailed    = "failed"
	StatusCancelled = "cancelled"
)

// State snapshots the job for the API responses.
func (j *Job) State() map[string]any {
	j.mu.Lock()
	defer j.mu.Unlock()
	state := map[string]any{
		"id":         j.ID,
		"tool":       j.Tool,
		"status":     j.Status,
		"started_at": j.StartedAt.Format(time.RFC3339),
		"total":      j.Total,
		"processed":  j.Processed,
		"current":    j.Current,
		"summary":    j.Summary,
		"missing":    j.Missing,
		"errors":     j.Errors,
	}
	if !j.FinishedAt.IsZero() {
		state["finished_at"] = j.FinishedAt.Format(time.RFC3339)
	}
	return state
}

func (j *Job) set(fn func()) {
	j.mu.Lock()
	fn()
	j.mu.Unlock()
}

func (j *Job) addError(err error, key string) {
	j.set(func() {
		j.Errors = append(j.Errors, key+": "+err.Error())
		if n, ok := j.Summary["errors"]; ok {
			j.Summary["errors"] = n + 1
		} else {
			j.Summary["errors"] = 1
		}
	})
}

var (
	regMu   sync.Mutex
	jobs    = map[string]*Job{}
	running *Job
)

// Start launches a tool as a background job. One job at a time: a second
// start while one is running is refused.
func Start(app core.App, s3 S3Store, toolID string) (*Job, error) {
	d, ok := descriptor(toolID)
	if !ok {
		return nil, fmt.Errorf("unknown tool: %s", toolID)
	}
	regMu.Lock()
	defer regMu.Unlock()
	if running != nil {
		return nil, fmt.Errorf("a job is already running: %s", running.Tool)
	}
	buf := make([]byte, 8)
	if _, err := rand.Read(buf); err != nil {
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	job := &Job{
		ID:        hex.EncodeToString(buf),
		Tool:      d.ID,
		Status:    StatusRunning,
		StartedAt: time.Now().UTC(),
		Summary:   map[string]int{},
		cancel:    cancel,
	}
	if d.ID == "find-lost-files" {
		job.Summary = map[string]int{"scanned": 0, "ok": 0, "corrected": 0, "moved": 0, "duplicates": 0, "errors": 0}
	}
	jobs[job.ID] = job
	running = job
	go func() {
		err := runTool(ctx, app, s3, d.ID, job)
		regMu.Lock()
		if running == job {
			running = nil
		}
		regMu.Unlock()
		job.set(func() {
			job.cancel = nil
			job.FinishedAt = time.Now().UTC()
			if ctx.Err() != nil {
				job.Status = StatusCancelled
			} else if err != nil {
				job.Status = StatusFailed
				job.Errors = append(job.Errors, err.Error())
			} else {
				job.Status = StatusDone
			}
			job.Current = ""
		})
	}()
	return job, nil
}

// Cancel asks the running job to stop at the next object boundary.
func Cancel(jobID string) bool {
	regMu.Lock()
	job := jobs[jobID]
	regMu.Unlock()
	if job == nil {
		return false
	}
	job.mu.Lock()
	cancel := job.cancel
	job.mu.Unlock()
	if cancel == nil {
		return false
	}
	cancel()
	return true
}

// JobByID returns a job by id, nil when unknown.
func JobByID(id string) *Job {
	regMu.Lock()
	defer regMu.Unlock()
	return jobs[id]
}

// LatestJob returns the running job if any, otherwise the most recent one
// (the tools page restores its panel on reload).
func LatestJob() *Job {
	regMu.Lock()
	defer regMu.Unlock()
	if running != nil {
		return running
	}
	var latest *Job
	for _, j := range jobs {
		if latest == nil || j.StartedAt.After(latest.StartedAt) {
			latest = j
		}
	}
	return latest
}

func runTool(ctx context.Context, app core.App, s3 S3Store, toolID string, job *Job) error {
	switch toolID {
	case "find-lost-files":
		return runFindLostFiles(ctx, app, s3, job)
	}
	return fmt.Errorf("unknown tool: %s", toolID)
}

// runFindLostFiles reconciles the bucket with the database (US-41): every
// object outside inbox/ is hashed and matched against documents by sha256.
// In place: nothing. Moved in the bucket outside the application: the
// record's file_key is corrected and a relocated event is appended. Unknown
// of the database: moved to inbox/ for automatic processing. A record whose
// file_key exists nowhere is only reported.
func runFindLostFiles(ctx context.Context, app core.App, s3 S3Store, job *Job) error {
	objects, err := s3.ListAll(ctx)
	if err != nil {
		return fmt.Errorf("list bucket: %w", err)
	}
	keySet := make(map[string]bool, len(objects))
	for _, o := range objects {
		keySet[o.Key] = true
	}
	job.set(func() { job.Total = len(objects) })

	for _, o := range objects {
		if ctx.Err() != nil {
			return nil
		}
		job.set(func() { job.Current = o.Key })
		if err := reconcileObject(ctx, app, s3, job, o.Key, keySet); err != nil {
			app.Logger().Error("find-lost-files: object failed", "key", o.Key, "error", err)
			job.addError(err, o.Key)
		}
		job.set(func() { job.Processed++ })
	}

	// Records whose file_key exists nowhere: reported, never acted upon.
	// Records still under inbox/ are interrupted filings (US-11), the
	// poller's business, not orphans.
	records, err := app.FindAllRecords("documents")
	if err != nil {
		return fmt.Errorf("list documents: %w", err)
	}
	missing := []string{}
	for _, r := range records {
		key := r.GetString("file_key")
		if key == "" || keySet[key] || strings.HasPrefix(key, "inbox/") {
			continue
		}
		missing = append(missing, key)
		app.Logger().Warn("find-lost-files: record file missing from bucket", "record", r.Id, "file_key", key)
	}
	job.set(func() {
		job.Missing = missing
		job.Summary["missing"] = len(missing)
	})
	job.set(func() {
		app.Logger().Info("find-lost-files: done", "scanned", job.Summary["scanned"], "ok", job.Summary["ok"],
			"corrected", job.Summary["corrected"], "moved", job.Summary["moved"],
			"duplicates", job.Summary["duplicates"], "errors", job.Summary["errors"], "missing", len(missing))
	})
	return nil
}

// findRecord wraps FindFirstRecordByFilter: a missing record is a nil
// result, not an error.
func findRecord(app core.App, filter string, params dbx.Params) (*core.Record, error) {
	record, err := app.FindFirstRecordByFilter("documents", filter, params)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	return record, nil
}

func reconcileObject(ctx context.Context, app core.App, s3 S3Store, job *Job, key string, keySet map[string]bool) error {
	hash, err := s3.Hash(ctx, key)
	if err != nil {
		return fmt.Errorf("hash: %w", err)
	}
	job.set(func() { job.Summary["scanned"]++ })

	record, err := findRecord(app, "sha256 = {:h}", dbx.Params{"h": hash})
	if err != nil {
		return fmt.Errorf("lookup by sha256: %w", err)
	}

	if record == nil {
		// Unknown of the database: back to inbox/ for automatic processing.
		finalKey, err := s3.ResolveConflict(ctx, "inbox/"+baseName(key))
		if err != nil {
			return fmt.Errorf("conflict resolution: %w", err)
		}
		if err := moveObject(ctx, s3, key, finalKey); err != nil {
			return err
		}
		job.set(func() { job.Summary["moved"]++ })
		app.Logger().Info("find-lost-files: unknown object moved to inbox", "from", key, "to", finalKey)
		return nil
	}

	oldKey := record.GetString("file_key")
	if oldKey == key {
		job.set(func() { job.Summary["ok"]++ })
		return nil
	}
	if keySet[oldKey] {
		// The record points at an object that still exists: this scanned
		// object is a stray copy, left in place and reported.
		job.set(func() { job.Summary["duplicates"]++ })
		app.Logger().Warn("find-lost-files: stray copy left in place", "key", key, "record", record.Id, "file_key", oldKey)
		return nil
	}

	// The file was moved in the bucket outside the application: the record
	// follows it, a relocated event keeps the trace. No other record may
	// already reference the new key.
	other, err := findRecord(app, "file_key = {:k} && id != {:id}", dbx.Params{"k": key, "id": record.Id})
	if err != nil {
		return fmt.Errorf("lookup by file_key: %w", err)
	}
	if other != nil {
		return fmt.Errorf("another record (%s) already references %s", other.Id, key)
	}
	before := pbapp.Snapshot{"file_key": oldKey}
	after := pbapp.Snapshot{"file_key": key}
	txErr := app.RunInTransaction(func(tx core.App) error {
		record.Set("file_key", key)
		if err := tx.Save(record); err != nil {
			return err
		}
		return pbapp.AppendEvent(tx, record.Id, "relocated", before, after, nil)
	})
	if txErr != nil {
		return fmt.Errorf("relocation save: %w", txErr)
	}
	job.set(func() { job.Summary["corrected"]++ })
	app.Logger().Info("find-lost-files: file_key corrected", "record", record.Id, "from", oldKey, "to", key)
	return nil
}

// moveObject copies src to dst, verifies the copy, then deletes the source.
func moveObject(ctx context.Context, s3 S3Store, src, dst string) error {
	if err := s3.Copy(ctx, src, dst); err != nil {
		return fmt.Errorf("copy: %w", err)
	}
	if !s3.Exists(ctx, dst) {
		return fmt.Errorf("target %s not verified after copy", dst)
	}
	if err := s3.Remove(ctx, src); err != nil {
		return fmt.Errorf("remove source: %w", err)
	}
	return nil
}

func baseName(key string) string {
	if i := strings.LastIndex(key, "/"); i >= 0 {
		return key[i+1:]
	}
	return key
}
