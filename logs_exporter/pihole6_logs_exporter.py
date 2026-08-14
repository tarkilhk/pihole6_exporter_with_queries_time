#!/usr/bin/env python3

import os
import time
import requests
import urllib3
import logging
import argparse
import json
import random
import signal
import socket
import tempfile
import threading
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from urllib.parse import urlsplit, urlunsplit

from prometheus_client import CollectorRegistry, Gauge, start_http_server


class LokiPushError(RuntimeError):
    """A secret-safe Loki delivery failure with a retry classification."""

    def __init__(self, reason, transient, status_code=None):
        self.reason = reason
        self.transient = transient
        self.status_code = status_code
        classification = "transient" if transient else "permanent"
        status = f", HTTP {status_code}" if status_code is not None else ""
        super().__init__(f"Loki push failed ({classification}: {reason}{status})")


@dataclass(frozen=True)
class RetryPolicy:
    """Validated capped exponential backoff policy with bounded jitter."""

    initial_delay: float = 1
    max_delay: float = 60
    jitter_ratio: float = 0.2

    def __post_init__(self):
        if self.initial_delay <= 0 or self.max_delay <= 0:
            raise ValueError("retry delays must be positive")
        if self.initial_delay > self.max_delay:
            raise ValueError("initial retry delay cannot exceed maximum retry delay")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("retry jitter must be between 0 and 1")

    def delay(self, attempt, random_value=None):
        if attempt < 1:
            raise ValueError("attempt must be at least 1")
        random_value = random.random() if random_value is None else random_value
        if not 0 <= random_value <= 1:
            raise ValueError("random_value must be between 0 and 1")
        exponential = self.initial_delay * (2 ** min(attempt - 1, 62))
        capped = min(self.max_delay, exponential)
        return capped * ((1 - self.jitter_ratio) + (self.jitter_ratio * random_value))


class DeliveryHealth:
    """Own delivery state, Prometheus metrics, and metrics-server lifecycle."""

    def __init__(self):
        self.watermark = None
        self.consecutive_failures_count = 0
        self._metrics_server = None
        self._metrics_thread = None
        self.registry = CollectorRegistry()
        self.last_success = Gauge(
            "pihole_logs_exporter_last_successful_loki_push_timestamp_seconds",
            "Unix timestamp of the last successful Loki push.",
            registry=self.registry,
        )
        self.consecutive_failures = Gauge(
            "pihole_logs_exporter_consecutive_loki_push_failures",
            "Number of consecutive Loki push failures.",
            registry=self.registry,
        )
        self.export_lag = Gauge(
            "pihole_logs_exporter_export_lag_seconds",
            "Seconds between the current time and the persisted export watermark.",
            registry=self.registry,
        )
        self.export_lag.set_function(
            lambda: max(0, time.time() - self.watermark)
            if self.watermark is not None
            else 0
        )

    def set_watermark(self, timestamp):
        self.watermark = timestamp

    def record_push_failure(self):
        self.consecutive_failures_count += 1
        self.consecutive_failures.set(self.consecutive_failures_count)

    def record_push_success(self):
        self.consecutive_failures_count = 0
        self.consecutive_failures.set(0)
        self.last_success.set(time.time())

    def start_server(self, address, port):
        self._metrics_server, self._metrics_thread = start_http_server(
            port,
            addr=address,
            registry=self.registry,
        )
        logging.info("Delivery metrics listening on http://%s:%d/metrics", address, port)

    def stop_server(self):
        if self._metrics_server is None:
            return
        self._metrics_server.shutdown()
        self._metrics_server.server_close()
        self._metrics_thread.join(timeout=1)
        self._metrics_server = None
        self._metrics_thread = None


class ContinuousExporter:
    """Supervise one-shot exports and retry only transient Loki failures."""

    def __init__(
        self,
        exporter,
        delivery_health,
        retry_policy=None,
        poll_interval=30,
        stop_event=None,
        random_fn=random.random,
    ):
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.exporter = exporter
        self.delivery_health = delivery_health
        self.retry_policy = retry_policy or RetryPolicy()
        self.poll_interval = poll_interval
        self.stop_event = stop_event or threading.Event()
        self.random_fn = random_fn

    def run(self):
        logging.info("Starting continuous export mode with poll interval %.3fs", self.poll_interval)

        while not self.stop_event.is_set():
            try:
                self.exporter.run()
            except LokiPushError as error:
                if not error.transient:
                    logging.error(
                        "Permanent Loki failure; continuous mode is stopping: classification=permanent reason=%s status=%s",
                        error.reason,
                        error.status_code if error.status_code is not None else "none",
                    )
                    raise
                attempt = self.delivery_health.consecutive_failures_count
                delay = self.retry_policy.delay(attempt, self.random_fn())
                logging.warning(
                    "Retrying Loki push: classification=transient reason=%s status=%s attempt=%d next_retry_seconds=%.3f",
                    error.reason,
                    error.status_code if error.status_code is not None else "none",
                    attempt,
                    delay,
                )
                if self.stop_event.wait(delay):
                    break
                continue

            if self.stop_event.wait(self.poll_interval):
                break

        logging.info("Continuous export mode stopped cleanly")


def safe_url(url):
    """Return only a URL's scheme and host so logs cannot expose credentials."""
    if not url:
        return "<unset>"
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        if ":" in hostname:
            hostname = f"[{hostname}]"
        if parsed.port:
            hostname = f"{hostname}:{parsed.port}"
        return urlunsplit((parsed.scheme, hostname, "", "", ""))
    except (TypeError, ValueError):
        return "<invalid URL>"


class PiholeLogsExporter:
    """
    Exports Pi-hole query logs to a Loki-compatible endpoint (like Grafana Alloy).
    """
    CACHE_TTL = 3600
    DEFAULT_LOOKBACK_SECONDS = 1800

    def __init__(self, host, key, loki_target, state_file, server_name=None, delivery_health=None):
        # Prefer environment variable if set, otherwise use provided host
        env_host = os.getenv('PIHOLE_URL')
        if env_host:
            host = env_host
        elif host is None or host == '':
            raise ValueError("PIHOLE_URL environment variable must be set, or --host argument must be provided")

        self.host = host.rstrip('/')  # Remove trailing slash if present
        self.key = key
        self.loki_target = loki_target
        self.state_file = state_file
        # Use provided server_name if set and not empty, otherwise default to hostname
        self.server_name = (server_name.strip() if server_name and server_name.strip() else None) or socket.gethostname() or 'unknown'
        self.using_auth = False
        self.sid = None
        self.hostname_cache = {}
        self.persisted_watermark = None
        self.delivery_health = delivery_health

        # Disable SSL warnings for self-signed certificates
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        logging.info(
            "Initializing Pi-hole Logs Exporter with host: %s, loki_target: %s, state_file: %s",
            host,
            safe_url(loki_target),
            state_file,
        )

        if key is not None:
            self.using_auth = True
            logging.info("Authentication enabled - will attempt to get session ID")
            self.sid = self.get_sid(key)
        else:
            logging.warning("No API token provided. Pi-hole v6 may require authentication to access query logs. Some information may not be available.")

    def get_sid(self, key):
        """Authenticates with the Pi-hole API and returns a session ID."""
        auth_url = f"{self.host}/api/auth"
        headers = {"accept": "application/json", "content-type": "application/json"}
        json_data = {"password": key}
        logging.info(f"Attempting to authenticate with Pi-hole API at {auth_url}")
        try:
            req = requests.post(auth_url, verify=False, headers=headers, json=json_data, timeout=10)

            # Handle HTTP 401 (Unauthorized) - some Pi-hole instances return this for auth failures
            if req.status_code == 401:
                try:
                    reply = req.json()
                    session = reply.get('session', {})
                    error_msg = session.get('message', 'Authentication failed (HTTP 401)')
                    logging.error(f"Authentication failed with HTTP 401: {error_msg}")
                    raise ValueError(f"Authentication failed: {error_msg}")
                except (json.JSONDecodeError, KeyError):
                    logging.error(f"Authentication failed with HTTP 401. Response: {req.text}")
                    raise ValueError("Authentication failed: HTTP 401 Unauthorized")

            # For other status codes, raise if not successful
            req.raise_for_status()
            reply = req.json()

            # Extract session ID with better error handling
            # Handle both response formats:
            # 1. HTTP 401 (already handled above)
            # 2. HTTP 200 with error message in body (some instances)
            try:
                session = reply.get('session', {})
                sid = session.get('sid')
                is_valid = session.get('valid', False)
                error_msg = session.get('message', '')

                # Determine if authentication failed based on multiple indicators
                # Some instances return HTTP 200 but indicate failure in the response body
                auth_failed = False
                failure_reason = None

                # Check 1: Session marked as invalid
                if not is_valid:
                    auth_failed = True
                    failure_reason = error_msg or 'Session is not valid'

                # Check 2: Session ID is null/empty AND there's an error message
                elif not sid or not sid.strip():
                    auth_failed = True
                    failure_reason = error_msg or 'missing valid session'

                # Check 3: Error message indicates failure (catch-all for any error messages)
                elif error_msg and any(keyword in error_msg.lower() for keyword in
                                      ['incorrect', 'invalid', 'failed', 'error', 'unauthorized', 'denied']):
                    auth_failed = True
                    failure_reason = error_msg

                # If authentication failed, raise error
                if auth_failed:
                    logging.error(f"Authentication failed: {failure_reason}")
                    raise ValueError(f"Authentication failed: {failure_reason}")

                # Success case: valid session with non-empty session ID
                if sid and sid.strip():
                    logging.info("Successfully authenticated with Pi-hole API.")
                    logging.info(f"Session ID obtained: {sid[:20]}... (length: {len(sid)})")
                    return sid
            except KeyError as e:
                logging.error(f"Session ID not found in API response. Response keys: {list(reply.keys())}")
                # Try alternative response structures
                if 'session' in reply:
                    logging.error(f"Session object exists but structure is different: {reply['session']}")
                raise ValueError(f"Invalid authentication response structure: {e}")
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to authenticate with Pi-hole API: {e}")
            raise

    def logout(self):
        """Logs out from the Pi-hole API session."""
        # Check if already logged out (idempotent)
        if not self.using_auth:
            logging.debug("Authentication not enabled, skipping logout.")
            return

        if not self.sid:
            logging.warning("Session ID is None when logout called. This may indicate a problem with session management.")
            logging.warning(f"Current state: using_auth={self.using_auth}, sid={self.sid}")
            return

        # Store session ID before clearing it
        session_id = self.sid
        logging.info(f"Logging out with session ID: {session_id[:20]}...")
        logout_url = f"{self.host}/api/auth?sid={session_id}"
        headers = {"accept": "application/json"}

        # Clear session ID immediately to prevent double logout attempts
        self.sid = None
        self.using_auth = False

        try:
            req = requests.delete(logout_url, verify=False, headers=headers, timeout=10)
            req.raise_for_status()
            logging.info("Successfully logged out from Pi-hole API session.")
        except requests.exceptions.RequestException as e:
            logging.warning(f"Failed to log out from Pi-hole API: {e}")

    def get_api_call(self, api_path, _retry_on_auth_failure=True):
        """Makes a GET request to the Pi-hole API."""
        url = f"{self.host}/api/{api_path}"
        headers = {"accept": "application/json"}
        if self.using_auth and self.sid:
            headers["sid"] = self.sid
            logging.debug(f"Using session ID for API call: {self.sid[:10]}...")
        elif self.using_auth and not self.sid:
            logging.warning("Authentication enabled but session ID is missing for API call")

        logging.info(f"Making API call to: {url}")
        try:
            req = requests.get(url, verify=False, headers=headers, timeout=30)

            if req.status_code == 401 and self.using_auth and _retry_on_auth_failure:
                logging.warning("Pi-hole API returned 401; session likely expired. Re-authenticating and retrying...")
                self.sid = self.get_sid(self.key)
                return self.get_api_call(api_path, _retry_on_auth_failure=False)

            req.raise_for_status()
            reply = req.json()
            logging.debug(f"API response for {api_path}: {reply}")
            return reply
        except requests.exceptions.RequestException as e:
            logging.error(f"API call failed: {e}")
            raise
        except json.JSONDecodeError:
            logging.error(f"Failed to parse JSON response from {api_path}. Raw response: {req.text}")
            raise

    def read_last_timestamp(self):
        """Reads the last successfully processed timestamp from the state file."""
        try:
            with open(self.state_file, 'r') as f:
                content = f.read().strip()
                if not content:
                    ts = int(time.time()) - self.DEFAULT_LOOKBACK_SECONDS
                    self.persisted_watermark = ts
                    if self.delivery_health:
                        self.delivery_health.set_watermark(ts)
                    logging.info(f"State file {self.state_file} is empty. Starting from {self.DEFAULT_LOOKBACK_SECONDS} seconds ago (timestamp {ts}).")
                    return ts
                timestamp = int(content)
                self.persisted_watermark = timestamp
                if self.delivery_health:
                    self.delivery_health.set_watermark(timestamp)
                logging.info(f"Read last timestamp from state file: {timestamp} ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp))})")
                return timestamp
        except FileNotFoundError:
            ts = int(time.time()) - self.DEFAULT_LOOKBACK_SECONDS
            self.persisted_watermark = ts
            if self.delivery_health:
                self.delivery_health.set_watermark(ts)
            logging.info(f"State file not found at {self.state_file}. Starting from {self.DEFAULT_LOOKBACK_SECONDS} seconds ago (timestamp {ts}).")
            return ts
        except (ValueError, TypeError) as e:
            logging.error(f"Invalid timestamp in state file: {e}. Starting fresh.")
            ts = int(time.time()) - self.DEFAULT_LOOKBACK_SECONDS
            self.persisted_watermark = ts
            if self.delivery_health:
                self.delivery_health.set_watermark(ts)
            return ts

    def write_last_timestamp(self, timestamp):
        """Atomically writes the latest timestamp to the state file."""
        state_path = os.path.abspath(self.state_file)
        state_dir = os.path.dirname(state_path)
        temp_path = None
        try:
            os.makedirs(state_dir, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(prefix=f".{os.path.basename(state_path)}.", dir=state_dir)
            with os.fdopen(fd, 'w') as f:
                f.write(str(timestamp))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, state_path)
            temp_path = None
            self.persisted_watermark = timestamp
            if self.delivery_health:
                self.delivery_health.set_watermark(timestamp)
            logging.info(f"Wrote timestamp to state file: {timestamp} ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp))})")
        except OSError as e:
            logging.error(f"Error writing to state file {self.state_file}: {e}")
            raise
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

    def resolve_hostname(self, ip):
        """Resolve an IP address to a hostname with caching. Returns only the short hostname."""
        now = time.time()
        cached = self.hostname_cache.get(ip)
        if cached and now - cached[1] < self.CACHE_TTL:
            return cached[0]
        try:
            fqdn = socket.gethostbyaddr(ip)[0]
            hostname = fqdn.split('.')[0] if fqdn else ip
        except Exception:
            hostname = ip
        self.hostname_cache[ip] = (hostname, now)
        return hostname

    def _flatten_dict(self, d: dict, parent_key: str = '', sep: str = '_') -> dict:
        """
        Flattens a nested dictionary.
        Example: {'a': {'b': 1}} -> {'a_b': 1}
        """
        items = []
        for k, v in d.items():
            new_key = parent_key + sep + k if parent_key else k
            if isinstance(v, dict):
                items.extend(self._flatten_dict(v, new_key, sep=sep).items())
            else:
                items.append((new_key, v))
        return dict(items)

    def fetch_queries(self, from_ts, until_ts):
        """Fetches queries from the Pi-hole API within a given time range."""
        logging.info(f"Fetching queries from {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(from_ts + 1))} to {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(until_ts))}")
        api_path = f"queries?from={from_ts + 1}&until={until_ts}&length=1000000"
        reply = self.get_api_call(api_path)
        queries = reply.get("queries", [])
        logging.info(f"Fetched {len(queries)} new queries.")
        if len(queries) == 0:
            logging.debug(f"API response structure: {list(reply.keys())}")
            logging.debug(f"Full API response (first 500 chars): {str(reply)[:500]}")
        return queries

    def format_for_loki(self, queries):
        """Formats Pi-hole queries into Loki stream format."""
        streams = {}
        max_timestamp = 0

        logging.info(f"Formatting {len(queries)} queries for Loki")

        for q in queries:
            # The query time from Pi-hole v6 API is in the 'time' field as a Unix timestamp.
            q_ts = q.get('time')
            if not q_ts:
                logging.warning(f"Query missing 'time' field, skipping: {q}")
                continue

            q_ts = int(float(q_ts))
            max_timestamp = max(max_timestamp, q_ts)
            ts_ns = str(q_ts * 1_000_000_000)

            client_ip = q.get('client', {}).get('ip', 'unknown')
            client_name = self.resolve_hostname(client_ip)

            # Update the original query dict with the short hostname so it's reflected in the flattened log line.
            if 'client' in q and isinstance(q['client'], dict):
                q['client']['name'] = client_name

            # Create stream labels for Loki.
            stream_labels = {
                "job": "pihole_logs_exporter",
                "service": "pihole_query_log",
                "server": self.server_name,
                "host": self.server_name,
                "type": q.get('type', 'unknown'),
                "status": q.get('status', 'unknown'),
            }
            structured_metadata = {
                "domain": str(q.get('domain', 'unknown')),
                "client_ip": str(client_ip),
                "client_name": str(client_name),
            }

            labels_tuple = tuple(sorted(stream_labels.items()))

            if labels_tuple not in streams:
                streams[labels_tuple] = {
                    "stream": stream_labels,
                    "values": []
                }

            flat_q = self._flatten_dict(q)
            log_line = json.dumps(flat_q, separators=(",", ":"), sort_keys=True)
            streams[labels_tuple]["values"].append([ts_ns, log_line, structured_metadata])

        logging.info(f"Formatted {len(streams)} unique streams for Loki")
        return list(streams.values()), max_timestamp

    def send_to_loki(self, streams):
        """Sends a batch of log streams to the Loki endpoint."""
        if not streams:
            logging.info("No new logs to send to Loki.")
            return

        payload = {"streams": streams}
        headers = {"Content-Type": "application/json"}
        loki_url = self.get_loki_url()

        logging.info(f"Sending {len(streams)} streams to Loki at {safe_url(loki_url)}")
        log_count = sum(len(s['values']) for s in streams)
        logging.info(f"Total log entries to send: {log_count}")

        try:
            response = requests.post(loki_url, data=json.dumps(payload), headers=headers, timeout=15)
        except requests.exceptions.Timeout:
            self._record_loki_failure()
            logging.warning("Loki push classification=transient reason=timeout")
            raise LokiPushError("timeout", transient=True) from None
        except requests.exceptions.ConnectionError:
            self._record_loki_failure()
            logging.warning("Loki push classification=transient reason=connection_error")
            raise LokiPushError("connection_error", transient=True) from None
        except requests.exceptions.RequestException:
            self._record_loki_failure()
            logging.error("Loki push classification=permanent reason=request_error")
            raise LokiPushError("request_error", transient=False) from None

        status_code = response.status_code
        if 200 <= status_code < 300:
            if self.delivery_health:
                self.delivery_health.record_push_success()
            logging.info(f"Successfully sent {log_count} log entries to Loki.")
            return

        transient = status_code == 429 or 500 <= status_code < 600
        classification = "transient" if transient else "permanent"
        self._record_loki_failure()
        logging.log(
            logging.WARNING if transient else logging.ERROR,
            "Loki push classification=%s reason=http_status status=%d",
            classification,
            status_code,
        )
        raise LokiPushError("http_status", transient=transient, status_code=status_code)

    def _record_loki_failure(self):
        if self.delivery_health:
            self.delivery_health.record_push_failure()

    def get_loki_url(self):
        """Generate the full Loki URL from the target."""
        # Validate that loki_target is provided and not empty
        if not self.loki_target or self.loki_target.strip() == "":
            raise ValueError("LOKI_URL environment variable is not set or is empty")

        try:
            parsed = urlsplit(self.loki_target)
        except ValueError as e:
            raise ValueError("LOKI_URL is invalid. Please provide a complete HTTP(S) URL") from e
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("LOKI_URL must be a complete URL with http:// or https:// and a hostname")

        return f"{self.loki_target.rstrip('/')}/loki/api/v1/push"

    def run(self):
        """Main execution logic."""
        logging.info("Starting Pi-hole log export run.")
        # Verify session ID is still valid if authentication is enabled
        if self.using_auth:
            if self.sid:
                logging.debug(f"Session ID verified at start of run: {self.sid[:20]}...")
            else:
                logging.error("Session ID is missing at start of run! This should not happen.")

        loki_url = self.get_loki_url()
        logging.info(f"Loki target validated: {safe_url(loki_url)}")

        last_ts = self.read_last_timestamp()
        current_ts = int(time.time())

        logging.info(f"Current timestamp: {current_ts} ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(current_ts))})")

        if last_ts >= current_ts:
            logging.info("Last timestamp is current, no new logs to fetch.")
            return

        queries = self.fetch_queries(last_ts, current_ts)

        if not queries:
            self.write_last_timestamp(current_ts)
            logging.info("No new queries found in the time range.")
            return

        loki_streams, max_ts = self.format_for_loki(queries)

        if loki_streams:
            self.send_to_loki(loki_streams)
            self.write_last_timestamp(max_ts)
        else:
            # No valid queries were formatted, but we should still advance the timestamp
            self.write_last_timestamp(current_ts)

        logging.info("Pi-hole log export run finished.")

def setup_logging(log_level, log_file=None):
    """Setup logging with both console and file handlers."""
    # Create formatter
    formatter = logging.Formatter('time="%(asctime)s" level="%(levelname)s" message="%(message)s"')

    # Create logger
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, log_level.upper()))

    # urllib3 DEBUG records include raw HTTP request targets, which may carry
    # Loki tenant paths or query-string credentials. Keep transport logs above
    # DEBUG even when application-level debugging is enabled.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # Clear any existing handlers
    logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler (if specified)
    if log_file:
        # Ensure log directory exists
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            try:
                os.makedirs(log_dir, exist_ok=True)
                logging.info(f"Created log directory: {log_dir}")
            except Exception as e:
                logging.error(f"Failed to create log directory {log_dir}: {e}")

        # Create rotating file handler
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=5*1024*1024,  # 5MB
            backupCount=5
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Pi-hole v6 Log Exporter for Loki.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("-H", "--host", dest="host", type=str, required=False, default=os.getenv("PIHOLE_URL"),
                        help="Full URL of the Pi-hole instance. Defaults to PIHOLE_URL env var if not provided.")
    parser.add_argument("-k", "--key", dest="key", type=str, required=False, default=os.getenv("PIHOLE_API_TOKEN"),
                        help="Pi-hole API token. Can also be set via PIHOLE_API_TOKEN env var.")
    parser.add_argument("-t", "--loki-target", dest="loki_target", type=str, required=False, default=os.getenv("LOKI_TARGET"),
                        help="Base URL of the Loki/Alloy server (e.g., http://localhost:3100). Can also be set via LOKI_TARGET env var.")
    parser.add_argument("-s", "--state-file", dest="state_file", type=str, required=False, default="/var/tmp/pihole_logs_exporter.state",
                        help="Path to the state file for storing the last timestamp.")
    parser.add_argument("--server", dest="server_name", type=str, required=False, default=os.getenv("SERVER_NAME"),
                        help="Server identifier for Loki labels (e.g., 'pihole-vm', 'tarkilnas'). Defaults to hostname if not set. Can also be set via SERVER_NAME env var.")
    parser.add_argument("-l", "--log-level", dest="log_level", type=str, required=False, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Set the logging level.")
    parser.add_argument("--log-file", dest="log_file", type=str, required=False, default="/var/log/pihole6_exporter/pihole_logs_exporter.log",
                        help="Path to the log file for detailed logging.")
    parser.add_argument("--continuous", action="store_true",
                        help="Keep running and retry transient Loki failures. One-shot mode remains the default.")
    parser.add_argument("--poll-interval", type=float, default=30,
                        help="Seconds between successful runs in continuous mode.")
    parser.add_argument("--retry-initial-delay", type=float, default=1,
                        help="Initial retry delay in seconds for transient Loki failures.")
    parser.add_argument("--retry-max-delay", type=float, default=60,
                        help="Maximum retry delay in seconds for transient Loki failures.")
    parser.add_argument("--retry-jitter", type=float, default=0.2,
                        help="Downward jitter ratio from 0 to 1 for retry delays.")
    parser.add_argument("--metrics-address", default="0.0.0.0",
                        help="Address for the continuous-mode Prometheus endpoint.")
    parser.add_argument("--metrics-port", type=int, default=9101,
                        help="Port for the continuous-mode Prometheus endpoint.")

    args = parser.parse_args()

    # Validate required arguments
    if not args.loki_target:
        parser.error("LOKI_TARGET must be provided either via -t/--loki-target argument or LOKI_TARGET environment variable")
    retry_policy = None
    if args.continuous:
        if args.poll_interval <= 0:
            parser.error("--poll-interval must be positive")
        if not 1 <= args.metrics_port <= 65535:
            parser.error("--metrics-port must be between 1 and 65535")
        try:
            retry_policy = RetryPolicy(
                initial_delay=args.retry_initial_delay,
                max_delay=args.retry_max_delay,
                jitter_ratio=args.retry_jitter,
            )
        except ValueError as error:
            parser.error(str(error))

    # Setup logging
    setup_logging(args.log_level, args.log_file)
    logging.info("=== Pi-hole Logs Exporter Starting ===")
    logging.info(f"Configuration:")
    logging.info(f"  Pi-hole host: {args.host}")
    logging.info(f"  Loki target: {safe_url(args.loki_target)}")
    logging.info(f"  State file: {args.state_file}")
    logging.info(f"  Server name: {args.server_name or socket.gethostname() or 'unknown'}")
    logging.info(f"  Log level: {args.log_level}")
    logging.info(f"  Log file: {args.log_file}")
    logging.info(f"  Mode: {'continuous' if args.continuous else 'one-shot'}")

    exporter = None
    delivery_health = DeliveryHealth() if args.continuous else None
    try:
        logging.info("Creating Pi-hole Logs Exporter instance...")
        exporter = PiholeLogsExporter(
            host=args.host,
            key=args.key,
            loki_target=args.loki_target,
            state_file=args.state_file,
            server_name=args.server_name,
            delivery_health=delivery_health,
        )
        if args.continuous:
            stop_event = threading.Event()

            def request_shutdown(signum, _frame):
                logging.info("Received signal %s; requesting clean shutdown", signum)
                stop_event.set()

            signal.signal(signal.SIGTERM, request_shutdown)
            signal.signal(signal.SIGINT, request_shutdown)
            delivery_health.start_server(args.metrics_address, args.metrics_port)
            supervisor = ContinuousExporter(
                exporter=exporter,
                delivery_health=delivery_health,
                retry_policy=retry_policy,
                poll_interval=args.poll_interval,
                stop_event=stop_event,
            )
            supervisor.run()
        else:
            logging.info("Starting one-shot log export run...")
            exporter.run()
        logging.info("=== Pi-hole Logs Exporter Completed Successfully ===")
    except LokiPushError as e:
        logging.critical("Exporter failed to deliver logs: %s", e)
        logging.error("=== Pi-hole Logs Exporter Failed ===")
        exit(1)
    except Exception as e:
        logging.critical(f"Exporter failed to initialize or run: {e}", exc_info=True)
        logging.error("=== Pi-hole Logs Exporter Failed ===")
        exit(1)
    finally:
        if delivery_health:
            delivery_health.stop_server()
        # Ensure logout happens even if initialization fails
        if exporter:
            logging.info("Ensuring logout from Pi-hole API session")
            exporter.logout()
