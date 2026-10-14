package kritaclient

import (
	"encoding/json"
	"testing"
)

func TestLayerCommands_whenInvalidLayerID_rejectBeforeTransport(t *testing.T) {
	for _, action := range []string{"delete_layer", "merge_down"} {
		for _, example := range []struct{ name, params string }{
			{"missing", `{}`}, {"empty", `{"layer_id":""}`},
			{"null", `{"layer_id":null}`}, {"number", `{"layer_id":42}`},
			{"boolean", `{"layer_id":true}`}, {"array", `{"layer_id":[]}`},
			{"object", `{"layer_id":{}}`}, {"extra", `{"layer_id":"layer-1","extra":true}`},
		} {
			t.Run(action+"/"+example.name, func(t *testing.T) {
				for _, parse := range []func(string, json.RawMessage) (Command, error){Parse, ParseTool} {
					_, err := parse(action, json.RawMessage(example.params))
					if err == nil {
						t.Fatalf("accepted %s", example.params)
					}
				}
				_, err := Parse("batch", json.RawMessage(`{"commands":[{"action":"`+action+`","params":`+example.params+`}]}`))
				if err == nil {
					t.Fatalf("accepted invalid batch child %s", example.params)
				}
			})
		}
	}
}

func TestLayerCommands_whenExplicitLayerID_preserveSingleAndBatchPayloads(t *testing.T) {
	for _, action := range []string{"delete_layer", "merge_down"} {
		t.Run(action, func(t *testing.T) {
			params := json.RawMessage(`{"layer_id":"{12345678-1234-1234-1234-123456789abc}"}`)
			want := json.RawMessage(`{"action":"` + action + `","params":` + string(params) + `}`)
			command, err := ParseTool(action, params)
			if err != nil {
				t.Fatal(err)
			}
			payload, err := command.MarshalJSON()
			if err != nil {
				t.Fatal(err)
			}
			assertJSON(t, payload, want)
			batchParams := json.RawMessage(`{"commands":[` + string(want) + `]}`)
			batch, err := Parse("batch", batchParams)
			if err != nil {
				t.Fatal(err)
			}
			assertJSON(t, batch.Params(), batchParams)
		})
	}
}
