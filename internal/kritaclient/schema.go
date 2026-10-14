package kritaclient

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math/big"
	"regexp"
	"slices"
	"strconv"
)

type Object map[string]json.RawMessage

type Schema struct {
	Type                 string             `json:"type,omitempty"`
	Properties           map[string]*Schema `json:"properties,omitempty"`
	Required             []string           `json:"required,omitempty"`
	AdditionalProperties *bool              `json:"additionalProperties,omitempty"`
	Default              json.RawMessage    `json:"default,omitempty"`
	AnyOf                []*Schema          `json:"anyOf,omitempty"`
	Enum                 []string           `json:"enum,omitempty"`
	Minimum              *float64           `json:"minimum,omitempty"`
	Maximum              *float64           `json:"maximum,omitempty"`
	ExclusiveMinimum     *float64           `json:"exclusiveMinimum,omitempty"`
	MinLength            int                `json:"minLength,omitempty"`
	Pattern              string             `json:"pattern,omitempty"`
	Items                *Schema            `json:"items,omitempty"`
	PrefixItems          []*Schema          `json:"prefixItems,omitempty"`
	MinItems             int                `json:"minItems,omitempty"`
	MaxItems             int                `json:"maxItems,omitempty"`
}

type ValidationError struct{ Field, Reason string }

func (e *ValidationError) Error() string { return e.Field + ": " + e.Reason }

func invalid(field, reason string) error { return &ValidationError{field, reason} }

func (s *Schema) parse(raw json.RawMessage) (json.RawMessage, error) {
	raw = bytes.TrimSpace(raw)
	if len(s.AnyOf) > 0 {
		for _, variant := range s.AnyOf {
			if parsed, err := variant.parse(raw); err == nil {
				return parsed, nil
			}
		}
		return nil, invalid("value", "does not match an allowed type or range")
	}
	switch s.Type {
	case "object":
		var fields Object
		if err := json.Unmarshal(raw, &fields); err != nil || fields == nil {
			return nil, invalid("value", "expected object")
		}
		if s.AdditionalProperties != nil && !*s.AdditionalProperties {
			for name := range fields {
				if _, ok := s.Properties[name]; !ok {
					return nil, invalid(name, "unknown parameter")
				}
			}
		}
		for name, property := range s.Properties {
			value, exists := fields[name]
			if !exists {
				if property.Default != nil {
					value = property.Default
				} else if slices.Contains(s.Required, name) {
					return nil, invalid(name, "required")
				} else {
					continue
				}
			}
			parsed, err := property.parse(value)
			if err != nil {
				return nil, fmt.Errorf("%s: %w", name, err)
			}
			if bytes.Equal(parsed, []byte("null")) {
				delete(fields, name)
			} else {
				fields[name] = parsed
			}
		}
		return json.Marshal(fields)
	case "array":
		var values []json.RawMessage
		if err := json.Unmarshal(raw, &values); err != nil || values == nil {
			return nil, invalid("value", "expected array")
		}
		if len(values) < s.MinItems || (s.MaxItems > 0 && len(values) > s.MaxItems) {
			return nil, invalid("array", "invalid number of items")
		}
		for index, value := range values {
			item := s.Items
			if index < len(s.PrefixItems) {
				item = s.PrefixItems[index]
			}
			if item != nil {
				parsed, err := item.parse(value)
				if err != nil {
					return nil, fmt.Errorf("index %d: %w", index, err)
				}
				values[index] = parsed
			}
		}
		return json.Marshal(values)
	case "string":
		var value string
		if bytes.Equal(raw, []byte("null")) || json.Unmarshal(raw, &value) != nil {
			return nil, invalid("value", "expected string")
		}
		if len(value) < s.MinLength {
			return nil, invalid("value", "must not be empty")
		}
		if len(s.Enum) > 0 && !slices.Contains(s.Enum, value) {
			return nil, invalid("value", "unsupported option")
		}
		if s.Pattern != "" && !hexColor.MatchString(value) {
			return nil, invalid("color", "expected #RGB, #RRGGBB or Qt #AARRGGBB")
		}
		return json.Marshal(value)
	case "integer":
		value, ok := new(big.Int).SetString(string(raw), 10)
		if !ok {
			return nil, invalid("value", "expected strict integer")
		}
		if s.ExclusiveMinimum != nil && value.Sign() <= 0 {
			return nil, invalid("value", "must be positive")
		}
		if s.Minimum != nil && new(big.Float).SetInt(value).Cmp(big.NewFloat(*s.Minimum)) < 0 {
			return nil, invalid("value", "below minimum")
		}
		if s.Maximum != nil && new(big.Float).SetInt(value).Cmp(big.NewFloat(*s.Maximum)) > 0 {
			return nil, invalid("value", "above maximum")
		}
	case "number":
		value, err := strconv.ParseFloat(string(raw), 64)
		if err != nil || !json.Valid(raw) {
			return nil, invalid("value", "expected finite number")
		}
		if (s.Minimum != nil && value < *s.Minimum) || (s.Maximum != nil && value > *s.Maximum) || (s.ExclusiveMinimum != nil && value <= *s.ExclusiveMinimum) {
			return nil, invalid("value", "outside finite bounds")
		}
	case "boolean":
		if !bytes.Equal(raw, []byte("true")) && !bytes.Equal(raw, []byte("false")) {
			return nil, invalid("value", "expected boolean")
		}
	case "null":
		if !bytes.Equal(raw, []byte("null")) {
			return nil, invalid("value", "expected null")
		}
	default:
		if !json.Valid(raw) {
			return nil, invalid("value", "invalid JSON")
		}
	}
	return raw, nil
}

var hexColor = regexp.MustCompile(`^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$`)

func pointer[T any](value T) *T { return &value }

func object(fields map[string]*Schema, required ...string) *Schema {
	return &Schema{Type: "object", Properties: fields, Required: required, AdditionalProperties: pointer(false)}
}

func defaulted(schema *Schema, value string) *Schema {
	copy := *schema
	copy.Default = json.RawMessage(value)
	return &copy
}

func nullable(schema *Schema) *Schema {
	return &Schema{AnyOf: []*Schema{schema, {Type: "null"}}, Default: json.RawMessage("null")}
}

func enumeration(values ...string) *Schema { return &Schema{Type: "string", Enum: values} }
