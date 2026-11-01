package config

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/expr-lang/expr"
)

var placeholderRe = regexp.MustCompile(`\{([^{}]+)\}`)

type tplPart struct {
	literal string
	program expr.Node
	hasExpr bool
}

// CompiledTemplate is a naming template with pre-compiled {expr} placeholders.
type CompiledTemplate struct {
	Name  string
	parts []tplPart
}

// CompileTemplate parses and compiles every placeholder expression (US-02).
func CompileTemplate(name, src string) (*CompiledTemplate, error) {
	ct := &CompiledTemplate{Name: name}
	last := 0
	for _, loc := range placeholderRe.FindAllStringSubmatchIndex(src, -1) {
		if loc[0] > last {
			ct.parts = append(ct.parts, tplPart{literal: src[last:loc[0]]})
		}
		inner := src[loc[2]:loc[3]]
		prog, err := CompileExpr(inner)
		if err != nil {
			return nil, fmt.Errorf("placeholder {%s}: %w", inner, err)
		}
		ct.parts = append(ct.parts, tplPart{program: prog, hasExpr: true})
		last = loc[1]
	}
	if last < len(src) {
		ct.parts = append(ct.parts, tplPart{literal: src[last:]})
	}
	return ct, nil
}

// Render evaluates the placeholders in the document context and concatenates
// the result. Unset optional fields render as an empty string; a rendered
// value containing "/" is invalid (it is the S3 key delimiter) (US-15).
func (ct *CompiledTemplate) Render(fields map[string]any, pages []string) (string, error) {
	env := ExprEnv(pages, fields)
	var b strings.Builder
	for _, p := range ct.parts {
		if !p.hasExpr {
			b.WriteString(p.literal)
			continue
		}
		v, err := expr.Run(p.program, env)
		if err != nil {
			return "", fmt.Errorf("template render error: %v", err)
		}
		s, err := renderValue(v)
		if err != nil {
			return "", err
		}
		if strings.Contains(s, "/") {
			return "", fmt.Errorf("rendered value %q must not contain '/'", s)
		}
		b.WriteString(s)
	}
	return b.String(), nil
}

func renderValue(v any) (string, error) {
	switch t := v.(type) {
	case nil:
		return "", nil
	case string:
		return t, nil
	case time.Time:
		return t.Format("2006-01-02"), nil
	case float64:
		return strconv.FormatFloat(t, 'f', -1, 64), nil
	case int:
		return strconv.Itoa(t), nil
	case int64:
		return strconv.FormatInt(t, 10), nil
	case bool:
		return strconv.FormatBool(t), nil
	default:
		return "", fmt.Errorf("cannot render value of type %T", v)
	}
}
