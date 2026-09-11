import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
TASKFILE = ROOT / ".taskfiles" / "telemetry.yml"
COMPOSE = ROOT / "telemetry" / "compose.yml"
COLLECTOR = ROOT / "telemetry" / "collector" / "otelcol-config.yml"
DASHBOARD = ROOT / "telemetry" / "grafana" / "dashboards" / "avian-codex-workbench.json"
DATASOURCES = ROOT / "telemetry" / "grafana" / "provisioning" / "datasources" / "avian.yml"
PROVISIONING = ROOT / "telemetry" / "grafana" / "provisioning" / "dashboards" / "avian.yml"
HELPER = ROOT / "scripts" / "telemetry" / "local_telemetry.py"
DOC = ROOT / "docs" / "local_codex_telemetry.md"
OPENCODE_PLUGIN = ROOT / "telemetry" / "opencode" / "avian_local_telemetry.js"
OPENCODE_FIXTURES = ROOT / "telemetry" / "opencode" / "fixtures" / "completed_operations.json"
PROXY = ROOT / "telemetry" / "ollama-gateway" / "proxy.go"
PROXY_TELEMETRY = ROOT / "telemetry" / "ollama-gateway" / "telemetry.go"


class LocalTelemetryTests(unittest.TestCase):
    def test_public_task_interface_and_repository_compose(self) -> None:
        root_task = (ROOT / "Taskfile.yml").read_text()
        self.assertRegex(
            root_task,
            r"(?m)^  telemetry: \.taskfiles/telemetry\.yml$",
        )
        taskfile = TASKFILE.read_text()
        tasks = taskfile.split("\ntasks:\n", 1)[1]
        expected = {
            "check", "doctor", "up", "status", "open", "logs", "down", "reset", "config", "probe",
            "opencode:config", "opencode:setup", "opencode:check",
            "ollama-gateway:up", "ollama-gateway:check",
        }
        declared = set(re.findall(r"(?m)^  ([a-z][a-z:-]+):$", tasks))
        self.assertEqual(declared, expected)
        self.assertIn("scripts/telemetry/local_telemetry.py", taskfile)
        helper = HELPER.read_text()
        self.assertIn('ROOT / "telemetry" / "compose.yml"', helper)

    def test_compose_is_local_pinned_and_persistent(self) -> None:
        compose = COMPOSE.read_text()
        self.assertRegex(compose, r"(?m)^name: avian-codex-telemetry$")
        self.assertRegex(
            compose,
            r"(?m)^    image: grafana/otel-lgtm:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$",
        )
        bindings = re.findall(r'(?m)^      - "([^"]+):(?:3000|4317|4318)"$', compose)
        self.assertEqual(len(bindings), 3)
        self.assertTrue(all(binding.startswith("127.0.0.1:") for binding in bindings))
        self.assertRegex(
            compose,
            r"(?m)^  avian-codex-telemetry-data:\n    name: avian-codex-telemetry-data$",
        )
        self.assertRegex(compose, r"(?m)^      - avian-codex-telemetry-data:/data$")
        self.assertIn(
            'PROMETHEUS_EXTRA_ARGS: "--enable-feature=otlp-deltatocumulative"',
            compose,
        )

    def test_privacy_filter_drops_payload_logs_and_keeps_no_debug_export(self) -> None:
        collector = COLLECTOR.read_text()
        self.assertIn('attributes["event.name"] == "codex.user_prompt"', collector)
        self.assertIn('attributes["event.name"] == "codex.tool_result"', collector)
        deleted = set(
            re.findall(
                r"(?m)^      - \{key: ([^,]+), action: delete\}$",
                collector,
            )
        )
        self.assertTrue({
            "prompt", "arguments", "output", "gen_ai.prompt",
            "gen_ai.tool.call.arguments", "user.email", "user.account_id",
        } <= deleted)
        self.assertNotIn("debug/", collector)
        self.assertIn("filter/usage_authority", collector)
        self.assertIn('attributes["ai.usage.authority"] != true', collector)

    def test_lifecycle_safety_and_bounded_readiness(self) -> None:
        helper = HELPER.read_text()
        self.assertIn('compose(args, "up", "--detach", "--remove-orphans")', helper)
        self.assertIn("deadline = time.monotonic() + args.timeout", helper)
        down_body = helper.split("def down(", 1)[1].split("def reset(", 1)[0]
        self.assertNotIn("--volumes", down_body)
        reset_body = helper.split("def reset(", 1)[1].split("def config(", 1)[0]
        self.assertIn('args.confirm != "1"', reset_body)
        self.assertIn("docker.compose.project", reset_body)
        self.assertIn("VOLUME", reset_body)
        self.assertNotRegex(reset_body, r"volume.*prune|system.*prune")

    def test_configuration_is_safe_and_never_written(self) -> None:
        combined = TASKFILE.read_text() + HELPER.read_text() + DOC.read_text()
        self.assertIn("log_user_prompt = false", combined)
        config_body = HELPER.read_text().split("def config(", 1)[1].split("def parser(", 1)[0]
        self.assertNotRegex(config_body, r"open\([^)]*['\"]w|write_text|mkdir|touch")
        self.assertIn("did not modify any file", config_body)
        self.assertNotIn(".codex/config.toml:", (ROOT / "Taskfile.yml").read_text())

    def test_dashboard_and_provisioning_parse(self) -> None:
        dashboard = json.loads(DASHBOARD.read_text())
        self.assertEqual(dashboard["title"], "Avian Local AI Workbench")
        self.assertEqual(dashboard["uid"], "avian-codex-workbench")
        panel_titles = {panel["title"] for panel in dashboard["panels"]}
        self.assertIn("Sessions started", panel_titles)
        self.assertIn("Recent sanitized errors", panel_titles)
        self.assertIn("Local ingestion health", panel_titles)
        self.assertIn("Recent Codex traces", panel_titles)
        self.assertIn("Inference requests by client", panel_titles)
        self.assertIn("Sessions and turns", panel_titles)
        self.assertIn("Tool executions and failures", panel_titles)
        self.assertIn("Retries and compactions", panel_titles)
        self.assertIn("Generations", panel_titles)
        self.assertIn("Generation throughput", panel_titles)
        self.assertNotRegex(DASHBOARD.read_text().lower(), r"raw prompt|prompt text")
        rendered = DASHBOARD.read_text()
        self.assertNotIn("codex_api_request_total", rendered)
        self.assertIn("codex_websocket_request_total", rendered)
        self.assertIn("codex_tool_call_duration_ms_milliseconds_bucket", rendered)
        self.assertIn('job=~\\\"codex_cli_rs|codex_exec\\\"', rendered)
        self.assertIn('service_name=\\\"codex_cli_rs\\\"', rendered)
        health = next(panel for panel in dashboard["panels"] if panel["title"] == "Local ingestion health")
        self.assertEqual(health["datasource"]["uid"], "-- Mixed --")
        self.assertEqual(
            {target["datasource"]["uid"] for target in health["targets"]},
            {"avian-prometheus"},
        )
        self.assertIn("Healthy; waiting for completed response", json.dumps(health))
        self.assertIn("Authentic Codex telemetry observed", json.dumps(health))
        variables = {item["name"] for item in dashboard["templating"]["list"]}
        self.assertEqual(variables, {"client", "model", "provider", "project"})
        datasource_types = set(
            re.findall(r"(?m)^    type: (prometheus|loki|tempo)$", DATASOURCES.read_text())
        )
        self.assertEqual(datasource_types, {"prometheus", "loki", "tempo"})
        self.assertRegex(
            PROVISIONING.read_text(),
            r"(?m)^      path: /otel-lgtm/avian-codex-workbench\.json$",
        )

    def test_diagnostic_probes_all_backends_without_mixing_synthetic_usage(self) -> None:
        helper = HELPER.read_text()
        self.assertIn('PROBE_SERVICE = "avian-telemetry-probe"', helper)
        self.assertIn('CODEX_SERVICE = "codex_cli_rs"', helper)
        self.assertIn('"metrics": otlp_post(args, "metrics", metrics)', helper)
        self.assertIn('"logs": otlp_post(args, "logs", logs)', helper)
        self.assertIn('"traces": otlp_post(args, "traces", traces)', helper)
        self.assertIn("wait_for_transport_storage", helper)
        self.assertIn("dashboard_query_health", helper)
        self.assertIn("one or more sources have no recent authentic completion", helper)
        dashboard = json.loads(DASHBOARD.read_text())
        real_panels = [
            panel for panel in dashboard["panels"]
            if panel["title"] not in {"Local ingestion health", "Telemetry health by source"}
        ]
        self.assertNotIn("avian-telemetry-probe", json.dumps(real_panels))

    def test_local_artifacts_are_ignored_and_no_cloud_dependency_is_added(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text()
        self.assertRegex(gitignore, r"(?m)^\.avian/$")
        scoped = "\n".join(path.read_text() for path in (TASKFILE, COMPOSE, COLLECTOR, HELPER))
        self.assertNotRegex(scoped.lower(), r"gcloud|google cloud|grafana cloud|secret manager|service account|gcp credential")
        self.assertNotRegex(scoped, r"0\.0\.0\.0:\$\{(?:GRAFANA|OTLP)")

    def test_opencode_plugin_is_dependency_free_normalized_and_content_blind(self) -> None:
        plugin = OPENCODE_PLUGIN.read_text()
        self.assertIn('const PLUGIN_VERSION = "2.0.0"', plugin)
        self.assertNotRegex(plugin, r"(?m)^import ")
        self.assertNotIn('"chat.message"', plugin)
        self.assertNotIn('"shell.env"', plugin)
        self.assertNotIn("part.text", plugin)
        self.assertNotIn("part.delta", plugin)
        self.assertNotIn("part.state.input", plugin)
        self.assertNotIn("part.state.output", plugin)
        self.assertNotIn("error.data", plugin)
        self.assertIn("await transport.flush()", plugin)
        for field in (
            '"ai.client.operation.completed"',
            '"ai.tokens.input"',
            '"ai.tokens.output"',
            '"ai.tokens.reasoning"',
            '"ai.tokens.cached"',
            '"ai.duration.total_ms"',
            '"ai.tool_calls.count"',
            '"ai.retry.count"',
        ):
            self.assertIn(field, plugin)

        fixtures = json.loads(OPENCODE_FIXTURES.read_text())
        self.assertEqual(len(fixtures), 2)
        for fixture in fixtures:
            info = fixture["event"]["properties"]["info"]
            expected = fixture["expected"]
            self.assertEqual(expected["ai.model"], info["modelID"])
            self.assertEqual(expected["ai.provider"], info["providerID"])
            self.assertEqual(
                expected["ai.duration.total_ms"],
                info["time"]["completed"] - info["time"]["created"],
            )
            error_message = info.get("error", {}).get("data", {}).get("message")
            if error_message:
                self.assertNotIn(error_message, plugin)
        self.assertIsNone(fixtures[1]["expected"]["ai.inference.locality"])

    def test_inference_totals_exclude_duplicate_client_observations(self) -> None:
        dashboard = json.loads(DASHBOARD.read_text())
        for panel in dashboard["panels"]:
            if panel["id"] not in (17, 19, 20, 21, 22, 23, 24):
                continue
            for target in panel.get("targets", []):
                query = target.get("expr", "")
                self.assertIn("ai_usage_authority = `true`", query)
                self.assertIn("ai.inference.completed", query)
                self.assertNotIn("codex_cli_rs", query)
        self.assertIn("Direct traffic to", HELPER.read_text())

    def test_ollama_gateway_is_loopback_bounded_and_never_reads_request_bodies(self) -> None:
        proxy = PROXY.read_text()
        telemetry = PROXY_TELEMETRY.read_text()
        self.assertIn('"127.0.0.1:11435"', (PROXY.parent / "main.go").read_text())
        self.assertIn("!ip.IsLoopback()", proxy)
        self.assertIn("const maxMetadataToken = 256", proxy)
        self.assertIn("httputil.NewSingleHostReverseProxy", proxy)
        self.assertIn("reverse.FlushInterval = -1", proxy)
        self.assertNotIn("io.ReadAll(request.Body", proxy)
        self.assertNotIn("json.NewDecoder(request.Body", proxy)
        self.assertIn('const proxyServiceName = "ollama-gateway"', telemetry)
        for forbidden in ("prompt", "messages", "thinking", "images", "embeddings", "tool.arguments"):
            self.assertNotIn(f'attribute("{forbidden}"', telemetry)


if __name__ == "__main__":
    unittest.main()
