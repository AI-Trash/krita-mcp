package kritaclient

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestSend_whenLargeBatch_postsEntirePayloadOnce(t *testing.T) {
	commands := strings.Repeat(`{"action":"set_color","params":{"color":"#fff"}},`, 1500) + `{"action":"set_color","params":{"color":"#000"}}`
	command, err := Parse("batch", json.RawMessage(`{"commands":[`+commands+`]}`))
	if err != nil {
		t.Fatal(err)
	}
	var calls atomic.Int32
	var received []byte
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method != "POST" || r.Header.Get("Content-Type") != "application/json" {
			t.Error("invalid request headers")
		}
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Error(err)
			return
		}
		received = body
		fmt.Fprint(w, `{"status":"ok"}`)
	}))
	defer server.Close()
	client := New(server.URL + "/")
	defer client.Close()
	result := client.Send(t.Context(), command)
	if _, failed := result["error"]; failed {
		t.Fatal(string(result["error"]))
	}
	if calls.Load() != 1 {
		t.Fatalf("calls=%d", calls.Load())
	}
	want, err := command.MarshalJSON()
	if err != nil {
		t.Fatal(err)
	}
	assertJSON(t, received, want)
}

func TestParse_whenLateInvalidBatch_preventsAllHTTP(t *testing.T) {
	for _, last := range []string{
		`{"action":"stroke","params":{"points":[[0,0],[1,1]],"pressure":2}}`,
		`{"action":"batch","params":{"commands":[]}}`,
	} {
		params := json.RawMessage(`{"commands":[` + strings.Repeat(`{"action":"undo","params":{}},`, 1500) + last + `]}`)
		_, err := Parse("batch", params)
		if err == nil || !strings.Contains(err.Error(), "index 1500") {
			t.Fatalf("expected indexed preflight error: %v", err)
		}
	}
}

func TestSend_whenHTTPError_preservesPluginDetails(t *testing.T) {
	detail := `{"error":"Batch cancelled","completed_count":1,"results":[{"status":"ok"}],"failing_index":1,"failing_result":{"error":"Execution outcome unknown"},"atomic":false,"outcome_unknown":true}`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(503); fmt.Fprint(w, detail) }))
	defer server.Close()
	client := New(server.URL)
	defer client.Close()
	command, err := Parse("batch", json.RawMessage(`{"commands":[{"action":"deselect","params":{}}]}`))
	if err != nil {
		t.Fatal(err)
	}
	result := client.Send(t.Context(), command)
	encoded, err := json.Marshal(result)
	if err != nil {
		t.Fatal(err)
	}
	assertJSON(t, encoded, json.RawMessage(detail))
}

func TestSend_whenInvalidResponses_areActionable(t *testing.T) {
	for _, example := range []struct {
		status int
		body   string
	}{{503, `{"status":"busy"}`}, {200, `<html>broken</html>`}, {200, `[]`}, {200, `null`}, {302, `{}`}} {
		t.Run(fmt.Sprint(example.status, example.body), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(example.status)
				fmt.Fprint(w, example.body)
			}))
			defer server.Close()
			client := New(server.URL)
			defer client.Close()
			command, err := Parse("deselect", nil)
			if err != nil {
				t.Fatal(err)
			}
			result := client.Send(t.Context(), command)
			if !strings.Contains(string(result["error"]), fmt.Sprint(example.status)) || string(result["outcome_unknown"]) != "true" {
				t.Fatalf("result=%s", result)
			}
		})
	}
}

func TestSend_whenRedirect_doesNotFollowOrUseProxy(t *testing.T) {
	var redirected atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { redirected.Add(1) }))
	defer target.Close()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Location", target.URL)
		w.WriteHeader(307)
		fmt.Fprint(w, `{}`)
	}))
	defer server.Close()
	t.Setenv("HTTP_PROXY", target.URL)
	t.Setenv("HTTPS_PROXY", target.URL)
	t.Setenv("ALL_PROXY", target.URL)
	client := New(server.URL)
	defer client.Close()
	command, err := Parse("undo", nil)
	if err != nil {
		t.Fatal(err)
	}
	result := client.Send(t.Context(), command)
	if redirected.Load() != 0 || result["error"] == nil {
		t.Fatal("redirect/proxy was followed")
	}
	if client.ordinary.Transport.(*http.Transport).Proxy != nil {
		t.Fatal("environment proxy enabled")
	}
}

func TestSend_whenDisconnected_doesNotRetryMutation(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		connection, _, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Error(err)
			return
		}
		if err := connection.Close(); err != nil {
			t.Error(err)
		}
	}))
	defer server.Close()
	client := New(server.URL)
	defer client.Close()
	command, err := Parse("undo", nil)
	if err != nil {
		t.Fatal(err)
	}
	result := client.Send(t.Context(), command)
	if calls.Load() != 1 || string(result["outcome_unknown"]) != "true" {
		t.Fatalf("calls=%d result=%s", calls.Load(), result)
	}
}

func TestSend_whenCancelled_propagatesToHTTP(t *testing.T) {
	started := make(chan struct{})
	stopped := make(chan struct{})
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
	client := New(server.URL)
	defer client.Close()
	command, err := Parse("batch", json.RawMessage(`{"commands":[{"action":"undo","params":{}}]}`))
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	finished := make(chan Object, 1)
	go func() { finished <- client.Send(ctx, command) }()
	<-started
	cancel()
	result := <-finished
	if string(result["outcome_unknown"]) != "true" {
		t.Fatal("cancelled mutation outcome not marked unknown")
	}
	select {
	case <-stopped:
	case <-time.After(time.Second):
		t.Fatal("HTTP context was not cancelled")
	}
}

func TestHealth_whenConnectFails_isActionable(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	if err := listener.Close(); err != nil {
		t.Fatal(err)
	}
	client := New("http://" + address)
	defer client.Close()
	result := client.Health(t.Context())
	if !strings.Contains(string(result["error"]), "KRITA_URL / KRITA_MCP_PORT") {
		t.Fatal(string(result["error"]))
	}
}

func TestURLFromEnv_whenCustomPort(t *testing.T) {
	t.Setenv("KRITA_URL", "http://127.0.0.1:45678/")
	if URLFromEnv() != "http://127.0.0.1:45678/" {
		t.Fatal("custom port ignored")
	}
}
