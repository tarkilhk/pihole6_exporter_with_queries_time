# ✨ Pi-hole v6 Latency Metrics – Development Plan ✨
_Updated: Current Status & Next Steps_

---

## 1  Purpose & High-Level Goals
| Goal | Why it matters |
|------|----------------|
| **Expose DNS latency** (`pihole_dns_latency_seconds`) | Trend cache vs forwarded response times, alert when upstream gets slow |
| **Keep cardinality ≤ 40 series** | Safe for Raspberry Pi memory & Prometheus TSDB |
| **Stay simple & maintainable** | Direct script execution, no complex package structure |
| **Zero SD-card wear** | No DB queries, still based on `/api/queries` JSON |

---

## 2  Current Project Status ✅

**COMPLETED:**
- ✅ Basic Pi-hole v6 exporter working (`pihole6_exporter.py`)
- ✅ Deployment workflow (`.gitea/workflows/deploy.yml`)
- ✅ Systemd service file (`pihole6_exporter.service`)
- ✅ Environment file for API token (`pihole6_exporter.env`)
- ✅ Integration test (`tests/test_exporter_integration.py`)
- ✅ `_process_query()` method for per-query processing
- ✅ Per-minute metrics (`pihole_query_*_1m`)
- ✅ 24h summary metrics (`pihole_query_by_*`)

**CURRENT STRUCTURE:**
```
pihole6_exporter_with_queries_time/
├─ pihole6_exporter.py           # ← main exporter script
├─ pihole6_exporter.service      # ← systemd service
├─ pihole6_exporter.env          # ← API token
├─ requirements.txt              # ← dependencies
├─ tests/
│  └─ test_exporter_integration.py  # ← integration test
└─ .gitea/workflows/deploy.yml   # ← deployment automation
```

---

## 3  REMAINING TASKS - Latency Metrics

### 3.1 Add Latency Metrics to `_process_query()`
- [ ] **Import Histogram** from `prometheus_client`
- [ ] **Declare latency histogram** at class level:
  ```python
  from prometheus_client import Histogram
  
  # In PiholeCollector.__init__():
  self.dns_latency = Histogram(
      "pihole_dns_latency_seconds",
      "DNS query response time",
      ["status"],  # cache | forwarded
      buckets=(0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, float('inf'))
  )
  ```
- [ ] **Update `_process_query()`** to observe latency:
  ```python
  def _process_query(self, q):
      # ... existing code ...
      
      # Add latency tracking
      reply_time = q.get("reply_time", -1)
      if reply_time >= 0:  # Valid response time
          status_label = "cache" if status == "CACHED" else "forwarded"
          self.dns_latency.labels(status=status_label).observe(reply_time)
  ```

### 3.2 Add Helper Counters (Optional)
- [ ] **DNS queries total** by status
- [ ] **DNS timeouts total**
- [ ] **Cache hit ratio** gauge

### 3.3 Testing & Validation
- [ ] **Update integration test** to verify latency metrics
- [ ] **Manual test** with real Pi-hole instance
- [ ] **Check Prometheus scrape** for histogram buckets

---

## 4  Implementation Details

### 4.1 Latency Histogram Design
```python
# Buckets optimized for DNS response times
buckets=(0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, float('inf'))
# Labels: status=cache|forwarded
# Total series: 2 labels × 12 buckets = 24 series (well under 40 limit)
```

### 4.2 Query Processing Logic
```python
def _process_query(self, q):
    # Existing counter logic...
    type = q["type"]
    status = q["status"]
    # ... existing code ...
    
    # NEW: Latency tracking
    reply_time = q.get("reply_time", -1)
    if reply_time >= 0:  # Skip negative/missing times
        status_label = "cache" if status == "CACHED" else "forwarded"
        self.dns_latency.labels(status=status_label).observe(reply_time)
```

### 4.3 Expected Metrics Output
```
# HELP pihole_dns_latency_seconds DNS query response time
# TYPE pihole_dns_latency_seconds histogram
pihole_dns_latency_seconds_bucket{status="cache",le="0.001"} 45
pihole_dns_latency_seconds_bucket{status="cache",le="0.002"} 67
...
pihole_dns_latency_seconds_bucket{status="forwarded",le="0.05"} 12
pihole_dns_latency_seconds_count{status="cache"} 89
pihole_dns_latency_seconds_sum{status="cache"} 0.234
```

---

## 5  Testing Strategy

### 5.1 Update Integration Test
```python
def test_latency_metrics_in_collect():
    # Mock API responses with reply_time data
    # Call collect()
    # Verify histogram metrics are present
    assert "pihole_dns_latency_seconds" in metric_names
```

### 5.2 Manual Validation
```bash
# Deploy to Pi
curl localhost:9666/metrics | grep pihole_dns_latency

# Check in Grafana
histogram_quantile(0.95, pihole_dns_latency_seconds_bucket{status="forwarded"}) * 1000
```

---

## 6  Deployment Notes

- **No breaking changes** - existing metrics remain unchanged
- **Backward compatible** - old dashboards continue working
- **Memory impact** - ~24 additional time series (minimal)
- **Performance** - negligible overhead per query

---

## 7  Next Steps

1. **Implement latency histogram** in `_process_query()`
2. **Update integration test** to verify new metrics
3. **Test locally** with mock data
4. **Deploy to Pi** and validate with real traffic
5. **Create Grafana dashboard** for latency visualization

---

*Goal: Add DNS latency visibility while keeping the exporter simple and reliable.*
