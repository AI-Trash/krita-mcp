package mcpserver

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestStdio_whenLayerToolsDiscovered_exposesExplicitContracts(t *testing.T) {
	plugin := newPlugin(t)
	session := connectBinary(t, plugin, buildBinary(t))
	tools, err := session.ListTools(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(tools.Tools) != 31 {
		t.Fatalf("tools=%d", len(tools.Tools))
	}
	fixtures := map[string]json.RawMessage{
		"krita_delete_layer": json.RawMessage(`{"type":"object","properties":{"layer_id":{"type":"string","minLength":1}},"required":["layer_id"],"additionalProperties":false}`),
		"krita_merge_down":   json.RawMessage(`{"type":"object","properties":{"layer_id":{"type":"string","minLength":1}},"required":["layer_id"],"additionalProperties":false}`),
	}
	for _, tool := range tools.Tools {
		if schema, exists := fixtures[tool.Name]; exists {
			assertSchema(t, tool.InputSchema, schema)
			assertSchema(t, tool.OutputSchema, json.RawMessage(`{"type":"object"}`))
			delete(fixtures, tool.Name)
		}
	}
	if len(fixtures) != 0 {
		t.Fatalf("missing tools: %v", fixtures)
	}
}

func TestLayerTools_whenInvalidArguments_preventHTTP(t *testing.T) {
	plugin := newPlugin(t)
	session := connectMemory(t, plugin)
	for _, action := range []string{"delete_layer", "merge_down"} {
		for _, arguments := range []string{`{}`, `{"layer_id":""}`, `{"layer_id":null}`, `{"layer_id":1}`, `{"layer_id":true}`, `{"layer_id":[]}`, `{"layer_id":{}}`, `{"layer_id":"layer-1","extra":1}`} {
			t.Run(action+"/"+arguments, func(t *testing.T) {
				result, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: "krita_" + action, Arguments: json.RawMessage(arguments)})
				if err != nil || !result.IsError {
					t.Fatalf("result=%+v error=%v", result, err)
				}
			})
			t.Run("batch/"+action+"/"+arguments, func(t *testing.T) {
				params := json.RawMessage(`{"commands":[{"action":"` + action + `","params":` + arguments + `}]}`)
				result, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: "krita_batch", Arguments: params})
				if err != nil {
					t.Fatal(err)
				}
				encoded, err := json.Marshal(result.StructuredContent)
				if err != nil {
					t.Fatal(err)
				}
				var detail map[string]json.RawMessage
				if err := json.Unmarshal(encoded, &detail); err != nil {
					t.Fatal(err)
				}
				if result.IsError || len(detail["error"]) == 0 {
					t.Fatalf("batch result=%s", encoded)
				}
			})
		}
	}
	if len(plugin.received()) != 0 {
		t.Fatal("invalid layer command reached plugin")
	}
}

func TestStdio_whenLayerMutation_preservesIDsAndHTTPPayload(t *testing.T) {
	plugin := newPlugin(t)
	session := connectBinary(t, plugin, buildBinary(t))
	for _, example := range []struct{ name, action, reply string }{
		{"delete-active", "delete_layer", `{"status":"ok","deleted_layer_id":"layer-1","active_layer_id":"fallback-layer"}`},
		{"delete-last", "delete_layer", `{"status":"ok","deleted_layer_id":"layer-1","active_layer_id":null}`},
		{"merge", "merge_down", `{"status":"ok","layer_id":"new-uuid","merged_layer_id":"layer-1","active_layer_id":"new-uuid","layer_info":{"name":"Merged","opacity":0.75}}`},
	} {
		t.Run(example.name, func(t *testing.T) {
			plugin.respond(200, example.reply)
			before := len(plugin.received())
			result, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: "krita_" + example.action, Arguments: toolArguments[example.action]})
			if err != nil || result.IsError {
				t.Fatalf("result=%+v error=%v", result, err)
			}
			requests := plugin.received()
			if len(requests) != before+1 || requests[before].Method != "POST" || requests[before].Path != "/" {
				t.Fatalf("requests=%+v", requests)
			}
			assertJSON(t, requests[before].Body, json.RawMessage(`{"action":"`+example.action+`","params":{"layer_id":"layer-1"}}`))
			encoded, err := json.Marshal(result.StructuredContent)
			if err != nil {
				t.Fatal(err)
			}
			assertJSON(t, encoded, json.RawMessage(example.reply))
			text, ok := result.Content[0].(*mcp.TextContent)
			if !ok {
				t.Fatal("expected JSON text content")
			}
			assertJSON(t, json.RawMessage(text.Text), json.RawMessage(example.reply))
		})
	}
}

func TestStdio_whenLayerBatch_preservesWarningsAndUniqueQueryData(t *testing.T) {
	plugin := newPlugin(t)
	session := connectBinary(t, plugin, buildBinary(t))
	arguments := json.RawMessage(`{"commands":[{"action":"merge_down","params":{"layer_id":"layer-1"}},{"action":"delete_layer","params":{"layer_id":"layer-2"}},{"action":"list_layers","params":{}},{"action":"get_current_brush","params":{}}]}`)
	for _, example := range []struct {
		name, reply string
		status      int
	}{
		{"success-warnings", `{"status":"ok","completed_count":4,"atomic":false,"warnings":["history is unverified","sampled native pressure"],"results":[{"status":"ok","layer_id":"new-uuid","merged_layer_id":"layer-1","active_layer_id":"new-uuid"},{"status":"ok","deleted_layer_id":"layer-2","active_layer_id":"new-uuid"},{"layers":[{"layer_id":"new-uuid","name":"Merged"}],"active_layer_id":"new-uuid","query_marker":"layers-only"},{"preset":"Basic","size":17.5,"opacity":0.8,"query_marker":"brush-only"}]}`, 200},
		{"success-without-warnings", `{"status":"ok","completed_count":4,"atomic":false,"results":[{"layer_id":"new-uuid"},{"deleted_layer_id":"layer-2"},{"layers":[]},{"preset":"Basic"}]}`, 200},
		{"failure-unchanged", `{"error":"Layer not found","completed_count":1,"failing_index":1,"atomic":false,"results":[{"layer_id":"new-uuid","undo_steps_note":"history is unverified","fidelity_warning":"sampled native pressure"}],"failing_result":{"error":"Layer not found"}}`, 500},
	} {
		t.Run(example.name, func(t *testing.T) {
			plugin.respond(example.status, example.reply)
			before := len(plugin.received())
			result, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: "krita_batch", Arguments: arguments})
			if err != nil || result.IsError {
				t.Fatalf("result=%+v error=%v", result, err)
			}
			requests := plugin.received()
			if len(requests) != before+1 {
				t.Fatalf("batch requests=%d", len(requests)-before)
			}
			assertJSON(t, requests[before].Body, json.RawMessage(`{"action":"batch","params":`+string(arguments)+`}`))
			encoded, err := json.Marshal(result.StructuredContent)
			if err != nil {
				t.Fatal(err)
			}
			assertJSON(t, encoded, json.RawMessage(example.reply))
			text, ok := result.Content[0].(*mcp.TextContent)
			if !ok || strings.TrimSpace(text.Text) == "" {
				t.Fatal("expected JSON text content")
			}
			assertJSON(t, json.RawMessage(text.Text), json.RawMessage(example.reply))
		})
	}
}
