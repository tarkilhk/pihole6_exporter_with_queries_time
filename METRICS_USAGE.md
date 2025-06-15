# Pi-hole Exporter Metrics Usage Guide

This guide explains how to use the raw metrics exported by the Pi-hole exporter to create meaningful dashboards and alerts in Grafana/Prometheus.

## Philosophy: Raw Counters vs Computed Rates

Following Prometheus best practices, this exporter provides **raw counters** rather than pre-computed rates. This approach offers several advantages:

- **Flexibility**: Different time windows for rate calculations
- **Accuracy**: Prometheus handles counter resets and edge cases properly
- **Consistency**: Standard pattern across all Prometheus exporters
- **Alerting**: Better support for alerting rules

## Available Metrics

### DNS Error Monitoring

**Raw Metrics:**
- `pihole_dns_errors_1m{rcode="SERVFAIL"}` - Count of DNS errors by response code
- `pihole_dns_queries_processed_1m` - Total queries processed

**PromQL Examples:**

```promql
# DNS error rate over 5 minutes
rate(pihole_dns_errors_1m[5m]) / rate(pihole_dns_queries_processed_1m[5m]) * 100

# SERVFAIL error rate specifically
rate(pihole_dns_errors_1m{rcode="SERVFAIL"}[5m]) / rate(pihole_dns_queries_processed_1m[5m]) * 100

# Total error rate (all error types)
sum(rate(pihole_dns_errors_1m[5m])) / rate(pihole_dns_queries_processed_1m[5m]) * 100
```

### Cache Hit Ratio

**Raw Metrics:**
- `pihole_query_count{category="cached"}` - Cache hits (24h)
- `pihole_query_count{category="total"}` - Total queries (24h)

**PromQL Examples:**

```promql
# Current cache hit ratio (percentage)
pihole_query_count{category="cached"} / pihole_query_count{category="total"} * 100

# Cache hit ratio over time (for trending)
rate(pihole_query_count{category="cached"}[5m]) / rate(pihole_query_count{category="total"}[5m]) * 100
```

### DNS Latency

**Raw Metrics:**
- `pihole_dns_latency_seconds_1m_bucket{status="cache"}` - Histogram buckets for cached queries
- `pihole_dns_latency_seconds_1m_bucket{status="forwarded"}` - Histogram buckets for forwarded queries
- `pihole_dns_latency_seconds_1m_sum` - Total latency sum
- `pihole_dns_latency_seconds_1m_count` - Total query count

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
```

### DNS Timeouts

**Raw Metrics:**
- `pihole_dns_timeouts_1m` - Total timeout count

**PromQL Examples:**

```promql
# Timeout rate over 5 minutes
rate(pihole_dns_timeouts_1m[5m])

# Timeout percentage
rate(pihole_dns_timeouts_1m[5m]) / rate(pihole_dns_queries_processed_1m[5m]) * 100
```

## Grafana Dashboard Examples

### Single Stat Panels

**DNS Error Rate:**
```promql
sum(rate(pihole_dns_errors_1m[5m])) / rate(pihole_dns_queries_processed_1m[5m]) * 100
```
- Unit: Percent (0-100)
- Thresholds: Green < 1%, Yellow < 5%, Red >= 5%

**Cache Hit Ratio:**
```promql
pihole_query_count{category="cached"} / pihole_query_count{category="total"} * 100
```
- Unit: Percent (0-100)
- Thresholds: Red < 70%, Yellow < 85%, Green >= 85%

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

### Time Series Graphs

**DNS Error Rates by Type:**
```promql
rate(pihole_dns_errors_1m[5m]) / rate(pihole_dns_queries_processed_1m[5m]) * 100
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

## Prometheus Alerting Rules

```yaml
groups:
- name: pihole_alerts
  rules:
  - alert: PiHoleDNSErrorRateHigh
    expr: sum(rate(pihole_dns_errors_1m[5m])) / rate(pihole_dns_queries_processed_1m[5m]) * 100 > 5
    for: 2m
    labels:
      severity: warning
    annotations:
      summary: "Pi-hole DNS error rate is high"
      description: "DNS error rate is {{ $value | humanize }}% over the last 5 minutes"

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
    expr: rate(pihole_dns_timeouts_1m[5m]) > 0
    for: 1m
    labels:
      severity: critical
    annotations:
      summary: "Pi-hole DNS timeouts detected"
      description: "{{ $value | humanize }} DNS timeouts per second"

  - alert: PiHoleSpecificErrorTypeHigh
    expr: rate(pihole_dns_errors_1m{rcode="SERVFAIL"}[5m]) / rate(pihole_dns_queries_processed_1m[5m]) * 100 > 2
    for: 2m
    labels:
      severity: warning
    annotations:
      summary: "Pi-hole SERVFAIL errors high"
      description: "SERVFAIL error rate is {{ $value | humanize }}% over the last 5 minutes"
```

## Benefits of This Approach

1. **Flexible Time Windows**: Use different time ranges (`[1m]`, `[5m]`, `[1h]`) for different use cases
2. **Proper Counter Handling**: Prometheus automatically handles counter resets
3. **Accurate Rates**: Built-in functions handle edge cases and interpolation
4. **Standard Patterns**: Consistent with other Prometheus exporters
5. **Better Alerting**: More reliable threshold detection over time
6. **Status-based Analysis**: Separate monitoring of cache vs forwarded query performance

## Migration from Computed Rates

If you were previously using computed rate metrics, update your queries:

**Old (computed rates):**
```promql
pihole_dns_error_rate_percent{error_type="servfail"}
pihole_cache_hit_ratio_percent
```

**New (raw counters with rate calculation):**
```promql
rate(pihole_dns_errors_1m{rcode="SERVFAIL"}[5m]) / rate(pihole_dns_queries_processed_1m[5m]) * 100
pihole_query_count{category="cached"} / pihole_query_count{category="total"} * 100
``` 