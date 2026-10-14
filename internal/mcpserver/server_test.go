package mcpserver

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestStdio_whenBuiltBinary_all31ToolsRoundTrip(t *testing.T) {
	plugin := newPlugin(t)
	session := connectBinary(t, plugin, buildBinary(t))
	info := session.InitializeResult().ServerInfo
	if info.Name != "krita-mcp" || info.Version != "parity-test" || !strings.Contains(info.Description, "abc123") {
		t.Fatalf("build metadata missing: %+v", info)
	}
	tools, err := session.ListTools(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(tools.Tools) != 31 {
		t.Fatalf("tools=%d", len(tools.Tools))
	}
	for _, tool := range tools.Tools {
		action := strings.TrimPrefix(tool.Name, "krita_")
		arguments, exists := toolArguments[action]
		if !exists {
			t.Fatalf("unexpected tool %s", tool.Name)
		}
		before := len(plugin.received())
		result, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: tool.Name, Arguments: arguments})
		if err != nil || result.IsError {
			t.Fatalf("%s: result=%+v error=%v", tool.Name, result, err)
		}
		requests := plugin.received()
		if len(requests) != before+1 {
			t.Fatalf("%s HTTP count=%d", action, len(requests)-before)
		}
		last := requests[len(requests)-1]
		if action == "health" {
			if last.Path != "/health" || last.Method != "GET" {
				t.Fatal("health route changed")
			}
		} else {
			var envelope struct{ Action string }
			if err := json.Unmarshal(last.Body, &envelope); err != nil {
				t.Fatal(err)
			}
			if envelope.Action != action || last.Method != "POST" {
				t.Fatalf("wrong forwarding: %s", last.Body)
			}
		}
		if !isLegacy(action) {
			encoded, err := json.Marshal(result.StructuredContent)
			if err != nil {
				t.Fatal(err)
			}
			assertJSON(t, encoded, json.RawMessage(defaultReply))
		}
	}
}

func TestTools_whenInvalidInputs_preventHTTP(t *testing.T) {
	plugin := newPlugin(t)
	session := connectMemory(t, plugin)
	for _, example := range []struct{ action, arguments string }{
		{"stroke", `{"points":[[0,0],[1,1]],"pressure":2}`},
		{"stroke", `{"points":[[0,0],[1,1]],"curve":"bezier"}`},
		{"draw_shape", `{"shape":"line","x":0,"y":0,"line_width":2}`},
		{"gradient_polygon", `{"points":[[0,0],[1,0],[0,1]],"color":"#000","gradient_end_color":"#fff","preserve_alpha":true}`},
		{"health", `{"extra":1}`}, {"new_canvas", `{"width":true}`},
		{"set_layer_properties", `{"opacity":"0.5"}`},
	} {
		result, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: "krita_" + example.action, Arguments: json.RawMessage(example.arguments)})
		if err != nil || !result.IsError {
			t.Fatalf("%s: result=%+v error=%v", example.action, result, err)
		}
	}
	if len(plugin.received()) != 0 {
		t.Fatal("invalid tool input reached plugin")
	}
}

func TestTools_whenPluginFailure_preservesLegacyPrefix(t *testing.T) {
	plugin := newPlugin(t)
	plugin.respond(500, `{"error":"No active layer"}`)
	session := connectMemory(t, plugin)
	result, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: "krita_fill", Arguments: toolArguments["fill"]})
	if err != nil {
		t.Fatal(err)
	}
	if result.IsError {
		t.Fatal("legacy plugin errors must remain normal string results")
	}
	if text, ok := result.Content[0].(*mcp.TextContent); !ok || text.Text != "Error: No active layer" {
		t.Fatalf("result=%+v", result)
	}
}

func TestTools_whenGeneratedLayer_preservesAllMetadata(t *testing.T) {
	plugin := newPlugin(t)
	detail := `{"status":"ok","mode":"generated_layer","layer_id":"new-layer","target_layer_id":"original-layer","independent_layer":true,"history":"native layer attachment","undo_steps_verified":false}`
	plugin.respond(200, detail)
	session := connectMemory(t, plugin)
	result, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: "krita_gradient_polygon", Arguments: toolArguments["gradient_polygon"]})
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(result.StructuredContent)
	if err != nil {
		t.Fatal(err)
	}
	assertJSON(t, encoded, json.RawMessage(detail))
}

func TestTools_whenLateBatchInvalid_returnsErrorWithoutHTTP(t *testing.T) {
	plugin := newPlugin(t)
	session := connectMemory(t, plugin)
	arguments := json.RawMessage(`{"commands":[` + strings.Repeat(`{"action":"undo","params":{}},`, 1500) + `{"action":"batch","params":{"commands":[]}}]}`)
	result, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: "krita_batch", Arguments: arguments})
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(result.StructuredContent)
	if err != nil {
		t.Fatal(err)
	}
	if result.IsError || !strings.Contains(string(encoded), "index 1500") || len(plugin.received()) != 0 {
		t.Fatalf("result=%s requests=%d", encoded, len(plugin.received()))
	}
}

func TestStdio_whenLargeBatch_keepsPayloadAndFailurePrefix(t *testing.T) {
	plugin := newPlugin(t)
	detail := `{"error":"Layer not found","completed_count":1500,"failing_index":1500,"results":[{"status":"ok"}],"failing_result":{"error":"Layer not found"},"atomic":false,"outcome_unknown":true}`
	plugin.respond(500, detail)
	session := connectBinary(t, plugin, buildBinary(t))
	arguments := json.RawMessage(`{"commands":[` + strings.Repeat(`{"action":"set_color","params":{"color":"#fff"}},`, 1500) + `{"action":"undo","params":{}}]}`)
	result, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: "krita_batch", Arguments: arguments})
	if err != nil {
		t.Fatal(err)
	}
	requests := plugin.received()
	if len(requests) != 1 {
		t.Fatalf("batch POST count=%d", len(requests))
	}
	assertJSON(t, requests[0].Body, json.RawMessage(`{"action":"batch","params":`+string(arguments)+`}`))
	encoded, err := json.Marshal(result.StructuredContent)
	if err != nil {
		t.Fatal(err)
	}
	assertJSON(t, encoded, json.RawMessage(detail))
}
