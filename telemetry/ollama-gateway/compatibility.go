package main

import "strings"

// compatibilityParser observes JSON/SSE without buffering response bodies or
// content strings. Only bounded keys and allowlisted metadata values survive.
// It handles chat/completions usage chunks after finish_reason and Responses
// response.completed envelopes. Telemetry is emitted once, at EOF.
type compatibilityParser struct {
	frames                                          []metadataFrame
	inString, escaped, keyString                    bool
	capture                                         bool
	token                                           []byte
	primitive                                       bool
	outside                                         []byte
	streaming, doneMarker, responseComplete, broken bool
	metadata                                        responseMetadata
}

type metadataFrame struct {
	path              string
	key               string
	object, expectKey bool
}

func (p *compatibilityParser) Feed(data []byte) {
	for _, c := range data {
		p.feed(c)
	}
}

func (p *compatibilityParser) path() string {
	if len(p.frames) == 0 {
		return ""
	}
	f := p.frames[len(p.frames)-1]
	if !f.object {
		return f.path + "[]"
	}
	if f.path == "" {
		return f.key
	}
	return f.path + "." + f.key
}

func safeMetadataPath(path string) bool {
	switch path {
	case "model", "response.model", "type", "status", "response.status",
		"choices[].finish_reason", "usage.prompt_tokens", "usage.completion_tokens",
		"usage.input_tokens", "usage.output_tokens", "response.usage.input_tokens",
		"response.usage.output_tokens", "error", "response.error":
		return true
	}
	return false
}

func (p *compatibilityParser) append(c byte) {
	if len(p.token) < maxMetadataToken {
		p.token = append(p.token, c)
	} else if !p.keyString {
		p.broken = true
	}
}

func (p *compatibilityParser) feed(c byte) {
	if p.broken {
		return
	}
	if p.inString {
		if p.escaped {
			p.escaped = false
			if p.capture {
				p.append(c)
			}
			return
		}
		if c == '\\' {
			p.escaped = true
			return
		}
		if c != '"' {
			if p.capture {
				p.append(c)
			}
			return
		}
		p.inString = false
		if p.keyString {
			f := &p.frames[len(p.frames)-1]
			f.key, f.expectKey = string(p.token), false
		} else if p.capture {
			p.assign(string(p.token))
		}
		p.token = p.token[:0]
		return
	}
	if p.primitive {
		if !strings.ContainsRune(",}] \r\n\t", rune(c)) {
			p.append(c)
			return
		}
		p.assign(string(p.token))
		p.token = p.token[:0]
		p.primitive = false
	}
	if len(p.frames) == 0 && c != '{' {
		if c == '\n' {
			p.observeFraming()
			p.outside = p.outside[:0]
		} else if len(p.outside) < 64 {
			p.outside = append(p.outside, c)
		}
		return
	}
	switch c {
	case '{', '[':
		path := p.path()
		if path == "error" || path == "response.error" {
			p.metadata.hasError = true
		}
		if len(p.frames) >= 64 {
			p.broken = true
			return
		}
		p.frames = append(p.frames, metadataFrame{path: path, object: c == '{', expectKey: c == '{'})
		p.outside = p.outside[:0]
	case '}', ']':
		if len(p.frames) == 0 {
			p.broken = true
			return
		}
		f := p.frames[len(p.frames)-1]
		if f.object != (c == '}') {
			p.broken = true
			return
		}
		p.frames = p.frames[:len(p.frames)-1]
	case ',':
		f := &p.frames[len(p.frames)-1]
		f.expectKey = f.object
	case '"':
		f := p.frames[len(p.frames)-1]
		p.inString, p.keyString = true, f.object && f.expectKey
		p.capture = p.keyString || safeMetadataPath(p.path())
		if !p.keyString && (p.path() == "error" || p.path() == "response.error") {
			p.metadata.hasError = true
			p.capture = false
		}
		p.token = p.token[:0]
	case ':', ' ', '\t', '\r', '\n':
	default:
		if safeMetadataPath(p.path()) {
			p.primitive = true
			p.token = p.token[:0]
			p.append(c)
		}
	}
}

func (p *compatibilityParser) observeFraming() {
	if strings.TrimSpace(string(p.outside)) == "data: [DONE]" {
		p.doneMarker = true
	}
}

func (p *compatibilityParser) assign(value string) {
	switch p.path() {
	case "model", "response.model":
		p.metadata.model = value
	case "choices[].finish_reason":
		if value != "null" && value != "" {
			p.metadata.doneReason = value
			p.metadata.done = true
		}
	case "type":
		if value == "response.completed" {
			p.responseComplete = true
			p.metadata.done = true
		}
		if value == "response.failed" || value == "error" {
			p.metadata.hasError = true
		}
	case "status", "response.status":
		if value == "completed" {
			p.responseComplete = true
			p.metadata.done = true
		}
		if value == "failed" || value == "incomplete" {
			p.metadata.hasError = true
		}
	case "error", "response.error":
		p.metadata.hasError = value != "null"
	case "usage.prompt_tokens", "usage.input_tokens", "response.usage.input_tokens":
		parser := metadataParser{}
		parser.assignInt(value, &p.metadata.promptEvalCount, &p.metadata.hasPromptEvalCount)
		p.metadata.malformed = p.metadata.malformed || parser.malformed
	case "usage.completion_tokens", "usage.output_tokens", "response.usage.output_tokens":
		parser := metadataParser{}
		parser.assignInt(value, &p.metadata.evalCount, &p.metadata.hasEvalCount)
		p.metadata.malformed = p.metadata.malformed || parser.malformed
	}
}

func (p *compatibilityParser) End() responseMetadata {
	p.observeFraming()
	p.metadata.malformed = p.metadata.malformed || p.broken || len(p.frames) != 0 || p.inString
	if p.streaming && !p.doneMarker && !p.responseComplete && !p.metadata.hasError {
		p.metadata.malformed = true
	}
	return p.metadata
}
