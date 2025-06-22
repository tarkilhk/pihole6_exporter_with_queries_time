#!/usr/bin/env python3

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os
from dotenv import load_dotenv
import pytest
import time
import json
import tempfile
import re
from unittest.mock import patch, MagicMock

# Standard package import
from logs_exporter.pihole6_logs_exporter import PiholeLogsExporter

# Load environment variables from .env file
load_dotenv()

# Load static test data
def load_static_queries():
    """Load the static test data from JSON file."""
    json_path = Path(__file__).parent / "static_queries.json"
    with open(json_path, 'r') as f:
        data = json.load(f)
    return data["queries"]

STATIC_QUERIES = load_static_queries()

# Mock API response with static data
STATIC_QUERIES_RESPONSE = {
    "queries": STATIC_QUERIES
}

# Mock hostname resolution
def mock_gethostbyaddr(ip):
    return (f"host-{ip.replace('.', '-')}.local", [], [ip])

def mock_get_api_call(api_path):
    """Mock get_api_call that filters static data based on from/until parameters."""
    # Parse the api_path to extract from and until parameters
    # Example: "queries?from=1750622700&until=1750623000&length=1000000"
    match = re.search(r'from=(\d+)&until=(\d+)', api_path)
    if not match:
        # If no parameters found, return all data
        return {"queries": STATIC_QUERIES}
    
    from_ts = int(match.group(1))
    until_ts = int(match.group(2))
    
    # Filter queries based on timestamp range
    filtered_queries = []
    for query in STATIC_QUERIES:
        query_time = query["time"]
        if from_ts < query_time <= until_ts:
            filtered_queries.append(query)
    
    return {"queries": filtered_queries}

class TestLogsExporterStatic:
    """Test logs exporter using static data."""
    
    def setup_method(self):
        """Set up test fixtures."""
        # Create temporary directory for state file
        self.temp_dir = os.path.join(os.path.dirname(__file__), "temp_test_dir")
        os.makedirs(self.temp_dir, exist_ok=True)
        self.state_file = os.path.join(self.temp_dir, "test_state.txt")
        
        # Mock current time to 22 JUN 2025 20:00:00 UTC
        self.mock_now = 1750622400  # 22 JUN 2025 20:00:00 UTC
        
        # Get Loki target from environment variable
        loki_target = os.getenv("LOKI_TARGET")
        if not loki_target:
            pytest.skip("No LOKI_TARGET environment variable set.")
        
        # Create exporter with mocked authentication
        with patch.object(PiholeLogsExporter, 'get_sid', return_value="mock-session-id"):
            self.exporter = PiholeLogsExporter(
                host="localhost",
                key="test-key",
                loki_target="http://test-loki:3100/loki/api/v1/push",
                state_file=self.state_file
            )
    
    def teardown_method(self):
        """Clean up test fixtures."""
        # Remove temporary directory
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_no_state_file_starts_from_epoch(self):
        """Test 1: No state file causes read_last_timestamp to return 0, fetching all entries."""
        
        # 1. Ensure state file doesn't exist
        assert not os.path.exists(self.state_file), "State file should not exist for this test"

        # 2. Call read_last_timestamp and verify it returns 0
        from_ts = self.exporter.read_last_timestamp()
        assert from_ts == 0, "read_last_timestamp should return 0 for a missing state file"

        # 3. Now, test the fetch_queries integration with this timestamp
        with patch.object(self.exporter, 'get_api_call', side_effect=mock_get_api_call):
            with patch('time.time', return_value=self.mock_now):
                until_ts = self.mock_now
                result = self.exporter.fetch_queries(from_ts, until_ts)
                
                # We expect 52 entries because we're querying from 0 to 1750622400 (20:00:00)
                # The static data has 51 entries between 19:50:00-20:00:00 + 1 old entry from 20 JUN 2025
                assert len(result) == 52, f"Expected 52 queries when starting from epoch, but got {len(result)}"
                
                print(f"✅ Test 1 passed: No state file, read_last_timestamp returned {from_ts}, fetched {len(result)} queries.")
    
    def test_fetch_queries_with_state_file_returns_correct_count(self):
        """Test 2: State file at 19:55:00 should return correct count."""
        
        # Create state file with timestamp at 22 JUN 2025 19:55:00
        state_timestamp = 1750622100  # 22 JUN 2025 19:55:00 (earlier than mock_now)
        with open(self.state_file, 'w') as f:
            f.write(str(state_timestamp))
        
        with patch.object(self.exporter, 'get_api_call', side_effect=mock_get_api_call):
            with patch('time.time', return_value=self.mock_now):
                from_ts = state_timestamp
                until_ts = self.mock_now
                
                result = self.exporter.fetch_queries(from_ts, until_ts)
                
                # Should get 25 entries between 19:55:00 and 20:00:00
                # (5 entries per minute for 5 minutes: 19:55, 19:56, 19:57, 19:58, 19:59)
                assert len(result) == 25, f"Expected 25 queries between 19:55:00 and 20:00:00, but got {len(result)}"
                
                print(f"✅ Test 2 passed: Retrieved {len(result)} queries (from 19:55:00 to 20:00:00)")
    
    def test_write_last_timestamp_updates_state_file(self):
        """Test 3: write_last_timestamp should update state file correctly."""
        
        with patch.object(self.exporter, 'get_api_call', side_effect=mock_get_api_call):
            with patch('time.time', return_value=self.mock_now):
                from_ts = 0
                until_ts = self.mock_now
                
                result = self.exporter.fetch_queries(from_ts, until_ts)
                max_timestamp = max(q["time"] for q in result)
                
                self.exporter.write_last_timestamp(max_timestamp)
                
                assert os.path.exists(self.state_file)
                
                with open(self.state_file, 'r') as f:
                    written_timestamp = int(f.read().strip())
                
                assert written_timestamp == max_timestamp
                
                print(f"✅ Test 3 passed: State file updated with timestamp {written_timestamp}")

    def test_format_for_loki_creates_correct_structure(self):
        """Test 4: Verify that format_for_loki creates the correct Loki stream structure."""
        
        # Create sample queries that match the structure from our static data
        sample_queries = [
            {
                "time": "1704067200",  # 2024-01-01 00:00:00 UTC
                "client": {"ip": "192.168.1.100"},
                "type": "A",
                "status": "gravity",
                "domain": "example.com"
            },
            {
                "time": "1704067260",  # 2024-01-01 00:01:00 UTC
                "client": {"ip": "192.168.1.101"},
                "type": "AAAA",
                "status": "forwarded",
                "domain": "google.com"
            },
            {
                "time": "1704067320",  # 2024-01-01 00:02:00 UTC
                "client": {"ip": "192.168.1.100"},  # Same client as first query
                "type": "A",
                "status": "gravity",
                "domain": "ads.example.com"
            }
        ]

        # Mock the resolve_hostname method to return predictable hostnames
        with patch.object(self.exporter, 'resolve_hostname', side_effect=lambda ip: f"host-{ip.split('.')[-1]}"):
            streams, max_timestamp = self.exporter.format_for_loki(sample_queries)

        # Verify the structure - each unique combination of labels creates a separate stream
        assert len(streams) == 3, f"Expected 3 unique streams (based on unique label combinations), got {len(streams)}"
        assert max_timestamp == 1704067320, f"Expected max timestamp 1704067320, got {max_timestamp}"

        # Find streams by their unique characteristics
        stream_example_com = None
        stream_google_com = None
        stream_ads_example_com = None
        
        for stream in streams:
            domain = stream['stream']['domain']
            if domain == 'example.com':
                stream_example_com = stream
            elif domain == 'google.com':
                stream_google_com = stream
            elif domain == 'ads.example.com':
                stream_ads_example_com = stream

        # Verify stream for example.com
        assert stream_example_com is not None, "Stream for example.com not found"
        assert len(stream_example_com['values']) == 1, f"Expected 1 value for example.com, got {len(stream_example_com['values'])}"
        
        # Check labels for stream_example_com
        expected_labels_example = {
            "job": "pihole_logs_exporter",
            "service": "pihole_query_log",
            "host": "localhost",
            "client_ip": "192.168.1.100",
            "client_name": "host-100",
            "type": "A",
            "status": "gravity",
            "domain": "example.com"
        }
        assert stream_example_com['stream'] == expected_labels_example, f"Labels mismatch for example.com: {stream_example_com['stream']}"

        # Check timestamp for stream_example_com (should be in nanoseconds)
        assert stream_example_com['values'][0][0] == "1704067200000000000", f"Timestamp should be 1704067200000000000, got {stream_example_com['values'][0][0]}"

        # Verify stream for google.com
        assert stream_google_com is not None, "Stream for google.com not found"
        assert len(stream_google_com['values']) == 1, f"Expected 1 value for google.com, got {len(stream_google_com['values'])}"
        
        # Check labels for stream_google_com
        expected_labels_google = {
            "job": "pihole_logs_exporter",
            "service": "pihole_query_log",
            "host": "localhost",
            "client_ip": "192.168.1.101",
            "client_name": "host-101",
            "type": "AAAA",
            "status": "forwarded",
            "domain": "google.com"
        }
        assert stream_google_com['stream'] == expected_labels_google, f"Labels mismatch for google.com: {stream_google_com['stream']}"

        # Verify stream for ads.example.com
        assert stream_ads_example_com is not None, "Stream for ads.example.com not found"
        assert len(stream_ads_example_com['values']) == 1, f"Expected 1 value for ads.example.com, got {len(stream_ads_example_com['values'])}"
        
        # Check labels for stream_ads_example_com
        expected_labels_ads = {
            "job": "pihole_logs_exporter",
            "service": "pihole_query_log",
            "host": "localhost",
            "client_ip": "192.168.1.100",
            "client_name": "host-100",
            "type": "A",
            "status": "gravity",
            "domain": "ads.example.com"
        }
        assert stream_ads_example_com['stream'] == expected_labels_ads, f"Labels mismatch for ads.example.com: {stream_ads_example_com['stream']}"

        # Verify that log values are JSON strings
        for stream in streams:
            for value in stream['values']:
                assert len(value) == 2, f"Each value should have [timestamp, log_line], got {value}"
                assert isinstance(value[1], str), f"Log line should be a string, got {type(value[1])}"
                # Verify it's valid JSON
                try:
                    json.loads(value[1])
                except json.JSONDecodeError:
                    assert False, f"Log line is not valid JSON: {value[1]}"

        print(f"✅ Test 4 passed: Loki formatting creates correct structure with {len(streams)} streams and {sum(len(s['values']) for s in streams)} total entries")

if __name__ == "__main__":
    # Run all tests
    test_instance = TestLogsExporterStatic()
    
    print("🧪 Running Logs Exporter Static Tests...")
    print("=" * 50)
    
    test_instance.setup_method()
    
    try:
        test_instance.test_no_state_file_starts_from_epoch()
        test_instance.test_fetch_queries_with_state_file_returns_correct_count()
        test_instance.test_write_last_timestamp_updates_state_file()
        test_instance.test_format_for_loki_creates_correct_structure()
        print("=" * 50)
        print("🎉 All tests passed!")
    finally:
        test_instance.teardown_method() 