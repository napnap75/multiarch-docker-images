package pbapp

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"ganigeorgiev.com/pocketbase/pocketbase/core"
	"github.com/napnap75/archivist/internal/config"
	"github.com/tailscale/hujson"
)

type editorRuleGroup struct {
	Header string `json:"header"`
	Rules  string `json:"rules"`
}

type configEditorSource struct {
	Source    string            `json:"source"`
	Fields    []config.Field    `json:"fields"`
	Templates map[string]string `json:"templates"`
	Groups    []editorRuleGroup `json:"groups"`
}

type configDraftRequest struct {
	Source string   `json:"source"`
	Pages  []string `json:"pages"`
}

func parseEditorSource(source []byte) (configEditorSource, error) {
	standard, err := hujson.Standardize(bytes.Clone(source))
	if err != nil {
		return configEditorSource{}, fmt.Errorf("invalid JSONC: %w", err)
	}
	var decoded struct {
		Fields    []config.Field    `json:"fields"`
		Templates map[string]string `json:"templates"`
	}
	if err := json.Unmarshal(standard, &decoded); err != nil {
		return configEditorSource{}, fmt.Errorf("invalid configuration: %w", err)
	}

	root, err := hujson.Parse(source)
	if err != nil {
		return configEditorSource{}, fmt.Errorf("invalid JSONC: %w", err)
	}
	rulesValue := root.Find("/rules")
	if rulesValue == nil {
		return configEditorSource{}, fmt.Errorf("configuration is missing rules")
	}
	rulesArray, ok := rulesValue.Value.(*hujson.Array)
	if !ok {
		return configEditorSource{}, fmt.Errorf("configuration rules must be an array")
	}

	result := configEditorSource{
		Source:    string(source),
		Fields:    decoded.Fields,
		Templates: decoded.Templates,
	}
	var groupRules []json.RawMessage
	groupHeader := ""
	started := false
	flush := func() error {
		if !started {
			return nil
		}
		data, err := marshalEditorJSON(groupRules)
		if err != nil {
			return err
		}
		result.Groups = append(result.Groups, editorRuleGroup{Header: groupHeader, Rules: string(data)})
		groupRules = nil
		return nil
	}
	for _, element := range rulesArray.Elements {
		if header := sectionHeader(element.BeforeExtra); header != "" {
			if err := flush(); err != nil {
				return configEditorSource{}, fmt.Errorf("format rule group: %w", err)
			}
			groupHeader = header
			started = true
		} else if !started {
			started = true
			groupHeader = ""
		}

		rule := element
		rule.BeforeExtra = nil
		rule.AfterExtra = nil
		standardRule, err := hujson.Standardize(rule.Pack())
		if err != nil {
			return configEditorSource{}, fmt.Errorf("invalid rule JSONC: %w", err)
		}
		groupRules = append(groupRules, json.RawMessage(standardRule))
	}
	if err := flush(); err != nil {
		return configEditorSource{}, fmt.Errorf("format rule group: %w", err)
	}
	if len(result.Groups) == 0 {
		result.Groups = append(result.Groups, editorRuleGroup{Rules: "[]"})
	}
	if result.Templates == nil {
		result.Templates = map[string]string{}
	}
	return result, nil
}

func sectionHeader(extra hujson.Extra) string {
	for _, line := range strings.Split(string(extra), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "// --- ") {
			return line
		}
	}
	return ""
}

func composeEditorSource(fields []config.Field, templates map[string]string, groups []editorRuleGroup) ([]byte, error) {
	fieldsJSON, err := marshalEditorJSON(fields)
	if err != nil {
		return nil, fmt.Errorf("encode fields: %w", err)
	}
	templatesJSON, err := marshalEditorJSON(templates)
	if err != nil {
		return nil, fmt.Errorf("encode templates: %w", err)
	}

	var out strings.Builder
	out.WriteString("{\n  \"fields\": ")
	writeIndentedJSON(&out, fieldsJSON, "  ")
	out.WriteString(",\n  \"rules\": [\n")

	hasRule := false
	separatorWritten := false
	for _, group := range groups {
		header := strings.TrimSpace(group.Header)
		if header != "" {
			if strings.ContainsAny(header, "\r\n") || !strings.HasPrefix(header, "// --- ") {
				return nil, fmt.Errorf("invalid rule section heading %q", group.Header)
			}
			if hasRule && !separatorWritten {
				out.WriteString(",\n")
				separatorWritten = true
			}
			out.WriteString("    " + header + "\n")
		}

		standard, err := hujson.Standardize([]byte(group.Rules))
		if err != nil {
			return nil, fmt.Errorf("invalid rules in %q: %w", header, err)
		}
		var rules []json.RawMessage
		if err := json.Unmarshal(standard, &rules); err != nil {
			return nil, fmt.Errorf("rules in %q must be a JSON array: %w", header, err)
		}
		for _, rule := range rules {
			var object map[string]json.RawMessage
			if err := json.Unmarshal(rule, &object); err != nil || object == nil {
				return nil, fmt.Errorf("each rule in %q must be an object", header)
			}
			if hasRule && !separatorWritten {
				out.WriteString(",\n")
			}
			prettyRule, err := marshalEditorJSON(json.RawMessage(rule))
			if err != nil {
				return nil, fmt.Errorf("encode rule: %w", err)
			}
			out.WriteString("    ")
			writeIndentedJSON(&out, prettyRule, "    ")
			hasRule = true
			separatorWritten = false
		}
	}
	out.WriteString("\n  ],\n  \"templates\": ")
	writeIndentedJSON(&out, templatesJSON, "  ")
	out.WriteString("\n}\n")
	return []byte(out.String()), nil
}

func marshalEditorJSON(value any) ([]byte, error) {
	var out bytes.Buffer
	encoder := json.NewEncoder(&out)
	encoder.SetEscapeHTML(false)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(out.Bytes(), []byte("\n")), nil
}

func writeIndentedJSON(out *strings.Builder, value []byte, continuationIndent string) {
	lines := bytes.Split(value, []byte("\n"))
	out.Write(lines[0])
	for _, line := range lines[1:] {
		out.WriteByte('\n')
		out.WriteString(continuationIndent)
		out.Write(line)
	}
}

func handleConfigSource(re *core.RequestEvent, manager *config.Manager) error {
	if !re.HasSuperuserAuth() {
		return re.ForbiddenError("superusers only", nil)
	}
	return manager.Read(func(snapshot *config.Snapshot) error {
		source, err := parseEditorSource([]byte(snapshot.Source))
		if err != nil {
			return re.JSON(500, map[string]any{"message": "active configuration cannot be read by the editor", "error": err.Error()})
		}
		return re.JSON(200, source)
	})
}

func handleConfigDraft(re *core.RequestEvent, filesPrefix string) error {
	if !re.HasSuperuserAuth() {
		return re.ForbiddenError("superusers only", nil)
	}
	body, err := io.ReadAll(re.Request.Body)
	if err != nil {
		return re.JSON(400, map[string]any{"message": "unreadable body", "error": err.Error()})
	}
	var request configDraftRequest
	if err := json.Unmarshal(body, &request); err != nil {
		return re.JSON(400, map[string]any{"message": "invalid JSON body", "error": err.Error()})
	}
	cfg, err := config.LoadSource([]byte(request.Source))
	if err != nil {
		return re.JSON(422, map[string]any{"message": "configuration is invalid", "errors": []string{err.Error()}})
	}
	if len(request.Pages) == 0 {
		errs := []string{}
		if _, ok := cfg.Templates["default"]; !ok {
			errs = append(errs, "unknown template: default")
		}
		return re.JSON(200, dryRunResponse{
			Fields:   map[string]any{},
			Template: "default",
			Errors:   errs,
			Trace:    []config.TraceEntry{},
		})
	}
	fields, rendered, errs, trace := cfg.EvaluateWithTrace(request.Pages)
	return re.JSON(200, dryRunResponse{
		Fields:   fields,
		Template: templateName(fields),
		Rendered: withFilesPrefix(filesPrefix, rendered),
		Errors:   nonNil(errs),
		Trace:    nonNilTrace(trace),
	})
}

func handleSaveConfig(re *core.RequestEvent, manager *config.Manager, path string) error {
	if !re.HasSuperuserAuth() {
		return re.ForbiddenError("superusers only", nil)
	}
	body, err := io.ReadAll(re.Request.Body)
	if err != nil {
		return re.JSON(400, map[string]any{"message": "unreadable body", "error": err.Error()})
	}
	var request configDraftRequest
	if err := json.Unmarshal(body, &request); err != nil {
		return re.JSON(400, map[string]any{"message": "invalid JSON body", "error": err.Error()})
	}
	cfg, err := config.LoadSource([]byte(request.Source))
	if err != nil {
		return re.JSON(422, map[string]any{"message": "configuration is invalid", "errors": []string{err.Error()}})
	}

	if err := manager.Update(func(previous *config.Snapshot) (*config.Snapshot, error) {
		if err := writeConfigAtomically(path, []byte(request.Source)); err != nil {
			return nil, fmt.Errorf("configuration file write failed: %w", err)
		}
		if err := ensureDocuments(re.App, cfg); err != nil {
			restoreErr := writeConfigAtomically(path, []byte(previous.Source))
			schemaRollbackErr := ensureDocuments(re.App, previous.Config)
			return nil, fmt.Errorf("configuration schema migration failed: %v; file rollback: %s; schema rollback: %s",
				err, errorString(restoreErr), errorString(schemaRollbackErr))
		}
		CleanupEnumValues(re.App, cfg)
		return &config.Snapshot{Config: cfg, Source: request.Source}, nil
	}); err != nil {
		return re.JSON(500, map[string]any{"message": "configuration activation failed", "error": err.Error()})
	}
	return re.JSON(200, map[string]any{"status": "active", "fields": len(cfg.FieldList), "templates": sortedTemplateNames(cfg)})
}

func writeConfigAtomically(path string, source []byte) error {
	directory := filepath.Dir(path)
	base := filepath.Base(path)
	mode := os.FileMode(0600)
	if info, err := os.Stat(path); err == nil {
		mode = info.Mode().Perm()
	} else if !os.IsNotExist(err) {
		return err
	}

	file, err := os.CreateTemp(directory, "."+base+"-*.tmp")
	if err != nil {
		return err
	}
	temporaryPath := file.Name()
	defer os.Remove(temporaryPath)
	if err := file.Chmod(mode); err != nil {
		file.Close()
		return err
	}
	if _, err := file.Write(source); err != nil {
		file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return err
	}
	return nil
}

func errorString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}
