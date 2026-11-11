// Routes.go implements the review-dashboard application routes of
// arch.md section 7.3: configuration metadata (US-31), PDF proxy (US-34),
// dry-run in its two exclusive modes (US-35) and the full correction (US-36).
package pbapp

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	"ganigeorgiev.com/pocketbase/pocketbase/apis"
	"ganigeorgiev.com/pocketbase/pocketbase/core"
	"github.com/napnap75/archivist/internal/config"
	"github.com/napnap75/archivist/internal/storage"
)

// BindRoutes registers the dashboard application routes. They are
// superuser-only: the dashboard authenticates through the standard
// PocketBase auth, but these routes bypass the record API and its hooks.
func BindRoutes(app core.App, cfg *config.Config, s3 *storage.Client) {
	app.OnServe().BindFunc(func(e *core.ServeEvent) error {
		if !e.Router.HasRoute(http.MethodGet, "/{path...}") {
			e.Router.GET("/{path...}", apis.Static(os.DirFS("pb_public"), true))
		}
		e.Router.GET("/api/archivist/config", func(re *core.RequestEvent) error {
			if !re.HasSuperuserAuth() {
				return re.ForbiddenError("superusers only", nil)
			}
			return re.JSON(200, configMetadata(cfg))
		})
		e.Router.GET("/api/archivist/documents/{id}/pdf", func(re *core.RequestEvent) error {
			return handlePdf(re, s3)
		})
		e.Router.POST("/api/archivist/documents/{id}/dry-run", func(re *core.RequestEvent) error {
			return handleDryRun(re, cfg)
		})
		e.Router.POST("/api/archivist/documents/{id}/correct", func(re *core.RequestEvent) error {
			return handleCorrect(re, cfg, s3)
		})
		return e.Next()
	})
}

// configMetadata serves the read-only configuration the UI is driven by:
// declared fields, enum vocabularies, the fixed statuses, template names.
// Nothing technical leaks (no S3 credentials, no rules).
func configMetadata(cfg *config.Config) map[string]any {
	return map[string]any{
		"fields":    cfg.FieldList,
		"enums":     enumVocabularies(cfg),
		"statuses":  []string{"pending", "processed", "validated"},
		"templates": sortedTemplateNames(cfg),
	}
}

func sortedTemplateNames(cfg *config.Config) []string {
	out := make([]string, 0, len(cfg.Templates))
	for name := range cfg.Templates {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

// enumVocabularies computes each enum field vocabulary from the constants
// the rules assign (US-31): the UI dropdowns offer exactly what the rule
// engine can set.
func enumVocabularies(cfg *config.Config) map[string][]string {
	enums := map[string][]string{}
	seen := map[string]map[string]bool{}
	var walk func(rules []*config.Rule)
	walk = func(rules []*config.Rule) {
		for _, r := range rules {
			for _, s := range r.Set {
				if s.Eval != "" {
					continue
				}
				f, ok := cfg.Fields[s.Field]
				if !ok || f.Type != "enum" {
					continue
				}
				v, _ := s.Value.(string)
				if v == "" {
					continue
				}
				if seen[s.Field] == nil {
					seen[s.Field] = map[string]bool{}
				}
				if !seen[s.Field][v] {
					seen[s.Field][v] = true
					enums[s.Field] = append(enums[s.Field], v)
				}
			}
			walk(r.Rules)
		}
	}
	walk(cfg.Rules)
	for _, values := range enums {
		sort.Strings(values)
	}
	return enums
}

// handlePdf streams the document from S3; the browser never reaches the
// S3 cluster (US-34).
func handlePdf(re *core.RequestEvent, s3 *storage.Client) error {
	if !re.HasSuperuserAuth() {
		return re.ForbiddenError("superusers only", nil)
	}
	id := re.Request.PathValue("id")
	record, err := re.App.FindRecordById("documents", id)
	if err != nil {
		return re.JSON(404, map[string]any{"message": "document not found"})
	}
	key := record.GetString("file_key")
	if key == "" {
		return re.JSON(404, map[string]any{"message": "document has no file"})
	}
	ctx := re.Request.Context()
	if !s3.Exists(ctx, key) {
		return re.JSON(404, map[string]any{"message": "object not found in S3", "key": key})
	}
	data, _, err := s3.Get(ctx, key)
	if err != nil {
		return re.JSON(500, map[string]any{"message": "S3 download failed", "error": err.Error()})
	}
	re.Response.Header().Set("Content-Disposition", "inline; filename=\""+fileName(key)+"\"")
	return re.Blob(200, "application/pdf", data)
}

func fileName(key string) string {
	if i := strings.LastIndex(key, "/"); i >= 0 {
		return key[i+1:]
	}
	return key
}

// dryRunResponse is the single response shape of the two exclusive
// dry-run modes (US-35).
type dryRunResponse struct {
	Fields   map[string]any `json:"fields"`
	Template string         `json:"template"`
	Rendered string         `json:"rendered"`
	Errors   []string       `json:"errors"`
}

func handleDryRun(re *core.RequestEvent, cfg *config.Config) error {
	if !re.HasSuperuserAuth() {
		return re.ForbiddenError("superusers only", nil)
	}
	id := re.Request.PathValue("id")
	record, err := re.App.FindRecordById("documents", id)
	if err != nil {
		return re.JSON(404, map[string]any{"message": "document not found"})
	}
	pages := recordPages(record)

	body, err := io.ReadAll(re.Request.Body)
	if err != nil {
		return re.JSON(400, map[string]any{"message": "unreadable body", "error": err.Error()})
	}

	// Mode A - no body: the pipeline proposal, the rule tree re-evaluated
	// against the stored pages. User values never take part.
	if len(bytes.TrimSpace(body)) == 0 {
		fields, rendered, errs := cfg.Evaluate(pages)
		return re.JSON(200, dryRunResponse{
			Fields:   fields,
			Template: templateName(fields),
			Rendered: rendered,
			Errors:   nonNil(errs),
		})
	}

	// Mode B - field values: the template render of the merged record and
	// user values, against the stored pages.
	var submitted map[string]any
	if err := json.Unmarshal(body, &submitted); err != nil {
		return re.JSON(400, map[string]any{"message": "invalid JSON body", "error": err.Error()})
	}
	merged, convErrs, unknown, _ := mergeSubmitted(cfg, FieldsFromRecord(cfg, record), submitted)
	if len(unknown) > 0 {
		return re.JSON(400, map[string]any{"message": "unknown field(s)", "errors": unknown})
	}
	tplName, rendered, evalErrs := evaluateMerged(cfg, merged, pages)
	if len(convErrs) > 0 {
		rendered = ""
	}
	return re.JSON(200, dryRunResponse{
		Fields:   merged,
		Template: tplName,
		Rendered: rendered,
		Errors:   nonNil(append(convErrs, evalErrs...)),
	})
}

// handleCorrect is the full correction of US-36: conversion, render,
// conflict-suffix resolution, S3 copy, verification, source deletion,
// record update to status validated, corrected event with the full diff -
// and a rollback leaving everything unchanged when any step fails.
func handleCorrect(re *core.RequestEvent, cfg *config.Config, s3 *storage.Client) error {
	if !re.HasSuperuserAuth() {
		return re.ForbiddenError("superusers only", nil)
	}
	id := re.Request.PathValue("id")
	record, err := re.App.FindRecordById("documents", id)
	if err != nil {
		return re.JSON(404, map[string]any{"message": "document not found"})
	}
	pages := recordPages(record)

	body, err := io.ReadAll(re.Request.Body)
	if err != nil {
		return re.JSON(400, map[string]any{"message": "unreadable body", "error": err.Error()})
	}
	var submitted map[string]any
	if len(bytes.TrimSpace(body)) > 0 {
		if err := json.Unmarshal(body, &submitted); err != nil {
			return re.JSON(400, map[string]any{"message": "invalid JSON body", "error": err.Error()})
		}
	}

	merged, convErrs, unknown, cleared := mergeSubmitted(cfg, FieldsFromRecord(cfg, record), submitted)
	if len(convErrs) > 0 || len(unknown) > 0 {
		return re.JSON(400, map[string]any{"message": "invalid values", "errors": append(convErrs, unknown...)})
	}
	tplName, rendered, evalErrs := evaluateMerged(cfg, merged, pages)
	if len(evalErrs) > 0 {
		return re.JSON(400, map[string]any{"message": "correction refused", "errors": evalErrs})
	}

	before := SnapshotFromRecord(cfg, record, true)
	oldKey := record.GetString("file_key")
	target := "files/" + rendered

	ctx := re.Request.Context()
	refiled := false
	finalKey := oldKey
	if target != oldKey {
		finalKey, err = s3.ResolveConflict(ctx, target)
		if err != nil {
			return re.JSON(500, map[string]any{"message": "conflict resolution failed", "error": err.Error()})
		}
		if err := s3.Copy(ctx, oldKey, finalKey); err != nil {
			return re.JSON(500, map[string]any{"message": "S3 copy failed", "error": err.Error()})
		}
		if !s3.Exists(ctx, finalKey) {
			return re.JSON(500, map[string]any{"message": "target not verified after copy", "key": finalKey})
		}
		if err := s3.Remove(ctx, oldKey); err != nil {
			_ = s3.Remove(ctx, finalKey) // best-effort rollback of the copy
			return re.JSON(500, map[string]any{"message": "S3 source deletion failed", "error": err.Error()})
		}
		refiled = true
		record.Set("file_key", finalKey)
	}
	for name := range cleared {
		record.Set(name, nil)
	}
	for name, v := range merged {
		record.Set(name, v)
	}
	record.Set("status", "validated")
	after := SnapshotFromRecord(cfg, record, true)

	// The record save and the corrected event commit together; on failure
	// the S3 state is restored (US-36).
	txErr := re.App.RunInTransaction(func(tx core.App) error {
		if err := tx.Save(record); err != nil {
			return err
		}
		return AppendEvent(tx, record.Id, "corrected", before, after, nil)
	})
	if txErr != nil {
		if refiled {
			_ = s3.Copy(ctx, finalKey, oldKey)
			_ = s3.Remove(ctx, finalKey)
		}
		return re.JSON(500, map[string]any{"message": "correction failed; everything rolled back", "error": txErr.Error()})
	}
	return re.JSON(200, map[string]any{
		"id":       record.Id,
		"file_key": record.GetString("file_key"),
		"status":   "validated",
		"template": tplName,
		"rendered": rendered,
		"fields":   merged,
	})
}

// recordPages decodes the stored page texts of a document (US-13).
func recordPages(record *core.Record) []string {
	var pages []string
	if raw := record.GetString("pages"); raw != "" {
		if err := json.Unmarshal([]byte(raw), &pages); err != nil {
			return nil
		}
	}
	return pages
}

// mergeSubmitted converts the submitted values to their native types and
// merges them over the record values. A null or empty string clears the
// field. It reports per-field conversion errors, unknown fields, and the
// cleared names (the caller must unset them on the record).
func mergeSubmitted(cfg *config.Config, merged map[string]any, submitted map[string]any) (map[string]any, []string, []string, map[string]bool) {
	var convErrs, unknown []string
	cleared := map[string]bool{}
	for name, raw := range submitted {
		f, ok := cfg.Fields[name]
		if !ok {
			unknown = append(unknown, "unknown field: "+name)
			continue
		}
		v, clear, err := convertUserValue(f, raw)
		if err != nil {
			convErrs = append(convErrs, fmt.Sprintf("field %s: %v", name, err))
			continue
		}
		if clear {
			cleared[name] = true
			delete(merged, name)
			continue
		}
		merged[name] = v
	}
	return merged, convErrs, unknown, cleared
}

// convertUserValue converts one submitted JSON value to the declared field
// type (US-35 mode B).
func convertUserValue(f config.Field, raw any) (v any, clear bool, err error) {
	if raw == nil {
		return nil, true, nil
	}
	switch f.Type {
	case "text", "enum":
		s, ok := raw.(string)
		if !ok {
			return nil, false, fmt.Errorf("(%s): value is not a string", f.Type)
		}
		if s == "" {
			return nil, true, nil
		}
		return s, false, nil
	case "number":
		switch t := raw.(type) {
		case float64:
			return t, false, nil
		case string:
			if t == "" {
				return nil, true, nil
			}
			n, perr := strconv.ParseFloat(t, 64)
			if perr != nil {
				return nil, false, fmt.Errorf("(number): %q is not numeric", t)
			}
			return n, false, nil
		default:
			return nil, false, fmt.Errorf("(number): value is not numeric")
		}
	case "date":
		s, ok := raw.(string)
		if !ok {
			return nil, false, fmt.Errorf("(date): value is not a string")
		}
		if s == "" {
			return nil, true, nil
		}
		for _, layout := range []string{"2006-01-02", time.RFC3339} {
			if d, perr := time.Parse(layout, s); perr == nil {
				return d, false, nil
			}
		}
		return nil, false, fmt.Errorf("(date): cannot parse %q", s)
	case "boolean":
		b, ok := raw.(bool)
		if !ok {
			return nil, false, fmt.Errorf("(boolean): value is not a boolean")
		}
		return b, false, nil
	}
	return nil, false, fmt.Errorf("unsupported field type %q", f.Type)
}

// evaluateMerged mirrors cfg.Evaluate on an already-built field map: the
// mandatory check plus the template render against the stored pages. The
// rendered name is empty when any error remains.
func evaluateMerged(cfg *config.Config, merged map[string]any, pages []string) (string, string, []string) {
	var errs []string
	for _, f := range cfg.FieldList {
		if f.Mandatory {
			if v, ok := merged[f.Name]; !ok || v == nil {
				errs = append(errs, "mandatory field missing: "+f.Name)
			}
		}
	}
	tplName := templateName(merged)
	rendered := ""
	if tpl, ok := cfg.Templates[tplName]; !ok {
		errs = append(errs, "unknown template: "+tplName)
	} else if r, rerr := tpl.Render(merged, pages); rerr != nil {
		errs = append(errs, rerr.Error())
	} else {
		rendered = r
	}
	if len(errs) > 0 {
		rendered = ""
	}
	return tplName, rendered, errs
}

func templateName(fields map[string]any) string {
	if v, ok := fields["template"].(string); ok && v != "" {
		return v
	}
	return "default"
}

func nonNil(errs []string) []string {
	if errs == nil {
		return []string{}
	}
	return errs
}
