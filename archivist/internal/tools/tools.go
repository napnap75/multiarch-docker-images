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
	"github.com/napnap75/archivist/internal/config"
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
		Description: "Parcourt les fichiers PDF sous les préfixes pending et files (recouvre tout le bucket lorsque files est configuré comme racine), recalcule leur empreinte sha256 et la compare à la base. " +
			"Un objet à sa place connue ne bouge pas ; un objet déplacé hors de l'application voit son file_key corrigé avec un événement relocated dans l'historique ; " +
			"un objet inconnu de la base est déplacé vers la zone inbox pour être traité automatiquement ; " +
			"les enregistrements dont le fichier a disparu du bucket sont signalés dans le résumé, sans action automatique.",
	},
	{
		ID:   "refile-all",
		Name: "Re-filer tous les documents",
		Description: "Recalcule la clé cible de chaque document processed ou validated (champs enregistrés + préfixe files et templates de la configuration active) et déplace les objets qui ne sont plus à leur place — après un changement de ARCHIVIST_S3_FILES_PREFIX ou d'un template de nommage. " +
			"Un document déjà bien classé ne bouge pas ; un document dont le template enregistré n'existe plus ou ne rend plus de clé valide est laissé en place et signalé (skipped) ; " +
			"les documents pending restent à la review queue. Chaque déplacement met à jour file_key et ajoute un événement relocated dans l'historique.",
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
	ListReconciliationPDFs(ctx context.Context) ([]storage.InboxObject, error)
	Hash(ctx context.Context, key string) (string, error)
	Exists(ctx context.Context, key string) bool
	Copy(ctx context.Context, srcKey, dstKey string) error
	Remove(ctx context.Context, key string) error
	ResolveConflict(ctx context.Context, desired string) (string, error)
	Prefixes() storage.Prefixes
}

// ConfigStore is the surface of the configuration manager the tools use;
// a seam kept for unit tests.
type ConfigStore interface {
	Read(fn func(*config.Snapshot) error) error
}

// Job is one tool run: identity, live progress, final summary. A job in
// dry-run mode runs every check but mutates nothing in S3 or in the
// database: the planned moves land in Planned, not in the bucket.
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
	Skipped    []string
	Planned    []string
	Errors     []string

	DryRun bool

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
		"skipped":    j.Skipped,
		"planned":    j.Planned,
		"errors":     j.Errors,
		"dry_run":    j.DryRun,
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

// Start launches a tool as a background job, for real or in dry-run mode
// (every check, no mutation: S3 objects, records and events untouched). One
// job at a time: a second start while one is running is refused.
func Start(app core.App, s3 S3Store, manager ConfigStore, toolID string, dryRun bool) (*Job, error) {
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
		DryRun:    dryRun,
		cancel:    cancel,
	}
	if d.ID == "find-lost-files" {
		job.Summary = map[string]int{"scanned": 0, "ok": 0, "corrected": 0, "moved": 0, "duplicates": 0, "errors": 0}
	}
	if d.ID == "refile-all" {
		job.Summary = map[string]int{"scanned": 0, "unchanged": 0, "moved": 0, "skipped": 0, "errors": 0}
	}
	jobs[job.ID] = job
	running = job
	go func() {
		err := runTool(ctx, app, s3, manager, d.ID, dryRun, job)
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

func runTool(ctx context.Context, app core.App, s3 S3Store, manager ConfigStore, toolID string, dryRun bool, job *Job) error {
	switch toolID {
	case "find-lost-files":
		return runFindLostFiles(ctx, app, s3, job, dryRun)
	case "refile-all":
		return runRefileAll(ctx, app, s3, manager, job, dryRun)
	}
	return fmt.Errorf("unknown tool: %s", toolID)
}

// runFindLostFiles reconciles the pending and files prefix PDFs with the database (US-41).
// In place: nothing. Moved in the bucket outside the application: the
// record's file_key is corrected and a relocated event is appended. Unknown
// of the database: moved to the inbox for automatic processing. A record
// whose file_key exists nowhere is only reported. In dry-run mode every
// outcome is computed and counted but nothing is written: no move, no save,
// no event.
func runFindLostFiles(ctx context.Context, app core.App, s3 S3Store, job *Job, dryRun bool) error {
	prefixes := s3.Prefixes()
	objects, err := s3.ListReconciliationPDFs(ctx)
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
		if err := reconcileObject(ctx, app, s3, job, o.Key, keySet, dryRun); err != nil {
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
		if key == "" || !prefixes.IsReconciliationKey(key) || keySet[key] {
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
			"duplicates", job.Summary["duplicates"], "errors", job.Summary["errors"], "missing", len(missing), "dry_run", dryRun)
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

func reconcileObject(ctx context.Context, app core.App, s3 S3Store, job *Job, key string, keySet map[string]bool, dryRun bool) error {
	prefixes := s3.Prefixes()
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
		// Unknown of the database: back to the inbox for automatic processing.
		if dryRun {
			job.set(func() { job.Summary["moved"]++ })
			app.Logger().Info("find-lost-files dry-run: unknown object would move to inbox", "from", key, "to", prefixes.Inbox+baseName(key))
			return nil
		}
		finalKey, err := s3.ResolveConflict(ctx, prefixes.Inbox+baseName(key))
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
	if dryRun {
		job.set(func() { job.Summary["corrected"]++ })
		app.Logger().Info("find-lost-files dry-run: file_key would be corrected", "record", record.Id, "from", oldKey, "to", key)
		return nil
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

// runRefileAll re-computes the target key of every processed or validated
// document from the active compiled configuration (US-43) — after a change
// of the files prefix or of a naming template — and moves the objects that
// are no longer where their name says. Pending documents belong to the
// review queue and are never touched. In dry-run mode every target is
// computed and checked but nothing is written; the planned moves are listed
// in the job.
func runRefileAll(ctx context.Context, app core.App, s3 S3Store, manager ConfigStore, job *Job, dryRun bool) error {
	return manager.Read(func(snapshot *config.Snapshot) error {
		return refileAll(ctx, app, s3, snapshot.Config, job, dryRun)
	})
}

func refileAll(ctx context.Context, app core.App, s3 S3Store, cfg *config.Config, job *Job, dryRun bool) error {
	records, err := app.FindAllRecords("documents")
	if err != nil {
		return fmt.Errorf("list documents: %w", err)
	}
	var eligible []*core.Record
	for _, r := range records {
		status := r.GetString("status")
		if status == "processed" || status == "validated" {
			eligible = append(eligible, r)
		}
	}
	job.set(func() {
		job.Total = len(eligible)
		job.Skipped = []string{}
		if dryRun {
			job.Planned = []string{}
		}
	})
	for _, record := range eligible {
		if ctx.Err() != nil {
			return nil
		}
		job.set(func() { job.Current = record.GetString("file_key") })
		if err := refileRecord(ctx, app, s3, cfg, job, record, dryRun); err != nil {
			app.Logger().Error("refile-all: document failed", "record", record.Id, "file_key", record.GetString("file_key"), "error", err)
			job.addError(err, record.GetString("file_key"))
		}
		job.set(func() { job.Processed++ })
	}
	job.set(func() {
		app.Logger().Info("refile-all: done", "scanned", job.Summary["scanned"], "unchanged", job.Summary["unchanged"],
			"moved", job.Summary["moved"], "skipped", job.Summary["skipped"], "errors", job.Summary["errors"], "dry_run", dryRun)
	})
	return nil
}

// refileRecord re-renders one document's naming template from its stored
// fields, moves the object when the target differs from the current key,
// and traces the move as a relocated event. A stored template that no
// longer exists, or a failing or empty render, leaves the object in place
// and is reported (skipped); a skip is not an error. In dry-run mode the
// move is only planned: the target is computed and the collision resolved
// (read-only), the object, the record and the history stay untouched.
func refileRecord(ctx context.Context, app core.App, s3 S3Store, cfg *config.Config, job *Job, record *core.Record, dryRun bool) error {
	oldKey := record.GetString("file_key")
	job.set(func() { job.Summary["scanned"]++ })

	tplName := record.GetString("template")
	if tplName == "" {
		tplName = "default"
	}
	tpl, ok := cfg.Templates[tplName]
	if !ok {
		return jobSkip(job, oldKey, "template "+tplName+" introuvable dans la configuration active")
	}
	rendered, err := tpl.Render(pbapp.FieldsFromRecord(cfg, record), nil)
	if err != nil {
		return jobSkip(job, oldKey, "rendu échoué: "+err.Error())
	}
	if rendered == "" {
		return jobSkip(job, oldKey, "rendu vide")
	}
	target := s3.Prefixes().Files + rendered
	if target == oldKey {
		job.set(func() { job.Summary["unchanged"]++ })
		return nil
	}
	finalKey, err := s3.ResolveConflict(ctx, target)
	if err != nil {
		return fmt.Errorf("conflict resolution: %w", err)
	}
	if dryRun {
		planned := oldKey + " → " + finalKey
		if finalKey != target {
			planned += " (collision : suffixe de conflit appliqué)"
		}
		job.set(func() {
			if job.Planned == nil {
				job.Planned = []string{}
			}
			job.Planned = append(job.Planned, planned)
			job.Summary["moved"]++
		})
		app.Logger().Info("refile-all dry-run: document would be re-filed", "record", record.Id, "from", oldKey, "to", finalKey)
		return nil
	}
	if err := moveObject(ctx, s3, oldKey, finalKey); err != nil {
		return err
	}
	txErr := app.RunInTransaction(func(tx core.App) error {
		record.Set("file_key", finalKey)
		if err := tx.Save(record); err != nil {
			return err
		}
		return pbapp.AppendEvent(tx, record.Id, "relocated", pbapp.Snapshot{"file_key": oldKey}, pbapp.Snapshot{"file_key": finalKey}, nil)
	})
	if txErr != nil {
		_ = s3.Copy(ctx, finalKey, oldKey) // best-effort rollback of the move
		_ = s3.Remove(ctx, finalKey)
		return fmt.Errorf("relocation save: %w", txErr)
	}
	app.Logger().Info("refile-all: document re-filed", "record", record.Id, "from", oldKey, "to", finalKey)
	job.set(func() { job.Summary["moved"]++ })
	return nil
}

// jobSkip leaves a document in place and reports the reason in the job's
// skipped list and summary.
func jobSkip(job *Job, key, reason string) error {
	job.set(func() {
		if job.Skipped == nil {
			job.Skipped = []string{}
		}
		job.Skipped = append(job.Skipped, key+" — "+reason)
		job.Summary["skipped"]++
	})
	return nil
}
