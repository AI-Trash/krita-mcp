package mcpserver

import (
	"context"
	"encoding/json"
	"errors"
	"strings"

	"github.com/AI-Trash/krita-mcp/internal/kritaclient"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func New(implementation *mcp.Implementation, client *kritaclient.Client) *mcp.Server {
	server := mcp.NewServer(implementation, nil)
	for action, description := range descriptions {
		tool := &mcp.Tool{Name: "krita_" + action, Description: description, InputSchema: kritaclient.InputSchema(action)}
		words := strings.Split(tool.Name, "_")
		for index, word := range words {
			words[index] = strings.ToUpper(word[:1]) + word[1:]
		}
		tool.Title = strings.Join(words, " ")
		if isLegacy(action) {
			tool.OutputSchema = json.RawMessage(`{"type":"object","properties":{"result":{"type":"string"}},"required":["result"],"x-fastmcp-wrap-result":true}`)
		} else {
			tool.OutputSchema = json.RawMessage(`{"type":"object","additionalProperties":{}}`)
		}
		server.AddTool(tool, func(ctx context.Context, request *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			command, err := kritaclient.ParseTool(action, request.Params.Arguments)
			if err != nil {
				var childError *kritaclient.BatchError
				if errors.As(err, &childError) {
					return structured(kritaclient.ErrorResult(err.Error()))
				}
				return &mcp.CallToolResult{IsError: true, Content: []mcp.Content{&mcp.TextContent{Text: err.Error()}}}, nil
			}
			var result kritaclient.Object
			if action == "health" {
				result = client.Health(ctx)
			} else {
				result = client.Send(ctx, command)
			}
			if isLegacy(action) {
				text := legacyText(command, result)
				return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: text}}, StructuredContent: struct {
					Result string `json:"result"`
				}{text}}, nil
			}
			return structured(result)
		})
	}
	return server
}

func structured(result kritaclient.Object) (*mcp.CallToolResult, error) {
	encoded, err := json.Marshal(result)
	if err != nil {
		return nil, err
	}
	return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: string(encoded)}}, StructuredContent: result}, nil
}
