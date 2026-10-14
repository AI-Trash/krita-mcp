package mcpserver

import (
	"errors"
	"os/exec"
	"strings"
	"testing"
)

func TestBinary_whenVersionFlag_exposesReleaseMetadata(t *testing.T) {
	binary := buildBinary(t)
	output, err := exec.CommandContext(t.Context(), binary, "--version").Output()
	if err != nil {
		t.Fatal(err)
	}
	if string(output) != "krita-mcp parity-test (commit abc123, built 2026-10-14)\n" {
		t.Fatalf("metadata=%s", output)
	}
}

func TestBinary_whenDrawingCLIRequested_rejectsWithoutProtocolOutput(t *testing.T) {
	binary := buildBinary(t)
	command := exec.CommandContext(t.Context(), binary, "stroke")
	var stderr strings.Builder
	command.Stderr = &stderr
	output, err := command.Output()
	var exit *exec.ExitError
	if !errors.As(err, &exit) || exit.ExitCode() != 2 || len(output) != 0 || !strings.Contains(stderr.String(), "Usage:") {
		t.Fatalf("stdout=%s stderr=%s error=%v", output, stderr.String(), err)
	}
}
