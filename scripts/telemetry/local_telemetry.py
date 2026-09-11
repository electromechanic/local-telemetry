#!/usr/bin/env python3
"""Operate the standalone, localhost-only AI telemetry workbench."""

from __future__ import annotations

import argparse
import hashlib
import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.11 is repository-pinned.
    tomllib = None


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "telemetry" / "compose.yml"
PROJECT = "avian-codex-telemetry"
VOLUME = "avian-codex-telemetry-data"
DASHBOARD_UID = "avian-codex-workbench"
DATASOURCE_UIDS = ("avian-prometheus", "avian-loki", "avian-tempo")
CODEX_SERVICE = "codex_cli_rs"
OPENCODE_SERVICE = "opencode"
GATEWAY_SERVICE = "ollama-gateway"
PROBE_SERVICE = "avian-telemetry-probe"
OPENCODE_PLUGIN_VERSION = "2.0.0"
OPENCODE_PLUGIN_SOURCE = ROOT / "telemetry" / "opencode" / "avian_local_telemetry.js"
PROXY_SOURCE = ROOT / "telemetry" / "ollama-gateway"
TELEMETRY_STATE = ROOT / ".avian" / "telemetry"
# Retain the existing owned process paths so an upgrade can replace it safely.
PROXY_BINARY = TELEMETRY_STATE / "alpaca-ollama-proxy"
PROXY_PID = TELEMETRY_STATE / "alpaca-ollama-proxy.pid"
PROXY_LOG = TELEMETRY_STATE / "alpaca-ollama-proxy.log"


def run(command: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        env=os.environ.copy(),
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def compose_command(args: argparse.Namespace, *extra: str) -> list[str]:
    return [
        "docker", "compose", "--project-name", PROJECT, "--file", str(COMPOSE_FILE),
        *extra,
    ]


def port_environment(args: argparse.Namespace) -> dict[str, str]:
    return {
        "GRAFANA_PORT": str(args.grafana_port),
        "OTLP_GRPC_PORT": str(args.otlp_grpc_port),
        "OTLP_HTTP_PORT": str(args.otlp_http_port),
        "TELEMETRY_CONFIG_REVISION": hashlib.sha256(
            (ROOT / "telemetry/collector/otelcol-config.yml").read_bytes()
            + (ROOT / "telemetry/grafana/dashboards/avian-codex-workbench.json").read_bytes()
        ).hexdigest(),
    }


def compose(args: argparse.Namespace, *extra: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(port_environment(args))
    return subprocess.run(
        compose_command(args, *extra), cwd=ROOT, env=env, check=check, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def url(args: argparse.Namespace, path: str = "") -> str:
    return f"http://127.0.0.1:{args.grafana_port}{path}"


def dashboard_url(args: argparse.Namespace) -> str:
    return url(args, f"/d/{DASHBOARD_UID}/avian-local-ai-workbench")


def proxy_url(args: argparse.Namespace, path: str = "") -> str:
    return f"http://127.0.0.1:{args.ollama_gateway_port}{path}"


def ollama_url(args: argparse.Namespace, path: str = "") -> str:
    return f"http://127.0.0.1:{args.ollama_port}{path}"


def http_ok(target: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(target, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return False


def http_json(
    target: str,
    *,
    data: dict[str, object] | None = None,
    timeout: float = 3.0,
) -> tuple[int | None, dict[str, object] | None]:
    encoded = json.dumps(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json"} if encoded is not None else {}
    request = urllib.request.Request(target, data=encoded, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode() or "{}")
            return response.status, payload if isinstance(payload, dict) else None
    except (urllib.error.URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError):
        return None, None


def grafana_api_url(args: argparse.Namespace, path: str, params: dict[str, object] | None = None) -> str:
    target = url(args, path)
    if params:
        target += "?" + urllib.parse.urlencode(params)
    return target


def grafana_backend_query(
    args: argparse.Namespace,
    uid: str,
    path: str,
    params: dict[str, object],
) -> dict[str, object] | None:
    _, payload = http_json(grafana_api_url(args, f"/api/datasources/proxy/uid/{uid}{path}", params))
    return payload


def tcp_ok(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def port_available(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Docker teardown can leave harmless TIME_WAIT sockets briefly behind.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def docker_ready() -> tuple[bool, list[str]]:
    problems: list[str] = []
    if shutil.which("docker") is None:
        return False, ["Docker CLI is unavailable; install Docker Engine/Desktop."]
    result = run(["docker", "version"], check=False, capture=True)
    if result.returncode != 0:
        problems.append("Docker Engine is unreachable; start Docker or fix socket permissions.")
    result = run(["docker", "compose", "version"], check=False, capture=True)
    if result.returncode != 0:
        problems.append("Docker Compose v2 is unavailable; install the Docker Compose plugin.")
    return not problems, problems


def proxy_pid() -> int | None:
    try:
        value = int(PROXY_PID.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return value if value > 1 else None


def proxy_process_owned(pid: int) -> bool:
    process = Path("/proc") / str(pid)
    if not process.exists():
        return False
    try:
        command = (process / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
    except OSError:
        return False
    return str(PROXY_BINARY) in command


def proxy_running(args: argparse.Namespace) -> bool:
    pid = proxy_pid()
    if not (pid and proxy_process_owned(pid)):
        return False
    status, health = http_json(proxy_url(args, "/healthz"))
    try:
        built = (TELEMETRY_STATE / "gateway-build.txt").read_text()
    except OSError:
        return False
    return status == 200 and isinstance(health, dict) and health.get("service") == GATEWAY_SERVICE and built == gateway_signature(args)


def gateway_signature(args: argparse.Namespace) -> str:
    source = b"".join(path.read_bytes() for path in sorted(PROXY_SOURCE.glob("*.go")) if not path.name.endswith("_test.go"))
    return hashlib.sha256(source + f"{args.ollama_gateway_port}:{args.ollama_port}:{args.otlp_http_port}".encode()).hexdigest()


def build_proxy() -> tuple[bool, str]:
    if shutil.which("go") is None:
        return False, "Go is unavailable; install the repository-pinned toolchain"
    TELEMETRY_STATE.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["go", "build", "-trimpath", "-o", str(PROXY_BINARY), "."],
        cwd=PROXY_SOURCE,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        return False, detail[-1] if detail else "Go build failed"
    return True, "proxy binary built"


def stop_proxy(*, quiet: bool = False) -> bool:
    pid = proxy_pid()
    if pid is None:
        if PROXY_PID.exists():
            PROXY_PID.unlink()
        if not quiet:
            print("Ollama gateway: already stopped")
        return True
    if not proxy_process_owned(pid):
        print(f"Refusing to stop PID {pid}: it is not the repository-owned Ollama gateway.", file=sys.stderr)
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and proxy_process_owned(pid):
        time.sleep(0.1)
    if proxy_process_owned(pid):
        os.kill(pid, signal.SIGKILL)
    if PROXY_PID.exists():
        PROXY_PID.unlink()
    if not quiet:
        print("Ollama gateway: stopped")
    return True


def start_proxy(args: argparse.Namespace, timeout: float | None = None) -> int:
    if proxy_running(args):
        print(f"Ollama gateway: already running at {proxy_url(args)}")
        return 0
    pid = proxy_pid()
    if pid is not None and proxy_process_owned(pid) and not stop_proxy(quiet=True):
        return 1
    if not port_available(args.ollama_gateway_port):
        print(f"Ollama gateway port 127.0.0.1:{args.ollama_gateway_port} is occupied by another process.", file=sys.stderr)
        return 1
    built, detail = build_proxy()
    if not built:
        print(f"Ollama gateway build failed: {detail}", file=sys.stderr)
        return 1
    log_handle = PROXY_LOG.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            [
                str(PROXY_BINARY),
                "--listen", f"127.0.0.1:{args.ollama_gateway_port}",
                "--upstream", ollama_url(args),
                "--otlp-http", f"http://127.0.0.1:{args.otlp_http_port}",
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_handle.close()
    PROXY_PID.write_text(f"{process.pid}\n", encoding="utf-8")
    (TELEMETRY_STATE / "gateway-build.txt").write_text(gateway_signature(args))
    deadline = time.monotonic() + (timeout if timeout is not None else 30)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            print(f"Ollama gateway exited during startup; inspect {PROXY_LOG}.", file=sys.stderr)
            return 1
        if http_ok(proxy_url(args, "/healthz")):
            print(f"Ollama gateway: ready at {proxy_url(args)}")
            return 0
        time.sleep(0.2)
    stop_proxy(quiet=True)
    print("Ollama gateway did not become ready before the timeout.", file=sys.stderr)
    return 1


def ollama_upstream_ready(args: argparse.Namespace) -> tuple[bool, str]:
    status, payload = http_json(ollama_url(args, "/api/version"))
    version = payload.get("version") if isinstance(payload, dict) else None
    if status == 200 and isinstance(version, str):
        return True, f"Ollama {version} at {ollama_url(args)}"
    return False, f"Ollama is unavailable at {ollama_url(args)}"


def print_gateway_routing(args: argparse.Namespace) -> None:
    print(f"Shared Ollama endpoint: {proxy_url(args)} (unattributed requests use client=unknown)")
    print(f"Alpaca external-instance URL: {proxy_url(args, '/clients/alpaca')}")
    print(f"OpenCode Ollama provider options.baseURL: {proxy_url(args, '/clients/opencode/v1')}")
    print(f"Ollama CLI: OLLAMA_HOST={proxy_url(args)} ollama run <model>")
    print(f"Direct traffic to {ollama_url(args)} bypasses telemetry; health does not verify client routing.")


def ollama_gateway_check(args: argparse.Namespace) -> int:
    running = proxy_running(args)
    upstream, upstream_message = ollama_upstream_ready(args)
    binding = tcp_ok(args.ollama_gateway_port)
    print(f"Ollama gateway health: {'ready' if running else 'not ready'} ({proxy_url(args, '/healthz')})")
    print(f"Ollama gateway binding: {'loopback reachable' if binding else 'not reachable'} (127.0.0.1:{args.ollama_gateway_port})")
    print(f"Ollama upstream: {'ready' if upstream else 'not ready'} ({upstream_message})")
    print_gateway_routing(args)
    return 0 if running and upstream and binding else 1


def ollama_gateway_up(args: argparse.Namespace) -> int:
    return start_proxy(args, getattr(args, "timeout", 120))


def ps_records(args: argparse.Namespace) -> list[dict[str, object]]:
    result = compose(args, "ps", "--all", "--format", "json", check=False, capture=True)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    raw = result.stdout.strip()
    try:
        decoded = json.loads(raw)
        return decoded if isinstance(decoded, list) else [decoded]
    except json.JSONDecodeError:
        records = []
        for line in raw.splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records


def config_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    return Path(codex_home).expanduser() / "config.toml" if codex_home else Path.home() / ".codex" / "config.toml"


def exporter_endpoint(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("otlp-http", "otlp-grpc"):
        nested = value.get(key)
        if isinstance(nested, dict) and isinstance(nested.get("endpoint"), str):
            return nested["endpoint"]
    return None


def codex_config_ready(args: argparse.Namespace) -> tuple[bool, str]:
    path = config_path()
    if not path.is_file():
        return False, "user-level Codex OTEL configuration is not present"
    if tomllib is None:
        return False, "Python tomllib is unavailable, so Codex configuration could not be checked"
    try:
        with path.open("rb") as handle:
            otel = tomllib.load(handle).get("otel")
    except (OSError, tomllib.TOMLDecodeError):
        return False, "user-level Codex configuration could not be parsed"
    if not isinstance(otel, dict) or otel.get("log_user_prompt") is not False:
        return False, "Codex OTEL is missing or raw prompt logging is not explicitly disabled"
    expected = f"http://127.0.0.1:{args.otlp_http_port}"
    endpoints = [exporter_endpoint(otel.get(key)) for key in ("exporter", "metrics_exporter", "trace_exporter")]
    if not all(endpoint and endpoint.startswith(expected) for endpoint in endpoints):
        return False, "Codex logs, metrics, and traces are not all routed to this local receiver"
    return True, "user-level Codex OTEL routing appears ready"


def opencode_config_dir() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    return Path(configured).expanduser() / "opencode" if configured else Path.home() / ".config" / "opencode"


def opencode_plugin_path() -> Path:
    return opencode_config_dir() / "plugins" / OPENCODE_PLUGIN_SOURCE.name


def opencode_version() -> str | None:
    executable = shutil.which("opencode")
    if executable is None:
        return None
    result = run([executable, "--version"], check=False, capture=True)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def opencode_plugin_ready() -> tuple[bool, str]:
    version = opencode_version()
    if version is None:
        return False, "OpenCode is not installed or not executable"
    target = opencode_plugin_path()
    if not target.is_file():
        return False, f"OpenCode {version}; privacy-safe plugin is not installed"
    try:
        matches = target.read_bytes() == OPENCODE_PLUGIN_SOURCE.read_bytes()
    except OSError:
        return False, f"OpenCode {version}; plugin could not be read"
    if not matches:
        return False, f"OpenCode {version}; installed plugin differs from repository v{OPENCODE_PLUGIN_VERSION}"
    return True, f"OpenCode {version}; privacy-safe plugin v{OPENCODE_PLUGIN_VERSION} is installed"


def opencode_setup(args: argparse.Namespace) -> int:
    version = opencode_version()
    if version is None:
        print("OpenCode is not installed or not executable.", file=sys.stderr)
        return 1
    target = opencode_plugin_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.read_bytes() == OPENCODE_PLUGIN_SOURCE.read_bytes():
        print(f"OpenCode {version}: privacy-safe plugin v{OPENCODE_PLUGIN_VERSION} is already current.")
        print("No user configuration was changed.")
        return 0
    backup: Path | None = None
    if target.exists():
        timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = target.with_name(f"{target.name}.backup-{timestamp}")
        shutil.copy2(target, backup)
    temporary = target.with_name(f".{target.name}.tmp")
    shutil.copy2(OPENCODE_PLUGIN_SOURCE, temporary)
    os.replace(temporary, target)
    target.chmod(0o600)
    print(f"OpenCode {version}: installed privacy-safe plugin v{OPENCODE_PLUGIN_VERSION} at {target}.")
    if backup:
        print(f"Previous plugin backup: {backup}")
    print("No opencode.jsonc setting, credential, provider, model, or unrelated plugin was changed.")
    print("New OpenCode processes load the plugin automatically; restart an existing process to enable it there.")
    return 0


def opencode_check(args: argparse.Namespace) -> int:
    ready, message = opencode_plugin_ready()
    print(f"OpenCode integration: {'ready' if ready else 'not ready'} ({message})")
    print(f"OTLP HTTP: http://127.0.0.1:{args.otlp_http_port}")
    print("Content capture: disabled by construction (metadata allowlist only)")
    print("Dependency install: not required; OpenCode's embedded Bun loader loads the local plugin")
    return 0 if ready else 1


def opencode_config(args: argparse.Namespace) -> int:
    print("OpenCode local telemetry uses repository plugin v2.0.0 with no npm dependency.")
    print("Idempotent setup: task telemetry:opencode:setup")
    print(f"Installed path: {opencode_plugin_path()}")
    print(f"OTLP endpoint default: http://127.0.0.1:{args.otlp_http_port}")
    print("Optional sanitized overrides: telemetry/opencode/opencode.telemetry.env.example")
    print("This command did not modify any file. No prompt/response/reasoning/tool/file/environment content is read.")
    return 0


def preflight(args: argparse.Namespace) -> int:
    ready, problems = docker_ready()
    print(f"Docker Engine and Compose: {'ready' if ready else 'not ready'}")
    for problem in problems:
        print(f"  - {problem}")

    records = ps_records(args) if ready else []
    stack_exists = bool(records)
    port_specs = (
        ("Grafana", args.grafana_port),
        ("OTLP gRPC", args.otlp_grpc_port),
        ("OTLP HTTP", args.otlp_http_port),
        ("Ollama gateway", args.ollama_gateway_port),
    )
    ports_ok = True
    for name, port in port_specs:
        available = port_available(port)
        if available:
            state = "available"
        elif (
            (port != args.ollama_gateway_port and stack_exists)
            or (port == args.ollama_gateway_port and (pid := proxy_pid()) is not None and proxy_process_owned(pid))
        ) and tcp_ok(port):
            state = "in use by the local telemetry stack"
        else:
            state = "occupied by another process"
            ports_ok = False
        print(f"{name} 127.0.0.1:{port}: {state}")

    print(f"Grafana reachability: {'ready' if http_ok(url(args, '/api/health')) else 'not reachable'}")
    print(f"OTLP HTTP reachability: {'ready' if tcp_ok(args.otlp_http_port) else 'not reachable'}")
    configured, message = codex_config_ready(args)
    print(f"Codex configuration: {message}")
    if not configured:
        print("  Run `task telemetry:config`, merge the snippet into the user-level config, and restart Codex.")
    return 0 if ready and ports_ok else 1


def container_healthy(args: argparse.Namespace) -> bool:
    records = ps_records(args)
    return bool(records) and all(
        str(record.get("State", "")).lower() == "running"
        and str(record.get("Health", "")).lower() == "healthy"
        for record in records
    )


def collector_healthy(args: argparse.Namespace) -> bool:
    result = compose(
        args, "exec", "-T", "lgtm", "curl", "-fsS", "http://127.0.0.1:13133/ready",
        check=False, capture=True,
    )
    if result.returncode != 0:
        return False
    try:
        return json.loads(result.stdout).get("status") == "Server available"
    except (json.JSONDecodeError, AttributeError):
        return False


def datasource_health(args: argparse.Namespace) -> tuple[bool, int]:
    healthy = 0
    for uid in DATASOURCE_UIDS:
        status, payload = http_json(grafana_api_url(args, f"/api/datasources/uid/{uid}/health"))
        if status == 200 and isinstance(payload, dict) and payload.get("status") == "OK":
            healthy += 1
    return healthy == len(DATASOURCE_UIDS), healthy


def otlp_post(args: argparse.Namespace, signal: str, payload: dict[str, object]) -> bool:
    status, _ = http_json(
        f"http://127.0.0.1:{args.otlp_http_port}/v1/{signal}", data=payload, timeout=5.0,
    )
    return status is not None and 200 <= status < 300


def emit_transport_probe(args: argparse.Namespace, client: str = "transport") -> tuple[str, str, dict[str, bool]]:
    probe_id = str(uuid.uuid4())
    now = time.time_ns()
    end = now + 1_000_000
    resource = {"attributes": [
        {"key": "service.name", "value": {"stringValue": PROBE_SERVICE}},
        {"key": "avian.synthetic", "value": {"boolValue": True}},
        {"key": "ai.client", "value": {"stringValue": client}},
    ]}
    point_attributes = [
        {"key": "avian.synthetic", "value": {"boolValue": True}},
        {"key": "ai.client", "value": {"stringValue": client}},
        {"key": "probe_id", "value": {"stringValue": probe_id}},
    ]
    metrics = {
        "resourceMetrics": [{
            "resource": resource,
            "scopeMetrics": [{"scope": {"name": "avian.telemetry.probe"}, "metrics": [{
                "name": "avian.telemetry.transport_probe",
                "description": "Synthetic local OTLP transport probe",
                "unit": "1", "sum": {"aggregationTemporality": 2, "isMonotonic": True,
                "dataPoints": [{"attributes": point_attributes, "asInt": "1",
                                "timeUnixNano": str(now), "startTimeUnixNano": str(now)}]},
            }]}],
        }]}
    logs = {
        "resourceLogs": [{"resource": resource, "scopeLogs": [{
            "scope": {"name": "avian.telemetry.probe"},
            "logRecords": [{
                "timeUnixNano": str(now), "severityNumber": 9, "severityText": "INFO",
                "body": {"stringValue": "synthetic local transport probe"},
                "attributes": [
                    {"key": "event.name", "value": {"stringValue": "avian.telemetry.transport_probe"}},
                    *point_attributes,
                ],
            }],
        }]}],
    }
    trace_id = uuid.uuid4().hex
    traces = {
        "resourceSpans": [{"resource": resource, "scopeSpans": [{
            "scope": {"name": "avian.telemetry.probe"},
            "spans": [{
                "traceId": trace_id, "spanId": trace_id[:16],
                "name": "avian.telemetry.transport_probe", "kind": 1,
                "startTimeUnixNano": str(now), "endTimeUnixNano": str(end),
                "attributes": point_attributes, "status": {"code": 1},
            }],
        }]}],
    }
    return probe_id, trace_id, {
        "metrics": otlp_post(args, "metrics", metrics),
        "logs": otlp_post(args, "logs", logs),
        "traces": otlp_post(args, "traces", traces),
    }


def synthetic_probe(args: argparse.Namespace) -> int:
    requested = args.client.strip().lower()
    clients = ("codex", "opencode", "ollama") if requested == "all" else (requested,)
    if any(client not in {"codex", "opencode", "ollama"} for client in clients):
        print("CLIENT must be codex, opencode, ollama, or all.", file=sys.stderr)
        return 2
    if not collector_healthy(args):
        print("Collector is unavailable; start the workbench first.", file=sys.stderr)
        return 1
    failed = False
    for client in clients:
        probe_id, trace_id, accepted = emit_transport_probe(args, client)
        stored = wait_for_transport_storage(args, probe_id, trace_id) if all(accepted.values()) else {
            "metrics": False, "logs": False, "traces": False,
        }
        ready = all(accepted.values()) and all(stored.values())
        failed = failed or not ready
        detail = ", ".join(f"{name}={'stored' if stored[name] else 'missing'}" for name in stored)
        print(f"[{'PASS' if ready else 'FAIL'}] Synthetic {client} probe ({detail}; avian.synthetic=true)")
    print("Synthetic probes use a dedicated service/metric and are excluded from authentic usage totals.")
    return 1 if failed else 0


def result_count(payload: dict[str, object] | None, key: str = "result") -> int:
    if not isinstance(payload, dict):
        return 0
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get(key), list):
        return len(data[key])
    return 0


def wait_for_transport_storage(
    args: argparse.Namespace,
    probe_id: str,
    trace_id: str,
    timeout: float = 30.0,
) -> dict[str, bool]:
    quoted_probe = json.dumps(probe_id)
    prometheus_query = (
        'avian_telemetry_transport_probe_total{job="avian-telemetry-probe",'
        f'avian_synthetic="true",probe_id={quoted_probe}' + "}"
    )
    loki_query = (
        'sum(count_over_time({service_name="avian-telemetry-probe"} '
        '| event_name = `avian.telemetry.transport_probe` '
        f'| probe_id = `{probe_id}` [5m]))'
    )
    deadline = time.monotonic() + timeout
    stored = {"metrics": False, "logs": False, "traces": False}
    while time.monotonic() < deadline and not all(stored.values()):
        if not stored["metrics"]:
            payload = grafana_backend_query(
                args, "avian-prometheus", "/api/v1/query", {"query": prometheus_query},
            )
            stored["metrics"] = result_count(payload) > 0
        if not stored["logs"]:
            payload = grafana_backend_query(
                args, "avian-loki", "/loki/api/v1/query", {"query": loki_query},
            )
            stored["logs"] = result_count(payload) > 0
        if not stored["traces"]:
            _, payload = http_json(grafana_api_url(
                args, f"/api/datasources/proxy/uid/avian-tempo/api/traces/{trace_id}",
            ))
            stored["traces"] = bool(isinstance(payload, dict) and payload.get("batches"))
        if not all(stored.values()):
            time.sleep(0.5)
    return stored


def recent_authentic_completions(args: argparse.Namespace, lookback: str = "30m") -> tuple[int, int]:
    logs_query = (
        f'sum(count_over_time({{service_name="{CODEX_SERVICE}"}} '
        '| event_name = `codex.sse_event` | event_kind = `response.completed` '
        f'[{lookback}])) or vector(0)'
    )
    logs_payload = grafana_backend_query(
        args, "avian-loki", "/loki/api/v1/query", {"query": logs_query},
    )
    metrics_query = (
        'sum(count_over_time(codex_websocket_event_total{'
        'job=~"codex_cli_rs|codex_exec",kind="response.completed",success="true"}'
        f'[{lookback}])) or vector(0)'
    )
    metrics_payload = grafana_backend_query(
        args, "avian-prometheus", "/api/v1/query", {"query": metrics_query},
    )
    try:
        log_events = int(float(logs_payload["data"]["result"][0]["value"][1]))  # type: ignore[index]
    except (KeyError, IndexError, TypeError, ValueError):
        log_events = 0
    try:
        metric_samples = int(float(metrics_payload["data"]["result"][0]["value"][1]))  # type: ignore[index]
    except (KeyError, IndexError, TypeError, ValueError):
        metric_samples = 0
    return log_events, metric_samples


def loki_count(args: argparse.Namespace, query: str) -> int:
    payload = grafana_backend_query(args, "avian-loki", "/loki/api/v1/query", {"query": query})
    try:
        return int(float(payload["data"]["result"][0]["value"][1]))  # type: ignore[index]
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


def recent_client_activity(args: argparse.Namespace, lookback: str = "30m") -> dict[str, int]:
    codex_logs, codex_metrics = recent_authentic_completions(args, lookback)
    return {
        "codex": max(codex_logs, codex_metrics),
        "opencode": loki_count(
            args,
            f'sum(count_over_time({{service_name="{OPENCODE_SERVICE}"}} '
            f'| event_name = `opencode.operation.completed` [{lookback}])) or vector(0)',
        ),
        "ollama": loki_count(
            args,
            f'sum(count_over_time({{service_name="{GATEWAY_SERVICE}"}} '
            f'| event_name = `ollama.inference.completed` [{lookback}])) or vector(0)',
        ),
    }


def collector_ingestion_active(args: argparse.Namespace) -> bool:
    query = "sum(increase(otelcol_receiver_accepted_log_records_total[30m])) or vector(0)"
    payload = grafana_backend_query(args, "avian-prometheus", "/api/v1/query", {"query": query})
    try:
        return float(payload["data"]["result"][0]["value"][1]) > 0  # type: ignore[index]
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def expand_dashboard_query(query: str) -> str:
    return (
        query.replace("$__rate_interval", "5m")
        .replace("$__range", "30m")
        .replace("$__auto", "1m")
        .replace("${client:regex}", ".*")
        .replace("${model:regex}", ".*")
        .replace("${provider:regex}", ".*")
        .replace("${project:regex}", ".*")
        .replace("$client", ".*")
        .replace("$model", ".*")
        .replace("$provider", ".*")
        .replace("$project", ".*")
    )


def dashboard_query_health(args: argparse.Namespace) -> tuple[bool, int, int, list[str]]:
    status, response = http_json(grafana_api_url(args, f"/api/dashboards/uid/{DASHBOARD_UID}"))
    if status != 200 or not isinstance(response, dict):
        return False, 0, 0, ["dashboard is not provisioned"]
    meta = response.get("meta")
    dashboard = response.get("dashboard")
    if not isinstance(meta, dict) or not meta.get("provisioned") or not isinstance(dashboard, dict):
        return False, 0, 0, ["dashboard is not file-provisioned"]

    total = 0
    with_data = 0
    errors: list[str] = []
    now = time.time()
    for panel in dashboard.get("panels", []):
        if not isinstance(panel, dict):
            continue
        panel_source = panel.get("datasource") if isinstance(panel.get("datasource"), dict) else {}
        for target in panel.get("targets", []):
            if not isinstance(target, dict) or target.get("hide"):
                continue
            total += 1
            target_source = target.get("datasource") if isinstance(target.get("datasource"), dict) else {}
            uid = target_source.get("uid") or panel_source.get("uid")
            query = target.get("expr") or target.get("query")
            if not isinstance(uid, str) or not isinstance(query, str):
                errors.append(f"{panel.get('title', 'panel')}/{target.get('refId', '?')}: missing query or datasource")
                continue
            source_types = {
                "avian-prometheus": "prometheus", "avian-loki": "loki", "avian-tempo": "tempo",
            }
            source_type = source_types.get(uid)
            rendered_target = dict(target)
            rendered_target["datasource"] = {"uid": uid, "type": source_type}
            if "expr" in rendered_target:
                rendered_target["expr"] = expand_dashboard_query(query)
            else:
                rendered_target["query"] = expand_dashboard_query(query)
            rendered_target.setdefault("intervalMs", 60_000)
            rendered_target.setdefault("maxDataPoints", 1000)
            ref_id = str(target.get("refId", "A"))
            _, payload = http_json(
                grafana_api_url(args, "/api/ds/query"),
                data={
                    "from": str(int((now - 1800) * 1000)),
                    "to": str(int(now * 1000)),
                    "queries": [rendered_target],
                },
                timeout=10.0,
            )
            result = payload.get("results", {}).get(ref_id) if isinstance(payload, dict) else None
            valid = isinstance(result, dict) and result.get("status") == 200 and not result.get("error")
            frames = result.get("frames", []) if isinstance(result, dict) else []
            count = 0
            if isinstance(frames, list):
                for frame in frames:
                    values = frame.get("data", {}).get("values", []) if isinstance(frame, dict) else []
                    if isinstance(values, list) and any(isinstance(column, list) and column for column in values):
                        count += 1
            if valid:
                with_data += int(count > 0)
            else:
                detail = result.get("error") if isinstance(result, dict) else "no response"
                errors.append(f"{panel.get('title', 'panel')}/{target.get('refId', '?')}: {detail}")
    return not errors and total > 0, total, with_data, errors


def doctor(args: argparse.Namespace, *, for_up: bool = False) -> int:
    if for_up:
        return preflight(args)

    docker_ok, _ = docker_ready()
    containers_ok = docker_ok and container_healthy(args)
    print(f"[{'PASS' if containers_ok else 'FAIL'}] Container health")
    collector_ok = containers_ok and collector_healthy(args)
    print(f"[{'PASS' if collector_ok else 'FAIL'}] Collector reachability")

    configured, config_message = codex_config_ready(args)
    print(f"[{'PASS' if configured else 'DEGRADED'}] Codex configuration: {config_message}")

    opencode_ready, opencode_message = opencode_plugin_ready()
    print(f"[{'PASS' if opencode_ready else 'DEGRADED'}] OpenCode telemetry path: {opencode_message}")

    proxy_ok = proxy_running(args)
    print(f"[{'PASS' if proxy_ok else 'FAIL'}] Ollama gateway health ({proxy_url(args, '/healthz')})")
    upstream_ok, upstream_message = ollama_upstream_ready(args)
    print(f"[{'PASS' if upstream_ok else 'FAIL'}] Ollama upstream health ({upstream_message})")

    datasources_ok, healthy_sources = datasource_health(args) if containers_ok else (False, 0)
    print(f"[{'PASS' if datasources_ok else 'FAIL'}] Grafana datasources ({healthy_sources}/{len(DATASOURCE_UIDS)})")

    transport_ok = False
    if collector_ok and datasources_ok:
        probe_id, trace_id, accepted = emit_transport_probe(args)
        stored = wait_for_transport_storage(args, probe_id, trace_id) if all(accepted.values()) else {
            "metrics": False, "logs": False, "traces": False,
        }
        transport_ok = all(accepted.values()) and all(stored.values())
        detail = ", ".join(f"{name}={'pass' if stored[name] else 'fail'}" for name in stored)
    else:
        detail = "not run because the collector or datasources are unavailable"
    print(f"[{'PASS' if transport_ok else 'FAIL'}] Synthetic OTLP transport ({detail})")

    ingestion_ok = collector_ingestion_active(args) if datasources_ok else False
    print(f"[{'PASS' if ingestion_ok else 'FAIL'}] Collector ingestion counters")

    activity = recent_client_activity(args) if datasources_ok else {name: 0 for name in ("codex", "opencode", "ollama")}
    for client in ("codex", "opencode", "ollama"):
        count = activity[client]
        print(f"[{'PASS' if count > 0 else 'IDLE'}] Recent authentic {client} data "
              f"({count} completed operations in 30m; {'observed' if count > 0 else 'no observed completion; verify routing'})")

    dashboard_ok, target_count, data_count, query_errors = (
        dashboard_query_health(args) if datasources_ok else (False, 0, 0, ["datasources unavailable"])
    )
    print(f"[{'PASS' if dashboard_ok else 'FAIL'}] Dashboard provisioning and queries "
          f"({target_count} valid targets; {data_count} currently return data)")
    for error in query_errors[:3]:
        print(f"  - {error}")

    if not all((containers_ok, collector_ok, proxy_ok, upstream_ok, datasources_ok, transport_ok, ingestion_ok, dashboard_ok)):
        print("Overall: FAILED")
        return 1
    if not configured or not opencode_ready:
        print("Overall: DEGRADED (one or more client integrations require configuration)")
        return 1
    if not all(value > 0 for value in activity.values()):
        print_gateway_routing(args)
        print("Overall: IDLE (transport healthy; one or more sources have no recent authentic completion; verify routing)")
        return 0
    print("Overall: HEALTHY (authentic Codex, OpenCode, and Ollama gateway telemetry observed)")
    return 0


def up(args: argparse.Namespace) -> int:
    if doctor(args, for_up=True) != 0:
        return 1
    compose(args, "up", "--detach", "--remove-orphans")
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        records = ps_records(args)
        healthy = records and all(str(record.get("Health", "")).lower() == "healthy" for record in records)
        provisioned = http_ok(url(args, f"/api/dashboards/uid/{DASHBOARD_UID}"))
        if healthy and provisioned and tcp_ok(args.otlp_http_port):
            if start_proxy(args, max(1, deadline - time.monotonic())) != 0:
                return 1
            configured, _ = codex_config_ready(args)
            opencode_ready, _ = opencode_plugin_ready()
            print(f"Avian Local AI Workbench: {dashboard_url(args)}")
            print(f"Codex configuration: {'ready' if configured else 'required; run `task telemetry:config` and restart Codex'}")
            print(f"OpenCode integration: {'ready' if opencode_ready else 'required; run `task telemetry:opencode:setup`'}")
            print_gateway_routing(args)
            print("Run `task telemetry:doctor` to exercise all three OTLP signals and every dashboard query.")
            return 0
        time.sleep(1)
    print(f"Telemetry stack did not become ready within {args.timeout} seconds.", file=sys.stderr)
    print("Run `task telemetry:logs` for concise operational diagnostics.", file=sys.stderr)
    return 1


def status(args: argparse.Namespace) -> int:
    records = ps_records(args)
    grafana = http_ok(url(args, "/api/health"))
    otlp_http = tcp_ok(args.otlp_http_port)
    otlp_grpc = tcp_ok(args.otlp_grpc_port)
    proxy = proxy_running(args)
    upstream, _ = ollama_upstream_ready(args)
    if not records or all(str(record.get("State", "")).lower() not in {"running", "restarting"} for record in records):
        state = "partially healthy" if proxy else "stopped"
    elif all(str(record.get("Health", "")).lower() == "healthy" for record in records) and grafana and otlp_http and otlp_grpc and proxy:
        state = "running"
    elif any(str(record.get("Health", "")).lower() == "unhealthy" for record in records):
        state = "unhealthy"
    else:
        state = "partially healthy"
    print(f"Local telemetry: {state}")
    print(f"Grafana: {url(args)} ({'reachable' if grafana else 'not reachable'})")
    print(f"Dashboard: {dashboard_url(args)}")
    print(f"OTLP gRPC: 127.0.0.1:{args.otlp_grpc_port} ({'reachable' if otlp_grpc else 'not reachable'})")
    print(f"OTLP HTTP: http://127.0.0.1:{args.otlp_http_port} ({'reachable' if otlp_http else 'not reachable'})")
    print(f"Ollama gateway: {proxy_url(args)} ({'healthy' if proxy else 'not reachable'})")
    print(f"Ollama upstream: {ollama_url(args)} ({'healthy' if upstream else 'not reachable'})")
    return 0 if state in {"running", "stopped"} else 1


def open_dashboard(args: argparse.Namespace) -> int:
    target = dashboard_url(args)
    opened = webbrowser.open(target, new=2)
    print(f"Avian Local AI Workbench: {target}")
    if not opened:
        print("No supported browser opener was detected; use the URL above.")
    return 0


def logs(args: argparse.Namespace) -> int:
    result = compose(args, "logs", "--no-color", "--tail", "120", "lgtm", check=False)
    if PROXY_LOG.is_file():
        print(f"\nOllama gateway log ({PROXY_LOG}):")
        for line in PROXY_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]:
            print(line)
    return result.returncode


def down(args: argparse.Namespace) -> int:
    if not stop_proxy():
        return 1
    compose(args, "down", "--remove-orphans")
    print(f"Stopped the complete {PROJECT} workbench; persistent volume {VOLUME} was preserved.")
    return 0


def reset(args: argparse.Namespace) -> int:
    if args.confirm != "1":
        print("Refusing to delete local telemetry. Re-run with `CONFIRM=1`.", file=sys.stderr)
        return 2
    if not stop_proxy():
        return 1
    compose(args, "down", "--remove-orphans")
    inspected = run(["docker", "volume", "inspect", VOLUME], check=False, capture=True)
    if inspected.returncode == 0:
        metadata = json.loads(inspected.stdout)[0]
        labels = metadata.get("Labels") or {}
        if metadata.get("Name") != VOLUME or labels.get("com.docker.compose.project") != PROJECT:
            print(f"Refusing to delete volume {VOLUME}: ownership labels do not match {PROJECT}.", file=sys.stderr)
            return 1
        run(["docker", "volume", "rm", VOLUME])
        print(f"Deleted only Docker volume {VOLUME}; collected local telemetry cannot be recovered.")
    else:
        print(f"No {VOLUME} volume exists; the project stack is removed.")
    return 0


def config(args: argparse.Namespace) -> int:
    endpoint = f"http://127.0.0.1:{args.otlp_http_port}"
    print("Add or merge this exact block in the user-level Codex configuration (~/.codex/config.toml):\n")
    print("[otel]")
    print('environment = "avian-local"')
    print("log_user_prompt = false")
    print(f'exporter = {{ otlp-http = {{ endpoint = "{endpoint}/v1/logs", protocol = "binary" }} }}')
    print(f'metrics_exporter = {{ otlp-http = {{ endpoint = "{endpoint}/v1/metrics", protocol = "binary" }} }}')
    print(f'trace_exporter = {{ otlp-http = {{ endpoint = "{endpoint}/v1/traces", protocol = "binary" }} }}')
    print("\nThis command did not modify any file. Restart Codex after updating the user-level configuration.")
    print("Project-scoped .codex/config.toml cannot route telemetry and will be ignored for this setting.")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--grafana-port", type=int, default=3000)
    result.add_argument("--otlp-grpc-port", type=int, default=4317)
    result.add_argument("--otlp-http-port", type=int, default=4318)
    result.add_argument("--ollama-gateway-port", type=int, default=11435)
    result.add_argument("--ollama-port", type=int, default=11434)
    subparsers = result.add_subparsers(dest="command", required=True)
    for name in (
        "doctor", "status", "open", "logs", "down", "config",
        "opencode-config", "opencode-setup", "opencode-check", "ollama-gateway-check",
    ):
        subparsers.add_parser(name)
    up_parser = subparsers.add_parser("up")
    up_parser.add_argument("--timeout", type=int, default=120)
    reset_parser = subparsers.add_parser("reset")
    reset_parser.add_argument("--confirm", default="0")
    proxy_parser = subparsers.add_parser("ollama-gateway-up")
    proxy_parser.add_argument("--timeout", type=int, default=120)
    probe_parser = subparsers.add_parser("probe")
    probe_parser.add_argument("--client", default="all")
    return result


def main() -> int:
    args = parser().parse_args()
    ports = (args.grafana_port, args.otlp_grpc_port, args.otlp_http_port, args.ollama_gateway_port, args.ollama_port)
    if not all(1 <= value <= 65535 for value in ports):
        print("Ports must be integers from 1 through 65535.", file=sys.stderr)
        return 2
    if len(set(ports)) != len(ports):
        print("Grafana, OTLP, Ollama gateway, and Ollama ports must be distinct.", file=sys.stderr)
        return 2
    handlers = {
        "doctor": doctor, "up": up, "status": status, "open": open_dashboard,
        "logs": logs, "down": down, "reset": reset, "config": config,
        "opencode-config": opencode_config, "opencode-setup": opencode_setup,
        "opencode-check": opencode_check, "ollama-gateway-up": ollama_gateway_up,
        "ollama-gateway-check": ollama_gateway_check, "probe": synthetic_probe,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
