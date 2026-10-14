package mcpserver

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestTools_whenCallCancelled_cancelsPluginContext(t *testing.T) {
	started, stopped := make(chan struct{}), make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, err := io.Copy(io.Discard, r.Body); err != nil {
			t.Error(err)
			return
		}
		close(started)
		<-r.Context().Done()
		close(stopped)
	}))
	defer server.Close()
	session := connectMemory(t, &pluginFixture{server: server})
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	finished := make(chan error, 1)
	go func() {
		_, err := session.CallTool(ctx, &mcp.CallToolParams{Name: "krita_batch", Arguments: toolArguments["batch"]})
		finished <- err
	}()
	<-started
	cancel()
	if err := <-finished; err == nil {
		t.Fatal("cancelled MCP request succeeded")
	}
	select {
	case <-stopped:
	case <-time.After(5 * time.Second):
		t.Fatal("MCP cancellation did not reach HTTP")
	}
}
