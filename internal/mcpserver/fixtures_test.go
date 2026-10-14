package mcpserver

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"sync"
	"testing"
	"time"

	"github.com/AI-Trash/krita-mcp/internal/kritaclient"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

var toolArguments = map[string]json.RawMessage{
	"health": json.RawMessage(`{}`), "new_canvas": json.RawMessage(`{}`),
	"set_color": json.RawMessage(`{"color":"#123456"}`), "set_brush": json.RawMessage(`{"preset":"Basic","size":20,"opacity":0.8}`),
	"stroke": json.RawMessage(`{"points":[[1.25,2.5,0.25],[20,20]],"curve":"linear"}`),
	"fill":   json.RawMessage(`{"x":20,"y":30}`), "draw_shape": json.RawMessage(`{"shape":"rectangle","x":20,"y":30}`),
	"get_canvas": json.RawMessage(`{"show_grid":true,"diff_with_reference":"/tmp/reference.png"}`),
	"undo":       json.RawMessage(`{}`), "redo": json.RawMessage(`{}`), "clear": json.RawMessage(`{}`),
	"save": json.RawMessage(`{"path":"/tmp/art.kra"}`), "get_color_at": json.RawMessage(`{"x":20,"y":30}`),
	"list_brushes": json.RawMessage(`{}`), "open_file": json.RawMessage(`{"path":"/tmp/art.kra"}`),
	"fill_polygon":     json.RawMessage(`{"points":[[0,0],[20,0],[0,20]]}`),
	"gradient_polygon": json.RawMessage(`{"points":[[0,0],[20,0],[0,20]],"color":"#000000","gradient_end_color":"#ffffff","gradient_type":"radial"}`),
	"create_layer":     json.RawMessage(`{}`), "set_layer_properties": json.RawMessage(`{"opacity":0,"visible":false}`),
	"list_layers": json.RawMessage(`{}`), "set_active_layer": json.RawMessage(`{"layer_id":"layer-1"}`),
	"delete_layer": json.RawMessage(`{"layer_id":"layer-1"}`), "merge_down": json.RawMessage(`{"layer_id":"layer-1"}`),
	"select_rect":     json.RawMessage(`{"x":0,"y":0,"width":20,"height":30}`),
	"select_ellipse":  json.RawMessage(`{"x":0,"y":0,"width":20,"height":30}`),
	"select_polygon":  json.RawMessage(`{"points":[[0,0],[20,0],[0,20]]}`),
	"select_by_alpha": json.RawMessage(`{}`), "deselect": json.RawMessage(`{}`), "fill_selection": json.RawMessage(`{}`),
	"get_current_brush": json.RawMessage(`{}`), "batch": json.RawMessage(`{"commands":[{"action":"deselect","params":{}}]}`),
}

const defaultReply = `{"status":"ok","plugin":"kritamcp","brushes":["Basic"],"color":"#123456","r":18,"g":52,"b":86,"name":"Art","width":800,"height":600,"path":"/tmp/canvas.png","diff_path":"/tmp/canvas_diff.png"}`

type receivedRequest struct {
	Method, Path string
	Body         json.RawMessage
}
type pluginFixture struct {
	server   *httptest.Server
	mu       sync.Mutex
	requests []receivedRequest
	reply    string
	status   int
}

func newPlugin(t *testing.T) *pluginFixture {
	t.Helper()
	plugin := &pluginFixture{reply: defaultReply, status: 200}
	plugin.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Error(err)
			return
		}
		plugin.mu.Lock()
		plugin.requests = append(plugin.requests, receivedRequest{r.Method, r.URL.Path, body})
		status, reply := plugin.status, plugin.reply
		plugin.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		if _, err := io.WriteString(w, reply); err != nil {
			t.Error(err)
		}
	}))
	t.Cleanup(plugin.server.Close)
	return plugin
}

func (p *pluginFixture) respond(status int, reply string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.status, p.reply = status, reply
}

func (p *pluginFixture) received() []receivedRequest {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]receivedRequest(nil), p.requests...)
}

func connectMemory(t *testing.T, plugin *pluginFixture) *mcp.ClientSession {
	t.Helper()
	httpClient := kritaclient.New(plugin.server.URL)
	t.Cleanup(httpClient.Close)
	server := New(&mcp.Implementation{Name: "krita-mcp", Version: "test"}, httpClient)
	first, second := mcp.NewInMemoryTransports()
	serverSession, err := server.Connect(t.Context(), first, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := serverSession.Close(); err != nil {
			t.Error(err)
		}
	})
	client := mcp.NewClient(&mcp.Implementation{Name: "contract-test", Version: "1"}, nil)
	session, err := client.Connect(t.Context(), second, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := session.Close(); err != nil {
			t.Error(err)
		}
	})
	return session
}

func buildBinary(t *testing.T) string {
	t.Helper()
	binary := filepath.Join(t.TempDir(), "krita-mcp.exe")
	ctx, cancel := context.WithTimeout(t.Context(), 120*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, "go", "build", "-ldflags=-X main.version=parity-test -X main.commit=abc123 -X main.date=2026-10-14", "-o", binary, "./cmd/krita-mcp")
	command.Dir = filepath.Join("..", "..")
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("build: %v: %s", err, output)
	}
	return binary
}

func connectBinary(t *testing.T, plugin *pluginFixture, binary string) *mcp.ClientSession {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.WithoutCancel(t.Context()), 120*time.Second)
	t.Cleanup(cancel)
	command := exec.CommandContext(ctx, binary)
	command.Env = append(os.Environ(), "KRITA_URL="+plugin.server.URL)
	command.Dir = t.TempDir()
	client := mcp.NewClient(&mcp.Implementation{Name: "stdio-contract-test", Version: "1"}, nil)
	session, err := client.Connect(t.Context(), &mcp.CommandTransport{Command: command}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := session.Close(); err != nil {
			t.Error(err)
		}
	})
	return session
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
