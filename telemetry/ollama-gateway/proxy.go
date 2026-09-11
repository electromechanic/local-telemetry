package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
)

type eventSink interface {
	Emit(generationEvent)
}

type generationEvent struct {
	Client            string
	Operation         string
	Model             string
	Outcome           string
	DoneReason        string
	HTTPStatus        int
	CompletedAt       time.Time
	WallDuration      time.Duration
	TotalDurationNS   int64
	LoadDurationNS    int64
	PromptEvalCount   int64
	PromptEvalNS      int64
	EvalCount         int64
	EvalDurationNS    int64
	HasTotalDuration  bool
	HasLoadDuration   bool
	HasPromptCount    bool
	HasPromptDuration bool
	HasEvalCount      bool
	HasEvalDuration   bool
	MalformedResponse bool
}

type observationKey struct{}

type observation struct {
	requestContext context.Context
	operation      string
	client         string
	compatible     *compatibilityParser
	started        time.Time
	sink           eventSink
	status         int
	parser         metadataParser
	once           sync.Once
}

func newProxy(listen, upstream string, sink eventSink, logger *slog.Logger) (http.Handler, error) {
	if err := requireLoopbackAddress(listen); err != nil {
		return nil, err
	}
	target, err := url.Parse(upstream)
	if err != nil || target.Scheme == "" || target.Host == "" {
		return nil, fmt.Errorf("invalid Ollama upstream %q", upstream)
	}
	if target.Scheme != "http" && target.Scheme != "https" {
		return nil, fmt.Errorf("Ollama upstream scheme must be http or https")
	}

	reverse := httputil.NewSingleHostReverseProxy(target)
	reverse.FlushInterval = -1
	baseDirector := reverse.Director
	reverse.Director = func(request *http.Request) {
		baseDirector(request)
		request.Host = target.Host
	}
	reverse.ModifyResponse = func(response *http.Response) error {
		observed, _ := response.Request.Context().Value(observationKey{}).(*observation)
		if observed == nil {
			return nil
		}
		observed.status = response.StatusCode
		if observed.compatible != nil {
			observed.compatible.streaming = strings.HasPrefix(response.Header.Get("Content-Type"), "text/event-stream")
		}
		response.Body = &observedBody{ReadCloser: response.Body, observed: observed}
		return nil
	}
	reverse.ErrorHandler = func(writer http.ResponseWriter, request *http.Request, proxyErr error) {
		observed, _ := request.Context().Value(observationKey{}).(*observation)
		if observed != nil {
			outcome := "failure"
			if errors.Is(request.Context().Err(), context.Canceled) {
				outcome = "cancellation"
			}
			observed.finish(outcome, false)
		}
		logger.Warn("Ollama upstream request failed", "path", request.URL.Path, "error_type", fmt.Sprintf("%T", proxyErr))
		http.Error(writer, "Ollama upstream unavailable", http.StatusBadGateway)
	}

	return http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path == "/healthz" {
			if request.Method != http.MethodGet && request.Method != http.MethodHead {
				writer.WriteHeader(http.StatusMethodNotAllowed)
				return
			}
			writer.Header().Set("Content-Type", "application/json")
			writer.Header().Set("Cache-Control", "no-store")
			writer.WriteHeader(http.StatusOK)
			if request.Method == http.MethodGet {
				_, _ = io.WriteString(writer, `{"status":"ready","service":"ollama-gateway"}`+"\n")
			}
			return
		}

		client := "unknown"
		// Attribution is an optional local diagnostic label, never authentication.
		// A URL prefix supports clients (including Alpaca) without custom headers.
		if strings.HasPrefix(request.URL.Path, "/clients/") {
			parts := strings.SplitN(strings.TrimPrefix(request.URL.Path, "/clients/"), "/", 2)
			if len(parts) != 2 || knownClient(parts[0]) == "unknown" {
				http.Error(writer, "unknown gateway client", http.StatusBadRequest)
				return
			}
			client = parts[0]
			request.URL.Path = "/" + parts[1]
			request.URL.RawPath = ""
		} else {
			client = knownClient(request.Header.Get("X-Avian-Client"))
		}
		request.Header.Del("X-Avian-Client")
		operation, instrument := generationOperation(request.Method, request.URL.Path)
		if !instrument {
			reverse.ServeHTTP(writer, request)
			return
		}
		observed := &observation{
			requestContext: request.Context(),
			operation:      operation,
			client:         client,
			started:        time.Now(),
			sink:           sink,
		}
		if strings.HasPrefix(request.URL.Path, "/v1/") {
			observed.compatible = &compatibilityParser{}
		}
		observed.parser.onFinal = func(metadata responseMetadata) {
			observed.finishMetadata(metadata)
		}
		request = request.WithContext(context.WithValue(request.Context(), observationKey{}, observed))
		reverse.ServeHTTP(writer, request)
	}), nil
}

func knownClient(value string) string {
	switch value {
	case "alpaca", "opencode", "codex", "cli":
		return value
	default:
		return "unknown"
	}
}

func generationOperation(method, path string) (string, bool) {
	if method != http.MethodPost {
		return "", false
	}
	switch path {
	case "/api/chat", "/v1/chat/completions":
		return "chat", true
	case "/api/generate", "/v1/completions":
		return "generate", true
	case "/v1/responses":
		return "responses", true
	default:
		return "", false
	}
}

func requireLoopbackAddress(address string) error {
	host, _, err := net.SplitHostPort(address)
	if err != nil {
		return fmt.Errorf("listen address must include an explicit loopback host and port: %w", err)
	}
	ip := net.ParseIP(host)
	if ip == nil || !ip.IsLoopback() {
		return fmt.Errorf("listen address %q is not an IP loopback address", address)
	}
	return nil
}

type observedBody struct {
	io.ReadCloser
	observed *observation
	sawEOF   bool
}

func (body *observedBody) Read(buffer []byte) (int, error) {
	count, err := body.ReadCloser.Read(buffer)
	if count > 0 {
		if body.observed.compatible != nil {
			body.observed.compatible.Feed(buffer[:count])
		} else {
			body.observed.parser.Feed(buffer[:count])
		}
	}
	if errors.Is(err, io.EOF) {
		body.sawEOF = true
		if parser := body.observed.compatible; parser != nil {
			metadata := parser.End()
			if !metadata.done && !metadata.hasError {
				metadata.malformed = true
			}
			body.observed.finishMetadata(metadata)
			return count, err
		}
		body.observed.parser.End()
		if !body.observed.parser.finalObserved {
			outcome := "failure"
			if errors.Is(body.observed.requestContext.Err(), context.Canceled) {
				outcome = "cancellation"
			}
			body.observed.finish(outcome, true)
		}
	}
	return count, err
}

func (body *observedBody) Close() error {
	if !body.sawEOF {
		outcome := "failure"
		if errors.Is(body.observed.requestContext.Err(), context.Canceled) {
			outcome = "cancellation"
		}
		body.observed.finish(outcome, false)
	}
	return body.ReadCloser.Close()
}

func (observed *observation) finishMetadata(metadata responseMetadata) {
	outcome := "success"
	if metadata.hasError || observed.status >= http.StatusBadRequest {
		outcome = "failure"
	}
	if metadata.malformed {
		outcome = "failure"
	}
	observed.once.Do(func() {
		observed.sink.Emit(generationEvent{
			Operation:         observed.operation,
			Client:            observed.client,
			Model:             sanitizeDimension(metadata.model, 128),
			Outcome:           outcome,
			DoneReason:        sanitizeDimension(metadata.doneReason, 64),
			HTTPStatus:        observed.status,
			CompletedAt:       time.Now().UTC(),
			WallDuration:      time.Since(observed.started),
			TotalDurationNS:   metadata.totalDuration,
			LoadDurationNS:    metadata.loadDuration,
			PromptEvalCount:   metadata.promptEvalCount,
			PromptEvalNS:      metadata.promptEvalDuration,
			EvalCount:         metadata.evalCount,
			EvalDurationNS:    metadata.evalDuration,
			HasTotalDuration:  metadata.hasTotalDuration,
			HasLoadDuration:   metadata.hasLoadDuration,
			HasPromptCount:    metadata.hasPromptEvalCount,
			HasPromptDuration: metadata.hasPromptEvalDuration,
			HasEvalCount:      metadata.hasEvalCount,
			HasEvalDuration:   metadata.hasEvalDuration,
			MalformedResponse: metadata.malformed,
		})
	})
}

func (observed *observation) finish(outcome string, malformed bool) {
	observed.once.Do(func() {
		observed.sink.Emit(generationEvent{
			Operation:         observed.operation,
			Client:            observed.client,
			Outcome:           outcome,
			HTTPStatus:        observed.status,
			CompletedAt:       time.Now().UTC(),
			WallDuration:      time.Since(observed.started),
			MalformedResponse: malformed,
		})
	})
}

func sanitizeDimension(value string, limit int) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	var result strings.Builder
	for _, char := range value {
		if result.Len() >= limit {
			break
		}
		switch {
		case char >= 'a' && char <= 'z', char >= 'A' && char <= 'Z', char >= '0' && char <= '9':
			result.WriteRune(char)
		case strings.ContainsRune("._:/+-", char):
			result.WriteRune(char)
		default:
			result.WriteByte('_')
		}
	}
	return result.String()
}

type responseMetadata struct {
	model                 string
	doneReason            string
	done                  bool
	hasError              bool
	malformed             bool
	totalDuration         int64
	loadDuration          int64
	promptEvalCount       int64
	promptEvalDuration    int64
	evalCount             int64
	evalDuration          int64
	hasTotalDuration      bool
	hasLoadDuration       bool
	hasPromptEvalCount    bool
	hasPromptEvalDuration bool
	hasEvalCount          bool
	hasEvalDuration       bool
}

// metadataParser is a bounded top-level JSON tokenizer. It never retains an
// Ollama prompt, message, response, thinking block, image, embedding, tool
// payload, or unknown field. Only the short allowlist below is copied.
type metadataParser struct {
	depth         int
	inString      bool
	escaped       bool
	captureKey    bool
	captureValue  bool
	expectKey     bool
	expectValue   bool
	activeKey     string
	stringBuffer  []byte
	primitive     []byte
	current       responseMetadata
	sawObject     bool
	malformed     bool
	finalObserved bool
	onFinal       func(responseMetadata)
}

const maxMetadataToken = 256

func (parser *metadataParser) Feed(data []byte) {
	for _, char := range data {
		parser.feedByte(char)
	}
}

func (parser *metadataParser) End() {
	if parser.depth != 0 || parser.inString {
		parser.malformed = true
	}
}

func (parser *metadataParser) feedByte(char byte) {
	if parser.inString {
		if parser.escaped {
			parser.escaped = false
			if parser.captureKey || parser.captureValue {
				parser.appendString(char)
			}
			return
		}
		if char == '\\' {
			parser.escaped = true
			return
		}
		if char != '"' {
			if parser.captureKey || parser.captureValue {
				parser.appendString(char)
			}
			return
		}
		parser.inString = false
		value := string(parser.stringBuffer)
		parser.stringBuffer = parser.stringBuffer[:0]
		if parser.captureKey {
			parser.activeKey = value
			parser.captureKey = false
			parser.expectKey = false
			return
		}
		if parser.captureValue {
			parser.assignString(parser.activeKey, value)
			parser.captureValue = false
			parser.expectValue = false
			parser.activeKey = ""
		}
		return
	}

	switch char {
	case '{':
		if parser.depth == 0 {
			parser.current = responseMetadata{}
			parser.malformed = false
			parser.sawObject = true
		}
		parser.depth++
		if parser.depth == 1 {
			parser.expectKey = true
		} else if parser.depth == 2 && parser.expectValue {
			parser.expectValue = false
		}
	case '[':
		parser.depth++
		if parser.depth == 2 && parser.expectValue {
			parser.expectValue = false
		}
	case '}':
		if parser.depth == 1 {
			parser.finishPrimitive()
		}
		parser.depth--
		if parser.depth < 0 {
			parser.depth = 0
			parser.malformed = true
		}
		if parser.depth == 0 {
			parser.current.malformed = parser.current.malformed || parser.malformed
			if parser.current.done || parser.current.hasError {
				parser.finalObserved = true
				if parser.onFinal != nil {
					parser.onFinal(parser.current)
				}
			}
		}
	case ']':
		parser.depth--
		if parser.depth < 0 {
			parser.depth = 0
			parser.malformed = true
		}
	case '"':
		parser.inString = true
		parser.stringBuffer = parser.stringBuffer[:0]
		parser.captureKey = parser.depth == 1 && parser.expectKey
		parser.captureValue = parser.depth == 1 && parser.expectValue && isSafeStringKey(parser.activeKey)
		if parser.depth == 1 && parser.expectValue && parser.activeKey == "error" {
			parser.current.hasError = true
		}
	case ':':
		if parser.depth == 1 && parser.activeKey != "" {
			parser.expectValue = true
			parser.primitive = parser.primitive[:0]
		}
	case ',':
		if parser.depth == 1 {
			parser.finishPrimitive()
			parser.expectKey = true
			parser.expectValue = false
			parser.activeKey = ""
		}
	default:
		if parser.depth == 1 && parser.expectValue && char != ' ' && char != '\t' && char != '\r' && char != '\n' {
			if len(parser.primitive) < maxMetadataToken {
				parser.primitive = append(parser.primitive, char)
			} else {
				parser.malformed = true
			}
		}
	}
}

func (parser *metadataParser) appendString(char byte) {
	if len(parser.stringBuffer) < maxMetadataToken {
		parser.stringBuffer = append(parser.stringBuffer, char)
	} else {
		parser.malformed = true
	}
}

func isSafeStringKey(key string) bool {
	return key == "model" || key == "done_reason"
}

func (parser *metadataParser) assignString(key, value string) {
	switch key {
	case "model":
		parser.current.model = value
	case "done_reason":
		parser.current.doneReason = value
	case "error":
		parser.current.hasError = value != ""
	}
}

func (parser *metadataParser) finishPrimitive() {
	if !parser.expectValue || parser.activeKey == "" {
		parser.primitive = parser.primitive[:0]
		return
	}
	raw := strings.TrimSpace(string(parser.primitive))
	parser.primitive = parser.primitive[:0]
	parser.expectValue = false
	if raw == "" {
		return
	}
	switch parser.activeKey {
	case "done":
		value, err := strconv.ParseBool(raw)
		if err != nil {
			parser.malformed = true
			return
		}
		parser.current.done = value
	case "error":
		parser.current.hasError = raw != "null" && raw != "false"
	case "total_duration":
		parser.assignInt(raw, &parser.current.totalDuration, &parser.current.hasTotalDuration)
	case "load_duration":
		parser.assignInt(raw, &parser.current.loadDuration, &parser.current.hasLoadDuration)
	case "prompt_eval_count":
		parser.assignInt(raw, &parser.current.promptEvalCount, &parser.current.hasPromptEvalCount)
	case "prompt_eval_duration":
		parser.assignInt(raw, &parser.current.promptEvalDuration, &parser.current.hasPromptEvalDuration)
	case "eval_count":
		parser.assignInt(raw, &parser.current.evalCount, &parser.current.hasEvalCount)
	case "eval_duration":
		parser.assignInt(raw, &parser.current.evalDuration, &parser.current.hasEvalDuration)
	}
}

func (parser *metadataParser) assignInt(raw string, destination *int64, present *bool) {
	value, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || value < 0 {
		parser.malformed = true
		return
	}
	*destination = value
	*present = true
}
