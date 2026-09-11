# Local Telemetry

A private, localhost-only AI usage dashboard for Codex, OpenCode, and
Alpaca/Ollama, with Grafana, OpenTelemetry, Prometheus, Loki, and Tempo.

## Getting started

Install Docker Engine with Compose v2, Python 3.11+, Go 1.27.1, and Task v3
(validated with 3.52.0). Source checks also require Node (validated with
24.19.0) or the OpenCode Bun runtime.

Run commands from this repository root:

```sh
task check
task telemetry:up
task telemetry:open
```

Use `task --list` for all commands. See the [workbench runbook](docs/local_codex_telemetry.md)
for client setup, privacy and usage accounting, diagnostics, and lifecycle commands.
`task telemetry:doctor` sends labeled synthetic probes to validate local ingestion;
source checks use fake upstreams and require no running services or credentials.

The GitHub Actions source workflow runs policy, gateway, and plugin tests plus
Go vulnerability scanning, without starting the workbench or accessing credentials.

## Extraction from Avian Golf

This repository owns the complete workbench: `telemetry/` contains the stack,
gateway, dashboard, and plugin; `scripts/telemetry/` owns local lifecycle commands;
`tests/` and `.taskfiles/` provide standalone validation. It requires no Avian
checkout, toolchain policy, application code, or cloud resources.

Existing Compose project/volume names, dashboard and datasource UIDs, plugin
filename, metrics, and client headers retain their historical `avian` identifiers.
These are persisted data and client contracts; retaining them preserves existing
history, bookmarks, and installed integrations. Runtime files in `.avian/telemetry/`
are local and ignored by Git. Never commit logs, PID files, binaries, or user config.

When relocating a running installation, first run `task telemetry:down` in the
old checkout, move its `.avian/telemetry/` directory here, then run
`task telemetry:up` here. `down` preserves the Docker data volume; `up` reuses it
and recreates file mounts using this checkout. Do not run `telemetry:reset` during
relocation. Existing clients continue using the same localhost ports; retain any
port overrides when restarting. Normal operation is Linux-oriented because gateway
ownership checks use `/proc`.
