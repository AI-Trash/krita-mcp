package mcpserver

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	"github.com/AI-Trash/krita-mcp/internal/kritaclient"
)

func isLegacy(action string) bool {
	switch action {
	case "health", "new_canvas", "set_color", "set_brush", "fill", "draw_shape", "undo", "redo", "clear", "save", "get_color_at", "list_brushes", "open_file":
		return true
	default:
		return false
	}
}

func value(fields kritaclient.Object, key, fallback string) string {
	raw, exists := fields[key]
	if !exists {
		return fallback
	}
	var text string
	if json.Unmarshal(raw, &text) == nil && string(raw) != "null" {
		return text
	}
	switch string(raw) {
	case "null":
		return "None"
	case "true":
		return "True"
	case "false":
		return "False"
	default:
		return string(raw)
	}
}

func legacyText(command kritaclient.Command, result kritaclient.Object) string {
	if _, failed := result["error"]; failed {
		return "Error: " + value(result, "error", "None")
	}
	var params kritaclient.Object
	if err := json.Unmarshal(command.Params(), &params); err != nil {
		return "Error: " + err.Error()
	}
	param := func(key string) string { return value(params, key, "None") }
	field := func(key string) string { return value(result, key, "None") }
	switch command.Action() {
	case "health":
		return "Krita is running. Plugin: " + value(result, "plugin", "unknown")
	case "new_canvas":
		return fmt.Sprintf("Created canvas: %sx%s, background: %s", param("width"), param("height"), param("background"))
	case "set_color":
		return "Color set to " + param("color")
	case "set_brush":
		opacity := param("opacity")
		if opacity != "None" {
			number, err := strconv.ParseFloat(opacity, 64)
			if err != nil {
				return "Error: " + err.Error()
			}
			opacity = strconv.FormatFloat(number, 'g', -1, 64)
			if !strings.ContainsAny(opacity, ".eE") {
				opacity += ".0"
			}
		}
		return fmt.Sprintf("Brush set: preset=%s, size=%s, opacity=%s", param("preset"), param("size"), opacity)
	case "fill":
		return fmt.Sprintf("Filled at (%s, %s) with radius %s", param("x"), param("y"), param("radius"))
	case "draw_shape":
		return fmt.Sprintf("Drew %s at (%s, %s)", param("shape"), param("x"), param("y"))
	case "undo":
		return "Undo requested; history change unverified"
	case "redo":
		return "Redo requested; history change unverified"
	case "clear":
		return "Active-layer native fill with " + param("color") + "; respects selection, opacity, and alpha lock"
	case "save":
		return "Saved to " + param("path")
	case "get_color_at":
		return fmt.Sprintf("Color at (%s, %s): %s (R:%s, G:%s, B:%s)", param("x"), param("y"), value(result, "color", "unknown"), field("r"), field("g"), field("b"))
	case "list_brushes":
		var brushes []string
		if raw, exists := result["brushes"]; exists {
			if err := json.Unmarshal(raw, &brushes); err != nil {
				return "Error: " + err.Error()
			}
		}
		if len(brushes) == 0 {
			return "No brushes found matching filter"
		}
		return fmt.Sprintf("Available brushes (%d):\n  - %s", len(brushes), strings.Join(brushes, "\n  - "))
	case "open_file":
		return fmt.Sprintf("Opened: %s (%sx%s)", value(result, "name", "unknown"), field("width"), field("height"))
	default:
		return ""
	}
}
