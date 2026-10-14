package kritaclient

import (
	"encoding/json"
	"fmt"
	"strconv"
)

type Command struct {
	action string
	params json.RawMessage
}

type BatchError struct{ Err error }

func (e *BatchError) Error() string { return e.Err.Error() }
func (e *BatchError) Unwrap() error { return e.Err }

func ParseTool(action string, raw json.RawMessage) (Command, error) {
	if len(raw) == 0 {
		raw = json.RawMessage("{}")
	}
	if action == "health" {
		params, err := InputSchema(action).parse(raw)
		return Command{action, params}, err
	}
	if action == "draw_shape" {
		var fields Object
		if err := json.Unmarshal(raw, &fields); err != nil {
			return Command{}, fmt.Errorf("decode parameters: %w", err)
		}
		if _, exists := fields["line_width"]; exists {
			return Command{}, invalid("line_width", "unknown tool parameter (available only in batch)")
		}
	}
	return Parse(action, raw)
}

func (c Command) Action() string          { return c.action }
func (c Command) Params() json.RawMessage { return append(json.RawMessage(nil), c.params...) }

func Parse(action string, raw json.RawMessage) (Command, error) {
	schema, ok := actionSchemas[action]
	if !ok {
		return Command{}, invalid("action", "Unknown action: "+action)
	}
	if len(raw) == 0 {
		raw = json.RawMessage("{}")
	}
	params, err := schema.parse(raw)
	if err != nil {
		return Command{}, fmt.Errorf("Invalid %s parameters: %w", action, err)
	}
	var fields Object
	if err := json.Unmarshal(params, &fields); err != nil {
		return Command{}, fmt.Errorf("decode parameters: %w", err)
	}
	switch action {
	case "batch":
		var entries []struct {
			Action string          `json:"action"`
			Params json.RawMessage `json:"params"`
		}
		if err := json.Unmarshal(fields["commands"], &entries); err != nil {
			return Command{}, fmt.Errorf("decode batch: %w", err)
		}
		children := make([]json.RawMessage, 0, len(entries))
		for index, entry := range entries {
			if _, known := actionSchemas[entry.Action]; !known || entry.Action == "batch" {
				return Command{}, &BatchError{invalid("commands", fmt.Sprintf("Invalid batch action at index %d: %s", index, entry.Action))}
			}
			child, err := Parse(entry.Action, entry.Params)
			if err != nil {
				return Command{}, &BatchError{fmt.Errorf("Invalid batch parameters at index %d: %w", index, err)}
			}
			encoded, err := child.MarshalJSON()
			if err != nil {
				return Command{}, fmt.Errorf("encode batch child: %w", err)
			}
			children = append(children, encoded)
		}
		encoded, err := json.Marshal(children)
		if err != nil {
			return Command{}, fmt.Errorf("encode batch commands: %w", err)
		}
		fields["commands"] = encoded
	case "stroke":
		if string(fields["curve"]) == `"bezier"` {
			var points []json.RawMessage
			if err := json.Unmarshal(fields["points"], &points); err != nil {
				return Command{}, fmt.Errorf("decode points: %w", err)
			}
			if len(points) < 4 || (len(points)-1)%3 != 0 {
				return Command{}, invalid("points", "Cubic Bezier strokes require 3n+1 points, with n >= 1")
			}
		}
	case "fill_polygon", "gradient_polygon":
		feather, err := strconv.ParseFloat(string(fields["feather"]), 64)
		if err != nil {
			return Command{}, fmt.Errorf("decode feather: %w", err)
		}
		if string(fields["preserve_alpha"]) == "true" && (action == "gradient_polygon" || feather > 0) {
			return Command{}, invalid("preserve_alpha", "Generated independent layers do not support preserve_alpha=True")
		}
	}
	params, err = json.Marshal(fields)
	if err != nil {
		return Command{}, fmt.Errorf("encode parameters: %w", err)
	}
	return Command{action, params}, nil
}

func (c Command) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Action string          `json:"action"`
		Params json.RawMessage `json:"params"`
	}{c.action, c.params})
}
