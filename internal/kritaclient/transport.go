package kritaclient

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strings"
	"time"
)

const DefaultURL = "http://127.0.0.1:5678"

type Client struct {
	url                           string
	ordinary, slow, batch, health *http.Client
}

func URLFromEnv() string {
	if url, exists := os.LookupEnv("KRITA_URL"); exists {
		return url
	}
	return DefaultURL
}

func New(url string) *Client {
	return &Client{url: strings.TrimRight(url, "/"), ordinary: newHTTP(30 * time.Second),
		slow: newHTTP(120 * time.Second), batch: newHTTP(0), health: newHTTP(5 * time.Second)}
}

func (c *Client) Close() {
	for _, client := range []*http.Client{c.ordinary, c.slow, c.batch, c.health} {
		client.CloseIdleConnections()
	}
}

func newHTTP(readTimeout time.Duration) *http.Client {
	finite := readTimeout
	if finite == 0 {
		finite = 120 * time.Second
	}
	dialer := &net.Dialer{Timeout: finite, KeepAlive: 30 * time.Second}
	transport := &http.Transport{
		DisableKeepAlives: true,
		Proxy:             nil, TLSHandshakeTimeout: finite, ResponseHeaderTimeout: readTimeout,
		IdleConnTimeout: 90 * time.Second, MaxIdleConns: 32, MaxIdleConnsPerHost: 32,
		DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			connection, err := dialer.DialContext(ctx, network, address)
			if err != nil {
				return nil, fmt.Errorf("connect: %w", err)
			}
			return &deadlineConn{Conn: connection, readTimeout: readTimeout, writeTimeout: finite}, nil
		},
	}
	return &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
}

type deadlineConn struct {
	net.Conn
	readTimeout, writeTimeout time.Duration
}

func (c *deadlineConn) Read(buffer []byte) (int, error) {
	if c.readTimeout > 0 {
		if err := c.SetReadDeadline(time.Now().Add(c.readTimeout)); err != nil {
			return 0, fmt.Errorf("read deadline: %w", err)
		}
	}
	return c.Conn.Read(buffer)
}

func (c *deadlineConn) Write(buffer []byte) (int, error) {
	if err := c.SetWriteDeadline(time.Now().Add(c.writeTimeout)); err != nil {
		return 0, fmt.Errorf("write deadline: %w", err)
	}
	return c.Conn.Write(buffer)
}

func (c *Client) Send(ctx context.Context, command Command) Object {
	payload, err := command.MarshalJSON()
	if err != nil {
		return ErrorResult("Encode Krita request: " + err.Error())
	}
	client := c.ordinary
	switch command.action {
	case "batch":
		client = c.batch
	case "get_canvas", "save":
		client = c.slow
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(payload))
	if err != nil {
		return ErrorResult("Invalid KRITA_URL: " + err.Error())
	}
	request.GetBody = nil
	request.Header.Set("Content-Type", "application/json")
	return c.request(client, request)
}

func (c *Client) Health(ctx context.Context) Object { return c.get(ctx, "/health") }
func (c *Client) Info(ctx context.Context) Object   { return c.get(ctx, "/info") }

func (c *Client) get(ctx context.Context, path string) Object {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, c.url+path, nil)
	if err != nil {
		return ErrorResult("Invalid KRITA_URL: " + err.Error())
	}
	return c.request(c.health, request)
}

func (c *Client) request(client *http.Client, request *http.Request) Object {
	response, err := client.Do(request)
	if err != nil {
		return c.transportError(request.Method, err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		return c.transportError(request.Method, err)
	}
	var result Object
	if err := json.Unmarshal(body, &result); err != nil || result == nil {
		result = ErrorResult(fmt.Sprintf("Krita returned HTTP %d with invalid JSON or a non-object response. Check plugin installation/version.", response.StatusCode))
		result["outcome_unknown"] = json.RawMessage(fmt.Sprint(request.Method == http.MethodPost))
		return result
	}
	if _, failed := result["error"]; (response.StatusCode < 200 || response.StatusCode >= 300) && !failed {
		result = ErrorResult(fmt.Sprintf("Krita returned HTTP %d", response.StatusCode))
		result["outcome_unknown"] = json.RawMessage(fmt.Sprint(request.Method == http.MethodPost))
	}
	return result
}

func (c *Client) transportError(method string, err error) Object {
	var operation *net.OpError
	if errors.As(err, &operation) && operation.Op == "dial" {
		return ErrorResult(fmt.Sprintf("Cannot connect to Krita at %s. Enable the whole MCP plugin, restart Krita, and check KRITA_URL / KRITA_MCP_PORT. %v", c.url, err))
	}
	message := fmt.Sprintf("Krita HTTP transport failed: %v. Execution outcome may be unknown; no automatic retry was made.", err)
	var timeout net.Error
	if errors.As(err, &timeout) && timeout.Timeout() {
		message = fmt.Sprintf("Krita request timed out: %v. Execution outcome is unknown; inspect Krita before retrying. No automatic retry was made.", err)
	}
	result := ErrorResult(message)
	result["outcome_unknown"] = json.RawMessage(fmt.Sprint(method == http.MethodPost))
	return result
}

func ErrorResult(message string) Object {
	encoded, _ := json.Marshal(message)
	return Object{"error": encoded}
}
