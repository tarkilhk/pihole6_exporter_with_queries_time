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
                state_file=self.state_file,
                server_name="test-server"
            )
    
    def teardown_method(self):
        """Clean up test fixtures."""
        # Remove temporary directory
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_no_state_file_starts_from_epoch(self):
        """Test 1: No state file causes read_last_timestamp to return now-1h, fetching recent entries only."""
        # 1. Ensure state file doesn't exist
        assert not os.path.exists(self.state_file), "State file should not exist for this test"

        # 2. Call read_last_timestamp and verify it returns now-1h
        with patch('time.time', return_value=self.mock_now):
            from_ts = self.exporter.read_last_timestamp()
            assert from_ts == self.mock_now - 3600, f"read_last_timestamp should return now-1h for a missing state file, got {from_ts}"

        # 3. Now, test the fetch_queries integration with this timestamp
        with patch.object(self.exporter, 'get_api_call', side_effect=mock_get_api_call):
            with patch('time.time', return_value=self.mock_now):
                until_ts = self.mock_now
                result = self.exporter.fetch_queries(from_ts, until_ts)
                # We expect 51 entries because we're querying from 19:00:00 to 20:00:00 (1 hour)
                assert len(result) == 51, f"Expected 51 queries when starting from now-1h, but got {len(result)}"
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

        # Verify the structure - only low-cardinality labels create streams.
        # The two A/gravity queries differ by domain but share one stream.
        assert len(streams) == 2, f"Expected 2 unique streams (based on low-cardinality label combinations), got {len(streams)}"
        assert max_timestamp == 1704067320, f"Expected max timestamp 1704067320, got {max_timestamp}"

        # Find streams by their low-cardinality labels
        stream_a_gravity = None
        stream_aaaa_forwarded = None

        for stream in streams:
            assert "domain" not in stream['stream']
            assert "client_ip" not in stream['stream']
            assert "client_name" not in stream['stream']
            assert "service_name" not in stream['stream']

            if stream['stream']['type'] == 'A' and stream['stream']['status'] == 'gravity':
                stream_a_gravity = stream
            elif stream['stream']['type'] == 'AAAA' and stream['stream']['status'] == 'forwarded':
                stream_aaaa_forwarded = stream

        # Verify stream for A/gravity
        assert stream_a_gravity is not None, "Stream for A/gravity not found"
        assert len(stream_a_gravity['values']) == 2, f"Expected 2 values for A/gravity, got {len(stream_a_gravity['values'])}"

        # Check labels for stream_a_gravity
        expected_labels_a_gravity = {
            "job": "pihole_logs_exporter",
            "service": "pihole_query_log",
            "server": "test-server",
            "host": "localhost",
            "type": "A",
            "status": "gravity",
        }
        assert stream_a_gravity['stream'] == expected_labels_a_gravity, f"Labels mismatch for A/gravity: {stream_a_gravity['stream']}"

        # Check timestamps and structured metadata for stream_a_gravity
        assert stream_a_gravity['values'][0][0] == "1704067200000000000", f"Timestamp should be 1704067200000000000, got {stream_a_gravity['values'][0][0]}"
        assert stream_a_gravity['values'][0][2] == {
            "domain": "example.com",
            "client_ip": "192.168.1.100",
            "client_name": "host-100",
        }
        assert stream_a_gravity['values'][1][2] == {
            "domain": "ads.example.com",
            "client_ip": "192.168.1.100",
            "client_name": "host-100",
        }

        # Verify stream for google.com
        assert stream_aaaa_forwarded is not None, "Stream for AAAA/forwarded not found"
        assert len(stream_aaaa_forwarded['values']) == 1, f"Expected 1 value for AAAA/forwarded, got {len(stream_aaaa_forwarded['values'])}"
        
        # Check labels for stream_aaaa_forwarded
        expected_labels_aaaa_forwarded = {
            "job": "pihole_logs_exporter",
            "service": "pihole_query_log",
            "server": "test-server",
            "host": "localhost",
            "type": "AAAA",
            "status": "forwarded",
        }
        assert stream_aaaa_forwarded['stream'] == expected_labels_aaaa_forwarded, f"Labels mismatch for AAAA/forwarded: {stream_aaaa_forwarded['stream']}"
        assert stream_aaaa_forwarded['values'][0][2] == {
            "domain": "google.com",
            "client_ip": "192.168.1.101",
            "client_name": "host-101",
        }

        # Verify that log values include structured metadata and JSON strings
        for stream in streams:
            for value in stream['values']:
                assert len(value) == 3, f"Each value should have [timestamp, log_line, structured_metadata], got {value}"
                assert isinstance(value[1], str), f"Log line should be a string, got {type(value[1])}"
                assert isinstance(value[2], dict), f"Structured metadata should be a dict, got {type(value[2])}"
                # Verify it's valid JSON
                try:
                    parsed_log = json.loads(value[1])
                except json.JSONDecodeError:
                    assert False, f"Log line is not valid JSON: {value[1]}"
                assert "domain" in parsed_log
                assert "client_ip" in parsed_log
                assert "client_name" in parsed_log

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
