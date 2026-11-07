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
	"github.com/expr-lang/expr/vm"
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
	SetKnownFields(c.FieldList)

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
			if err := checkRuleTemplates([]*Rule{child}, c); err != nil {
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
func CompileExpr(src string) (*vm.Program, error) {
	env := ExprEnv(nil, nil)
	for _, name := range knownFields {
		switch knownFieldTypes[name] {
		case "number":
			env[name] = float64(0)
		case "date":
			env[name] = time.Time{}
		case "boolean":
			env[name] = false
		default:
			env[name] = ""
		}
	}
	opts := append([]expr.Option{expr.Env(env)}, exprOptions()...)
	return expr.Compile(src, opts...)
}

func exprOptions() []expr.Option {
	return []expr.Option{
		expr.Function("firstMatch", FirstMatch),
		expr.Function("frPeriod", FrPeriod),
		expr.Function("frDate", FrenchDate),
	}
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
var knownFieldTypes map[string]string

func SetKnownFields(fields []Field) {
	knownFields = nil
	knownFieldTypes = make(map[string]string, len(fields))
	for _, f := range fields {
		knownFields = append(knownFields, f.Name)
		knownFieldTypes[f.Name] = f.Type
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
			program, err := CompileExpr(r.When)
			if err != nil {
				errs = append(errs, fmt.Sprintf("when: %v", err))
				continue
			}
			res, err := expr.Run(program, env)
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
					program, err := CompileExpr(s.Eval)
					if err != nil {
						errs = append(errs, fmt.Sprintf("eval %s: %v", s.Field, err))
						continue
					}
					v, err = expr.Run(program, env)
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

// frDateRe matches a French natural-language date, with or without a day:
// "le 19 janvier 2026", "Janvier 2026", "1er févr. 2026". Abbreviated month
// names are accepted, accents are optional.
var frDateRe = regexp.MustCompile(`(?i)(?:le\s+)?(?:(1er|\d{1,2})\s+)?(janv\.?|janvier|f[eé]vr\.?|f[eé]vrier|mars|avr\.?|avril|mai|juin|juil\.?|juillet|ao[uû]t|sept\.?|septembre|oct\.?|octobre|nov\.?|novembre|d[eé]c\.?|d[eé]cembre)\s+(\d{4})`)

// frMonths maps the folded month names to their numbers. Keys are lowercase
// and unaccented; foldFrench normalizes what the regex captured.
var frMonths = map[string]time.Month{
	"janv": time.January, "janvier": time.January,
	"fevr": time.February, "fevrier": time.February,
	"mars": time.March,
	"avr": time.April, "avril": time.April,
	"mai": time.May,
	"juin": time.June,
	"juil": time.July, "juillet": time.July,
	"aout": time.August,
	"sept": time.September, "septembre": time.September,
	"oct": time.October, "octobre": time.October,
	"nov": time.November, "novembre": time.November,
	"dec": time.December, "decembre": time.December,
}

// foldFrench lowercases and strips the French diacritics used in month
// names plus the abbreviation dot, so the lookup tolerates "Février",
// "FEVRIER" and "févr." alike.
func foldFrench(s string) string {
	r := strings.ToLower(s)
	for _, a := range [][2]string{{"é", "e"}, {"è", "e"}, {"ê", "e"}, {"û", "u"}, {"â", "a"}} {
		r = strings.ReplaceAll(r, a[0], a[1])
	}
	return strings.TrimSuffix(r, ".")
}

// FrenchDate is the frDate(text) helper (arch.md §5). It extracts and
// parses a French natural-language date — "le 19 janvier 2026", the day
// being optional ("Janvier 2026" gives the first of the month) and
// abbreviated month names accepted ("1er févr. 2026") — into a native
// date. Go layouts only know English month names, so date(value, layout)
// cannot parse these. A text containing no French date is a rule error
// and routes the document to pending/.
func FrenchDate(args ...any) (any, error) {
	if len(args) == 0 {
		return nil, fmt.Errorf("frDate: text required")
	}
	text, _ := args[0].(string)
	m := frDateRe.FindStringSubmatch(text)
	if m == nil {
		return nil, fmt.Errorf("frDate: no French date found")
	}
	day := 1
	if m[1] != "" && !strings.EqualFold(m[1], "1er") {
		d, err := strconv.Atoi(m[1])
		if err != nil {
			return nil, fmt.Errorf("frDate: %w", err)
		}
		day = d
	}
	month, ok := frMonths[foldFrench(m[2])]
	if !ok {
		return nil, fmt.Errorf("frDate: unknown month %q", m[2])
	}
	year, err := strconv.Atoi(m[3])
	if err != nil {
		return nil, fmt.Errorf("frDate: %w", err)
	}
	d := time.Date(year, month, day, 0, 0, 0, 0, time.UTC)
	if d.Month() != month || d.Day() != day {
		return nil, fmt.Errorf("frDate: %d %s %d is not a valid date", day, m[2], year)
	}
	return d, nil
}

var frenchMonths = []string{"Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
	"Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre"}

// FrPeriod is the frPeriod(value, unit, shift, layout?) helper (arch.md §5).
func FrPeriod(args ...any) (any, error) {
	if len(args) < 3 {
		return "", fmt.Errorf("frPeriod: value, unit and shift required")
	}
	d, ok := args[0].(time.Time)
	if !ok {
		return "", fmt.Errorf("frPeriod: value must be a native date")
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
		return "", fmt.Errorf("frPeriod: unknown unit %q (want month|quarter|half|year)", unit)
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
