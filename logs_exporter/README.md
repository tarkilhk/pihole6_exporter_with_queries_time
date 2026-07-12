# Pi-hole v6 Logs Exporter

This directory contains the Pi-hole v6 logs exporter that exports query logs to Loki-compatible endpoints.

## Files

- `pihole6_logs_exporter.py` - Main logs exporter script
- `pihole6_logs_exporter.timer` - Systemd timer to run the service periodically

## Usage

The logs exporter fetches Pi-hole query logs and sends them to a Loki-compatible endpoint (like Grafana Alloy) for log aggregation and analysis.

### Installation

1. Copy the timer file to systemd:
   ```bash
   sudo cp pihole6_logs_exporter.timer /etc/systemd/system/
   ```

2. Enable and start the timer:
   ```bash
   sudo systemctl enable pihole6_logs_exporter.timer
   sudo systemctl start pihole6_logs_exporter.timer
   ```

### Configuration

The timer uses environment variables that can be set in `etc/pihole6_exporter/pihole6_exporter.env`:

- `PIHOLE_URL` - Pi-hole server URL (default: http://localhost:80)
- `PIHOLE_API_TOKEN` - Pi-hole API token for authentication
- `LOKI_TARGET` - Loki server URL (e.g., http://localhost:3100/loki/api/v1/push)
- `SERVER_NAME` - Stable server identifier for Loki `host` and `server` labels (e.g., 'pihole-vm', 'tarkilnas'). Defaults to hostname if not set.
- `STATE_FILE` - Path to state file for tracking last processed timestamp (default: /var/tmp/pihole_logs_exporter.state)

### Manual Execution

```bash
python pihole6_logs_exporter.py -H http://localhost:80 -k YOUR_API_TOKEN -t http://localhost:3100 --server pihole-vm
```

## Features

- Exports Pi-hole query logs to Loki format
- Keeps high-cardinality DNS fields (`domain`, `client_ip`, `client_name`) out of Loki stream labels and sends them as structured metadata plus JSON log fields
- Uses a stable `SERVER_NAME` for Loki `host` / `server` labels
- Maintains state to avoid duplicate log entries
- Resolves client IPs to hostnames
- Configurable initial history fetch
- Exits non-zero on authentication, API, Loki push, or state-write failures
- Runs every 30 seconds via systemd timer
