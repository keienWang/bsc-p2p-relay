// SPDX-License-Identifier: GPL-3.0-or-later
//go:build officialtxpool

package relay

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/go-logr/logr/funcr"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

const otelLogChildEnv = "BSC_RELAY_OTEL_LOG_CHILD"
const otelLogChildOK = "OTEL_LOG_REDACTION_CHILD_OK"

// The child is this already-built test binary, not another go command. It
// isolates otel.SetLogger from the rest of the tagged relay suite.
func TestOTelEndpointLogRedaction(t *testing.T) {
	if os.Getenv(otelLogChildEnv) == "1" {
		otelEndpointLogProbe(t)
		if !t.Failed() {
			fmt.Println(otelLogChildOK)
		}
		return
	}
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, executable, "-test.run=^TestOTelEndpointLogRedaction$", "-test.count=1", "-test.v=true")
	cmd.WaitDelay = time.Second
	for _, entry := range os.Environ() {
		key, _, _ := strings.Cut(entry, "=")
		// Do not read real telemetry endpoints, headers or feature settings.
		if strings.HasPrefix(key, "OTEL_") || key == otelLogChildEnv {
			continue
		}
		cmd.Env = append(cmd.Env, entry)
	}
	cmd.Env = append(cmd.Env, otelLogChildEnv+"=1")
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("isolated OTel log regression failed: %v\n%s", err, output)
	}
	if !strings.Contains(string(output), otelLogChildOK) {
		t.Fatalf("isolated OTel log regression did not reach its assertions:\n%s", output)
	}
	t.Logf("isolated OTel log assertions:\n%s", output)
}

type otelNoRequestsTransport struct {
	attempts atomic.Int32
}

func (transport *otelNoRequestsTransport) RoundTrip(*http.Request) (*http.Response, error) {
	transport.attempts.Add(1)
	return nil, errors.New("OTel log regression forbids HTTP requests")
}

func otelEndpointLogProbe(t *testing.T) {
	const endpoint = "otel-log-sentinel.invalid:4318"
	const headerSentinel = "otel-header-sentinel"
	var mu sync.Mutex
	var lines []string
	// Use upstream's real JSON formatter, including its recursive MarshalLog
	// handling. No redaction hook, field filtering or log disabling is allowed.
	logger := funcr.NewJSON(func(line string) {
		mu.Lock()
		lines = append(lines, line)
		mu.Unlock()
	}, funcr.Options{Verbosity: 8, MaxLogDepth: 32})
	otel.SetLogger(logger)
	transport := new(otelNoRequestsTransport)
	client := otlptracehttp.NewClient(
		otlptracehttp.WithEndpoint(endpoint),
		otlptracehttp.WithHeaders(map[string]string{"x-test-sentinel": headerSentinel}),
		otlptracehttp.WithHTTPClient(&http.Client{Transport: transport}),
	)
	exporter := otlptrace.NewUnstarted(client)
	logger.Info("OTEL_LOG_CLIENT_PROBE", "client", client)
	logger.Info("OTEL_LOG_EXPORTER_PROBE", "exporter", exporter)
	// This constructor emits the real upstream "TracerProvider created" log.
	// WithResource still calls resource.Environment; the child clears OTEL_*
	// values before startup. The supplied resource itself is empty. No spans
	// are created and neither client nor exporter is started or flushed.
	provider := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exporter), sdktrace.WithResource(resource.Empty()))
	if err := provider.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := transport.attempts.Load(); got != 0 {
		t.Fatalf("log-only regression unexpectedly attempted %d HTTP requests", got)
	}
	mu.Lock()
	captured := append([]string(nil), lines...)
	mu.Unlock()
	byMessage := make(map[string]map[string]any)
	for _, line := range captured {
		if strings.Contains(line, "otel-log-sentinel") || strings.Contains(line, headerSentinel) {
			t.Errorf("OTel log leaked synthetic endpoint/sentinel: %s", line)
		}
		if strings.Contains(line, "<max-log-depth-exceeded>") || strings.Contains(line, "<panic:") {
			t.Errorf("OTel log formatter did not faithfully render the value: %s", line)
		}
		var record map[string]any
		if err := json.Unmarshal([]byte(line), &record); err != nil {
			t.Fatalf("OTel log is not JSON: %v", err)
		}
		if message, ok := record["msg"].(string); ok {
			byMessage[message] = record
		}
	}
	clientLog := otelLogObject(t, byMessage["OTEL_LOG_CLIENT_PROBE"]["client"])
	if clientLog["Type"] != "otlptracehttp" || len(clientLog) != 1 {
		t.Errorf("HTTP client's safe log shape changed: %#v", clientLog)
	}
	exporterLog := otelLogObject(t, byMessage["OTEL_LOG_EXPORTER_PROBE"]["exporter"])
	if exporterLog["Type"] != "otlptrace" || exporterLog["Client"] != "*otlptracehttp.client" {
		t.Errorf("exporter must log client type, not client contents: %#v", exporterLog)
	}
	config := otelLogObject(t, byMessage["TracerProvider created"]["config"])
	processors, ok := config["SpanProcessors"].([]any)
	if !ok || len(processors) != 1 {
		t.Fatalf("real upstream provider log missing one processor: %#v", config)
	}
	processor := otelLogObject(t, processors[0])
	if processor["Type"] != "SimpleSpanProcessor" || processor["Exporter"] != "*otlptrace.Exporter" {
		t.Errorf("SDK processor must log exporter type, not exporter contents: %#v", processor)
	}
	t.Log("captured positive logs: OTEL_LOG_CLIENT_PROBE, OTEL_LOG_EXPORTER_PROBE, TracerProvider created")
}

func otelLogObject(t *testing.T, value any) map[string]any {
	t.Helper()
	object, ok := value.(map[string]any)
	if !ok {
		t.Fatalf("required positive log object absent or wrong type: %T", value)
	}
	return object
}
