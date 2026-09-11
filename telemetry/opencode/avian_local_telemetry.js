// Avian local OpenCode telemetry plugin v2.0.0.
//
// This plugin deliberately reads only allowlisted event metadata. It never
// reads chat.message parts, text/reasoning deltas, tool input/output, file
// content, environment values, command arguments, or raw error messages.

const SERVICE_NAME = "opencode"
const CLIENT_NAME = "opencode"
const PLUGIN_VERSION = "2.0.0"
const MAX_QUEUE = 256
const MAX_STATE = 4096

const DEFAULT_LOCAL_PROVIDERS = new Set(["ollama"])
const DEFAULT_REMOTE_PROVIDERS = new Set([
  "amazon-bedrock", "anthropic", "azure", "cerebras", "cloudflare-ai-gateway",
  "cohere", "deepinfra", "deepseek", "fireworks-ai", "github-copilot",
  "google", "google-vertex", "groq", "mistral", "openai", "opencode",
  "openrouter", "perplexity", "togetherai", "vercel", "xai",
])

function configuredSet(name, fallback) {
  const raw = process.env[name]
  if (typeof raw !== "string" || raw.trim() === "") return fallback
  return new Set(raw.split(",").map((value) => value.trim()).filter(Boolean))
}

function safeDimension(value, limit = 128) {
  if (typeof value !== "string") return undefined
  const cleaned = value.trim().replace(/[^A-Za-z0-9._:/+@-]/g, "_").slice(0, limit)
  return cleaned || undefined
}

function projectName(directory, project) {
  const candidate = typeof project?.name === "string" && project.name
    ? project.name
    : directory
  const basename = String(candidate ?? "").replace(/[\\/]+$/, "").split(/[\\/]/).pop()
  return safeDimension(basename, 64)
}

async function hashID(value) {
  if (typeof value !== "string" || value === "") return undefined
  const bytes = new TextEncoder().encode(`avian-local-ai:v1:${value}`)
  const digest = await crypto.subtle.digest("SHA-256", bytes)
  return Array.from(new Uint8Array(digest).slice(0, 12), (byte) => byte.toString(16).padStart(2, "0")).join("")
}

function locality(provider, localProviders, remoteProviders) {
  if (localProviders.has(provider)) return "local"
  if (remoteProviders.has(provider)) return "remote"
  return undefined
}

function boundedSetAdd(set, value) {
  if (set.has(value)) return false
  set.add(value)
  if (set.size > MAX_STATE) set.delete(set.values().next().value)
  return true
}

function boundedMapSet(map, key, value) {
  if (!map.has(key) && map.size >= MAX_STATE) map.delete(map.keys().next().value)
  map.set(key, value)
}

function attr(key, value) {
  if (value === undefined || value === null) return undefined
  let wrapped
  if (typeof value === "boolean") wrapped = { boolValue: value }
  else if (typeof value === "number" && Number.isSafeInteger(value)) wrapped = { intValue: String(value) }
  else if (typeof value === "number" && Number.isFinite(value)) wrapped = { doubleValue: value }
  else wrapped = { stringValue: String(value) }
  return { key, value: wrapped }
}

function attributes(record) {
  return Object.entries(record).map(([key, value]) => attr(key, value)).filter(Boolean)
}

function resource() {
  return { attributes: attributes({
    "service.name": SERVICE_NAME,
    "ai.client": CLIENT_NAME,
    "telemetry.sdk.name": "avian-local",
    "telemetry.sdk.version": PLUGIN_VERSION,
  }) }
}

function timestampNano(milliseconds = Date.now()) {
  return String(BigInt(Math.trunc(milliseconds)) * 1000000n)
}

function randomHex(bytes) {
  return Array.from(crypto.getRandomValues(new Uint8Array(bytes)), (byte) => byte.toString(16).padStart(2, "0")).join("")
}

function logPayload(name, message, values, severity = "INFO", at = Date.now()) {
  const timestamp = timestampNano(at)
  return { resourceLogs: [{
    resource: resource(),
    scopeLogs: [{
      scope: { name: "avian.opencode.telemetry", version: PLUGIN_VERSION },
      logRecords: [{
        timeUnixNano: timestamp,
        observedTimeUnixNano: timestampNano(),
        severityNumber: severity === "ERROR" ? 17 : 9,
        severityText: severity,
        body: { stringValue: message },
        attributes: attributes({ "event.name": name, "ai.client": CLIENT_NAME, ...values }),
      }],
    }],
  }] }
}

function sumMetric(name, description, unit, value, at, values) {
  const timestamp = timestampNano(at)
  return {
    name, description, unit,
    sum: {
      aggregationTemporality: 1,
      isMonotonic: true,
      dataPoints: [{
        attributes: attributes(values),
        asInt: String(Math.trunc(value)),
        timeUnixNano: timestamp,
        startTimeUnixNano: timestamp,
      }],
    },
  }
}

function histogramMetric(name, description, unit, value, at, values) {
  const bounds = [100, 500, 1000, 5000, 10000, 30000, 60000, 300000]
  const bucket = bounds.findIndex((bound) => value <= bound)
  const index = bucket === -1 ? bounds.length : bucket
  const counts = Array(bounds.length + 1).fill("0")
  counts[index] = "1"
  const timestamp = timestampNano(at)
  return {
    name, description, unit,
    histogram: {
      aggregationTemporality: 1,
      dataPoints: [{
        attributes: attributes(values), count: "1", sum: value,
        explicitBounds: bounds, bucketCounts: counts,
        timeUnixNano: timestamp, startTimeUnixNano: timestamp,
      }],
    },
  }
}

function metricPayload(metrics) {
  return { resourceMetrics: [{
    resource: resource(),
    scopeMetrics: [{ scope: { name: "avian.opencode.telemetry", version: PLUGIN_VERSION }, metrics }],
  }] }
}

function tracePayload(name, start, end, values, outcome) {
  return { resourceSpans: [{
    resource: resource(),
    scopeSpans: [{
      scope: { name: "avian.opencode.telemetry", version: PLUGIN_VERSION },
      spans: [{
        traceId: randomHex(16), spanId: randomHex(8), name, kind: 3,
        startTimeUnixNano: timestampNano(start), endTimeUnixNano: timestampNano(end),
        attributes: attributes({ "ai.client": CLIENT_NAME, ...values }),
        status: { code: outcome === "success" ? 1 : 2 },
      }],
    }],
  }] }
}

function operationMetricAttributes(values) {
  return {
    "ai.client": CLIENT_NAME,
    "ai.operation": values["ai.operation"],
    "ai.model": values["ai.model"],
    "ai.provider": values["ai.provider"],
    "ai.inference.locality": values["ai.inference.locality"],
    "ai.outcome": values["ai.outcome"],
    "ai.telemetry.layer": "client",
    "ai.usage.authority": values["ai.usage.authority"],
    "avian.synthetic": false,
  }
}

function createTransport(endpoint, appLog) {
  let target
  try {
    target = new URL(endpoint)
  } catch {
    throw new Error("AVIAN_OPENCODE_OTLP_HTTP_ENDPOINT must be a URL")
  }
  if (target.protocol !== "http:" || !["127.0.0.1", "[::1]", "::1"].includes(target.hostname)) {
    throw new Error("OpenCode telemetry endpoint must be loopback HTTP")
  }
  const base = endpoint.replace(/\/$/, "")
  let pending = Promise.resolve()
  let queued = 0
  let dropped = 0

  async function post(signal, payload) {
    const response = await fetch(`${base}/v1/${signal}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
    if (!response.ok) throw new Error(`collector returned HTTP ${response.status}`)
  }

  function enqueue(signal, payload) {
    if (queued >= MAX_QUEUE) {
      dropped += 1
      return
    }
    queued += 1
    pending = pending.then(() => post(signal, payload)).catch(async (error) => {
      await appLog("warn", "local OTLP export failed", { signal, error_type: error?.name ?? "Error" })
    }).finally(() => { queued -= 1 })
  }

  return {
    enqueue,
    async flush() {
      await pending
      if (dropped > 0) {
        await appLog("warn", "local OTLP queue dropped records", { dropped })
        dropped = 0
      }
    },
  }
}

export const AvianLocalTelemetry = async ({ project, client, directory }) => {
  const endpoint = process.env.AVIAN_OPENCODE_OTLP_HTTP_ENDPOINT ?? "http://127.0.0.1:4318"
  const localProviders = configuredSet("AVIAN_OPENCODE_LOCAL_PROVIDERS", DEFAULT_LOCAL_PROVIDERS)
  const remoteProviders = configuredSet("AVIAN_OPENCODE_REMOTE_PROVIDERS", DEFAULT_REMOTE_PROVIDERS)
  const projectValue = projectName(directory, project)
  const appLog = async (level, message, extra = {}) => {
    await client.app.log({ body: { service: "avian-local-telemetry", level, message, extra } })
  }
  const transport = createTransport(endpoint, appLog)
  const completedMessages = new Set()
  const completedTools = new Set()
  const observedRetries = new Set()
  const sessions = new Map()
  const turnStats = new Map()
  const retryCounts = new Map()

  await appLog("info", "privacy-safe local telemetry enabled", {
    plugin_version: PLUGIN_VERSION,
    endpoint,
    content_capture: false,
  })

  async function emitSession(name, sessionID, values = {}, at = Date.now()) {
    const sessionHash = await hashID(sessionID)
    transport.enqueue("logs", logPayload(name, "OpenCode session lifecycle event", {
      "ai.session.id_hash": sessionHash,
      "ai.project.name": projectValue,
      "avian.synthetic": false,
      ...values,
    }, values["ai.outcome"] === "failure" ? "ERROR" : "INFO", at))
  }

  async function handleEvent(event) {
    switch (event.type) {
      case "session.created": {
        const info = event.properties?.info
        if (!info?.id) return
        boundedMapSet(sessions, info.id, { started: info.time?.created ?? Date.now() })
        const sessionHash = await hashID(info.id)
        const values = {
          "ai.client": CLIENT_NAME,
          "ai.project.name": projectValue,
          "ai.session.id_hash": sessionHash,
          "avian.synthetic": false,
        }
        transport.enqueue("logs", logPayload("opencode.session.created", "OpenCode session created", values, "INFO", info.time?.created))
        transport.enqueue("metrics", metricPayload([
          sumMetric("avian.ai.sessions", "Authentic AI client sessions", "{session}", 1, info.time?.created ?? Date.now(), {
            "ai.client": CLIENT_NAME, "avian.synthetic": false,
          }),
        ]))
        await transport.flush()
        break
      }
      case "session.idle": {
        const sessionID = event.properties?.sessionID
        if (!sessionID) return
        const state = sessions.get(sessionID)
        const duration = state?.started ? Date.now() - state.started : undefined
        await emitSession("opencode.session.idle", sessionID, {
          "ai.session.duration_ms": duration,
        })
        await transport.flush()
        break
      }
      case "session.compacted": {
        const sessionID = event.properties?.sessionID
        if (!sessionID) return
        await emitSession("opencode.session.compacted", sessionID)
        transport.enqueue("metrics", metricPayload([
          sumMetric("avian.ai.compactions", "OpenCode compaction events", "{compaction}", 1, Date.now(), {
            "ai.client": CLIENT_NAME, "avian.synthetic": false,
          }),
        ]))
        await transport.flush()
        break
      }
      case "session.error": {
        const sessionID = event.properties?.sessionID
        const errorName = safeDimension(event.properties?.error?.name, 64)
        const outcome = errorName === "MessageAbortedError" ? "cancellation" : "failure"
        await emitSession("opencode.session.error", sessionID, {
          "ai.outcome": outcome,
          "opencode.error.type": errorName,
        })
        await transport.flush()
        break
      }
      case "session.status": {
        const sessionID = event.properties?.sessionID
        const status = event.properties?.status
        if (!sessionID || status?.type !== "retry") return
        const attempt = Number.isSafeInteger(status.attempt) ? status.attempt : 0
        const retryKey = `${sessionID}:${attempt}`
        if (!boundedSetAdd(observedRetries, retryKey)) return
        retryCounts.set(sessionID, (retryCounts.get(sessionID) ?? 0) + 1)
        const sessionHash = await hashID(sessionID)
        transport.enqueue("logs", logPayload("opencode.retry", "OpenCode provider retry", {
          "ai.client": CLIENT_NAME,
          "ai.session.id_hash": sessionHash,
          "ai.project.name": projectValue,
          "opencode.retry.attempt": attempt,
          "avian.synthetic": false,
        }))
        transport.enqueue("metrics", metricPayload([
          sumMetric("avian.ai.retries", "AI provider retries", "{retry}", 1, Date.now(), {
            "ai.client": CLIENT_NAME, "avian.synthetic": false,
          }),
        ]))
        await transport.flush()
        break
      }
      case "message.part.updated": {
        const part = event.properties?.part
        if (part?.type !== "tool") return
        const state = part.state?.status
        if (state !== "completed" && state !== "error") return
        const callKey = `${part.sessionID}:${part.callID}`
        if (!boundedSetAdd(completedTools, callKey)) return
        const outcome = state === "completed" ? "success" : "failure"
        const toolName = safeDimension(part.tool, 64) ?? "unknown"
        const statsKey = `${part.sessionID}:${part.messageID}`
        const stats = turnStats.get(statsKey) ?? { tools: 0, toolFailures: 0 }
        stats.tools += 1
        if (outcome === "failure") stats.toolFailures += 1
        boundedMapSet(turnStats, statsKey, stats)
        const sessionHash = await hashID(part.sessionID)
        const start = Number.isFinite(part.state?.time?.start) ? part.state.time.start : Date.now()
        const end = Number.isFinite(part.state?.time?.end) ? part.state.time.end : Date.now()
        const values = {
          "ai.client": CLIENT_NAME,
          "ai.session.id_hash": sessionHash,
          "ai.project.name": projectValue,
          "ai.outcome": outcome,
          "opencode.tool.name": toolName,
          "opencode.tool.duration_ms": Math.max(0, end - start),
          "avian.synthetic": false,
        }
        transport.enqueue("logs", logPayload("opencode.tool.completed", "OpenCode tool execution completed", values, outcome === "success" ? "INFO" : "ERROR", end))
        transport.enqueue("metrics", metricPayload([
          sumMetric("avian.ai.tool.calls", "OpenCode tool executions", "{tool_call}", 1, end, {
            "ai.client": CLIENT_NAME, "tool.name": toolName, "ai.outcome": outcome, "avian.synthetic": false,
          }),
        ]))
        transport.enqueue("traces", tracePayload(`opencode.tool.${toolName}`, start, end, values, outcome))
        await transport.flush()
        break
      }
      case "message.updated": {
        const info = event.properties?.info
        if (info?.role !== "assistant" || !Number.isFinite(info.time?.completed)) return
        if (!boundedSetAdd(completedMessages, info.id)) return
        const completed = info.time.completed
        const created = Number.isFinite(info.time.created) ? info.time.created : completed
        const outcome = info.error?.name === "MessageAbortedError" ? "cancellation" : info.error ? "failure" : "success"
        const provider = safeDimension(info.providerID, 96)
        const model = safeDimension(info.modelID, 128)
        const inferredLocality = locality(provider, localProviders, remoteProviders)
        const sessionHash = await hashID(info.sessionID)
        const statsKey = `${info.sessionID}:${info.id}`
        const stats = turnStats.get(statsKey) ?? { tools: 0, toolFailures: 0 }
        turnStats.delete(statsKey)
        const retries = retryCounts.get(info.sessionID) ?? 0
        retryCounts.set(info.sessionID, 0)
        const operation = info.summary ? "summary" : "turn"
        const token = info.tokens ?? {}
        const values = {
          // Ollama requests are counted by the shared gateway. Unknown providers
          // remain client observations until locality is explicitly configured.
          "ai.event.name": inferredLocality === "remote" ? "ai.inference.completed" : "ai.client.operation.completed",
          "ai.telemetry.layer": "client",
          "ai.usage.authority": inferredLocality === "remote",
          "ai.client": CLIENT_NAME,
          "ai.operation": operation,
          "ai.model": model,
          "ai.provider": provider,
          "ai.inference.locality": inferredLocality,
          "ai.session.id_hash": sessionHash,
          "ai.project.name": projectValue,
          "ai.outcome": outcome,
          "ai.tokens.input": Number.isSafeInteger(token.input) ? token.input : undefined,
          "ai.tokens.output": Number.isSafeInteger(token.output) ? token.output : undefined,
          "ai.tokens.reasoning": Number.isSafeInteger(token.reasoning) ? token.reasoning : undefined,
          "ai.tokens.cached": Number.isSafeInteger(token.cache?.read) ? token.cache.read : undefined,
          "opencode.tokens.cache_write": Number.isSafeInteger(token.cache?.write) ? token.cache.write : undefined,
          "ai.duration.total_ms": Math.max(0, completed - created),
          "ai.tool_calls.count": stats.tools,
          "opencode.tool_failures.count": stats.toolFailures,
          "ai.retry.count": retries,
          "ai.finish_reason": safeDimension(info.finish, 64),
          "ai.completion.timestamp": new Date(completed).toISOString(),
          "opencode.error.type": safeDimension(info.error?.name, 64),
          "avian.synthetic": false,
        }
        transport.enqueue("logs", logPayload("opencode.operation.completed", "OpenCode operation completed", values, outcome === "success" ? "INFO" : "ERROR", completed))
        const metricAttrs = operationMetricAttributes(values)
        const metrics = [
          sumMetric("avian.ai.client.operations", "Completed authentic AI operations", "{operation}", 1, completed, metricAttrs),
          histogramMetric("avian.ai.client.duration", "AI operation duration", "ms", values["ai.duration.total_ms"], completed, { ...metricAttrs, "duration.phase": "total" }),
        ]
        for (const [type, count] of [
          ["input", values["ai.tokens.input"]],
          ["output", values["ai.tokens.output"]],
          ["reasoning", values["ai.tokens.reasoning"]],
          ["cached", values["ai.tokens.cached"]],
          ["cache_write", values["opencode.tokens.cache_write"]],
        ]) {
          if (Number.isSafeInteger(count) && count > 0) {
            metrics.push(sumMetric("avian.ai.client.tokens", "AI tokens reported by the source", "{token}", count, completed, { ...metricAttrs, "token.type": type }))
          }
        }
        // The authoritative series includes only verified remote client usage.
        // Local requests are observed independently by the Ollama gateway.
        if (values["ai.usage.authority"]) {
          metrics.push(...metrics.map((metric) => ({
            ...metric,
            name: metric.name.replace("avian.ai.client.operations", "avian.ai.inferences").replace("avian.ai.client.", "avian.ai."),
          })))
        }
        transport.enqueue("metrics", metricPayload(metrics))
        transport.enqueue("traces", tracePayload(`opencode.${operation}`, created, completed, values, outcome))
        // Short-lived `opencode run` processes may exit immediately after this
        // event. Wait for the localhost exporter so the final trace is not lost.
        await transport.flush()
        break
      }
    }
  }

  return {
    event: async ({ event }) => {
      try {
        await handleEvent(event)
      } catch (error) {
        await appLog("warn", "local telemetry event handling failed", { event_type: safeDimension(event?.type, 64), error_type: error?.name ?? "Error" })
      }
    },
    dispose: async () => { await transport.flush() },
  }
}
