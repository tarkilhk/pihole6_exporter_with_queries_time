# Pi-hole v6 Metrics Exporter

This directory contains the Pi-hole v6 metrics exporter that exports Prometheus-style metrics.

## Files

- `pihole6_metrics_exporter.py` - Main metrics exporter script
- `pihole6_metrics_exporter.service` - Systemd service file
- `tests/` - Integration tests for the metrics exporter

## Usage

The metrics exporter fetches Pi-hole statistics and exposes them as Prometheus metrics on an HTTP endpoint.

### Installation

1. Copy the service file to systemd:
   ```bash
   sudo cp pihole6_metrics_exporter.service /etc/systemd/system/
   ```

2. Enable and start the service:
   ```bash
   sudo systemctl enable pihole6_metrics_exporter
   sudo systemctl start pihole6_metrics_exporter
   ```

### Configuration

The service uses environment variables that can be set in `etc/pihole6_exporter/pihole6_exporter.env`:

- `PIHOLE_API_TOKEN` - Pi-hole API token

### Manual Execution

```bash
python pihole6_metrics_exporter.py -H localhost -k YOUR_API_TOKEN -p 9090
```

### Testing

Run the integration tests:
```bash
cd tests
python -m pytest test_metrics_exporter_integration.py
```

## Features

- Exports comprehensive Pi-hole v6 metrics in Prometheus format
- DNS latency histograms with cache/forwarded labels
- DNS timeout counters
- System resource metrics (CPU, memory, disk, network)
- SD card wear monitoring
- Configurable host, port, and log level
- Automatic authentication handling

## Metrics

The exporter provides various metrics including:
- `pihole_dns_latency_seconds` - DNS query latency histogram
- `pihole_dns_timeouts_total` - DNS timeout counter
- `pihole_queries_total` - Total query count
- `pihole_blocked_queries_total` - Blocked query count
- And many more system and Pi-hole specific metrics 