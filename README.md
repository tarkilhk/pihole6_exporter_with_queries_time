# pihole6_exporter: A Prometheus-style Exporter for Pi-hole ver. 6

This is a comprehensive Prometheus exporter for the new API in Pi-hole version 6, currently in beta.

[There is a Grafana Dashboard as well!](https://grafana.com/grafana/dashboards/21043-pi-hole-ver6-stats/)

## Features

✅ **DNS Latency Monitoring** - Histogram metrics for DNS response times  
✅ **DNS Error Tracking** - Error counters by response code (SERVFAIL, NXDOMAIN, etc.)  
✅ **Cache Hit Ratio** - Raw cache metrics for flexible rate calculations  
✅ **Timeout Detection** - DNS timeout monitoring and alerting  
✅ **Prometheus Best Practices** - Raw counters instead of computed rates  
✅ **Comprehensive Testing** - Full test suite with mock data  

## pihole6_exporter

### Running

```
usage: pihole6_exporter [-h] [-H HOST] [-p PORT] [-k KEY] [-l LOG_LEVEL]

Prometheus exporter for Pi-hole version 6+

optional arguments:
  -h, --help            show this help message and exit
  -H HOST, --host HOST  hostname/ip of pihole instance (default localhost)
  -p PORT, --port PORT  port to expose for scraping (default 9666)
  -k KEY, --key KEY     authentication token (if required)
  -l LOG_LEVEL, --log-level LOG_LEVEL
                        logging level (DEBUG, INFO, WARNING, ERROR)
```

### API Token Configuration

The exporter supports multiple methods for providing the Pi-hole API token:

1. **Command line argument**: Use `-k` or `--key` parameter
2. **Environment variable**: Set `PIHOLE_API_TOKEN` environment variable
3. **Token file**: Place the token in `/etc/pihole6_exporter/pihole6_exporter.env`

If using locally and you have the `Local clients need to authenticate to access the API` option un-selected, a key is not necessary. This key is the "app password", not the session ID that is created with it.

The session ID should stay active as long as it is used at least every 5 minutes. A typical scrape interval is 1m. Currently, if the session ID expires, a restart of the exporter is necessary.

### Requirements

* Python 3.7+
* Dependencies listed in `requirements.txt`:
  * `prometheus-client>=0.22.1` - Prometheus metrics library
  * `requests>=2.31.0` - HTTP library for Pi-hole API calls
  * `urllib3>=2.0.7` - HTTP client library
  * `pytest>=7.4.0` - Testing framework (for development)

### Installation

* Copy the exporter itself over to `/usr/local/bin`
* Copy the systemd service file over to `/etc/systemd/system/` (or anywhere systemd will find it)
    * Modify the `Exec=` line with any command line args (like a key) as needed. Currently there is no config file.
    * For debugging, add `-l DEBUG` to the command line
* `systemctl start pihole6_exporter` to start the exporter.
* `systemctl enable pihole6_exporter` to have it start automatically.

### Metrics Provided

All metrics derive from the `/stats/summary`, `/stats/upstreams` and `/queries` API calls, minus a few stats which can be derived from these metrics (e.g. the % of domains blocked).

#### 24-Hour Summary Metrics

These are per-24h metrics provided by the API, like the ones used in the stats on the web admin dashboard.

| Metric | Description | Labels |
|--------|-------------|--------|
| `pihole_query_by_type` | Count of queries by type over the last 24h | `query_type` (A, AAAA, SOA, etc.) |
| `pihole_query_by_status` | Count of queries by status over the last 24h | `query_status` (FORWARDED, CACHE, GRAVITY, etc.) |
| `pihole_query_replies` | Count of query replies by type over the last 24h | `reply_type` (CNAME, IP, NXDOMAIN, etc.) |
| `pihole_query_count` | Query count totals over last 24h | `category` (total, blocked, unique, forwarded, cached) |
| `pihole_client_count` | Count of total/active clients | `category` (active, total) |
| `pihole_domains_being_blocked` | Number of domains being blocked | *None* |
| `pihole_query_upstream_count` | Counts of total queries in the last 24h by upstream destination | `ip`, `port`, `name` |

#### Per-Minute Metrics

These are per-1m metrics that can be aggregated over time periods other than just 24h, and in various ways to derive the same stats as above and more. **Following Prometheus best practices, counter metrics export raw counts. Use `rate()` functions in PromQL/Grafana to compute rates.**

| Metric | Type | Description | Labels |
|--------|------|-------------|--------|
| `pihole_query_type_1m` | Gauge | Count of queries by type (1m) | `query_type` |
| `pihole_query_status_1m` | Gauge | Count of queries by status (1m) | `query_status` |
| `pihole_query_upstream_1m` | Gauge | Count of queries by upstream destination (1m) | `query_upstream` |
| `pihole_query_reply_1m` | Gauge | Count of queries by reply type (1m) | `query_reply` |
| `pihole_query_client_1m` | Gauge | Count of queries by client (1m) | `query_client` |
| `pihole_dns_latency_seconds_1m` | Histogram | DNS query response time in seconds (1m) | `status` |
| `pihole_dns_errors_1m` | Counter | DNS errors by response code (1m) | `rcode` |
| `pihole_dns_queries_processed_1m` | Counter | Total DNS queries processed (1m) | *None* |
| `pihole_dns_timeouts_1m` | Counter | DNS timeout queries (1m) | *None* |

### Using the Metrics

For detailed examples of how to use these metrics in Grafana and PromQL, including:
- DNS error rate calculations
- Cache hit ratio monitoring  
- Latency percentiles and averages
- Prometheus alerting rules

**See [METRICS_USAGE.md](METRICS_USAGE.md) for comprehensive usage examples.**

#### Quick Examples

```promql
# DNS error rate over 5 minutes
rate(pihole_dns_errors_1m[5m]) / rate(pihole_dns_queries_processed_1m[5m]) * 100

# Cache hit ratio
pihole_query_count{category="cached"} / pihole_query_count{category="total"} * 100

# 95th percentile DNS latency
histogram_quantile(0.95, rate(pihole_dns_latency_seconds_1m_bucket[5m]))

# Average DNS latency in milliseconds
rate(pihole_dns_latency_seconds_1m_sum[5m]) / rate(pihole_dns_latency_seconds_1m_count[5m]) * 1000
```

### Testing

The project includes comprehensive integration tests:

```bash
# Run all tests
python -m pytest tests/ -v

# Run specific test
python -m pytest tests/test_exporter_integration.py::test_dns_error_counters -v
```

## Questions/Comments?

Please open a git issue here. I can't promise a particular response time but I'll do my best.
