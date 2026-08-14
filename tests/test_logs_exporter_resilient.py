import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests
from prometheus_client import generate_latest

from logs_exporter.pihole6_logs_exporter import (
    ContinuousExporter,
    DeliveryHealth,
    LokiPushError,
    PiholeLogsExporter,
    RetryPolicy,
    safe_url,
)


QUERY = {
    "time": "150",
    "client": {"ip": "192.0.2.10"},
    "type": "A",
    "status": "forwarded",
    "domain": "example.test",
}

EXPORTER_SCRIPT = Path(__file__).parent.parent / "logs_exporter" / "pihole6_logs_exporter.py"


class FakePiholeHandler(BaseHTTPRequestHandler):
    query_time = 0

    def do_GET(self):
        payload = {
            "queries": [
                {
                    **QUERY,
                    "time": str(self.__class__.query_time),
                }
            ]
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *args):
        pass


class AlwaysUnavailableLokiHandler(BaseHTTPRequestHandler):
    attempted = threading.Event()

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.__class__.attempted.set()
        self.send_response(503)
        self.end_headers()

    def log_message(self, _format, *args):
        pass


@contextmanager
def running_http_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1)


def unused_local_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def clean_subprocess_environment():
    environment = os.environ.copy()
    for name in ("PIHOLE_URL", "PIHOLE_API_TOKEN", "LOKI_TARGET"):
        environment.pop(name, None)
    return environment


def make_exporter(tmp_path, loki_target="http://loki.test:3100"):
    state_file = tmp_path / "exporter.state"
    state_file.write_text("100")
    delivery_health = DeliveryHealth()
    exporter = PiholeLogsExporter(
        host="http://pihole.test",
        key=None,
        loki_target=loki_target,
        state_file=str(state_file),
        server_name="test-pihole",
        delivery_health=delivery_health,
    )
    return exporter, state_file


def run_supervised(
    exporter,
    stop_event,
    poll_interval=1,
    retry_initial_delay=0.001,
    retry_max_delay=0.001,
    retry_jitter_ratio=0,
):
    supervisor = ContinuousExporter(
        exporter=exporter,
        delivery_health=exporter.delivery_health,
        retry_policy=RetryPolicy(
            initial_delay=retry_initial_delay,
            max_delay=retry_max_delay,
            jitter_ratio=retry_jitter_ratio,
        ),
        poll_interval=poll_interval,
        stop_event=stop_event,
    )
    return supervisor.run()


def successful_response():
    return Mock(status_code=204)


def test_safe_url_omits_all_secret_bearing_components():
    assert safe_url("https://user:token@loki.test:3100/tenant-secret?token=secret#fragment") == (
        "https://loki.test:3100"
    )


def test_cli_failure_trace_never_exposes_loki_url_secrets(tmp_path):
    now = int(time.time())
    FakePiholeHandler.query_time = now - 30
    state_file = tmp_path / "exporter.state"
    state_file.write_text(str(now - 60))
    secret_path = "tenant-secret-placeholder"
    secret_token = "token-secret-placeholder"
    AlwaysUnavailableLokiHandler.attempted = threading.Event()

    with running_http_server(FakePiholeHandler) as pihole, \
         running_http_server(AlwaysUnavailableLokiHandler) as loki:
        result = subprocess.run(
            [
                sys.executable,
                str(EXPORTER_SCRIPT),
                "--host",
                f"http://127.0.0.1:{pihole.server_address[1]}",
                "--loki-target",
                f"http://127.0.0.1:{loki.server_address[1]}/{secret_path}?token={secret_token}",
                "--state-file",
                str(state_file),
                "--log-file",
                "",
                "--log-level",
                "DEBUG",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            env=clean_subprocess_environment(),
        )

    assert result.returncode == 1
    assert secret_path not in result.stderr
    assert secret_token not in result.stderr


@pytest.mark.parametrize(
    "failure",
    [
        requests.exceptions.ConnectionError("name resolution failed"),
        requests.exceptions.ConnectionError("connection refused"),
        requests.exceptions.Timeout("read timed out"),
    ],
)
def test_continuous_mode_retries_connection_and_timeout_then_succeeds(tmp_path, failure):
    exporter, state_file = make_exporter(tmp_path)
    stopped = threading.Event()
    original_write = exporter.write_last_timestamp

    def write_and_stop(timestamp):
        original_write(timestamp)
        stopped.set()

    with patch.object(exporter, "fetch_queries", return_value=[dict(QUERY)]) as fetch, \
         patch.object(exporter, "resolve_hostname", return_value="client"), \
         patch.object(exporter, "write_last_timestamp", side_effect=write_and_stop), \
         patch("logs_exporter.pihole6_logs_exporter.time.time", return_value=200), \
         patch("logs_exporter.pihole6_logs_exporter.requests.post", side_effect=[failure, successful_response()]) as post:
        run_supervised(exporter, stopped)

    assert post.call_count == 2
    assert fetch.call_count == 2
    assert state_file.read_text() == "150"
    assert exporter.delivery_health.consecutive_failures_count == 0


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_continuous_mode_retries_transient_http_status_then_succeeds(tmp_path, status_code):
    exporter, state_file = make_exporter(tmp_path)
    stopped = threading.Event()
    original_write = exporter.write_last_timestamp

    def write_and_stop(timestamp):
        original_write(timestamp)
        stopped.set()

    with patch.object(exporter, "fetch_queries", return_value=[dict(QUERY)]), \
         patch.object(exporter, "resolve_hostname", return_value="client"), \
         patch.object(exporter, "write_last_timestamp", side_effect=write_and_stop), \
         patch("logs_exporter.pihole6_logs_exporter.time.time", return_value=200), \
         patch(
             "logs_exporter.pihole6_logs_exporter.requests.post",
             side_effect=[Mock(status_code=status_code), successful_response()],
         ) as post:
        run_supervised(exporter, stopped)

    assert post.call_count == 2
    assert state_file.read_text() == "150"


@pytest.mark.parametrize("status_code", [400, 401, 403])
def test_continuous_mode_fails_fast_for_permanent_http_status(tmp_path, status_code):
    exporter, state_file = make_exporter(tmp_path)

    with patch.object(exporter, "fetch_queries", return_value=[dict(QUERY)]), \
         patch.object(exporter, "resolve_hostname", return_value="client"), \
         patch("logs_exporter.pihole6_logs_exporter.time.time", return_value=200), \
         patch(
             "logs_exporter.pihole6_logs_exporter.requests.post",
             return_value=Mock(status_code=status_code, text="secret response body"),
        ):
        with pytest.raises(LokiPushError) as error:
            run_supervised(exporter, threading.Event())

    assert error.value.transient is False
    assert error.value.status_code == status_code
    assert "secret response body" not in str(error.value)
    assert state_file.read_text() == "100"


def test_invalid_loki_configuration_fails_before_continuous_loop(tmp_path):
    exporter, state_file = make_exporter(tmp_path, loki_target="loki-without-a-scheme")

    with pytest.raises(ValueError, match="complete URL"):
        run_supervised(exporter, threading.Event())

    assert state_file.read_text() == "100"


def test_failed_push_never_advances_watermark(tmp_path):
    exporter, state_file = make_exporter(tmp_path)

    with patch.object(exporter, "fetch_queries", return_value=[dict(QUERY)]), \
         patch.object(exporter, "resolve_hostname", return_value="client"), \
         patch("logs_exporter.pihole6_logs_exporter.time.time", return_value=200), \
         patch(
             "logs_exporter.pihole6_logs_exporter.requests.post",
             side_effect=requests.exceptions.ConnectionError("down"),
         ):
        with pytest.raises(LokiPushError):
            exporter.run()

    assert state_file.read_text() == "100"


def test_retry_delay_is_exponential_jittered_and_capped():
    retry_policy = RetryPolicy(initial_delay=2, max_delay=10, jitter_ratio=0.25)
    delay_low = retry_policy.delay(3, random_value=0)
    delay_high = retry_policy.delay(3, random_value=1)
    capped = retry_policy.delay(10, random_value=1)

    assert delay_low == 6
    assert delay_high == 8
    assert capped == 10


def test_signal_event_interrupts_backoff_promptly(tmp_path):
    exporter, _ = make_exporter(tmp_path)
    stop_event = threading.Event()
    attempted = threading.Event()

    def fail_once():
        exporter.delivery_health.record_push_failure()
        attempted.set()
        raise LokiPushError("connection_error", transient=True)

    with patch.object(exporter, "run", side_effect=fail_once):
        worker = threading.Thread(
            target=run_supervised,
            kwargs={
                "exporter": exporter,
                "poll_interval": 30,
                "retry_initial_delay": 30,
                "retry_max_delay": 30,
                "retry_jitter_ratio": 0,
                "stop_event": stop_event,
            },
        )
        worker.start()
        assert attempted.wait(timeout=1)
        stop_event.set()
        worker.join(timeout=1)

    assert not worker.is_alive()


@pytest.mark.parametrize("shutdown_signal", [signal.SIGTERM, signal.SIGINT])
def test_cli_signal_interrupts_backoff_and_exits_cleanly(tmp_path, shutdown_signal):
    now = int(time.time())
    FakePiholeHandler.query_time = now - 30
    AlwaysUnavailableLokiHandler.attempted = threading.Event()
    state_file = tmp_path / "exporter.state"
    state_file.write_text(str(now - 60))

    with running_http_server(FakePiholeHandler) as pihole, \
         running_http_server(AlwaysUnavailableLokiHandler) as loki:
        process = subprocess.Popen(
            [
                sys.executable,
                str(EXPORTER_SCRIPT),
                "--continuous",
                "--host",
                f"http://127.0.0.1:{pihole.server_address[1]}",
                "--loki-target",
                f"http://127.0.0.1:{loki.server_address[1]}",
                "--state-file",
                str(state_file),
                "--log-file",
                "",
                "--retry-initial-delay",
                "30",
                "--retry-max-delay",
                "30",
                "--metrics-port",
                str(unused_local_port()),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=clean_subprocess_environment(),
        )
        try:
            assert AlwaysUnavailableLokiHandler.attempted.wait(timeout=3)
            process.send_signal(shutdown_signal)
            _stdout, stderr = process.communicate(timeout=2)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=1)

    assert process.returncode == 0, stderr
    assert state_file.read_text() == str(now - 60)


def test_delivery_metrics_are_machine_readable_and_reset_after_success(tmp_path):
    exporter, _ = make_exporter(tmp_path)
    exporter.persisted_watermark = 100
    exporter.delivery_health.set_watermark(100)
    exporter.delivery_health.record_push_failure()

    with patch("logs_exporter.pihole6_logs_exporter.time.time", return_value=160), \
         patch("logs_exporter.pihole6_logs_exporter.requests.post", return_value=successful_response()):
        exporter.send_to_loki([{"stream": {}, "values": [["1", "{}"]]}])
        metrics = generate_latest(exporter.delivery_health.registry).decode()

    assert "pihole_logs_exporter_last_successful_loki_push_timestamp_seconds 160.0" in metrics
    assert "pihole_logs_exporter_consecutive_loki_push_failures 0.0" in metrics
    assert "pihole_logs_exporter_export_lag_seconds 60.0" in metrics


def test_fake_loki_recovers_without_restarting_exporter_and_preserves_watermark(tmp_path):
    class RecoveringLokiHandler(BaseHTTPRequestHandler):
        statuses = [503, 204]
        payloads = []

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            self.__class__.payloads.append(json.loads(body))
            self.send_response(self.__class__.statuses.pop(0))
            self.end_headers()

        def log_message(self, _format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), RecoveringLokiHandler)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    exporter, state_file = make_exporter(
        tmp_path,
        loki_target=f"http://127.0.0.1:{server.server_address[1]}",
    )
    stopped = threading.Event()
    original_write = exporter.write_last_timestamp
    exporter_identity = id(exporter)

    def write_and_stop(timestamp):
        original_write(timestamp)
        stopped.set()

    try:
        with patch.object(exporter, "fetch_queries", return_value=[dict(QUERY)]) as fetch, \
             patch.object(exporter, "resolve_hostname", return_value="client"), \
             patch.object(exporter, "write_last_timestamp", side_effect=write_and_stop), \
             patch("logs_exporter.pihole6_logs_exporter.time.time", return_value=200):
            run_supervised(exporter, stopped)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1)

    assert id(exporter) == exporter_identity
    assert fetch.call_count == 2
    assert len(RecoveringLokiHandler.payloads) == 2
    assert RecoveringLokiHandler.payloads[0] == RecoveringLokiHandler.payloads[1]
    assert state_file.read_text() == "150"
