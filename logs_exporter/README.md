# Pi-hole v6 Logs Exporter

This directory contains the Pi-hole v6 logs exporter that exports query logs to Loki-compatible endpoints.

## Files

- `pihole6_logs_exporter.py` - Main logs exporter script
- `pihole6_logs_exporter.service` - Long-running systemd service

## Usage

The logs exporter fetches Pi-hole query logs and sends them to a Loki-compatible endpoint (like Grafana Alloy) for log aggregation and analysis.

### Installation

1. Copy the service file to systemd:
   ```bash
   sudo cp pihole6_logs_exporter.service /etc/systemd/system/
   ```

2. Enable and start the service:
   ```bash
   sudo systemctl enable --now pihole6_logs_exporter.service
   ```

### Configuration

The service uses environment variables that can be set in `/etc/pihole6_exporter/pihole6_exporter.env`:

- `PIHOLE_URL` - Pi-hole server URL (default: http://localhost:80)
- `PIHOLE_API_TOKEN` - Pi-hole API token for authentication
- `LOKI_TARGET` - Loki server base URL (e.g., http://localhost:3100)
- `SERVER_NAME` - Stable server identifier for Loki `host` and `server` labels (e.g., 'pihole-vm', 'tarkilnas'). Defaults to hostname if not set.
- `STATE_FILE` - Path to state file for tracking last processed timestamp (default: /var/tmp/pihole_logs_exporter.state)

### Running the exporter

The exporter always runs as a long-lived process. It polls Pi-hole every 30
seconds by default and keeps the same process alive across transient Pi-hole
API timeouts and connection failures, and across Loki DNS, connection, timeout,
HTTP 429, or HTTP 5xx failures:

```bash
python pihole6_logs_exporter.py \
  --poll-interval 30 \
  --retry-initial-delay 1 \
  --retry-max-delay 60 \
  --retry-jitter 0.2 \
  --metrics-port 9101 \
  -H http://localhost:80 \
  -k YOUR_API_TOKEN \
  -t http://loki:3100 \
  --server pihole-vm
```

Retry delays use capped exponential backoff. The jitter value is a ratio: the
default `0.2` randomly selects a delay from 80% through 100% of the capped
exponential delay. A successful export cycle resets the Pi-hole retry count. A
successful Loki push resets the Loki failure count and backoff. SIGTERM and
SIGINT interrupt both retry and polling waits so container shutdown does not
have to wait for the delay to expire.

Loki HTTP 400, 401, 403, and other non-429 4xx responses are classified as
permanent and stop the process. Pi-hole HTTP errors also fail fast. Missing or
malformed Loki URLs and invalid Pi-hole credentials fail fast as well. Logs
contain the classification, attempt number, and next delay but log only the
Loki URL scheme and host, omitting URL paths, credentials, query strings, and
response bodies.

The persisted watermark is written atomically and changes only after Loki
accepts a batch. A transient failure therefore causes the next attempt in the
same process to fetch the unsent interval again.

### Delivery health metrics

The exporter exposes Prometheus metrics at `/metrics` on port 9101 by default.
Change the listener with `--metrics-address` and `--metrics-port`.

| Metric | Meaning |
| --- | --- |
| `pihole_logs_exporter_last_successful_loki_push_timestamp_seconds` | Unix timestamp of the last accepted Loki batch; zero until the first successful push |
| `pihole_logs_exporter_consecutive_loki_push_failures` | Consecutive transient or permanent Loki push failures; reset only by an accepted batch |
| `pihole_logs_exporter_export_lag_seconds` | Current time minus the persisted watermark |

Example Prometheus alerts:

```promql
# No accepted Loki push for 10 minutes after the first successful delivery
(pihole_logs_exporter_last_successful_loki_push_timestamp_seconds > 0)
and
(time() - pihole_logs_exporter_last_successful_loki_push_timestamp_seconds > 600)

# The persisted query watermark is more than 10 minutes behind
pihole_logs_exporter_export_lag_seconds > 600

# Loki delivery is actively failing
pihole_logs_exporter_consecutive_loki_push_failures > 0
```

## Features

- Exports Pi-hole query logs to Loki format
- Keeps high-cardinality DNS fields (`domain`, `client_ip`, `client_name`) out of Loki stream labels and sends them as structured metadata plus JSON log fields
- Uses a stable `SERVER_NAME` for Loki `host` / `server` labels
- Maintains an atomic watermark state file to avoid skipped log entries
- Resolves client IPs to hostnames
- Configurable initial history fetch
- Exits non-zero on authentication, permanent Pi-hole HTTP, permanent Loki, configuration, or state-write failures
- Runs continuously with classified retry/backoff and Prometheus health metrics
- Polls every 30 seconds by default
