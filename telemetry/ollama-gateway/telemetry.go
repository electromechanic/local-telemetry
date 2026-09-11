package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const proxyServiceName = "ollama-gateway"

type exporter struct {
	endpoint string
	client   *http.Client
	logger   *slog.Logger
	events   chan generationEvent
	dropped  atomic.Uint64
	closed   chan struct{}
	once     sync.Once
}

func newExporter(endpoint string, logger *slog.Logger) (*exporter, error) {
	target, err := url.Parse(strings.TrimRight(endpoint, "/"))
	if err != nil || target.Scheme != "http" || target.Host == "" {
		return nil, fmt.Errorf("OTLP endpoint must be a loopback HTTP URL")
	}
	host := target.Hostname()
	ip := net.ParseIP(host)
	if ip == nil || !ip.IsLoopback() {
		return nil, fmt.Errorf("OTLP endpoint host %q is not an IP loopback address", host)
	}
	result := &exporter{
		endpoint: strings.TrimRight(endpoint, "/"),
		client:   &http.Client{Timeout: 5 * time.Second},
		logger:   logger,
		events:   make(chan generationEvent, 128),
		closed:   make(chan struct{}),
	}
	go result.run()
	return result, nil
}

func (exporter *exporter) Emit(event generationEvent) {
	select {
	case exporter.events <- event:
	default:
		exporter.dropped.Add(1)
		exporter.logger.Warn("dropping telemetry because the bounded queue is full", "dropped", exporter.dropped.Load())
	}
}

func (exporter *exporter) Close() {
	exporter.once.Do(func() {
		close(exporter.events)
		select {
		case <-exporter.closed:
		case <-time.After(10 * time.Second):
			exporter.logger.Warn("timed out flushing proxy telemetry")
		}
	})
}

func (exporter *exporter) run() {
	defer close(exporter.closed)
	for event := range exporter.events {
		for signal, payload := range map[string]any{
			"logs":    logPayload(event),
			"metrics": metricPayload(event),
			"traces":  tracePayload(event),
		} {
			if err := exporter.post(signal, payload); err != nil {
				exporter.logger.Warn("OTLP export failed", "signal", signal, "error_type", fmt.Sprintf("%T", err))
			}
		}
	}
}

func (exporter *exporter) post(signal string, payload any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	request, err := http.NewRequestWithContext(context.Background(), http.MethodPost, exporter.endpoint+"/v1/"+signal, bytes.NewReader(body))
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := exporter.client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("collector returned HTTP %d", response.StatusCode)
	}
	return nil
}

func resource() map[string]any {
	return map[string]any{"attributes": []any{
		attribute("service.name", proxyServiceName),
		attribute("telemetry.sdk.name", "avian-local"),
	}}
}

func commonEventAttributes(event generationEvent) []any {
	attributes := []any{
		attribute("event.name", "ollama.inference.completed"),
		attribute("ai.event.name", "ai.inference.completed"),
		attribute("ai.operation", event.Operation),
		attribute("ai.client", knownClient(event.Client)),
		attribute("ai.telemetry.layer", "inference"),
		attribute("ai.usage.authority", true),
		attribute("ai.provider", "ollama"),
		attribute("ai.inference.locality", "local"),
		attribute("ai.pricing", "local/unpriced"),
		attribute("ai.outcome", event.Outcome),
		attribute("avian.synthetic", false),
		attribute("ai.completion.timestamp", event.CompletedAt.Format(time.RFC3339Nano)),
		attribute("ai.duration.wall_ms", durationMS(event.WallDuration.Nanoseconds())),
		attribute("http.response.status_code", int64(event.HTTPStatus)),
	}
	if event.Model != "" {
		attributes = append(attributes, attribute("ai.model", event.Model), attribute("ollama.model", event.Model))
	}
	if event.DoneReason != "" {
		attributes = append(attributes, attribute("ai.finish_reason", event.DoneReason), attribute("ollama.done_reason", event.DoneReason))
	}
	if event.HasTotalDuration {
		attributes = append(attributes, attribute("ai.duration.total_ms", durationMS(event.TotalDurationNS)))
	}
	if event.HasLoadDuration {
		attributes = append(attributes, attribute("ai.duration.model_load_ms", durationMS(event.LoadDurationNS)))
	}
	if event.HasPromptCount {
		attributes = append(attributes, attribute("ai.tokens.input", event.PromptEvalCount))
	}
	if event.HasPromptDuration {
		attributes = append(attributes, attribute("ai.duration.prompt_evaluation_ms", durationMS(event.PromptEvalNS)))
	}
	if event.HasEvalCount {
		attributes = append(attributes, attribute("ai.tokens.output", event.EvalCount))
	}
	if event.HasEvalDuration {
		attributes = append(attributes, attribute("ai.duration.generation_ms", durationMS(event.EvalDurationNS)))
	}
	if event.HasEvalCount && event.EvalCount > 0 && event.HasEvalDuration && event.EvalDurationNS > 0 {
		attributes = append(attributes, attribute("ai.throughput.output_tokens_per_second", float64(event.EvalCount)/(float64(event.EvalDurationNS)/float64(time.Second))))
	}
	if event.MalformedResponse {
		attributes = append(attributes, attribute("ollama.response.malformed", true))
	}
	return attributes
}

func logPayload(event generationEvent) map[string]any {
	timestamp := strconv.FormatInt(event.CompletedAt.UnixNano(), 10)
	severity := int64(9)
	severityText := "INFO"
	if event.Outcome != "success" {
		severity, severityText = 17, "ERROR"
	}
	return map[string]any{"resourceLogs": []any{map[string]any{
		"resource": resource(),
		"scopeLogs": []any{map[string]any{
			"scope": map[string]any{"name": "avian.ollama.gateway"},
			"logRecords": []any{map[string]any{
				"timeUnixNano":         timestamp,
				"observedTimeUnixNano": timestamp,
				"severityNumber":       severity,
				"severityText":         severityText,
				"body":                 map[string]any{"stringValue": "Ollama gateway generation completed"},
				"attributes":           commonEventAttributes(event),
			}},
		}},
	}}}
}

func metricPayload(event generationEvent) map[string]any {
	timestamp := strconv.FormatInt(event.CompletedAt.UnixNano(), 10)
	base := metricAttributes(event)
	metrics := []any{
		sumMetric("avian.ai.inferences", "Observed inference attempts", "{operation}", 1, timestamp, base),
		histogramMetric("avian.ai.duration", "Gateway request wall duration", "ms", durationMS(event.WallDuration.Nanoseconds()), timestamp, appendAttribute(base, "duration.phase", "wall")),
	}
	if event.PromptEvalCount > 0 {
		metrics = append(metrics, sumMetric("avian.ai.tokens", "AI tokens reported by the source", "{token}", event.PromptEvalCount, timestamp, appendAttribute(base, "token.type", "input")))
	}
	if event.EvalCount > 0 {
		metrics = append(metrics, sumMetric("avian.ai.tokens", "AI tokens reported by the source", "{token}", event.EvalCount, timestamp, appendAttribute(base, "token.type", "output")))
	}
	if event.HasTotalDuration {
		metrics = append(metrics, histogramMetric("avian.ai.duration", "AI operation duration", "ms", durationMS(event.TotalDurationNS), timestamp, appendAttribute(base, "duration.phase", "total")))
	}
	if event.HasEvalDuration {
		metrics = append(metrics, histogramMetric("avian.ai.duration", "AI operation duration", "ms", durationMS(event.EvalDurationNS), timestamp, appendAttribute(base, "duration.phase", "generation")))
	}
	return map[string]any{"resourceMetrics": []any{map[string]any{
		"resource": resource(),
		"scopeMetrics": []any{map[string]any{
			"scope":   map[string]any{"name": "avian.ollama.gateway"},
			"metrics": metrics,
		}},
	}}}
}

func tracePayload(event generationEvent) map[string]any {
	traceID := randomHex(16)
	spanID := randomHex(8)
	end := event.CompletedAt.UnixNano()
	start := end - event.WallDuration.Nanoseconds()
	statusCode := int64(1)
	if event.Outcome != "success" {
		statusCode = 2
	}
	return map[string]any{"resourceSpans": []any{map[string]any{
		"resource": resource(),
		"scopeSpans": []any{map[string]any{
			"scope": map[string]any{"name": "avian.ollama.gateway"},
			"spans": []any{map[string]any{
				"traceId":           traceID,
				"spanId":            spanID,
				"name":              "ollama." + event.Operation,
				"kind":              3,
				"startTimeUnixNano": strconv.FormatInt(start, 10),
				"endTimeUnixNano":   strconv.FormatInt(end, 10),
				"attributes":        commonEventAttributes(event),
				"status":            map[string]any{"code": statusCode},
			}},
		}},
	}}}
}

func metricAttributes(event generationEvent) []any {
	result := []any{
		attribute("ai.operation", event.Operation),
		attribute("ai.client", knownClient(event.Client)),
		attribute("ai.telemetry.layer", "inference"),
		attribute("ai.usage.authority", true),
		attribute("ai.provider", "ollama"),
		attribute("ai.inference.locality", "local"),
		attribute("ai.outcome", event.Outcome),
		attribute("avian.synthetic", false),
	}
	if event.Model != "" {
		result = append(result, attribute("ai.model", event.Model))
	}
	return result
}

func appendAttribute(attributes []any, key string, value any) []any {
	result := append([]any{}, attributes...)
	return append(result, attribute(key, value))
}

func sumMetric(name, description, unit string, value int64, timestamp string, attributes []any) map[string]any {
	return map[string]any{
		"name":        name,
		"description": description,
		"unit":        unit,
		"sum": map[string]any{
			"aggregationTemporality": 1,
			"isMonotonic":            true,
			"dataPoints": []any{map[string]any{
				"attributes":        attributes,
				"asInt":             strconv.FormatInt(value, 10),
				"timeUnixNano":      timestamp,
				"startTimeUnixNano": timestamp,
			}},
		},
	}
}

func histogramMetric(name, description, unit string, value float64, timestamp string, attributes []any) map[string]any {
	bounds := []float64{100, 500, 1000, 5000, 10000, 30000, 60000, 300000}
	buckets := make([]string, len(bounds)+1)
	index := len(bounds)
	for candidate, bound := range bounds {
		if value <= bound {
			index = candidate
			break
		}
	}
	for candidate := range buckets {
		if candidate == index {
			buckets[candidate] = "1"
		} else {
			buckets[candidate] = "0"
		}
	}
	return map[string]any{
		"name":        name,
		"description": description,
		"unit":        unit,
		"histogram": map[string]any{
			"aggregationTemporality": 1,
			"dataPoints": []any{map[string]any{
				"attributes":        attributes,
				"count":             "1",
				"sum":               value,
				"explicitBounds":    bounds,
				"bucketCounts":      buckets,
				"timeUnixNano":      timestamp,
				"startTimeUnixNano": timestamp,
			}},
		},
	}
}

func attribute(key string, value any) map[string]any {
	wrapped := map[string]any{}
	switch typed := value.(type) {
	case string:
		wrapped["stringValue"] = typed
	case bool:
		wrapped["boolValue"] = typed
	case int:
		wrapped["intValue"] = strconv.Itoa(typed)
	case int64:
		wrapped["intValue"] = strconv.FormatInt(typed, 10)
	case float64:
		wrapped["doubleValue"] = typed
	default:
		wrapped["stringValue"] = fmt.Sprint(value)
	}
	return map[string]any{"key": key, "value": wrapped}
}

func durationMS(nanoseconds int64) float64 {
	return float64(nanoseconds) / float64(time.Millisecond)
}

func randomHex(size int) string {
	value := make([]byte, size)
	if _, err := rand.Read(value); err != nil {
		panic("crypto/rand unavailable")
	}
	return hex.EncodeToString(value)
}
