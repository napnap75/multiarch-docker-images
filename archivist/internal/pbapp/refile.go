package pbapp

import (
	"context"
	"fmt"

	"ganigeorgiev.com/pocketbase/pocketbase/core"
	"github.com/napnap75/archivist/internal/config"
	"github.com/napnap75/archivist/internal/storage"
)

// RefileStore is the surface of the storage client the re-file uses; a
// seam kept for unit tests (the tools' S3Store satisfies it structurally).
type RefileStore interface {
	Exists(ctx context.Context, key string) bool
	Copy(ctx context.Context, srcKey, dstKey string) error
	Remove(ctx context.Context, key string) error
	ResolveConflict(ctx context.Context, desired string) (string, error)
	Prefixes() storage.Prefixes
}

// RefileResult is the outcome of RefileOne: moved (FinalKey = the final,
// conflict-resolved key; moving to it is done unless DryRun), unchanged
// (the document is already where its name says), or skipped (Reason set,
// not an error - the document cannot be re-filed under the active
// configuration).
type RefileResult struct {
	Outcome   string // moved | unchanged | skipped
	FinalKey  string
	Collision bool   // a conflict suffix was (or would be) applied
	Reason    string
}

// RefileOne re-files one document exactly as the refile-all tool does
// per record (US-43): the stored template (default when the record
// carries none) is rendered from the stored fields under the active
// configuration; an unknown template, a failing or empty render skips
// the document (Outcome skipped, Reason set); a target equal to the
// current file_key is unchanged; otherwise the object moves - copy
// first, existence verified, then the source removed - the record's
// file_key is updated and a relocated event is appended, both in one
// transaction with a best-effort S3 rollback if the save fails. In
// dry-run mode every check runs but nothing is written: the collision
// is resolved read-only and Outcome moved carries the planned key.
func RefileOne(ctx context.Context, app core.App, s3 RefileStore, cfg *config.Config, record *core.Record, dryRun bool) (RefileResult, error) {
	oldKey := record.GetString("file_key")

	tplName := record.GetString("template")
	if tplName == "" {
		tplName = "default"
	}
	tpl, ok := cfg.Templates[tplName]
	if !ok {
		return RefileResult{Outcome: "skipped", Reason: "template " + tplName + " introuvable dans la configuration active"}, nil
	}
	rendered, err := tpl.Render(FieldsFromRecord(cfg, record), nil)
	if err != nil {
		return RefileResult{Outcome: "skipped", Reason: "rendu échoué: " + err.Error()}, nil
	}
	if rendered == "" {
		return RefileResult{Outcome: "skipped", Reason: "rendu vide"}, nil
	}
	target := s3.Prefixes().Files + rendered
	if target == oldKey {
		return RefileResult{Outcome: "unchanged"}, nil
	}
	finalKey, err := s3.ResolveConflict(ctx, target)
	if err != nil {
		return RefileResult{}, fmt.Errorf("conflict resolution: %w", err)
	}
	if dryRun {
		return RefileResult{Outcome: "moved", FinalKey: finalKey, Collision: finalKey != target}, nil
	}
	if err := s3.Copy(ctx, oldKey, finalKey); err != nil {
		return RefileResult{}, fmt.Errorf("S3 copy failed: %w", err)
	}
	if !s3.Exists(ctx, finalKey) {
		return RefileResult{}, fmt.Errorf("target %s not verified after copy", finalKey)
	}
	if err := s3.Remove(ctx, oldKey); err != nil {
		_ = s3.Copy(ctx, finalKey, oldKey) // best-effort rollback of the copy
		_ = s3.Remove(ctx, finalKey)
		return RefileResult{}, fmt.Errorf("S3 source deletion failed: %w", err)
	}
	txErr := app.RunInTransaction(func(tx core.App) error {
		record.Set("file_key", finalKey)
		if err := tx.Save(record); err != nil {
			return err
		}
		return AppendEvent(tx, record.Id, "relocated", Snapshot{"file_key": oldKey}, Snapshot{"file_key": finalKey}, nil)
	})
	if txErr != nil {
		_ = s3.Copy(ctx, finalKey, oldKey) // best-effort rollback of the move
		_ = s3.Remove(ctx, finalKey)
		return RefileResult{}, fmt.Errorf("relocation save: %w", txErr)
	}
	return RefileResult{Outcome: "moved", FinalKey: finalKey, Collision: finalKey != target}, nil
}
