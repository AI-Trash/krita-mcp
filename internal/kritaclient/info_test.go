package kritaclient

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestInfo_whenQueried_usesReadOnlyEndpoint(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" || r.URL.Path != "/info" {
			t.Errorf("request=%s %s", r.Method, r.URL.Path)
		}
		fmt.Fprint(w, `{"plugin":"kritamcp","version":"0.3.0"}`)
	}))
	defer server.Close()
	client := New(server.URL + "/")
	defer client.Close()
	result := client.Info(t.Context())
	if string(result["version"]) != `"0.3.0"` {
		t.Fatalf("result=%s", result)
	}
}
