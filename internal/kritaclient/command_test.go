package kritaclient

import (
	"encoding/json"
	"os"
	"reflect"
	"strconv"
	"strings"
	"testing"
)

type contractCase struct {
	Action  string          `json:"action"`
	Params  json.RawMessage `json:"params"`
	Valid   bool            `json:"valid"`
	Payload json.RawMessage `json:"payload"`
}

func contractCases(t *testing.T) []contractCase {
	t.Helper()
	data, err := os.ReadFile("testdata/client_reference.json")
	if err != nil {
		t.Fatal(err)
	}
	var reference struct {
		Cases []contractCase `json:"cases"`
	}
	if err := json.Unmarshal(data, &reference); err != nil {
		t.Fatal(err)
	}
	if len(reference.Cases) != 82 {
		t.Fatalf("reference cases=%d", len(reference.Cases))
	}
	return reference.Cases
}

func TestParse_whenContractInputs(t *testing.T) {
	for index, example := range contractCases(t) {
		t.Run(example.Action+"/"+strconv.Itoa(index), func(t *testing.T) {
			command, err := Parse(example.Action, example.Params)
			if (err == nil) != example.Valid {
				t.Fatalf("valid=%v error=%v input=%s", example.Valid, err, example.Params)
			}
			if err == nil {
				payload, err := command.MarshalJSON()
				if err != nil {
					t.Fatal(err)
				}
				assertJSON(t, payload, example.Payload)
			}
		})
	}
}

func TestParse_whenPointCountExceedsPerOperationLimit(t *testing.T) {
	params := json.RawMessage(`{"points":[` + strings.Repeat(`[0,0],`, 4096) + `[1,1]]}`)
	_, err := Parse("stroke", params)
	if err == nil {
		t.Fatal("accepted 4097 points")
	}
}

func TestParse_whenOptionalFalseZeroAndNull(t *testing.T) {
	command, err := Parse("set_layer_properties", json.RawMessage(`{"opacity":0,"visible":false,"preserve_alpha":false,"layer_id":null}`))
	if err != nil {
		t.Fatal(err)
	}
	assertJSON(t, command.Params(), json.RawMessage(`{"opacity":0,"visible":false,"preserve_alpha":false}`))
}

func TestParse_whenArbitraryInteger(t *testing.T) {
	command, err := Parse("new_canvas", json.RawMessage(`{"width":9007199254740993123456789}`))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(command.Params()), "9007199254740993123456789") {
		t.Fatal("integer precision lost")
	}
}

func assertJSON(t *testing.T, actual, expected json.RawMessage) {
	t.Helper()
	var got, want any
	if err := json.Unmarshal(actual, &got); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(expected, &want); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %s; want %s", actual, expected)
	}
}
