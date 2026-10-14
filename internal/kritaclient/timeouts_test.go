package kritaclient

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"testing/synctest"
	"time"
)

func TestTimeoutPolicy_whenNormalSlowHealthAndBatch(t *testing.T) {
	client := New("http://unused.invalid")
	defer client.Close()
	for _, example := range []struct {
		client       *http.Client
		read, finite time.Duration
	}{
		{client.ordinary, 30 * time.Second, 30 * time.Second}, {client.slow, 120 * time.Second, 120 * time.Second},
		{client.health, 5 * time.Second, 5 * time.Second}, {client.batch, 0, 120 * time.Second},
	} {
		transport := example.client.Transport.(*http.Transport)
		if !transport.DisableKeepAlives {
			t.Fatal("HTTP connections must not be reused across read budgets")
		}
		if transport.ResponseHeaderTimeout != example.read || transport.TLSHandshakeTimeout != example.finite || example.client.Timeout != 0 {
			t.Fatal("incorrect timeout policy")
		}
		listener, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		connection, err := transport.DialContext(t.Context(), "tcp", listener.Addr().String())
		if err != nil {
			t.Fatal(err)
		}
		deadlines := connection.(*deadlineConn)
		if deadlines.readTimeout != example.read || deadlines.writeTimeout != example.finite {
			t.Fatal("socket deadlines differ")
		}
		if err := connection.Close(); err != nil {
			t.Fatal(err)
		}
		if err := listener.Close(); err != nil {
			t.Fatal(err)
		}
	}
}

func TestSend_whenResponseStalls_onlyBatchWaits(t *testing.T) {
	for _, action := range []string{"undo", "save", "get_canvas", "batch"} {
		t.Run(action, func(t *testing.T) {
			release := make(chan struct{})
			started := make(chan struct{})
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				close(started)
				<-release
				fmt.Fprint(w, `{"status":"ok"}`)
			}))
			defer server.Close()
			client := New(server.URL)
			defer client.Close()
			client.ordinary = newHTTP(10 * time.Millisecond)
			client.slow = newHTTP(10 * time.Millisecond)
			params := json.RawMessage(`{}`)
			if action == "save" {
				params = json.RawMessage(`{"path":"art.kra"}`)
			}
			if action == "batch" {
				params = json.RawMessage(`{"commands":[{"action":"undo","params":{}}]}`)
			}
			command, err := Parse(action, params)
			if err != nil {
				t.Fatal(err)
			}
			finished := make(chan Object, 1)
			go func() { finished <- client.Send(t.Context(), command) }()
			<-started
			if action == "batch" {
				select {
				case result := <-finished:
					close(release)
					t.Fatalf("batch prematurely completed: %s", result)
				case <-time.After(30 * time.Millisecond):
				}
				close(release)
				result := <-finished
				if result["error"] != nil {
					t.Fatal(string(result["error"]))
				}
			} else {
				result := <-finished
				close(release)
				if result["error"] == nil || string(result["outcome_unknown"]) != "true" {
					t.Fatalf("expected timeout: %s", result)
				}
			}
		})
	}
}

func TestDeadlineConn_whenWriteBlocks_timesOut(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		first, second := net.Pipe()
		defer first.Close()
		defer second.Close()
		connection := &deadlineConn{Conn: first, writeTimeout: 120 * time.Second}
		_, err := connection.Write([]byte("unread"))
		if err == nil {
			t.Fatal("write deadline missing")
		}
	})
}

func TestDeadlineConn_whenBatchReadWaits_contextControlsLifetime(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		first, second := net.Pipe()
		defer first.Close()
		defer second.Close()
		connection := &deadlineConn{Conn: first, writeTimeout: 120 * time.Second}
		ctx, cancel := context.WithTimeout(t.Context(), 240*time.Second)
		defer cancel()
		go func() {
			<-ctx.Done()
			if err := second.Close(); err != nil {
				t.Error(err)
			}
		}()
		start := time.Now()
		_, err := connection.Read(make([]byte, 1))
		if err == nil || time.Since(start) != 240*time.Second {
			t.Fatal("batch read acquired a finite deadline")
		}
	})
}
