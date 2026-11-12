// Package pipeline implements US-10 to US-17: polling, download,
// extraction, classification, recording and filing.
package pipeline

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"ganigeorgiev.com/pocketbase/dbx"
	"ganigeorgiev.com/pocketbase/pocketbase/core"
	"github.com/ledongthuc/pdf"
	"github.com/napnap75/archivist/internal/config"
	"github.com/napnap75/archivist/internal/pbapp"
	"github.com/napnap75/archivist/internal/storage"
)

// pollState remembers size and ETag between two passes (US-10 write-completion wait).
type pollState struct {
	size int64
	etag string
}

var stable = map[string]pollState{}

// nonPDF remembers the objects already found not to be PDFs (key -> ETag at
// the check): they are left in place, so the pass skips them without
// re-downloading until their content changes.
var nonPDF = map[string]string{}

var pdfMagic = []byte("%PDF-")

// isPDF reports whether the content carries the PDF header.
func isPDF(data []byte) bool {
	return bytes.HasPrefix(data, pdfMagic)
}

func RunPoll(app core.App, cfg *config.Config, s3 *storage.Client) {
	ctx := context.Background()

	// US-11: recover interrupted filings first.
	recoverStuck(app, cfg, s3, ctx)

	objects, err := s3.ListInbox(ctx)
	if err != nil {
		app.Logger().Error("poll: list inbox failed", "error", err)
		return
	}

	for _, o := range objects {
		// US-10: an object referenced by a record is not a candidate.
		linked, err := app.FindFirstRecordByFilter("documents", "file_key = {:k}", dbx.Params{"k": o.Key})
		if err == nil && linked != nil {
			continue
		}
		prev, seen := stable[o.Key]
		stable[o.Key] = pollState{size: o.Size, etag: o.ETag}
		if !seen || prev.size != o.Size || prev.etag != o.ETag {
			continue // first sighting, or still uploading: wait for stability
		}
		delete(stable, o.Key)
		if err := processOne(app, cfg, s3, ctx, o.Key, o.ETag); err != nil {
			app.Logger().Error("poll: processing failed", "key", o.Key, "error", err)
		}
	}
}

func processOne(app core.App, cfg *config.Config, s3 *storage.Client, ctx context.Context, key, etag string) error {
	// Already found not to be a PDF and unchanged since: left in place,
	// nothing to do.
	if nonPDF[key] == etag {
		return nil
	}

	// US-12: download in memory, sha256 on the fly.
	data, hash, err := s3.Get(ctx, key)
	if err != nil {
		return fmt.Errorf("download: %w", err)
	}

	// Not a PDF: the object is left as-is in inbox/, never imported — no
	// record, no event; the skip is logged once per unchanged content.
	if !isPDF(data) {
		nonPDF[key] = etag
		app.Logger().Warn("inbox: object is not a PDF, left in place", "key", key)
		return nil
	}

	// US-12: duplicate rejection.
	dup, err := app.FindFirstRecordByFilter("documents", "sha256 = {:h}", dbx.Params{"h": hash})
	if err == nil && dup != nil {
		if err := s3.Remove(ctx, key); err != nil {
			return fmt.Errorf("duplicate cleanup: %w", err)
		}
		app.Logger().Info("duplicate removed from inbox", "key", key, "sha256", hash, "original", dup.GetString("file_key"))
		return nil
	}

	// US-13: page-by-page extraction.
	pages, extractErr := extractPages(data)
	var errs []string
	var fields map[string]any
	rendered := ""
	if extractErr != nil {
		// US-13: an unreadable PDF goes straight to pending/ with the
		// extraction failure as its only error; the rule engine never
		// runs on a document without pages.
		errs = append(errs, "unreadable PDF: "+extractErr.Error())
	} else {
		// US-14 / US-15: classification and verdict.
		var evalErrs []string
		fields, rendered, evalErrs = cfg.Evaluate(pages)
		errs = append(errs, evalErrs...)
	}
	valid := len(errs) == 0 && rendered != ""

	// US-37: new enum values join the official select lists before the
	// record save validates them.
	if err := pbapp.EnsureEnumValues(app, cfg, fields); err != nil {
		return fmt.Errorf("enum values: %w", err)
	}

	// US-16: the record is created before any physical move.
	coll, err := app.FindCollectionByNameOrId("documents")
	if err != nil {
		return err
	}
	record := core.NewRecord(coll)
	record.Set("sha256", hash)
	record.Set("file_key", key)
	pagesJSON, _ := json.Marshal(pages)
	record.Set("pages", string(pagesJSON))
	for name, v := range fields {
		record.Set(name, v)
	}
	if valid {
		record.Set("status", "processed")
	} else {
		record.Set("status", "pending")
	}
	if err := app.Save(record); err != nil {
		return fmt.Errorf("save record: %w", err)
	}

	// US-17: filing.
	newKey, err := fileDocument(s3, ctx, record, rendered)
	if err != nil {
		return err
	}
	if err := app.Save(record); err != nil {
		return fmt.Errorf("update record: %w", err)
	}

	// US-16: the imported event, appended once the filing is done.
	if err := pbapp.AppendImportedEvent(app, record.Id, key, newKey, valid, fields, errs); err != nil {
		app.Logger().Error("imported event append failed", "key", key, "error", err)
	}
	app.Logger().Info("document imported", "key", key, "file_key", newKey, "status", record.GetString("status"))
	return nil
}

// fileDocument moves the object to files/ (valid) or pending/ (invalid),
// updates file_key, and returns the final key. The record save is left to the
// caller so the state stays consistent on recovery.
func fileDocument(s3 *storage.Client, ctx context.Context, record *core.Record, rendered string) (string, error) {
	valid := record.GetString("status") == "processed"
	var target string
	if valid {
		// The rendered template is the full object path under files/.
		target = "files/" + rendered
	} else {
		target = "pending/" + baseName(record.GetString("file_key"))
	}
	finalKey, err := s3.ResolveConflict(ctx, target)
	if err != nil {
		return "", err
	}
	if err := s3.Copy(ctx, record.GetString("file_key"), finalKey); err != nil {
		return "", fmt.Errorf("copy: %w", err)
	}
	if !s3.Exists(ctx, finalKey) {
		return "", fmt.Errorf("target %s not verified after copy", finalKey)
	}
	if err := s3.Remove(ctx, record.GetString("file_key")); err != nil {
		return "", fmt.Errorf("remove source: %w", err)
	}
	record.Set("file_key", finalKey)
	return finalKey, nil
}

// recoverStuck completes interrupted filings: records whose file_key is still
// under inbox/ (US-11).
func recoverStuck(app core.App, cfg *config.Config, s3 *storage.Client, ctx context.Context) {
	stuck, err := app.FindRecordsByFilter("documents", "file_key ~ {:p}", "", 100, 0, dbx.Params{"p": "inbox/"})
	if err != nil {
		app.Logger().Error("recovery: query failed", "error", err)
		return
	}
	for _, record := range stuck {
		rendered := ""
		if record.GetString("status") == "processed" {
			// Re-render the template from the stored fields.
			tplName := record.GetString("template")
			if tplName == "" {
				tplName = "default"
			}
			tpl, ok := cfg.Templates[tplName]
			if !ok {
				app.Logger().Error("recovery: unknown template", "record", record.Id, "template", tplName)
				continue
			}
			var err error
			rendered, err = tpl.Render(pbapp.FieldsFromRecord(cfg, record), nil)
			if err != nil {
				app.Logger().Error("recovery: render failed", "record", record.Id, "error", err)
				continue
			}
		}
		newKey, err := fileDocument(s3, ctx, record, rendered)
		if err != nil {
			app.Logger().Error("recovery: filing failed", "record", record.Id, "file_key", record.GetString("file_key"), "error", err)
			continue
		}
		if err := app.Save(record); err != nil {
			app.Logger().Error("recovery: save failed", "record", record.Id, "error", err)
			continue
		}
		app.Logger().Info("recovery: filing completed", "record", record.Id, "file_key", newKey)
	}
}

func baseName(key string) string {
	if i := strings.LastIndex(key, "/"); i >= 0 {
		return key[i+1:]
	}
	return key
}

// extractPages extracts the PDF text page by page including headers and
// footers (US-13).
func extractPages(data []byte) ([]string, error) {
	r, err := pdf.NewReader(bytesReader(data), int64(len(data)))
	if err != nil {
		return nil, err
	}
	var pages []string
	for i := 1; i <= r.NumPage(); i++ {
		p := r.Page(i)
		if p.V.IsNull() {
			return nil, fmt.Errorf("page %d is null", i)
		}
		text, err := p.GetPlainText(nil)
		if err != nil {
			return nil, fmt.Errorf("page %d: %w", i, err)
		}
		pages = append(pages, text)
	}
	if len(pages) == 0 {
		return nil, fmt.Errorf("no page extracted")
	}
	return pages, nil
}

func bytesReader(data []byte) *strings.Reader {
	return strings.NewReader(string(data))
}
