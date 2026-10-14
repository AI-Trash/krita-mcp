package mcpserver

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestTools_whenEscapedBezierInvalid_preventsAllHTTP(t *testing.T) {
	stroke := `{"points":[[0,0],[1,1]],"curve":"\u0062ezier"}`
	for _, example := range []struct {
		action, arguments string
	}{
		{"stroke", stroke},
		{"batch", `{"commands":[` + strings.Repeat(`{"action":"undo","params":{}},`, 1500) + `{"action":"stroke","params":` + stroke + `}]}`},
	} {
		t.Run(example.action, func(t *testing.T) {
			plugin := newPlugin(t)
			session := connectMemory(t, plugin)
			result, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: "krita_" + example.action, Arguments: json.RawMessage(example.arguments)})
			if err != nil {
				t.Fatal(err)
			}
			if len(plugin.received()) != 0 {
				t.Fatal("invalid escaped Bezier reached HTTP")
			}
			if len(result.Content) != 1 {
				t.Fatalf("result=%+v", result)
			}
			text, ok := result.Content[0].(*mcp.TextContent)
			if !ok || !strings.Contains(text.Text, "3n+1") {
				t.Fatalf("missing Bezier validation error: %+v", result)
			}
			if example.action == "batch" && !strings.Contains(text.Text, "index 1500") {
				t.Fatal("missing late-child index")
			}
		})
	}
}

func TestTools_whenEscapedBezierValid_forwardsCanonicalStrings(t *testing.T) {
	plugin := newPlugin(t)
	session := connectMemory(t, plugin)
	arguments := json.RawMessage(`{"points":[[0,0],[0,10],[10,10],[10,0]],"curve":"\u0062ezier","color":"\u0023fff"}`)
	result, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: "krita_stroke", Arguments: arguments})
	if err != nil {
		t.Fatal(err)
	}
	if result.IsError {
		t.Fatalf("result=%+v", result)
	}
	requests := plugin.received()
	if len(requests) != 1 {
		t.Fatalf("requests=%d", len(requests))
	}
	var payload struct {
		Params map[string]json.RawMessage `json:"params"`
	}
	if err := json.Unmarshal(requests[0].Body, &payload); err != nil {
		t.Fatal(err)
	}
	if string(payload.Params["curve"]) != `"bezier"` || string(payload.Params["color"]) != `"#fff"` {
		t.Fatalf("strings not canonicalized: %s", requests[0].Body)
	}
}
