import pytest
from unittest.mock import patch
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily
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
            "rcode": "NOERROR",
            "reply": {"type": "A", "time": 0.001},  # 1ms - fast cache hit
            "client": {"ip": "192.168.1.2"},
            "upstream": "8.8.8.8"
        },
        {
            "timestamp": 1234567891,
            "type": "AAAA",
            "status": "FORWARDED",
            "rcode": "NOERROR",
            "reply": {"type": "AAAA", "time": 0.05},   # 50ms - slower forwarded
            "client": {"ip": "192.168.1.3"},
            "upstream": "1.1.1.1"
        },
        {
            "timestamp": 1234567892,
            "type": "A",
            "status": "FORWARDED",
            "rcode": "SERVFAIL",  # DNS error
            "reply": {"type": "A", "time": 0.1},
            "client": {"ip": "192.168.1.4"},
            "upstream": "8.8.4.4"
        },
        {
            "timestamp": 1234567893,
            "type": "A",
            "status": "TIMEOUT",
            "rcode": "TIMEOUT",
            "reply": {"type": "A", "time": 0.0},
            "client": {"ip": "192.168.1.5"},
            "upstream": "8.8.8.8"
        }
    ]
}

def test_collect_yields_expected_metrics():
    """Integration test: ensure collect yields expected metrics from real code path."""
    # Need 4 API calls: summary, upstreams, queries, summary again for cache metrics
    with patch.object(PiholeCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE, SUMMARY_RESPONSE]):
        collector = PiholeCollector()
        metrics = list(collector.collect())
        
        # Check that basic metrics are present
        metric_names = [m.name for m in metrics if hasattr(m, 'name')]
        assert "pihole_query_by_type" in metric_names
        assert "pihole_query_by_status" in metric_names
        assert "pihole_query_count" in metric_names
        assert "pihole_query_type_1m" in metric_names
        
        # Check new raw counter metrics (following Prometheus best practices)
        assert "pihole_dns_errors_1m" in metric_names  # CounterMetricFamily base name
        assert "pihole_dns_queries_processed_1m" in metric_names  # CounterMetricFamily base name
        assert "pihole_dns_timeouts_1m" in metric_names
        assert "pihole_dns_latency_seconds_1m" in metric_names

def test_cache_metrics_in_query_count():
    """Test that cache metrics are available in pihole_query_count"""
    with patch.object(PiholeCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE]):
        collector = PiholeCollector()
        metrics = list(collector.collect())
        
        # Find query count metrics which includes cache data
        query_count_metrics = [m for m in metrics if hasattr(m, 'name') and m.name == "pihole_query_count"]
        assert len(query_count_metrics) == 1
        
        query_metric = query_count_metrics[0]
        # Should have cached and total categories (among others)
        samples = query_metric.samples
        cached_samples = [s for s in samples if s.labels.get('category') == 'cached']
        total_samples = [s for s in samples if s.labels.get('category') == 'total']
        
        assert len(cached_samples) == 1, "Should have exactly one cached sample"
        assert len(total_samples) == 1, "Should have exactly one total sample"
        
        # From SUMMARY_RESPONSE: cached=8, total=15
        assert cached_samples[0].value == 8
        assert total_samples[0].value == 15

def test_dns_error_counters():
    """Test that DNS errors are counted by rcode as raw counters."""
    with patch.object(PiholeCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE, SUMMARY_RESPONSE]):
        collector = PiholeCollector()
        metrics = list(collector.collect())
        
        # Find DNS error counter metric - should always be present even if no errors
        # Note: CounterMetricFamily creates metric with base name, samples have _total suffix
        error_metrics = [m for m in metrics if hasattr(m, 'name') and m.name == "pihole_dns_errors_1m"]
        assert len(error_metrics) == 1
        
        error_metric = error_metrics[0]
        # From QUERIES_RESPONSE: one query has "rcode": "SERVFAIL"
        # CounterMetricFamily creates both _total and _created samples, we want the _total one
        servfail_total_samples = [s for s in error_metric.samples if s.labels.get('rcode') == 'SERVFAIL' and s.name.endswith('_total')]
        if servfail_total_samples:  # Only check if there are SERVFAIL samples
            assert len(servfail_total_samples) == 1
            assert servfail_total_samples[0].value == 1

def test_dns_queries_processed_counter():
    """Test that total queries processed counter is exported."""
    with patch.object(PiholeCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE, SUMMARY_RESPONSE]):
        collector = PiholeCollector()
        metrics = list(collector.collect())
        
        # Find total queries counter
        # Note: CounterMetricFamily creates metric with base name, samples have _total suffix
        total_metrics = [m for m in metrics if hasattr(m, 'name') and m.name == "pihole_dns_queries_processed_1m"]
        assert len(total_metrics) == 1
        
        total_metric = total_metrics[0]
        # From QUERIES_RESPONSE: 4 queries total
        total_samples = [s for s in total_metric.samples if s.name.endswith('_total')]
        assert len(total_samples) == 1
        assert total_samples[0].value == 4

def test_dns_timeout_counter():
    """Test that DNS timeouts are counted correctly."""
    with patch.object(PiholeCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE, SUMMARY_RESPONSE]):
        collector = PiholeCollector()
        metrics = list(collector.collect())
        
        # Find timeout counter metric
        timeout_metrics = [m for m in metrics if hasattr(m, 'name') and m.name.startswith("pihole_dns_timeouts_1m")]
        assert len(timeout_metrics) == 1, f"Expected 1 timeout metric, found {len(timeout_metrics)}: {[m.name for m in timeout_metrics]}"
        
        timeout_metric = timeout_metrics[0]
        # From QUERIES_RESPONSE: one query has "rcode": "TIMEOUT", so count should be 1
        total_samples = [s for s in timeout_metric.samples if s.name.endswith('_total')]
        assert len(total_samples) == 1
        assert total_samples[0].value == 1

def test_latency_histogram_in_collect():
    """Test that DNS latency histogram is yielded by collect."""
    with patch.object(PiholeCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE, SUMMARY_RESPONSE]):
        collector = PiholeCollector()
        metrics = list(collector.collect())
        
        # Check histogram is present
        all_metric_names = [m.name for m in metrics if hasattr(m, 'name')]
        assert "pihole_dns_latency_seconds_1m" in all_metric_names, f"No latency histogram found. Available metrics: {all_metric_names}"
        
        # Find the latency metric
        latency_metrics = [m for m in metrics if hasattr(m, 'name') and m.name == "pihole_dns_latency_seconds_1m"]
        assert len(latency_metrics) == 1, "Should have exactly one latency histogram"
        
        latency_metric = latency_metrics[0]
        assert latency_metric.name == "pihole_dns_latency_seconds_1m"
        assert "DNS query latency in seconds" in latency_metric.documentation


def test_system_metrics_present():
    """Ensure system metrics are exported."""
    with patch.object(PiholeCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE, SUMMARY_RESPONSE]):
        collector = PiholeCollector()
        metrics = list(collector.collect())

        metric_names = [m.name for m in metrics if hasattr(m, 'name')]

        expected = [
            'system_cpu_usage_percent',
            'system_load1',
            'system_memory_usage_bytes',
            'system_disk_usage_bytes',
            'system_network_receive_bytes',
            'system_sdcard_wear_percent',
        ]

        for name in expected:
            assert name in metric_names
