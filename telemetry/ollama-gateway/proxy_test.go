package main

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

type recordingSink struct {
	mu     sync.Mutex
	events []generationEvent
	notify chan struct{}
}

func newRecordingSink() *recordingSink {
	return &recordingSink{notify: make(chan struct{}, 16)}
}

func (sink *recordingSink) Emit(event generationEvent) {
	sink.mu.Lock()
	sink.events = append(sink.events, event)
	sink.mu.Unlock()
	select {
	case sink.notify <- struct{}{}:
	default:
	}
}

func (sink *recordingSink) wait(t *testing.T) generationEvent {
	t.Helper()
	select {
	case <-sink.notify:
	case <-time.After(3 * time.Second):
		t.Fatal("timed out waiting for telemetry")
	}
	sink.mu.Lock()
	defer sink.mu.Unlock()
	return sink.events[len(sink.events)-1]
}

func testProxy(t *testing.T, upstream http.Handler, sink eventSink) *httptest.Server {
	t.Helper()
	upstreamServer := httptest.NewServer(upstream)
	t.Cleanup(upstreamServer.Close)
	handler, err := newProxy("127.0.0.1:11435", upstreamServer.URL, sink, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	return server
}

func TestStreamingNDJSONPreservesBytesAndExtractsOnlyFinalMetadata(t *testing.T) {
	const response = "{\"model\":\"tiny-local\",\"message\":{\"content\":\"private response\",\"thinking\":\"private thought\",\"tool_calls\":[{\"arguments\":{\"secret\":\"value\"}}]},\"done\":false}\n" +
		"{\"model\":\"tiny-local\",\"message\":{\"content\":\"\"},\"done\":true,\"done_reason\":\"stop\",\"total_duration\":9000000,\"load_duration\":1000000,\"prompt_eval_count\":7,\"prompt_eval_duration\":2000000,\"eval_count\":11,\"eval_duration\":4000000}\n"
	sink := newRecordingSink()
	server := testProxy(t, http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.Header().Set("Content-Type", "application/x-ndjson")
		flusher := writer.(http.Flusher)
		for index := 0; index < len(response); {
			size := index%7 + 1
			end := min(index+size, len(response))
			_, _ = io.WriteString(writer, response[index:end])
			flusher.Flush()
			index = end
		}
	}), sink)

	request, _ := http.NewRequest(http.MethodPost, server.URL+"/api/chat", strings.NewReader(`{"messages":[{"content":"private prompt"}]}`))
	got, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(got.Body)
	got.Body.Close()
	if string(body) != response {
		t.Fatalf("stream changed in transit: %q", body)
	}
	event := sink.wait(t)
	if event.Outcome != "success" || event.Model != "tiny-local" || event.DoneReason != "stop" {
		t.Fatalf("unexpected event: %+v", event)
	}
	if event.PromptEvalCount != 7 || event.EvalCount != 11 || event.EvalDurationNS != 4_000_000 {
		t.Fatalf("usage metadata missing: %+v", event)
	}
	assertTelemetryPrivate(t, event)
}

func TestNonStreamingLargeResponseUsesBoundedMetadataTokens(t *testing.T) {
	privateResponse := strings.Repeat("private-response-", 100_000)
	payload, _ := json.Marshal(map[string]any{
		"model": "small-model", "response": privateResponse, "thinking": "private-thought",
		"done": true, "done_reason": "length", "prompt_eval_count": 3,
		"prompt_eval_duration": 2_000_000, "eval_count": 5, "eval_duration": 5_000_000,
	})
	sink := newRecordingSink()
	server := testProxy(t, http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write(payload)
	}), sink)
	response, err := http.Post(server.URL+"/api/generate", "application/json", strings.NewReader(`{"prompt":"private prompt"}`))
	if err != nil {
		t.Fatal(err)
	}
	forwarded, _ := io.ReadAll(response.Body)
	response.Body.Close()
	if string(forwarded) != string(payload) {
		t.Fatal("non-streaming response changed in transit")
	}
	event := sink.wait(t)
	if event.Outcome != "success" || event.Model != "small-model" || event.EvalCount != 5 {
		t.Fatalf("unexpected event: %+v", event)
	}
	assertTelemetryPrivate(t, event)
}

func TestFragmentedFinalRecordAcrossEveryBoundary(t *testing.T) {
	payload := []byte(`{"model":"fragmented","done":true,"prompt_eval_count":2,"prompt_eval_duration":3,"eval_count":4,"eval_duration":5}`)
	for size := 1; size <= len(payload); size++ {
		var got responseMetadata
		parser := metadataParser{onFinal: func(metadata responseMetadata) { got = metadata }}
		for offset := 0; offset < len(payload); offset += size {
			parser.Feed(payload[offset:min(offset+size, len(payload))])
		}
		parser.End()
		if !parser.finalObserved || got.model != "fragmented" || got.promptEvalCount != 2 || got.evalCount != 4 {
			t.Fatalf("fragment size %d failed: %+v", size, got)
		}
	}
}

func TestUpstreamErrorDoesNotExportErrorBody(t *testing.T) {
	sink := newRecordingSink()
	server := testProxy(t, http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.WriteHeader(http.StatusInternalServerError)
		_, _ = io.WriteString(writer, `{"error":"private upstream details"}`)
	}), sink)
	response, err := http.Post(server.URL+"/api/chat", "application/json", strings.NewReader(`{"messages":[]}`))
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, response.Body)
	response.Body.Close()
	event := sink.wait(t)
	if event.Outcome != "failure" || event.HTTPStatus != http.StatusInternalServerError {
		t.Fatalf("unexpected event: %+v", event)
	}
	assertTelemetryPrivate(t, event)
}

func TestHTTPErrorCannotBeReportedAsSuccess(t *testing.T) {
	sink := newRecordingSink()
	server := testProxy(t, http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.WriteHeader(http.StatusTooManyRequests)
		_, _ = io.WriteString(writer, `{"model":"m","done":true,"done_reason":"stop"}`)
	}), sink)
	response, err := http.Post(server.URL+"/api/generate", "application/json", strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, response.Body)
	response.Body.Close()
	event := sink.wait(t)
	if event.Outcome != "failure" || event.HTTPStatus != http.StatusTooManyRequests {
		t.Fatalf("unexpected event: %+v", event)
	}
}

func TestMalformedFinalAndMissingUsage(t *testing.T) {
	tests := []struct {
		name    string
		body    string
		outcome string
	}{
		{"malformed numeric metadata", `{"model":"m","done":true,"eval_count":oops}`, "failure"},
		{"missing usage remains missing", `{"model":"m","done":true,"done_reason":"stop"}`, "success"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			sink := newRecordingSink()
			server := testProxy(t, http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
				_, _ = io.WriteString(writer, test.body)
			}), sink)
			response, err := http.Post(server.URL+"/api/chat", "application/json", strings.NewReader(`{}`))
			if err != nil {
				t.Fatal(err)
			}
			_, _ = io.Copy(io.Discard, response.Body)
			response.Body.Close()
			event := sink.wait(t)
			if event.Outcome != test.outcome {
				t.Fatalf("unexpected event: %+v", event)
			}
			if strings.Contains(test.name, "missing") && (event.HasPromptCount || event.HasEvalCount || event.PromptEvalCount != 0 || event.EvalCount != 0) {
				t.Fatalf("missing usage was manufactured: %+v", event)
			}
		})
	}
}

func TestClientCancellationIsReported(t *testing.T) {
	started := make(chan struct{})
	sink := newRecordingSink()
	server := testProxy(t, http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("Content-Type", "application/x-ndjson")
		_, _ = io.WriteString(writer, `{"model":"m","message":{"content":"partial"},"done":false}`+"\n")
		writer.(http.Flusher).Flush()
		close(started)
		<-request.Context().Done()
	}), sink)
	ctx, cancel := context.WithCancel(context.Background())
	request, _ := http.NewRequestWithContext(ctx, http.MethodPost, server.URL+"/api/chat", strings.NewReader(`{}`))
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	<-started
	cancel()
	_, _ = io.Copy(io.Discard, response.Body)
	response.Body.Close()
	event := sink.wait(t)
	if event.Outcome != "cancellation" {
		t.Fatalf("unexpected event: %+v", event)
	}
}

func TestManagementAndUnknownEndpointsPassThroughWithoutTelemetry(t *testing.T) {
	sink := newRecordingSink()
	requests := []struct{ method, path string }{
		{http.MethodGet, "/api/tags"}, {http.MethodPost, "/api/show"},
		{http.MethodPost, "/api/pull"}, {http.MethodPost, "/api/create"},
		{http.MethodHead, "/api/blobs/sha256:test"}, {http.MethodPost, "/api/blobs/sha256:test"},
		{http.MethodPost, "/api/copy"}, {http.MethodDelete, "/api/delete"},
		{http.MethodPost, "/api/embed"}, {http.MethodPost, "/api/embeddings"},
		{http.MethodGet, "/api/ps"}, {http.MethodGet, "/api/version"},
		{http.MethodPatch, "/future/route"},
	}
	server := testProxy(t, http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("X-Upstream", "preserved")
		writer.WriteHeader(http.StatusCreated)
		_, _ = io.WriteString(writer, request.Method+" "+request.URL.Path)
	}), sink)
	for _, spec := range requests {
		request, _ := http.NewRequest(spec.method, server.URL+spec.path, strings.NewReader("opaque-body"))
		response, err := http.DefaultClient.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(response.Body)
		response.Body.Close()
		expectedBody := spec.method + " " + spec.path
		if response.StatusCode != http.StatusCreated || response.Header.Get("X-Upstream") != "preserved" || (spec.method != http.MethodHead && string(body) != expectedBody) {
			t.Fatalf("passthrough failed for %s %s", spec.method, spec.path)
		}
	}
	select {
	case <-sink.notify:
		t.Fatal("management traffic was instrumented as generation")
	default:
	}
}

func TestHealthAndLoopbackBinding(t *testing.T) {
	if err := requireLoopbackAddress("127.0.0.1:11435"); err != nil {
		t.Fatal(err)
	}
	for _, address := range []string{"0.0.0.0:11435", "localhost:11435", ":11435"} {
		if err := requireLoopbackAddress(address); err == nil {
			t.Fatalf("accepted non-explicit-loopback address %q", address)
		}
	}
	sink := newRecordingSink()
	server := testProxy(t, http.NotFoundHandler(), sink)
	response, err := http.Get(server.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(response.Body)
	response.Body.Close()
	if response.StatusCode != http.StatusOK || !strings.Contains(string(body), `"status":"ready"`) {
		t.Fatalf("unexpected health response: %d %s", response.StatusCode, body)
	}
}

func assertTelemetryPrivate(t *testing.T, event generationEvent) {
	t.Helper()
	combined, err := json.Marshal([]any{logPayload(event), metricPayload(event), tracePayload(event)})
	if err != nil {
		t.Fatal(err)
	}
	for _, forbidden := range []string{"private prompt", "private response", "private-response", "private thought", "private-thought", "secret", "tool_calls", "arguments", "embedding", "image"} {
		if strings.Contains(string(combined), forbidden) {
			t.Fatalf("telemetry contains forbidden payload %q", forbidden)
		}
	}
}
