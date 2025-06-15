import pytest
from unittest.mock import patch
from prometheus_client.core import GaugeMetricFamily
import importlib.util
import sys
from pathlib import Path

# Dynamically import the script as a module
script_path = Path(__file__).parent.parent / "pihole6_exporter.py"
spec = importlib.util.spec_from_file_location("pihole6_exporter", script_path)
pihole6_exporter = importlib.util.module_from_spec(spec)
sys.modules["pihole6_exporter"] = pihole6_exporter
spec.loader.exec_module(pihole6_exporter)

PiholeCollector = pihole6_exporter.PiholeCollector

# Sample API responses to mock
SUMMARY_RESPONSE = {
    "queries": {
        "types": {"A": 10, "AAAA": 5},
        "status": {"CACHED": 8, "FORWARDED": 7},
        "replies": {"A": 10, "AAAA": 5},
        "total": 15,
        "blocked": 2,
        "unique_domains": 12,
        "forwarded": 7,
        "cached": 8
    },
    "clients": {"active": 2, "total": 3},
    "gravity": {"domains_being_blocked": 1000}
}
UPSTREAMS_RESPONSE = {
    "upstreams": [
        {"ip": "8.8.8.8", "name": "Google", "port": 53, "count": 5},
        {"ip": "1.1.1.1", "name": "Cloudflare", "port": 53, "count": 2}
    ]
}
QUERIES_RESPONSE = {
    "queries": [
        {
            "timestamp": 1234567890,
            "type": "A",
            "status": "CACHED",
            "reply_time": 0.001,  # 1ms - fast cache hit
            "rcode": "NOERROR",
            "reply": {"type": "A"},
            "client": {"ip": "192.168.1.2"},
            "upstream": "8.8.8.8"
        },
        {
            "timestamp": 1234567891,
            "type": "AAAA",
            "status": "FORWARDED",
            "reply_time": 0.05,   # 50ms - slower forwarded
            "rcode": "NOERROR",
            "reply": {"type": "AAAA"},
            "client": {"ip": "192.168.1.3"},
            "upstream": "1.1.1.1"
        },
        {
            "timestamp": 1234567892,
            "type": "A",
            "status": "FORWARDED",
            "reply_time": -1,     # Invalid - should be skipped
            "rcode": "TIMEOUT",
            "reply": {"type": "A"},
            "client": {"ip": "192.168.1.4"},
            "upstream": "8.8.4.4"
        }
    ]
}

def test_collect_yields_expected_metrics():
    """Integration test: ensure collect yields expected metrics from real code path."""
    # Need 5 API calls: summary, upstreams, queries, summary again for cache ratio
    with patch.object(PiholeCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE, SUMMARY_RESPONSE]):
        collector = PiholeCollector()
        metrics = list(collector.collect())
        # Check that at least one known metric is present
        metric_names = [m.name for m in metrics if isinstance(m, GaugeMetricFamily)]
        assert "pihole_query_by_type" in metric_names
        assert "pihole_query_by_status" in metric_names
        assert "pihole_query_count" in metric_names
        assert "pihole_query_type_1m" in metric_names
        # Check new metrics
        assert "pihole_cache_hit_ratio_percent" in metric_names
        
        # Check counter metrics - look for the actual metric name, not sample name
        counter_names = [m.name for m in metrics if hasattr(m, 'name') and m.name.startswith('pihole_dns_timeouts')]
        assert len(counter_names) > 0, f"No timeout counter found. Available metrics: {[m.name for m in metrics if hasattr(m, 'name')]}"

def test_cache_hit_ratio_calculation():
    """Test that cache hit ratio is calculated correctly."""
    with patch.object(PiholeCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE, SUMMARY_RESPONSE]):
        collector = PiholeCollector()
        metrics = list(collector.collect())
        
        # Find cache hit ratio metric
        cache_metrics = [m for m in metrics if hasattr(m, 'name') and m.name == "pihole_cache_hit_ratio_percent"]
        assert len(cache_metrics) == 1
        
        cache_metric = cache_metrics[0]
        # From SUMMARY_RESPONSE: cached=8, total=15, so ratio should be 8/15*100 = 53.33%
        expected_ratio = (8 / 15) * 100
        assert abs(cache_metric.samples[0].value - expected_ratio) < 0.01  # Allow small floating point differences

def test_dns_timeout_counter():
    """Test that DNS timeouts are counted correctly."""
    with patch.object(PiholeCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE, SUMMARY_RESPONSE]):
        collector = PiholeCollector()
        metrics = list(collector.collect())
        
        # Find timeout counter metric - the metric name is "pihole_dns_timeouts", not "pihole_dns_timeouts_total"
        timeout_metrics = [m for m in metrics if hasattr(m, 'name') and m.name.startswith("pihole_dns_timeouts")]
        assert len(timeout_metrics) == 1, f"Expected 1 timeout metric, found {len(timeout_metrics)}: {[m.name for m in timeout_metrics]}"
        
        timeout_metric = timeout_metrics[0]
        # From QUERIES_RESPONSE: one query has "rcode": "TIMEOUT", so count should be 1
        # Look for the sample with name ending in "_total"
        total_samples = [s for s in timeout_metric.samples if s.name.endswith('_total')]
        assert len(total_samples) == 1
        assert total_samples[0].value == 1

def test_latency_histogram_in_collect():
    """Test that DNS latency histogram is yielded by collect."""
    # Need 4 API calls: summary, upstreams, queries, summary again for cache ratio
    with patch.object(PiholeCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE, SUMMARY_RESPONSE]):
        collector = PiholeCollector()
        metrics = list(collector.collect())
        
        # Check histogram is present
        all_metric_names = [m.name for m in metrics if hasattr(m, 'name')]
        assert "pihole_dns_latency_seconds" in all_metric_names, f"No latency histogram found. Available metrics: {all_metric_names}"
        
        # Find the latency metric
        latency_metrics = [m for m in metrics if hasattr(m, 'name') and m.name == "pihole_dns_latency_seconds"]
        assert len(latency_metrics) == 1, "Should have exactly one latency histogram"
        
        latency_metric = latency_metrics[0]
        assert latency_metric.name == "pihole_dns_latency_seconds"
        assert "DNS query latency in seconds" in latency_metric.documentation 

 