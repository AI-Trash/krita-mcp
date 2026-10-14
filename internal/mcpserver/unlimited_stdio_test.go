package mcpserver

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestStdio_whenFrameAndResultExceedSDKDefault_hasNoHiddenCap(t *testing.T) {
	plugin := newPlugin(t)
	huge := strings.Repeat("x", mcp.DefaultMaxLineLength+1)
	plugin.respond(200, `{"history":"`+huge+`","completed_count":1501,"atomic":false}`)
	ctx, cancel := context.WithTimeout(context.WithoutCancel(t.Context()), 60*time.Second)
	t.Cleanup(cancel)
	command := exec.CommandContext(ctx, buildBinary(t))
	command.Env = append(os.Environ(), "KRITA_URL="+plugin.server.URL)
	command.Dir = t.TempDir()
	stdin, err := command.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout, err := command.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := stdin.Close(); err != nil {
			t.Error(err)
		}
		if err := command.Wait(); err != nil {
			t.Error(err)
		}
	})
	encoder, decoder := json.NewEncoder(stdin), json.NewDecoder(stdout)
	if err := encoder.Encode(json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"raw-contract-test","version":"1"}}}`)); err != nil {
		t.Fatal(err)
	}
	var initialized struct {
		Result struct{ ProtocolVersion string }
	}
	if err := decoder.Decode(&initialized); err != nil {
		t.Fatal(err)
	}
	if initialized.Result.ProtocolVersion != "2025-06-18" {
		t.Fatal("older initialize contract changed")
	}
	if err := encoder.Encode(json.RawMessage(`{"jsonrpc":"2.0","method":"notifications/initialized"}`)); err != nil {
		t.Fatal(err)
	}
	arguments := `{"commands":[{"action":"new_canvas","params":{"name":"` + huge + `"}},` + strings.Repeat(`{"action":"undo","params":{}},`, 1499) + `{"action":"undo","params":{}}]}`
	if err := encoder.Encode(json.RawMessage(`{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"krita_batch","arguments":` + arguments + `}}`)); err != nil {
		t.Fatal(err)
	}
	var response struct {
		Result struct {
			StructuredContent struct {
				History        string
				CompletedCount int `json:"completed_count"`
			}
		}
		Error json.RawMessage
	}
	if err := decoder.Decode(&response); err != nil {
		t.Fatal(err)
	}
	if response.Error != nil || response.Result.StructuredContent.History != huge || response.Result.StructuredContent.CompletedCount != 1501 {
		t.Fatal("large result truncated/rejected")
	}
	requests := plugin.received()
	if len(requests) != 1 || len(requests[0].Body) <= mcp.DefaultMaxLineLength {
		t.Fatal("large batch was capped/chunked")
	}
}
