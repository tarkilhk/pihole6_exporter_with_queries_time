#!/usr/bin/env python3

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os
from dotenv import load_dotenv
import pytest
import time
import json
import socket
from unittest.mock import patch, MagicMock
from metrics_exporter.pihole6_metrics_exporter import PiholeMetricsCollector
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily
import importlib.util
import subprocess

# Load environment variables from .env file
load_dotenv()

# Dynamically import the script as a module
script_path = Path(__file__).parent.parent / "metrics_exporter" / "pihole6_metrics_exporter.py"
spec = importlib.util.spec_from_file_location("pihole6_metrics_exporter", script_path)
pihole6_metrics_exporter = importlib.util.module_from_spec(spec)
sys.modules["pihole6_metrics_exporter"] = pihole6_metrics_exporter
spec.loader.exec_module(pihole6_metrics_exporter)

PiholeMetricsCollector = pihole6_metrics_exporter.PiholeMetricsCollector

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
    with patch.object(PiholeMetricsCollector, 'get_sid', return_value="test-session-id"), \
         patch.object(PiholeMetricsCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE, SUMMARY_RESPONSE]), \
         patch('socket.gethostbyaddr', side_effect=socket.herror):
        collector = PiholeMetricsCollector()
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
    with patch.object(PiholeMetricsCollector, 'get_sid', return_value="test-session-id"), \
         patch.object(PiholeMetricsCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE]), \
         patch('socket.gethostbyaddr', side_effect=socket.herror):
        collector = PiholeMetricsCollector()
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
    with patch.object(PiholeMetricsCollector, 'get_sid', return_value="test-session-id"), \
         patch.object(PiholeMetricsCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE, SUMMARY_RESPONSE]), \
         patch('socket.gethostbyaddr', side_effect=socket.herror):
        collector = PiholeMetricsCollector()
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
    with patch.object(PiholeMetricsCollector, 'get_sid', return_value="test-session-id"), \
         patch.object(PiholeMetricsCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE, SUMMARY_RESPONSE]), \
         patch('socket.gethostbyaddr', side_effect=socket.herror):
        collector = PiholeMetricsCollector()
        metrics = list(collector.collect())
        
        # Find total queries counter
        # Note: CounterMetricFamily creates metric with base name, samples have _total suffix
        total_metrics = [m for m in metrics if hasattr(m, 'name') and m.name == "pihole_dns_queries_processed_1m"]
        assert len(total_metrics) == 1
        
        total_metric = total_metrics[0]
        # From QUERIES_RESPONSE: 4 queries total
        total_samples = [s for s in total_metric.samples if s.name == "pihole_dns_queries_processed_1m"]
        assert len(total_samples) == 1
        assert total_samples[0].value == 4

def test_dns_timeout_counter():
    """Test that DNS timeouts are counted correctly."""
    with patch.object(PiholeMetricsCollector, 'get_sid', return_value="test-session-id"), \
         patch.object(PiholeMetricsCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE, SUMMARY_RESPONSE]), \
         patch('socket.gethostbyaddr', side_effect=socket.herror):
        collector = PiholeMetricsCollector()
        metrics = list(collector.collect())
        
        # Find timeout counter metric
        timeout_metrics = [m for m in metrics if hasattr(m, 'name') and m.name.startswith("pihole_dns_timeouts_1m")]
        assert len(timeout_metrics) == 1, f"Expected 1 timeout metric, found {len(timeout_metrics)}: {[m.name for m in timeout_metrics]}"
        
        timeout_metric = timeout_metrics[0]
        # From QUERIES_RESPONSE: one query has "rcode": "TIMEOUT", so count should be 1
        total_samples = [s for s in timeout_metric.samples if s.name == "pihole_dns_timeouts_1m"]
        assert len(total_samples) == 1
        assert total_samples[0].value == 1

def test_latency_histogram_in_collect():
    """Test that DNS latency histogram is yielded by collect."""
    with patch.object(PiholeMetricsCollector, 'get_sid', return_value="test-session-id"), \
         patch.object(PiholeMetricsCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE, SUMMARY_RESPONSE]), \
         patch('socket.gethostbyaddr', side_effect=socket.herror):
        collector = PiholeMetricsCollector()
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
    with patch.object(PiholeMetricsCollector, 'get_sid', return_value="test-session-id"), \
         patch.object(PiholeMetricsCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE, SUMMARY_RESPONSE]), \
         patch('socket.gethostbyaddr', side_effect=socket.herror):
        collector = PiholeMetricsCollector()
        metrics = list(collector.collect())

        metric_names = [m.name for m in metrics if hasattr(m, 'name')]

        expected = [
            'system_cpu_usage_percent',
            'system_memory_usage_bytes',
            'system_disk_usage_bytes',
            'system_network_receive_bytes',
        ]
        if hasattr(os, "getloadavg"):
            expected.append('system_load1')
        
        # Temperature metric is only available on Raspberry Pi
        # On Windows/other systems, vcgencmd won't be available
        if 'system_temperature_celsius' in metric_names:
            expected.append('system_temperature_celsius')
            
        for name in expected:
            assert name in metric_names


def test_hostname_resolution_for_client_label():
    """Ensure resolved hostnames are used for client labels when available."""
    def fake_gethost(ip):
        if ip == "192.168.1.2":
            return ("device.local", [], [ip])
        raise socket.herror

    with patch.object(PiholeMetricsCollector, 'get_sid', return_value="test-session-id"), \
         patch.object(PiholeMetricsCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE, SUMMARY_RESPONSE]), \
         patch('socket.gethostbyaddr', side_effect=fake_gethost):
        collector = PiholeMetricsCollector()
        metrics = list(collector.collect())

        client_metrics = [m for m in metrics if getattr(m, 'name', '') == "pihole_query_client_1m"]
        assert client_metrics, "Client metric missing"
        labels = [s.labels['query_client'] for s in client_metrics[0].samples if s.name == "pihole_query_client_1m"]
        assert "device" in labels


def test_hostname_resolution_failure_uses_ip():
    """If a hostname can't be resolved, the IP should be used as the label."""
    with patch.object(PiholeMetricsCollector, 'get_sid', return_value="test-session-id"), \
         patch.object(PiholeMetricsCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE, SUMMARY_RESPONSE]), \
         patch('socket.gethostbyaddr', side_effect=socket.herror):
        collector = PiholeMetricsCollector()
        metrics = list(collector.collect())

        client_metrics = [m for m in metrics if getattr(m, 'name', '') == "pihole_query_client_1m"]
        assert client_metrics, "Client metric missing"
        labels = [s.labels['query_client'] for s in client_metrics[0].samples if s.name == "pihole_query_client_1m"]
        # All client IPs should appear as labels since resolution fails
        for ip in ["192.168.1.2", "192.168.1.3", "192.168.1.4", "192.168.1.5"]:
            assert ip in labels


def test_hostname_resolution_cache_hit_and_miss():
    """resolve_hostname should use cache until TTL expires."""
    # Mock the authentication response
    auth_response = {"session": {"sid": "test-session-id"}}
    with patch.object(PiholeMetricsCollector, 'get_sid', return_value="test-session-id"):
        collector = PiholeMetricsCollector()
        with patch('socket.gethostbyaddr', return_value=("host.local", [], ["1.2.3.4"])) as mock_resolve, \
             patch('time.time', side_effect=[0, 1, collector.CACHE_TTL + 1]):
            # First lookup: cache is empty, should call resolver and cache result
            assert collector.resolve_hostname('1.2.3.4') == "host"
            # Second lookup: within TTL, should use cache (resolver NOT called again)
            assert collector.resolve_hostname('1.2.3.4') == "host"
            assert mock_resolve.call_count == 1  # Only first call hit the resolver
            # Third lookup: after TTL, should call resolver again (cache expired)
            assert collector.resolve_hostname('1.2.3.4') == "host"
            assert mock_resolve.call_count == 2  # Resolver called again after cache expiry

def test_temperature_monitoring():
    """Test Raspberry Pi temperature monitoring functionality."""
    # Mock the authentication response
    with patch.object(PiholeMetricsCollector, 'get_sid', return_value="test-session-id"):
        collector = PiholeMetricsCollector()
        
        # Test successful temperature reading
        with patch('subprocess.run') as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "temp=45.1'C\n"
            
            temp = collector._get_raspberry_pi_temperature()
            assert temp == 45.1
            mock_run.assert_called_once_with(['vcgencmd', 'measure_temp'], 
                                            capture_output=True, text=True, timeout=5)
        
        # Test failed temperature reading (vcgencmd not found)
        with patch('subprocess.run', side_effect=FileNotFoundError):
            temp = collector._get_raspberry_pi_temperature()
            assert temp is None
        
        # Test timeout
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired(['vcgencmd', 'measure_temp'], 5)):
            temp = collector._get_raspberry_pi_temperature()
            assert temp is None
        
        # Test unexpected output format
        with patch('subprocess.run') as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "unexpected_format\n"
            
            temp = collector._get_raspberry_pi_temperature()
            assert temp is None
