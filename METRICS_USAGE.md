# Pi-hole Exporter Metrics Usage Guide

This guide explains how to use the raw metrics exported by the Pi-hole exporter to create meaningful dashboards and alerts in Grafana/Prometheus.

## Philosophy: Raw Metrics vs Computed Rates

Following Prometheus best practices, this exporter provides **raw metrics** rather than pre-computed rates. This approach offers several advantages:

- **Flexibility**: Different time windows and aggregation methods  
- **Accuracy**: Proper handling of metric resets and edge cases
- **Consistency**: Standard pattern across all Prometheus exporters
- **Alerting**: Better support for alerting rules

**Note**: The 1-minute window metrics are **gauges** that show counts from the last complete minute and reset each scrape, while histogram metrics remain as counters (which is correct for histograms).

## Complete Metrics Reference

### 24-Hour Summary Metrics (from Pi-hole API)

These metrics are based on Pi-hole's built-in 24-hour statistics:

#### Query Type Distribution
**Metric:** `pihole_query_by_type`
- **Type:** Gauge
- **Labels:** `query_type` (A, AAAA, PTR, etc.)
- **Description:** Count of queries by DNS record type over 24 hours

**PromQL Examples:**
```promql
# Show distribution of query types
pihole_query_by_type

# A record queries only
pihole_query_by_type{query_type="A"}
```

#### Query Status Distribution
**Metric:** `pihole_query_by_status`
- **Type:** Gauge  
- **Labels:** `query_status` (FORWARDED, CACHE, GRAVITY, etc.)
- **Description:** Count of queries by Pi-hole status over 24 hours

**PromQL Examples:**
```promql
# Show distribution of query statuses
pihole_query_by_status

# Blocked queries only
pihole_query_by_status{query_status="GRAVITY"}

# Cached queries only
pihole_query_by_status{query_status="CACHE"}
```

#### Reply Type Distribution
**Metric:** `pihole_query_replies`
- **Type:** Gauge
- **Labels:** `reply_type` (A, AAAA, NXDOMAIN, etc.)
- **Description:** Count of replies by DNS record type over 24 hours

**PromQL Examples:**
```promql
# Show distribution of reply types
pihole_query_replies

# NXDOMAIN replies only
pihole_query_replies{reply_type="NXDOMAIN"}
```

#### Query Count Totals
**Metric:** `pihole_query_count`
- **Type:** Gauge
- **Labels:** `category` (total, blocked, unique, forwarded, cached)
- **Description:** Total query counts by category over 24 hours

**PromQL Examples:**
```promql
# Total queries
pihole_query_count{category="total"}

# Blocked queries
pihole_query_count{category="blocked"}

# Cache hit ratio (percentage)
pihole_query_count{category="cached"} / pihole_query_count{category="total"} * 100

# Block rate (percentage)
pihole_query_count{category="blocked"} / pihole_query_count{category="total"} * 100
```

#### Client Counts
**Metric:** `pihole_client_count`
- **Type:** Gauge
- **Labels:** `category` (active, total)
- **Description:** Count of DNS clients

**PromQL Examples:**
```promql
# Active clients
pihole_client_count{category="active"}

# Total clients ever seen
pihole_client_count{category="total"}
```

#### Domains Being Blocked
**Metric:** `pihole_domains_being_blocked`
- **Type:** Gauge
- **Labels:** None
- **Description:** Number of domains on current blocklist

**PromQL Examples:**
```promql
# Total blocked domains count
pihole_domains_being_blocked
```

#### Upstream Distribution
**Metric:** `pihole_query_upstream_count`
- **Type:** Gauge
- **Labels:** `ip`, `name`, `port`
- **Description:** Query counts by upstream DNS server over 24 hours

**PromQL Examples:**
```promql
# Queries by upstream server
pihole_query_upstream_count

# Queries to specific upstream
pihole_query_upstream_count{ip="8.8.8.8"}
```

### 1-Minute Window Metrics (from query log analysis)

These metrics are computed from the actual query log for the last complete minute:

#### Per-Minute Query Breakdowns
**Metrics:**
- `pihole_query_type_1m` - Query types in last minute
- `pihole_query_status_1m` - Query statuses in last minute  
- `pihole_query_reply_1m` - Reply types in last minute
- `pihole_query_client_1m` - Queries per client in last minute
- `pihole_query_upstream_1m` - Queries per upstream in last minute

**PromQL Examples:**
```promql
# Query counts by type in last minute
pihole_query_type_1m

# Query counts by status in last minute
pihole_query_status_1m

# Top clients by query count
topk(10, pihole_query_client_1m)

# Upstream distribution counts
pihole_query_upstream_1m
```

#### DNS Error Monitoring
**Metrics:**
- `pihole_dns_errors_1m` - DNS errors by response code (Gauge)
- `pihole_dns_queries_processed_1m` - Total queries processed (Gauge)

**Note:** Common DNS error codes (SERVFAIL, NXDOMAIN, REFUSED, FORMERR, NOTIMP) are always exported, even with zero values.

**PromQL Examples:**
```promql
# DNS error rate (percentage of total queries in last minute)
sum(pihole_dns_errors_1m) / pihole_dns_queries_processed_1m * 100

# SERVFAIL error rate specifically
pihole_dns_errors_1m{rcode="SERVFAIL"} / pihole_dns_queries_processed_1m * 100

# Error rate by error type
pihole_dns_errors_1m / pihole_dns_queries_processed_1m * 100
```

#### DNS Timeout Monitoring
**Metric:** `pihole_dns_timeouts_1m`
- **Type:** Gauge
- **Labels:** None
- **Description:** DNS timeout queries in last minute

**PromQL Examples:**
```promql
# Timeout count in last minute
pihole_dns_timeouts_1m

# Timeout percentage
pihole_dns_timeouts_1m / pihole_dns_queries_processed_1m * 100
```

#### DNS Latency Monitoring
**Metric:** `pihole_dns_latency_seconds_1m`
- **Type:** Histogram
- **Labels:** `status` (cache, forwarded, blocked, retried, in_progress, other, unknown)
- **Description:** DNS query response time distribution

**Available Status Labels:**
- `cache` - Cached responses (CACHE, CACHE_STALE)
- `forwarded` - Forwarded to upstream (FORWARDED)
- `blocked` - Blocked queries (GRAVITY, REGEX, DENYLIST, etc.)
- `retried` - Retried queries (RETRIED, RETRIED_DNSSEC)
- `in_progress` - Queries in progress (IN_PROGRESS)
- `other` - Database busy or unknown (DBBUSY, UNKNOWN)
- `unknown` - Unrecognized Pi-hole statuses

**PromQL Examples:**
```promql
# Average DNS latency over 5 minutes (all queries)
rate(pihole_dns_latency_seconds_1m_sum[5m]) / rate(pihole_dns_latency_seconds_1m_count[5m])

# Average DNS latency for cached queries only
rate(pihole_dns_latency_seconds_1m_sum{status="cache"}[5m]) / rate(pihole_dns_latency_seconds_1m_count{status="cache"}[5m])

# Average DNS latency for forwarded queries only  
rate(pihole_dns_latency_seconds_1m_sum{status="forwarded"}[5m]) / rate(pihole_dns_latency_seconds_1m_count{status="forwarded"}[5m])

# 95th percentile latency (all queries)
histogram_quantile(0.95, rate(pihole_dns_latency_seconds_1m_bucket[5m]))

# 95th percentile latency for forwarded queries only
histogram_quantile(0.95, rate(pihole_dns_latency_seconds_1m_bucket{status="forwarded"}[5m]))

# Percentage of queries under 10ms
rate(pihole_dns_latency_seconds_1m_bucket{le="0.01"}[5m]) / rate(pihole_dns_latency_seconds_1m_count[5m]) * 100

# Convert to milliseconds for display
rate(pihole_dns_latency_seconds_1m_sum[5m]) / rate(pihole_dns_latency_seconds_1m_count[5m]) * 1000

# Cache vs forwarded latency comparison (in milliseconds)
rate(pihole_dns_latency_seconds_1m_sum{status="cache"}[5m]) / rate(pihole_dns_latency_seconds_1m_count{status="cache"}[5m]) * 1000
rate(pihole_dns_latency_seconds_1m_sum{status="forwarded"}[5m]) / rate(pihole_dns_latency_seconds_1m_count{status="forwarded"}[5m]) * 1000
```

## Grafana Dashboard Examples

### Single Stat Panels

**Cache Hit Ratio:**
```promql
pihole_query_count{category="cached"} / pihole_query_count{category="total"} * 100
```
- Unit: Percent (0-100)
- Thresholds: Red < 70%, Yellow < 85%, Green >= 85%

**Block Rate:**
```promql
pihole_query_count{category="blocked"} / pihole_query_count{category="total"} * 100
```
- Unit: Percent (0-100)
- Thresholds: Green > 20%, Yellow > 10%, Red <= 10%

**DNS Error Rate:**
```promql
sum(pihole_dns_errors_1m) / pihole_dns_queries_processed_1m * 100
```
- Unit: Percent (0-100)
- Thresholds: Green < 1%, Yellow < 5%, Red >= 5%

**Average DNS Latency:**
```promql
rate(pihole_dns_latency_seconds_1m_sum[5m]) / rate(pihole_dns_latency_seconds_1m_count[5m]) * 1000
```
- Unit: Milliseconds
- Thresholds: Green < 10ms, Yellow < 50ms, Red >= 50ms

**Average Cache Latency:**
```promql
rate(pihole_dns_latency_seconds_1m_sum{status="cache"}[5m]) / rate(pihole_dns_latency_seconds_1m_count{status="cache"}[5m]) * 1000
```
- Unit: Milliseconds
- Thresholds: Green < 5ms, Yellow < 20ms, Red >= 20ms

**Average Forwarded Latency:**
```promql
rate(pihole_dns_latency_seconds_1m_sum{status="forwarded"}[5m]) / rate(pihole_dns_latency_seconds_1m_count{status="forwarded"}[5m]) * 1000  
```
- Unit: Milliseconds
- Thresholds: Green < 20ms, Yellow < 100ms, Red >= 100ms

**Active Clients:**
```promql
pihole_client_count{category="active"}
```
- Unit: Count
- No thresholds needed

**Domains Blocked:**
```promql
pihole_domains_being_blocked
```
- Unit: Count
- No thresholds needed

### Time Series Graphs

**Query Count by Type:**
```promql
pihole_query_type_1m
```
- Legend: `{{query_type}}`
- Y-axis: Query count (last minute)

**Query Count by Status:**
```promql
pihole_query_status_1m
```
- Legend: `{{query_status}}`
- Y-axis: Query count (last minute)

**DNS Error Rates by Type:**
```promql
pihole_dns_errors_1m / pihole_dns_queries_processed_1m * 100
```
- Legend: `{{rcode}} errors`
- Y-axis: Percent

**DNS Latency Percentiles:**
```promql
histogram_quantile(0.50, rate(pihole_dns_latency_seconds_1m_bucket[5m])) * 1000  # 50th percentile
histogram_quantile(0.95, rate(pihole_dns_latency_seconds_1m_bucket[5m])) * 1000  # 95th percentile
histogram_quantile(0.99, rate(pihole_dns_latency_seconds_1m_bucket[5m])) * 1000  # 99th percentile
```
- Y-axis: Milliseconds

**Cache vs Forwarded Latency Comparison:**
```promql
rate(pihole_dns_latency_seconds_1m_sum{status="cache"}[5m]) / rate(pihole_dns_latency_seconds_1m_count{status="cache"}[5m]) * 1000
rate(pihole_dns_latency_seconds_1m_sum{status="forwarded"}[5m]) / rate(pihole_dns_latency_seconds_1m_count{status="forwarded"}[5m]) * 1000
```
- Legend: `{{status}} latency`
- Y-axis: Milliseconds

**Top Clients by Query Count:**
```promql
topk(10, pihole_query_client_1m)
```
- Legend: `{{query_client}}`
- Y-axis: Query count (last minute)

**Upstream Distribution:**
```promql
pihole_query_upstream_1m
```
- Legend: `{{query_upstream}}`
- Y-axis: Query count (last minute)

## Prometheus Alerting Rules

```yaml
groups:
- name: pihole_alerts
  rules:
  - alert: PiHoleDNSErrorRateHigh
    expr: sum(pihole_dns_errors_1m) / pihole_dns_queries_processed_1m * 100 > 5
    for: 2m
    labels:
      severity: warning
    annotations:
      summary: "Pi-hole DNS error rate is high"
      description: "DNS error rate is {{ $value | humanize }}% in the last minute"

  - alert: PiHoleCacheHitRateLow
    expr: pihole_query_count{category="cached"} / pihole_query_count{category="total"} * 100 < 70
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: "Pi-hole cache hit ratio is low"
      description: "Cache hit ratio is {{ $value | humanize }}%"

  - alert: PiHoleDNSLatencyHigh
    expr: histogram_quantile(0.95, rate(pihole_dns_latency_seconds_1m_bucket[5m])) > 0.1
    for: 2m
    labels:
      severity: warning
    annotations:
      summary: "Pi-hole DNS latency is high"
      description: "95th percentile DNS latency is {{ $value | humanize }}s"

  - alert: PiHoleForwardedLatencyHigh
    expr: histogram_quantile(0.95, rate(pihole_dns_latency_seconds_1m_bucket{status="forwarded"}[5m])) > 0.2
    for: 2m
    labels:
      severity: warning
    annotations:
      summary: "Pi-hole forwarded DNS latency is high"
      description: "95th percentile forwarded DNS latency is {{ $value | humanize }}s"

  - alert: PiHoleDNSTimeouts
    expr: pihole_dns_timeouts_1m > 0
    for: 1m
    labels:
      severity: critical
    annotations:
      summary: "Pi-hole DNS timeouts detected"
      description: "{{ $value | humanize }} DNS timeouts in the last minute"

  - alert: PiHoleSpecificErrorTypeHigh
    expr: pihole_dns_errors_1m{rcode="SERVFAIL"} / pihole_dns_queries_processed_1m * 100 > 2
    for: 2m
    labels:
      severity: warning
    annotations:
      summary: "Pi-hole SERVFAIL errors high"
      description: "SERVFAIL error rate is {{ $value | humanize }}% in the last minute"

  - alert: PiHoleBlockRateLow
    expr: pihole_query_count{category="blocked"} / pihole_query_count{category="total"} * 100 < 10
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: "Pi-hole block rate is low"
      description: "Block rate is {{ $value | humanize }}% which may indicate blocklist issues"

  - alert: PiHoleDown
    expr: up{job=~".*pihole.*"} == 0
    for: 1m
    labels:
      severity: critical
    annotations:
      summary: "Pi-hole exporter is down"
      description: "Pi-hole exporter has been down for more than 1 minute"
```

## Benefits of This Approach

1. **Flexible Time Windows**: Use different time ranges for different use cases
2. **Proper Metric Handling**: Appropriate metric types for different data patterns
3. **Accurate Calculations**: Built-in functions handle edge cases and interpolation
4. **Standard Patterns**: Consistent with other Prometheus exporters
5. **Better Alerting**: More reliable threshold detection over time
6. **Status-based Analysis**: Separate monitoring of cache vs forwarded query performance
7. **Complete Coverage**: All Pi-hole statistics available for monitoring and analysis
8. **Consistent Metrics**: DNS error codes always exported (even with zero values) for reliable monitoring

## Usage Notes

- **24-hour metrics** are great for overall trends and daily comparisons
- **1-minute gauge metrics** show windowed counts and are better for real-time monitoring and alerting
- **Histogram metrics** provide detailed latency analysis with percentiles (these remain as counters)
- **Histogram counter metrics** should always be used with `rate()` function for meaningful rates
- **Gauge metrics** can be used directly for current values or with aggregation functions for analysis 