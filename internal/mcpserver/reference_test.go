package mcpserver

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestCapturedReference_whenDiscoveryAndAllToolResults(t *testing.T) {
	data, err := os.ReadFile("testdata/tools_reference.json")
	if err != nil {
		t.Fatal(err)
	}
	var reference struct {
		Reply         json.RawMessage            `json:"reply"`
		OutputSchemas map[string]json.RawMessage `json:"output_schemas"`
		Tools         map[string]struct {
			InputSchema json.RawMessage `json:"input_schema"`
			Output      string          `json:"output"`
			Result      json.RawMessage `json:"result"`
		} `json:"tools"`
		BrushFormatting []struct {
			Arguments json.RawMessage `json:"arguments"`
			Result    json.RawMessage `json:"result"`
		} `json:"brush_formatting"`
	}
	if err := json.Unmarshal(data, &reference); err != nil {
		t.Fatal(err)
	}
	if len(reference.Tools) != 29 {
		t.Fatalf("reference tools=%d", len(reference.Tools))
	}
	plugin := newPlugin(t)
	plugin.respond(200, string(reference.Reply))
	session := connectBinary(t, plugin, buildBinary(t))
	tools, err := session.ListTools(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(tools.Tools) != len(reference.Tools)+2 {
		t.Fatalf("tools=%d", len(tools.Tools))
	}
	discovered := make(map[string]*mcp.Tool, len(tools.Tools))
	for _, tool := range tools.Tools {
		discovered[strings.TrimPrefix(tool.Name, "krita_")] = tool
	}
	for action, expected := range reference.Tools {
		tool, exists := discovered[action]
		if !exists {
			t.Fatalf("missing historical tool krita_%s", action)
		}
		t.Run(tool.Name, func(t *testing.T) {
			assertSchema(t, tool.InputSchema, expected.InputSchema)
			output, exists := reference.OutputSchemas[expected.Output]
			if !exists {
				t.Fatalf("unknown reference output %q", expected.Output)
			}
			assertSchema(t, tool.OutputSchema, output)
			result, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: tool.Name, Arguments: toolArguments[action]})
			if err != nil {
				t.Fatal(err)
			}
			switch expected.Output {
			case "legacy":
				assertLegacyResult(t, result, expected.Result)
			case "object":
				if result.IsError || len(result.Content) != 1 {
					t.Fatalf("result=%+v", result)
				}
				encoded, err := json.Marshal(result.StructuredContent)
				if err != nil {
					t.Fatal(err)
				}
				assertJSON(t, encoded, reference.Reply)
				text, ok := result.Content[0].(*mcp.TextContent)
				if !ok {
					t.Fatal("expected JSON text content")
				}
				assertJSON(t, json.RawMessage(text.Text), reference.Reply)
			default:
				t.Fatalf("unsupported reference output %q", expected.Output)
			}
		})
	}
	for _, example := range reference.BrushFormatting {
		result, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: "krita_set_brush", Arguments: example.Arguments})
		if err != nil {
			t.Fatal(err)
		}
		assertLegacyResult(t, result, example.Result)
	}
	if len(plugin.received()) != 31 {
		t.Fatalf("expected 29 tool calls plus two formatting cases, got %d", len(plugin.received()))
	}
}

func assertLegacyResult(t *testing.T, result *mcp.CallToolResult, expected json.RawMessage) {
	t.Helper()
	if result.IsError || len(result.Content) != 1 {
		t.Fatalf("result=%+v", result)
	}
	encoded, err := json.Marshal(result.StructuredContent)
	if err != nil {
		t.Fatal(err)
	}
	assertJSON(t, encoded, expected)
	var wrapper struct {
		Result string `json:"result"`
	}
	if err := json.Unmarshal(expected, &wrapper); err != nil {
		t.Fatal(err)
	}
	text, ok := result.Content[0].(*mcp.TextContent)
	if !ok || text.Text != wrapper.Result {
		t.Fatalf("legacy content differs: %+v", result.Content)
	}
}

func assertSchema(t *testing.T, actual any, expected json.RawMessage) {
	t.Helper()
	encoded, err := json.Marshal(actual)
	if err != nil {
		t.Fatal(err)
	}
	var schema any
	if err := json.Unmarshal(encoded, &schema); err != nil {
		t.Fatal(err)
	}
	encoded, err = json.Marshal(normalizeSchema(schema))
	if err != nil {
		t.Fatal(err)
	}
	assertJSON(t, encoded, expected)
}

func normalizeSchema(schema any) any {
	switch value := schema.(type) {
	case []any:
		for index, item := range value {
			value[index] = normalizeSchema(item)
		}
	case map[string]any:
		for key, item := range value {
			empty, isObject := item.(map[string]any)
			if (key == "properties" && isObject && len(empty) == 0) ||
				(key == "additionalProperties" && ((isObject && len(empty) == 0) || item == true)) {
				delete(value, key)
			} else {
				value[key] = normalizeSchema(item)
			}
		}
	}
	return schema
}
