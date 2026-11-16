package pbapp

import (
	"os"
	"strings"
	"testing"

	"github.com/napnap75/archivist/internal/config"
)

func TestEditorSourcePreservesRuleSectionComments(t *testing.T) {
	source := []byte(strings.ReplaceAll(`
	{
	  "fields": [{"name":"kind","french":"Type","type":"text","mandatory":true}],
	  "rules": [
	    // --- Alpha --------------------------------
		    {"when":"pages[0] contains @alpha@ && pages[0] contains @beta@","set":[{"field":"kind","value":"Alpha"}]},
	    // --- Beta ---------------------------------
	    {"when":"pages[0] contains @beta@","set":[{"field":"kind","value":"Beta"}]}
	  ],
	  "templates": {"default":"{kind}.pdf"}
	}
	`, "@", "`"))

	editor, err := parseEditorSource(source)
	if err != nil {
		t.Fatalf("parse editor source: %v", err)
	}
	if len(editor.Groups) != 2 {
		t.Fatalf("got %d rule groups, want 2", len(editor.Groups))
	}
	if editor.Groups[0].Header != "// --- Alpha --------------------------------" || editor.Groups[1].Header != "// --- Beta ---------------------------------" {
		t.Fatalf("rule section headers were not preserved: %#v", editor.Groups)
	}
	if !strings.Contains(editor.Groups[0].Rules, "&&") || strings.Contains(editor.Groups[0].Rules, `\u0026`) {
		t.Fatalf("logical operator was escaped in editor rule text: %s", editor.Groups[0].Rules)
	}

	rebuilt, err := composeEditorSource(editor.Fields, editor.Templates, editor.Groups)
	if err != nil {
		t.Fatalf("compose editor source: %v", err)
	}
	if !strings.Contains(string(rebuilt), editor.Groups[0].Header) || !strings.Contains(string(rebuilt), editor.Groups[1].Header) {
		t.Fatalf("composed source lost section comments:\n%s", rebuilt)
	}
	if !strings.Contains(string(rebuilt), "&&") || strings.Contains(string(rebuilt), `\u0026`) {
		t.Fatalf("logical operator was escaped in composed source:\n%s", rebuilt)
	}
	if _, err := config.LoadSource(rebuilt); err != nil {
		t.Fatalf("composed source does not compile: %v\n%s", err, rebuilt)
	}
}

func TestEditorRoundTripsProjectConfig(t *testing.T) {
	source, err := os.ReadFile("../../tests/config.jsonc")
	if err != nil {
		t.Fatalf("read project test config: %v", err)
	}
	editor, err := parseEditorSource(source)
	if err != nil {
		t.Fatalf("parse project config for editor: %v", err)
	}
	if len(editor.Groups) < 2 {
		t.Fatalf("project config groups = %d, want multiple commented sections", len(editor.Groups))
	}
	rebuilt, err := composeEditorSource(editor.Fields, editor.Templates, editor.Groups)
	if err != nil {
		t.Fatalf("compose project config: %v", err)
	}
	if _, err := config.LoadSource(rebuilt); err != nil {
		t.Fatalf("recompiled project config: %v", err)
	}
}
