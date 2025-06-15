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
            "reply_time": 0.001,
            "rcode": "NOERROR",
            "reply": {"type": "A"},
            "client": {"ip": "192.168.1.2"},
            "upstream": "8.8.8.8"
        },
        {
            "timestamp": 1234567891,
            "type": "AAAA",
            "status": "FORWARDED",
            "reply_time": 0.05,
            "rcode": "NOERROR",
            "reply": {"type": "AAAA"},
            "client": {"ip": "192.168.1.3"},
            "upstream": "1.1.1.1"
        }
    ]
}

def test_collect_yields_expected_metrics():
    """Integration test: ensure collect yields expected metrics from real code path."""
    with patch.object(PiholeCollector, 'get_api_call', side_effect=[SUMMARY_RESPONSE, UPSTREAMS_RESPONSE, QUERIES_RESPONSE]):
        collector = PiholeCollector()
        metrics = list(collector.collect())
        # Check that at least one known metric is present
        metric_names = [m.name for m in metrics if isinstance(m, GaugeMetricFamily)]
        assert "pihole_query_by_type" in metric_names
        assert "pihole_query_by_status" in metric_names
        assert "pihole_query_count" in metric_names
        assert "pihole_query_type_1m" in metric_names
        # Optionally, check values/labels for more detail 