package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/AI-Trash/krita-mcp/internal/kritaclient"
	"github.com/AI-Trash/krita-mcp/internal/mcpserver"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

var (
	version = "dev"
	commit  = "none"
	date    = "unknown"
)

func main() {
	if len(os.Args) == 2 && os.Args[1] == "--version" {
		fmt.Printf("krita-mcp %s (commit %s, built %s)\n", version, commit, date)
		return
	}
	if len(os.Args) != 1 {
		fmt.Fprintln(os.Stderr, "Usage: krita-mcp [--version]; without arguments starts the stdio MCP server")
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	client := kritaclient.New(kritaclient.URLFromEnv())
	defer client.Close()
	server := mcpserver.New(&mcp.Implementation{Name: "krita-mcp", Version: version,
		Description: fmt.Sprintf("Krita MCP bridge (commit %s, built %s)", commit, date)}, client)
	if err := server.Run(ctx, &mcp.StdioTransport{MaxLineLength: -1}); err != nil && ctx.Err() == nil {
		slog.Error("MCP server stopped", "error", err)
		os.Exit(1)
	}
}
