// Package pipeline implements US-10 to US-17: polling, download,
// extraction, classification, recording and filing.
package pipeline

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"strings"

	"github.com/ledongthuc/pdf"
	"github.com/napnap75/archivist/internal/config"
	"github.com/napnap75/archivist/internal/pbapp"
	"github.com/napnap75/archivist/internal/storage"
	"github.com/pocketbase/pocketbase/core"
	"github.com/pocketbase/pocketbase/tools/dbx"
)

// pollState remembers size and ETag between two passes (US-10 write-completion wait).
type pollState struct {
	size int64
	etag string
}

var stable = map[string]pollState{}

func RunPoll(app *core.App, cfg *config.Config, s3 *storage.Client) {
	ctx := context.Background()

	// US-11: recover interrupted filings first.
	recoverStuck(app, cfg, s3, ctx)

	objects, err := s3.ListInbox(ctx)
	if err != nil {
		log.Printf("poll: list inbox: %v", err)
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
		if err := processOne(app, cfg, s3, ctx, o.Key); err != nil {
			log.Printf("poll: process %s: %v", o.Key, err)
		}
	}
}

func processOne(app *core.App, cfg *config.Config, s3 *storage.Client, ctx context.Context, key string) error {
	// US-12: download in memory, sha256 on the fly.
	data, hash, err := s3.Get(ctx, key)
	if err != nil {
		return fmt.Errorf("download: %w", err)
	}

	// US-12: duplicate rejection.
	dup, err := app.FindFirstRecordByFilter("documents", "sha256 = {:h}", dbx.Params{"h": hash})
	if err == nil && dup != nil {
		if err := s3.Remove(ctx, key); err != nil {
			return fmt.Errorf("duplicate cleanup: %w", err)
		}
		log.Printf("duplicate %s (sha256 %s) removed from inbox, original is %s", key, hash, dup.GetString("file_key"))
		return nil
	}

	// US-13: page-by-page extraction.
	pages, extractErr := extractPages(data)
	var errs []string
	if extractErr != nil {
		errs = append(errs, "unreadable PDF: "+extractErr.Error())
		pages = nil
	}

	// US-14 / US-15: classification and verdict.
	fields, rendered, evalErrs := cfg.Evaluate(pages)
	errs = append(errs, evalErrs...)
	valid := len(errs) == 0 && rendered != ""

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
		log.Printf("imported event for %s: %v", key, err)
	}
	log.Printf("imported %s -> %s (status %s)", key, newKey, record.GetString("status"))
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
func recoverStuck(app *core.App, cfg *config.Config, s3 *storage.Client, ctx context.Context) {
	stuck, err := app.FindRecordsByFilter("documents", "file_key ~ {:p}", "", 100, 0, dbx.Params{"p": "inbox/"})
	if err != nil {
		log.Printf("recovery: %v", err)
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
				log.Printf("recovery: record %s: unknown template %q", record.Id, tplName)
				continue
			}
			var err error
			rendered, err = tpl.Render(pbapp.FieldsFromRecord(cfg, record), nil)
			if err != nil {
				log.Printf("recovery: record %s: %v", record.Id, err)
				continue
			}
		}
		newKey, err := fileDocument(s3, ctx, record, rendered)
		if err != nil {
			log.Printf("recovery: record %s (%s): %v", record.Id, record.GetString("file_key"), err)
			continue
		}
		if err := app.Save(record); err != nil {
			log.Printf("recovery: save record %s: %v", record.Id, err)
			continue
		}
		log.Printf("recovery: record %s completed filing to %s", record.Id, newKey)
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
		pages = append(pages, p.GetPlainText(nil))
	}
	if len(pages) == 0 {
		return nil, fmt.Errorf("no page extracted")
	}
	return pages, nil
}

func bytesReader(data []byte) *strings.Reader {
	return strings.NewReader(string(data))
}
