package pbapp

import (
	"context"
	"fmt"
	"strings"

	"ganigeorgiev.com/pocketbase/pocketbase/core"
	"github.com/napnap75/archivist/internal/config"
)

// ReclassifyResult is the outcome of ReclassifyOne: updated (the fields
// were replaced by the rule proposal and the object re-filed - FinalKey is
// the final, conflict-resolved key), unchanged (the proposal is identical
// to the stored state, nothing to write), or skipped (Reason set, not an
// error - the rules cannot re-classify the stored pages, the document is
// left untouched).
type ReclassifyResult struct {
	Outcome   string // updated | unchanged | skipped
	FinalKey  string
	Collision bool   // a conflict suffix was applied to the move
	Reason    string // for skipped
}

// ReclassifyOne runs the whole rule engine against the stored pages
// (US-46): the same classification as the import, from the stored pages
// instead of the incoming object. The proposal replaces the declared
// fields wholesale, the status goes back to processed like at import, and
// the object is re-filed under the rendered template like RefileOne.
// A proposal carrying errors skips the document: nothing is modified and
// no event is recorded. A change is committed in one transaction (record
// save + corrected event carrying the full diff) with a best-effort S3
// rollback of the move when the save fails.
func ReclassifyOne(ctx context.Context, app core.App, s3 RefileStore, cfg *config.Config, record *core.Record) (ReclassifyResult, error) {
	pages := recordPages(record)
	fields, rendered, errs, _ := cfg.EvaluateWithTrace(pages)
	if len(errs) > 0 || rendered == "" {
		return ReclassifyResult{Outcome: "skipped", Reason: "classification en erreur: " + strings.Join(errs, "; ")}, nil
	}

	before := SnapshotFromRecord(cfg, record, true)
	oldKey := record.GetString("file_key")
	target := s3.Prefixes().Files + rendered
	finalKey := oldKey
	moved := false
	if target != oldKey {
		var err error
		finalKey, err = s3.ResolveConflict(ctx, target)
		if err != nil {
			return ReclassifyResult{}, fmt.Errorf("conflict resolution: %w", err)
		}
		if err := s3.Copy(ctx, oldKey, finalKey); err != nil {
			return ReclassifyResult{}, fmt.Errorf("S3 copy failed: %w", err)
		}
		if !s3.Exists(ctx, finalKey) {
			return ReclassifyResult{}, fmt.Errorf("target %s not verified after copy", finalKey)
		}
		if err := s3.Remove(ctx, oldKey); err != nil {
			_ = s3.Copy(ctx, finalKey, oldKey) // best-effort rollback of the copy
			_ = s3.Remove(ctx, finalKey)
			return ReclassifyResult{}, fmt.Errorf("S3 source deletion failed: %w", err)
		}
		moved = true
		record.Set("file_key", finalKey)
	}

	// The proposal replaces the stored fields: a declared field the rules
	// no longer produce is unset, as it would be at import.
	for _, f := range cfg.FieldList {
		if v, ok := fields[f.Name]; ok {
			record.Set(f.Name, v)
		} else {
			record.Set(f.Name, nil)
		}
	}
	record.Set("status", "processed")

	after := SnapshotFromRecord(cfg, record, true)
	dBefore, dAfter := DiffSnapshots(before, after)
	if len(dBefore) == 0 && len(dAfter) == 0 {
		return ReclassifyResult{Outcome: "unchanged"}, nil
	}

	// US-37: new enum values join the official select lists before the
	// record save validates them.
	if err := EnsureEnumValues(app, cfg, fields); err != nil {
		return ReclassifyResult{}, fmt.Errorf("enum values: %w", err)
	}

	txErr := app.RunInTransaction(func(tx core.App) error {
		if err := tx.Save(record); err != nil {
			return err
		}
		return AppendEvent(tx, record.Id, "corrected", before, after, nil)
	})
	if txErr != nil {
		if moved {
			_ = s3.Copy(ctx, finalKey, oldKey) // best-effort rollback of the move
			_ = s3.Remove(ctx, finalKey)
		}
		return ReclassifyResult{}, fmt.Errorf("reclassification save: %w", txErr)
	}
	return ReclassifyResult{Outcome: "updated", FinalKey: finalKey, Collision: moved && finalKey != target}, nil
}
