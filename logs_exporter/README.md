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

- `PIHOLE_API_TOKEN` - Pi-hole API token
- `LOKI_URL` - Loki endpoint URL (default: http://localhost:3100/loki/api/v1/push)
- `STATE_FILE` - State file location (default: /var/tmp/pihole_logs_exporter.state)
- `INITIAL_MINUTES` - Initial history fetch in minutes (default: 5)

### Manual Execution

```bash
python pihole6_logs_exporter.py -H localhost -k YOUR_API_TOKEN -u http://localhost:3100/loki/api/v1/push
```

## Features

- Exports Pi-hole query logs to Loki format
- Maintains state to avoid duplicate log entries
- Resolves client IPs to hostnames
- Configurable initial history fetch
- Automatic retry on failures
- Runs every 30 seconds via systemd timer 