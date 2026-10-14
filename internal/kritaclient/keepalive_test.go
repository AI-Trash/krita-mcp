package kritaclient

import (
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestSend_whenIdleBetweenRequests_usesFreshConnectionAndReadBudget(t *testing.T) {
	const readBudget = 500 * time.Millisecond
	addresses := make(chan string, 2)
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, err := io.Copy(io.Discard, r.Body); err != nil {
			t.Error(err)
			return
		}
		addresses <- r.RemoteAddr
		if calls.Add(1) == 2 {
			select {
			case <-time.After(200 * time.Millisecond):
			case <-r.Context().Done():
				return
			}
		}
		if _, err := fmt.Fprint(w, `{"status":"ok"}`); err != nil {
			t.Error(err)
		}
	}))
	defer server.Close()
	client := New(server.URL)
	client.ordinary = newHTTP(readBudget)
	defer client.Close()
	command, err := Parse("undo", nil)
	if err != nil {
		t.Fatal(err)
	}
	if result := client.Send(t.Context(), command); result["error"] != nil {
		t.Fatal(string(result["error"]))
	}
	<-time.After(400 * time.Millisecond)
	result := client.Send(t.Context(), command)
	if result["error"] != nil {
		t.Errorf("second response lost its fresh read budget: %s", result["error"])
	}
	if calls.Load() != 2 {
		t.Fatalf("HTTP attempts=%d", calls.Load())
	}
	if first, second := <-addresses, <-addresses; first == second {
		t.Error("HTTP connection was reused")
	}
}
