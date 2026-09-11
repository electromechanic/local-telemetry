package main

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestGatewayAttributionAndRouting(t *testing.T) {
	for _, tc := range []struct{ path, header, client string }{
		{"/api/chat", "", "unknown"}, {"/api/chat", "opencode", "opencode"},
		{"/clients/alpaca/api/chat", "opencode", "alpaca"}, {"/api/chat", "private arbitrary label", "unknown"},
	} {
		t.Run(tc.path+tc.header, func(t *testing.T) {
			sink := newRecordingSink()
			server := testProxy(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/api/chat" || r.Header.Get("X-Avian-Client") != "" {
					t.Errorf("incorrect upstream routing: %s", r.URL.Path)
				}
				io.WriteString(w, `{"model":"qwen","done":true,"eval_count":3}`)
			}), sink)
			request, _ := http.NewRequest("POST", server.URL+tc.path, strings.NewReader(`{}`))
			request.Header.Set("X-Avian-Client", tc.header)
			response, err := http.DefaultClient.Do(request)
			if err != nil {
				t.Fatal(err)
			}
			io.Copy(io.Discard, response.Body)
			response.Body.Close()
			event := sink.wait(t)
			if event.Client != tc.client {
				t.Fatalf("client=%s", event.Client)
			}
			data, _ := json.Marshal(logPayload(event))
			if strings.Contains(string(data), "private arbitrary") || !strings.Contains(string(data), "ai.inference.completed") {
				t.Fatal(string(data))
			}
		})
	}
}

func TestCompatibleUsageAndBytePreservation(t *testing.T) {
	for _, tc := range []struct {
		name, path, contentType, body, outcome string
		input, output                          int64
	}{
		{"chat", "/v1/chat/completions", "application/json", `{"model":"qwen","choices":[{"message":{"content":"private response"},"finish_reason":"stop"}],"usage":{"prompt_tokens":7,"completion_tokens":11}}`, "success", 7, 11},
		{"late-stream-usage", "/v1/chat/completions", "text/event-stream", "data: {\"model\":\"qwen\",\"choices\":[{\"delta\":{\"content\":\"private response\"},\"finish_reason\":\"stop\"}]}\n\ndata: {\"choices\":[],\"usage\":{\"prompt_tokens\":7,\"completion_tokens\":11}}\n\ndata: [DONE]\n\n", "success", 7, 11},
		{"no-usage", "/v1/completions", "application/json", `{"model":"qwen","choices":[{"text":"private response","finish_reason":"length"}]}`, "success", 0, 0},
		{"responses", "/v1/responses", "application/json", `{"model":"qwen","status":"completed","output":[{"content":[{"text":"private response"}]}],"usage":{"input_tokens":7,"output_tokens":11}}`, "success", 7, 11},
		{"responses-stream", "/v1/responses", "text/event-stream", "event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":{\"model\":\"qwen\",\"status\":\"completed\",\"usage\":{\"input_tokens\":7,\"output_tokens\":11}}}\n\n", "success", 7, 11},
		{"error", "/v1/chat/completions", "application/json", `{"error":{"message":"private response","type":"internal"}}`, "failure", 0, 0},
		{"truncated", "/v1/chat/completions", "text/event-stream", "data: {\"model\":\"qwen\",\"choices\":[{\"finish_reason\":\"stop\"}]}\n\n", "failure", 0, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			sink := newRecordingSink()
			server := testProxy(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", tc.contentType)
				for _, b := range []byte(tc.body) {
					w.Write([]byte{b})
					w.(http.Flusher).Flush()
				}
			}), sink)
			response, err := http.Post(server.URL+"/clients/opencode"+tc.path, "application/json", strings.NewReader(`{}`))
			if err != nil {
				t.Fatal(err)
			}
			body, _ := io.ReadAll(response.Body)
			response.Body.Close()
			if string(body) != tc.body {
				t.Fatal("changed response")
			}
			event := sink.wait(t)
			if event.Outcome != tc.outcome || event.PromptEvalCount != tc.input || event.EvalCount != tc.output || event.Client != "opencode" {
				t.Fatalf("unexpected event: %+v", event)
			}
			if tc.input == 0 && event.HasPromptCount {
				t.Fatal("fabricated usage")
			}
			sink.mu.Lock()
			defer sink.mu.Unlock()
			if len(sink.events) != 1 {
				t.Fatal("double counted response")
			}
			assertTelemetryPrivate(t, event)
		})
	}
}

func TestCompatibleParserBoundedAcrossFragments(t *testing.T) {
	body := `{"model":"qwen","choices":[{"message":{"content":"` + strings.Repeat("private response", 100000) + `"},"finish_reason":"stop"}],"usage":{"prompt_tokens":7,"completion_tokens":11}}`
	for _, size := range []int{1, 7, 1024} {
		p := compatibilityParser{}
		for offset := 0; offset < len(body); offset += size {
			p.Feed([]byte(body[offset:min(offset+size, len(body))]))
			if len(p.token) > maxMetadataToken || len(p.frames) > 64 || len(p.outside) > 64 {
				t.Fatal("unbounded parser")
			}
		}
		m := p.End()
		if !m.done || m.malformed || m.evalCount != 11 {
			t.Fatalf("size %d: %+v", size, m)
		}
	}
}
