// Package pbapp: official enum value lists (US-03, US-37).
//
// Declared enum fields are PocketBase select columns. The application owns
// their value lists: creation with a single "-----" placeholder, values
// added when a processed document or a dashboard action introduces them,
// unused values removed at startup. The special "template" field always
// keeps exactly the template names of the configuration.
package pbapp

import (
	"fmt"
	"sort"

	"ganigeorgiev.com/pocketbase/pocketbase/core"
	"github.com/napnap75/archivist/internal/config"
)

// DummyEnumValue is the placeholder that keeps a select field valid when a
// collection is created before any real value exists. It survives cleanup
// only while it is the field's only value (a PocketBase select needs at
// least one).
const DummyEnumValue = "-----"

// enumFieldValues reads the official select values of a declared enum
// field, placeholder excluded, sorted.
func enumFieldValues(app core.App, name string) ([]string, error) {
	coll, err := app.FindCollectionByNameOrId("documents")
	if err != nil {
		return nil, err
	}
	field, _ := coll.Fields.GetByName(name).(*core.SelectField)
	if field == nil {
		return nil, fmt.Errorf("field %s is not an enum", name)
	}
	values := make([]string, 0, len(field.Values))
	for _, v := range field.Values {
		if v != DummyEnumValue {
			values = append(values, v)
		}
	}
	sort.Strings(values)
	return values, nil
}

// enumVocabulary returns the initial values of an enum field at collection
// creation: the template names for the special "template" field, the dummy
// placeholder otherwise.
func enumVocabulary(cfg *config.Config, name string) []string {
	if name == "template" {
		if names := sortedTemplateNames(cfg); len(names) > 0 {
			return names
		}
	}
	return []string{DummyEnumValue}
}

// AddEnumValue appends value to the field's official select list when it
// is missing; the record save that follows can then validate against it.
func AddEnumValue(app core.App, name, value string) error {
	coll, err := app.FindCollectionByNameOrId("documents")
	if err != nil {
		return err
	}
	field, _ := coll.Fields.GetByName(name).(*core.SelectField)
	if field == nil {
		return fmt.Errorf("field %s is not an enum", name)
	}
	for _, v := range field.Values {
		if v == value {
			return nil
		}
	}
	field.Values = append(field.Values, value)
	return app.Save(coll)
}

// EnsureEnumValues adds every enum value carried by fields to its official
// list, so the subsequent record save passes the select validation.
func EnsureEnumValues(app core.App, cfg *config.Config, fields map[string]any) error {
	for _, f := range cfg.FieldList {
		if f.Type != "enum" {
			continue
		}
		v, _ := fields[f.Name].(string)
		if v == "" {
			continue
		}
		if err := AddEnumValue(app, f.Name, v); err != nil {
			return fmt.Errorf("enum %s: %w", f.Name, err)
		}
	}
	return nil
}

// CleanupEnumValues runs at startup: the "template" field keeps exactly the
// configured template names; every other enum field keeps only the values
// still used by a document; the dummy placeholder survives only when a
// field has no other value (a PocketBase select needs at least one).
func CleanupEnumValues(app core.App, cfg *config.Config) {
	for _, f := range cfg.FieldList {
		if f.Type != "enum" {
			continue
		}
		coll, err := app.FindCollectionByNameOrId("documents")
		if err != nil {
			return
		}
		field, _ := coll.Fields.GetByName(f.Name).(*core.SelectField)
		if field == nil {
			continue
		}

		var want []string
		if f.Name == "template" {
			want = sortedTemplateNames(cfg)
		} else {
			used, err := distinctValues(app, f.Name)
			if err != nil {
				app.Logger().Error("documents: enum cleanup query failed", "field", f.Name, "error", err)
				continue
			}
			want = used
		}
		if len(want) == 0 {
			want = []string{DummyEnumValue}
		}
		sort.Strings(want)

		same := len(want) == len(field.Values)
		if same {
			for i, v := range want {
				if field.Values[i] != v {
					same = false
					break
				}
			}
		}
		if same {
			continue
		}
		removed := diffStrings(field.Values, want)
		field.Values = want
		if err := app.Save(coll); err != nil {
			app.Logger().Error("documents: enum cleanup save failed", "field", f.Name, "error", err)
			continue
		}
		app.Logger().Info("documents: enum values cleaned up", "field", f.Name, "removed", removed)
	}
}

// distinctValues returns the sorted non-empty distinct values of a column.
func distinctValues(app core.App, name string) ([]string, error) {
	var rows []struct {
		V string `db:"v"`
	}
	err := app.DB().NewQuery(
		fmt.Sprintf(`SELECT DISTINCT "%s" AS v FROM documents WHERE "%s" IS NOT NULL AND TRIM("%s") != '' ORDER BY v`, name, name, name),
	).All(&rows)
	if err != nil {
		return nil, err
	}
	values := make([]string, 0, len(rows))
	for _, row := range rows {
		values = append(values, row.V)
	}
	return values, nil
}

func diffStrings(from, to []string) []string {
	keep := map[string]bool{}
	for _, v := range to {
		keep[v] = true
	}
	var removed []string
	for _, v := range from {
		if !keep[v] {
			removed = append(removed, v)
		}
	}
	return removed
}
