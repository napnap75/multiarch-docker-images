// Package config loads, validates and compiles the functional configuration
// (config.jsonc) per US-02.
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/expr-lang/expr"
	"github.com/tailscale/hujson"
)

// Field is one declared extraction field.
type Field struct {
	Name      string `json:"name"`
	Type      string `json:"type"` // text | number | date | boolean | enum
	Mandatory bool   `json:"mandatory"`
}

// Assignment is one "set" entry of a rule.
type Assignment struct {
	Field string `json:"field"`
	Value any    `json:"value"` // constant (mutually exclusive with Eval)
	Eval  string `json:"eval"`  // expression evaluated in the document context
}

// Rule is one node of the ordered rule tree.
type Rule struct {
	When   string       `json:"when"` // required expr expression
	Set    []Assignment `json:"set"`
	Delete []string     `json:"delete"`
	Rules  []*Rule      `json:"rules"`
}

type rawConfig struct {
	Fields    []Field           `json:"fields"`
	Rules     []*Rule           `json:"rules"`
	Templates map[string]string `json:"templates"`
}

// Config is the compiled functional configuration.
type Config struct {
	Fields    map[string]Field
	FieldList []Field
	Rules     []*Rule
	Templates map[string]*CompiledTemplate
}

var validTypes = map[string]bool{"text": true, "number": true, "date": true, "boolean": true, "enum": true}

// Load parses config.jsonc (comments allowed), validates it and compiles every
// regex and expression. Any error rejects the whole file (US-02, US-05).
func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	// JSONC: standardize to plain JSON before unmarshalling.
	std, err := hujson.Standardize(data)
	if err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	var raw rawConfig
	if err := json.Unmarshal(std, &raw); err != nil {
		return nil, fmt.Errorf("decode %s: %w", path, err)
	}

	c := &Config{Fields: map[string]Field{}, Templates: map[string]*CompiledTemplate{}}

	// --- fields -----------------------------------------------------------
	seen := map[string]bool{}
	for _, f := range raw.Fields {
		if f.Name == "" {
			return nil, fmt.Errorf("field with empty name")
		}
		if seen[f.Name] {
			return nil, fmt.Errorf("duplicate field %q", f.Name)
		}
		if !validTypes[f.Type] {
			return nil, fmt.Errorf("field %q: invalid type %q (want text|number|date|boolean|enum)", f.Name, f.Type)
		}
		seen[f.Name] = true
		c.Fields[f.Name] = f
		c.FieldList = append(c.FieldList, f)
	}

	// --- rules --------------------------------------------------------------
	if err := validateRules(raw.Rules, c.Fields); err != nil {
		return nil, err
	}
	c.Rules = raw.Rules

	// --- templates -----------------------------------------------------------
	for name, src := range raw.Templates {
		ct, err := CompileTemplate(name, src)
		if err != nil {
			return nil, fmt.Errorf("template %q: %w", name, err)
		}
		c.Templates[name] = ct
	}

	// Cross-reference check (US-02): template names used in the rules exist.
	if err := checkRuleTemplates(raw.Rules, c); err != nil {
		return nil, err
	}
	return c, nil
}

func checkRuleTemplates(rules []*Rule, c *Config) error {
	for _, r := range rules {
		for _, s := range r.Set {
			if s.Field == "template" {
				name, _ := s.Value.(string)
				if name != "" && s.Eval == "" {
					if _, ok := c.Templates[name]; !ok {
						return fmt.Errorf("rule sets template %q which does not exist in templates", name)
					}
				}
			}
		}
		for _, child := range r.Rules {
			if err := checkRuleTemplates(child, c); err != nil {
				return err
			}
		}
	}
	return nil
}

func validateRules(rules []*Rule, fields map[string]Field) error {
	for _, r := range rules {
		if r.When == "" {
			return fmt.Errorf("rule with empty/missing when expression")
		}
		if _, err := CompileExpr(r.When); err != nil {
			return fmt.Errorf("when expression: %w", err)
		}
		for _, s := range r.Set {
			f, ok := fields[s.Field]
			if !ok {
				return fmt.Errorf("set: unknown field %q", s.Field)
			}
			if s.Eval != "" {
				if _, err := CompileExpr(s.Eval); err != nil {
					return fmt.Errorf("set %s: %w", s.Field, err)
				}
			} else if err := ValidateValue(f, s.Value); err != nil {
				return fmt.Errorf("set %s: %w", s.Field, err)
			}
		}
		for _, d := range r.Delete {
			if _, ok := fields[d]; !ok {
				return fmt.Errorf("delete: unknown field %q", d)
			}
		}
		if err := validateRules(r.Rules, fields); err != nil {
			return err
		}
	}
	return nil
}

// CompileExpr compiles one expression against the typed document context.
func CompileExpr(src string) (expr.Node, error) {
	return expr.Compile(src, expr.Env(ExprEnv(nil, nil)))
}

// ExprEnv builds the evaluation environment: pages + declared fields.
// Unset fields are exposed as nil so that references to them stay falsy
// instead of failing at runtime.
func ExprEnv(pages []string, fields map[string]any) map[string]any {
	env := map[string]any{}
	for _, f := range knownFields {
		env[f] = nil
	}
	env["pages"] = pages
	for k, v := range fields {
		env[k] = v
	}
	return env
}

var knownFields []string

func SetKnownFields(fields []Field) {
	knownFields = nil
	for _, f := range fields {
		knownFields = append(knownFields, f.Name)
	}
}

// ValidateValue checks that a rule-assigned value matches the declared type
// (type mismatch is a rule error, US-14).
func ValidateValue(f Field, v any) error {
	switch f.Type {
	case "text", "enum":
		if _, ok := v.(string); !ok {
			return fmt.Errorf("field %s (%s): value is not a string", f.Name, f.Type)
		}
	case "number":
		switch v.(type) {
		case float64, int, int64:
		default:
			return fmt.Errorf("field %s (number): value is not numeric", f.Name)
		}
	case "date":
		if _, ok := v.(time.Time); !ok {
			return fmt.Errorf("field %s (date): value is not a native date", f.Name)
		}
	case "boolean":
		if _, ok := v.(bool); !ok {
			return fmt.Errorf("field %s (boolean): value is not a boolean", f.Name)
		}
	}
	return nil
}

// Classify evaluates the ordered rule tree against the document pages and
// returns the extracted fields plus the list of rule errors (US-14).
func (c *Config) Classify(pages []string) (map[string]any, []string) {
	fields := map[string]any{}
	var errs []string

	var walk func(rules []*Rule)
	walk = func(rules []*Rule) {
		for _, r := range rules {
			env := ExprEnv(pages, fields)
			res, err := expr.Eval(r.When, env)
			if err != nil {
				errs = append(errs, fmt.Sprintf("when: %v", err))
				continue
			}
			if !truthy(res) {
				continue
			}
			for _, s := range r.Set {
				var v any
				if s.Eval != "" {
					var err error
					v, err = expr.Eval(s.Eval, env)
					if err != nil {
						errs = append(errs, fmt.Sprintf("eval %s: %v", s.Field, err))
						continue
					}
				} else {
					v = s.Value
				}
				if err := ValidateValue(c.Fields[s.Field], v); err != nil {
					errs = append(errs, err.Error())
					continue
				}
				fields[s.Field] = v
			}
			for _, d := range r.Delete {
				delete(fields, d)
			}
			walk(r.Rules)
		}
	}
	walk(c.Rules)
	return fields, errs
}

func truthy(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case bool:
		return t
	case string:
		return t != ""
	default:
		return true
	}
}

// Evaluate runs classification and computes the verdict (US-15).
// It returns the fields, the rendered file name (empty when invalid) and the
// per-cause error list.
func (c *Config) Evaluate(pages []string) (map[string]any, string, []string) {
	fields, errs := c.Classify(pages)

	// Mandatory fields.
	for _, f := range c.FieldList {
		if f.Mandatory {
			if v, ok := fields[f.Name]; !ok || v == nil {
				errs = append(errs, "mandatory field missing: "+f.Name)
			}
		}
	}

	// Template selection: the "template" field, or "default".
	tplName := "default"
	if v, ok := fields["template"].(string); ok && v != "" {
		tplName = v
	}
	tpl, ok := c.Templates[tplName]
	if !ok {
		errs = append(errs, "unknown template: "+tplName)
		return fields, "", errs
	}
	rendered, err := tpl.Render(fields, pages)
	if err != nil {
		errs = append(errs, err.Error())
		return fields, "", errs
	}
	return fields, rendered, errs
}

// FirstMatch is the firstMatch(regex, texts?) helper (US-14 / arch.md §5).
// It returns the first match (first capture group when present) or an empty
// string when no match is found.
func FirstMatch(args ...any) (any, error) {
	if len(args) == 0 {
		return "", fmt.Errorf("firstMatch: regex required")
	}
	pattern, _ := args[0].(string)
	re, err := regexp.Compile(pattern)
	if err != nil {
		return "", fmt.Errorf("firstMatch: %w", err)
	}
	var texts []string
	if len(args) > 1 {
		switch t := args[1].(type) {
		case string:
			texts = []string{t}
		case []string:
			texts = t
		case []any:
			for _, e := range t {
				texts = append(texts, toString(e))
			}
		default:
			return "", fmt.Errorf("firstMatch: unsupported text argument")
		}
	}
	for _, t := range texts {
		if m := re.FindStringSubmatch(t); m != nil {
			if len(m) > 1 {
				return m[1], nil
			}
			return m[0], nil
		}
	}
	return "", nil
}

func toString(v any) string {
	switch t := v.(type) {
	case string:
		return t
	case nil:
		return ""
	default:
		return fmt.Sprintf("%v", t)
	}
}

var frenchMonths = []string{"Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
	"Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre"}

// Period is the period(value, unit, shift, layout?) helper (arch.md §5).
func Period(args ...any) (any, error) {
	if len(args) < 3 {
		return "", fmt.Errorf("period: value, unit and shift required")
	}
	d, ok := args[0].(time.Time)
	if !ok {
		return "", fmt.Errorf("period: value must be a native date")
	}
	unit, _ := args[1].(string)
	shift, _ := toInt(args[2])
	var layout string
	if len(args) > 3 {
		layout, _ = args[3].(string)
	}

	y, m := d.Year(), int(d.Month())
	switch unit {
	case "month":
		total := (y*12 + (m - 1)) + shift
		y2, m2 := total/12, total%12
		if m2 < 0 {
			m2 += 12
			y2--
		}
		month := frenchMonths[m2]
		if layout == "" {
			layout = "January 2006"
		}
		first := time.Date(y2, time.Month(m2)+1, 1, 0, 0, 0, 0, time.UTC)
		s := first.Format(layout)
		return strings.Replace(s, "January", month, 1), nil
	case "quarter":
		q := (m-1)/3 + shift
		for q < 0 {
			q += 4
			y--
		}
		for q > 3 {
			q -= 4
			y++
		}
		return fmt.Sprintf("T%d %d", q+1, y), nil
	case "half":
		h := (m-1)/6 + shift
		for h < 0 {
			h += 2
			y--
		}
		for h > 1 {
			h -= 2
			y++
		}
		return fmt.Sprintf("S%d %d", h+1, y), nil
	case "year":
		y += shift
		if layout == "" {
			layout = "2006"
		}
		return time.Date(y, 1, 1, 0, 0, 0, 0, time.UTC).Format(layout), nil
	default:
		return "", fmt.Errorf("period: unknown unit %q (want month|quarter|half|year)", unit)
	}
}

func toInt(v any) (int, bool) {
	switch t := v.(type) {
	case int:
		return t, true
	case int64:
		return int(t), true
	case float64:
		return int(t), true
	case string:
		n, err := strconv.Atoi(t)
		return n, err == nil
	}
	return 0, false
}
