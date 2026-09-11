# Local AI Telemetry Workbench

This opt-in local workbench compares Codex, OpenCode, and Jeffser Alpaca
activity without adding product, application, GCP, staging, production, or
cloud telemetry. The pinned `grafana/otel-lgtm` container provides an
OpenTelemetry Collector, Prometheus, Loki, Tempo, and Grafana. A separate
repository-built Go process provides the localhost-only shared Ollama gateway.

The original integration baseline used Codex 0.149.1, OpenCode 1.18.23,
Alpaca 9.2.5 from Flathub, and Ollama 0.32.15. The relevant upstream references
are the [Codex configuration reference](https://developers.openai.com/codex/config-reference),
the [OpenCode plugin documentation](https://opencode.ai/docs/plugins/), the
[Ollama API reference](https://docs.ollama.com/api/introduction), and the
[Alpaca source repository](https://github.com/Jeffser/Alpaca).

## Architecture and authority

```text
Alpaca / OpenCode / CLI / other local clients
    └─ HTTP → shared gateway 127.0.0.1:11435 → Ollama 127.0.0.1:11434
                   └─ inference metadata ──┐
OpenCode plugin ── client metadata ────────┼─ OTLP → collector → Loki/Prometheus/Tempo → Grafana
Codex native ───── client metadata ────────┘
```

The gateway owns Ollama inference counts, tokens, latency, and failures.
OpenCode's plugin retains sessions, tools, retries, compactions, and completion
metadata for every provider. Only its explicitly classified **remote** provider
completions enter common inference totals. Local and unknown-provider client
observations remain in the OpenCode panels, with `ai.usage.authority=false`.
Unknown providers can be classified using the documented environment allowlists;
all providers pointed at this gateway must be classified local.

Common inference panels require `ai.event.name=ai.inference.completed` and
`ai.usage.authority=true`. Gateway and remote OpenCode inference metrics use
`avian.ai.inferences`, `avian.ai.tokens`, and `avian.ai.duration`; OpenCode's
separate diagnostic metrics use `avian.ai.client.*`. Never add diagnostic client
tokens to authoritative inference tokens. A local operation observed by both
sources contributes one gateway record, even if the client supplies usage too.
Gateway counts represent HTTP inference attempts, including failures and retries;
remote client counts represent completed assistant operations, not wire attempts.

Codex native sessions/tools/cloud usage remain in their dedicated panels. Its
existing completion metadata does not establish provider locality, so Codex is
kept out of common inference totals rather than risking duplicate local usage.
This is source-based accounting, not heuristic matching by model or timestamp.

Every Ollama client must use the gateway. Direct traffic to 11434 is invisible.
The gateway uses `ai.client=unknown` unless an allowlisted URL prefix or
`X-Avian-Client` header identifies the caller. Labels are diagnostic claims, not
authentication. They are never inferred from model names or request content.
The locality label assumes the upstream serves local models; routing Ollama
cloud models through this local gateway is outside this configuration.

The existing Compose project and persistent volume retain their historical
names, `avian-codex-telemetry` and `avian-codex-telemetry-data`, so upgrading
the workbench preserves already collected Codex data.

## Start, configure, and inspect

Prerequisites are Docker Engine, Docker Compose v2, Python 3.11+, Go 1.27.1, and
Task v3 (validated with 3.52.0). Source checks additionally use Node (or the
installed OpenCode Bun runtime via the `JS_RUNTIME` override).

```sh
task telemetry:opencode:setup
task telemetry:up
task telemetry:doctor
task telemetry:open
```

`telemetry:up` starts the LGTM stack and builds/starts the Ollama gateway. It is
idempotent and waits at most 120 seconds by default. `telemetry:doctor` checks
each client path independently, sends explicitly synthetic metric/log/trace
transport probes, waits for all three raw backends, reports authentic activity
or healthy-idle status for each client, and executes every Grafana panel query.

The dashboard URL is normally
`http://127.0.0.1:3000/d/avian-codex-workbench/avian-local-ai-workbench`.
The stable UID intentionally preserves existing bookmarks.

### Codex

Print the required user-level configuration without modifying it:

```sh
task telemetry:config
```

Merge the printed `[otel]` block into `~/.codex/config.toml` (or the explicit
`$CODEX_HOME/config.toml`). It routes logs, metrics, and traces to loopback OTLP
and keeps `log_user_prompt = false`. Project-scoped Codex configuration cannot
set telemetry routing. Restart Codex after changing the block.

### OpenCode

The repository owns `telemetry/opencode/avian_local_telemetry.js` v2.0.0. It
uses OpenCode's supported plugin/event API and `fetch` OTLP export, has no
external runtime dependency, and is loaded by OpenCode's embedded Bun runtime.
No system-wide npm or Bun install is needed.

```sh
task telemetry:opencode:config
task telemetry:opencode:setup
task telemetry:opencode:check
```

Setup copies the exact versioned file to OpenCode's user plugin directory. It
is idempotent, backs up a pre-existing file at that exact target before
replacement, and does not edit `opencode.jsonc`, providers, models,
credentials, or unrelated plugins. New OpenCode processes discover the plugin
automatically. Restart an already-running process after setup. Optional
loopback endpoint and provider-locality overrides are documented in
`telemetry/opencode/opencode.telemetry.env.example`.

To disable the integration, stop OpenCode and move only
`~/.config/opencode/plugins/avian_local_telemetry.js` out of the plugin
directory, then restart OpenCode. Restore its timestamped backup to roll back
a replaced older copy. OpenCode usage already in the local volume remains
available until an explicitly confirmed telemetry reset.

The maintained third-party candidate `@devtheops/opencode-plugin-otel` 1.5.1
(MPL-2.0, tag/commit
`a051cdac72d2c4eac1f7fc49dab6d309d5f4c500`) was inspected but not selected.
Its traces always contain full prompts, responses, tool arguments, and tool
outputs, and it labels Prometheus metrics with raw session IDs. Disabling those
traces would also lose required trace coverage, and the plugin does not emit
compaction events. The local plugin is the smaller privacy-preserving option.

### Shared Ollama gateway and client routing

```sh
task telemetry:ollama-gateway:up
task telemetry:ollama-gateway:check
```

All clients share port 11435. These optional prefixes attach an allowlisted
client label and are stripped before forwarding to Ollama:

| Client | Base URL / routing |
| --- | --- |
| Alpaca **Ollama External** instance | `http://127.0.0.1:11435/clients/alpaca` |
| OpenCode Ollama provider `options.baseURL` | `http://127.0.0.1:11435/clients/opencode/v1` |
| Other OpenAI-compatible client | `http://127.0.0.1:11435/v1` |
| Native Ollama client | `http://127.0.0.1:11435` |
| Ollama CLI | `OLLAMA_HOST=http://127.0.0.1:11435 ollama run <model>` |

Clients that support headers can send `X-Avian-Client: alpaca`, `opencode`,
`codex`, or `cli` instead. Unknown header values produce `unknown`; invalid
`/clients/` prefixes are rejected. A prefix takes precedence over the header.
The gateway removes the attribution header before forwarding. The CLI example
is intentionally unattributed because the CLI supplies no client header.

In Alpaca, select the new external instance for the conversation. Keep the
managed/direct instance for rollback; models and chats need not be deleted.
Restart Alpaca if the new instance does not appear. In OpenCode, merge the
following **options** into the existing Ollama provider, preserving its models
and other options, then restart OpenCode after updating the telemetry plugin:

```json
{
  "provider": {
    "ollama": {
      "options": { "baseURL": "http://127.0.0.1:11435/clients/opencode/v1" }
    }
  }
}
```

This follows [OpenCode provider base URL configuration](https://opencode.ai/docs/providers/)
and [Ollama's OpenAI-compatible API](https://docs.ollama.com/api/openai-compatibility).
The gateway does not change client configuration or move the Ollama daemon.

| Route | Observation |
| --- | --- |
| `POST /api/chat`, `/api/generate` | Native JSON/NDJSON completion metadata, tokens and timing |
| `POST /v1/chat/completions`, `/v1/completions` | JSON/SSE completion metadata and supplied usage |
| `POST /v1/responses` | JSON/SSE response completion and supplied usage |
| Model management, embeddings, version, other routes | Transparent pass-through, no inference event |
| `GET /healthz` | Gateway readiness only |

Both API formats preserve bytes, streaming, cancellation, status and headers.
Compatible streaming usage can arrive after `finish_reason`; the gateway waits
for EOF and exports once. Clients must request `stream_options.include_usage`
when needed for compatible streaming token counts. Missing counts stay absent;
they are never estimated from text. Compatible responses lack native Ollama
phase durations, so those charts only include native API data. Truncated or
cancelled streams count as unsuccessful attempts, not successful generations.

Upgrading keeps the existing local PID/binary/log filenames so `up` can stop the
previous owned proxy safely. A source/config fingerprint rebuilds stale gateway
code. The existing volume is preserved; historical Alpaca proxy data is not
rewritten into the new authoritative totals.

## Normalized schema

Source event names and tool-specific attributes are retained where safe. The
documented common layer adds these fields only when the source supplies them:

| Field | Meaning / cardinality rule |
| --- | --- |
| `service.name` | `codex_cli_rs`, `opencode`, or `ollama-gateway` |
| `ai.event.name` | `ai.inference.completed` for authoritative inference; `ai.client.operation.completed` for diagnostic client completions |
| `ai.telemetry.layer` | `inference` or `client` |
| `ai.usage.authority` | Whether this observation contributes to common inference totals |
| `ai.client` | `codex`, `opencode`, `alpaca`, `cli`, or `unknown` |
| `ai.operation` | turn, summary, chat, or generate |
| `ai.model`, `ai.provider` | bounded source identifiers; provider is absent for Codex when not emitted |
| `ai.inference.locality` | local/remote only for known or explicitly configured providers |
| `ai.session.id_hash` | truncated SHA-256 for OpenCode; absent for the Ollama gateway |
| `ai.project.name` | sanitized repository basename for OpenCode; no raw path |
| `ai.outcome` | success, failure, or cancellation |
| `ai.tokens.*` | input, output, cached, and reasoning values directly emitted by the source |
| `ai.duration.*` | total/load/prompt-evaluation/generation milliseconds when emitted |
| `ai.throughput.output_tokens_per_second` | output count divided by valid Ollama generation duration |
| `ai.tool_calls.count`, `ai.retry.count` | observed OpenCode counts |
| `ai.completion.timestamp` | source completion time or proxy observation time |
| `avian.synthetic` | true only for explicit probes; authentic events are false or source-selected |

Session hashes and project names remain Loki/Tempo metadata and never become
Prometheus labels. Common Prometheus labels are limited to client, operation,
model, provider, locality, outcome, token type, duration phase, and bounded tool
name. Dashboard model/provider/project filters are regex text fields so these
values do not need to become indexed Loki labels.

Missing values stay missing. Cached tokens are not added to input tokens;
reasoning tokens are not inferred from ordinary output. Local inference is
marked `local/unpriced`; no `$0` or cross-provider cost chart is produced.

## Privacy guarantees

The OpenCode plugin never reads prompt parts, response/reasoning deltas, tool
input/output, file content, environment values, command arguments, or raw error
messages. It reads only session lifecycle metadata, assistant completion
metadata, tool name/status/timing, retry attempt number, and compaction events.

The proxy forwards request bodies as opaque streams; its instrumentation never
reads, copies, or buffers them. Its response observer is a bounded top-level
streaming state machine with a 256-byte metadata-token cap. It copies only
model, done reason, duration, and token-count fields. Prompt, message, response,
thinking, image, embedding, system, and tool fields continue through the normal
client/upstream path but are discarded by the observer without storage or replay.

The collector remains a second boundary. It drops known prompt/tool-result log
events, deletes common content/error/path/environment keys from logs and spans,
and removes raw session, conversation, project, file, and path fields from
metric datapoints before Prometheus storage. No debug exporter, host receiver,
filesystem receiver, Docker receiver, cloud exporter, or non-loopback published
port is enabled.

Operational metadata such as model names, bounded tool names, timings, outcomes,
hashed OpenCode session IDs, and sanitized repository names remains sensitive
development metadata. Do not share the volume or screenshots casually.

## Supported and unavailable measurements

OpenCode supplies sessions, completed assistant operations, provider/model,
token categories, overall latency, tool executions/failures, retries,
compactions, errors, cancellations, and hashed session/project correlation. It
does not expose a provider-independent generation-only duration, so OpenCode
tokens/second is absent.

Ollama supplies model, done reason, total/load/prompt-evaluation/generation
durations, prompt/output token counts, and errors on completed responses. It
does not supply an Alpaca conversation ID, repository, retry count, reliable
tool-call count, or separate reasoning-token count. Alpaca also uses
non-streaming `/api/chat` for background title generation; distinguishing that
from a visible response would require inspecting forbidden content, so both are
honestly reported as chat generations.

Codex panels continue to use native namespaced telemetry. The normalized
collector layer adds common client/model/token/outcome fields to completed Codex
logs without altering the original event name or existing queries.

## Diagnostics, lifecycle, and probes

```sh
task telemetry:status
task telemetry:check
task telemetry:doctor
task telemetry:probe CLIENT=all
task telemetry:logs
task telemetry:down
task telemetry:up
task telemetry:reset CONFIRM=1
```

`doctor` reports **FAILED** for unavailable infrastructure, proxy/upstream,
datasources, transport storage, collector ingestion, or invalid dashboard
queries; **DEGRADED** for missing Codex/OpenCode client configuration; **IDLE**
when transport is healthy but one or more sources have no authentic
completion in the last 30 minutes (check routing as well as activity); and **HEALTHY** after authentic recent data
from Codex, OpenCode, and the gateway.

`probe` accepts `codex`, `opencode`, `ollama`, or `all`. Every probe uses
`service.name=avian-telemetry-probe` and `avian.synthetic=true`; it never emits
the authentic common operation metric or service names. All usage dashboards
select authentic services/events and therefore exclude probes.

`down` stops the proxy and only the repository Compose project while preserving
the named volume. `reset` refuses without `CONFIRM=1`, verifies Compose volume
ownership, and deletes only that volume. The deletion cannot be recovered.

## Timing and troubleshooting

Logs and traces normally appear within a few seconds. The native API exports
after the final `done: true` object; compatible APIs export at EOF after
collecting any final usage chunk. OpenCode flushes lifecycle and completion
telemetry to loopback before returning from the relevant plugin event. Codex
metrics may take about 60 seconds during a long-running process,
while short `codex exec` processes flush on exit. The dashboard refreshes every
15 seconds and defaults to six hours.

If one client is idle, generate a completed operation in that client rather than
treating the idle tile as a failure. For integration failures:

- run `task telemetry:opencode:check` and start a new OpenCode process;
- run `task telemetry:ollama-gateway:check`, verify Ollama on 11434, and confirm
  each local client selected its gateway URL on 11435;
- run `task telemetry:config` and restart Codex for Codex routing;
- run `task telemetry:logs` for collector/proxy operational logs;
- run `task telemetry:probe CLIENT=all` to separate transport from authentic
  application inactivity.

Override ports consistently when needed:

```sh
task telemetry:up GRAFANA_PORT=3300 OTLP_GRPC_PORT=5317 OTLP_HTTP_PORT=5318 OLLAMA_GATEWAY_PORT=12435
AVIAN_OPENCODE_OTLP_HTTP_ENDPOINT=http://127.0.0.1:5318 opencode
```

The OpenCode plugin endpoint environment must match a non-default OTLP HTTP
port. Configure Alpaca's external URL with the matching proxy port. Use
`TELEMETRY_TIMEOUT=180` for a slow first start.

## Adding another local AI client

Point its Ollama base URL at the shared gateway. Without an attribution label,
usage appears under `unknown` and still counts once. Extend `knownClient` and
the dashboard client selector when a new named client is needed. Keep any
client event integration separate from gateway inference totals. Preserve the
content allowlist and bounded parser, keep session/project values out of metric
labels, and leave unsupported measurements absent.

## Source validation and local activation

```sh
task telemetry:check
# Alternative when OpenCode is installed but standalone Node is not:
task telemetry:check JS_RUNTIME='env BUN_BE_BUN=1 opencode test'
task check
```

These tests use fake upstreams and fake exporters, including duplicate client
updates, native/compatible streams, late usage, and bounded content handling.
They do not generate model conversations. Activate with `task telemetry:up`,
install the updated client plugin with `task telemetry:opencode:setup`, then
restart OpenCode and select the gateway URLs above. `doctor` verifies storage
and dashboard queries; an actual completed client conversation is required to
verify that client's routing. No data reset or cloud change is required.
