#!/usr/bin/env python3

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os
from dotenv import load_dotenv
import pytest
import json
from unittest.mock import patch, MagicMock
from logs_exporter.pihole6_logs_exporter import PiholeLogsExporter
import time

# Load environment variables from .env file
load_dotenv()

# Load static test data structure for comparison
with open(Path(__file__).parent / "static_queries.json", "r") as f:
    STATIC_QUERIES = json.load(f)["queries"]

EXPECTED_FIELDS = set(STATIC_QUERIES[0].keys()) if STATIC_QUERIES else set()

def test_get_api_call_matches_static_structure():
    """
    Use PiholeLogsExporter.get_api_call to make a real API call to Pi-hole and verify the structure matches static_queries.json.
    """
    api_token = os.getenv("PIHOLE_API_TOKEN")
    pihole_host = os.getenv("PIHOLE_URL", "http://localhost:80")
    if not api_token:
        pytest.skip("No PIHOLE_API_TOKEN environment variable set.")
    exporter = PiholeLogsExporter(
        host=pihole_host,
        key=api_token,
        loki_target=os.getenv("LOKI_TARGET"),
        state_file="/tmp/irrelevant"
    )
    try:
        data = exporter.get_api_call("queries")
        print("Raw API response (first 500 chars):")
        print(json.dumps(data, indent=2)[:500] + "...")
    except Exception as e:
        pytest.skip(f"Could not reach Pi-hole API: {e}")
        return

    assert "queries" in data, f"API response missing 'queries' key: {data}"
    queries = data["queries"]
    assert isinstance(queries, list), f"'queries' should be a list, got {type(queries)}"
    if not queries:
        print("ℹ️  No queries returned by Pi-hole API (this is normal if Pi-hole is quiet)")
        return

    print(f"Number of queries returned: {len(queries)}")
    print("First query (full):")
    print(json.dumps(queries[0], indent=2))

    # Check that each query has the expected fields
    for i, query in enumerate(queries):
        assert isinstance(query, dict), f"Query at index {i} is not a dict: {query}"
        missing = EXPECTED_FIELDS - set(query.keys())
        assert not missing, f"Query at index {i} is missing fields: {missing}"
        print(f"✅ Query {i} matches expected structure.")
    print(f"✅ All {len(queries)} queries match the static structure.")
    
    # Logout from Pi-hole session
    exporter.logout()

def test_send_to_loki_integration():
    """
    Integration test: Send a test log stream to a real Loki backend.
    """
    # Get Loki target from environment variable
    loki_target = os.getenv("LOKI_TARGET")
    if not loki_target:
        pytest.skip("No LOKI_TARGET environment variable set.")
    
    exporter = PiholeLogsExporter(
        # host=os.getenv("PIHOLE_URL", "localhost"),
        # key=os.getenv("PIHOLE_API_TOKEN"),
        host=None,
        key=None,
        loki_target=loki_target,
        state_file="/tmp/irrelevant"
    )
    
    # Use the current time in nanoseconds for the log entry
    now_ns = str(int(time.time() * 1_000_000_000))
    
    # Create a flattened test log line dictionary
    log_line_dict = {
        "time": str(int(time.time())),
        "client_ip": "192.168.1.100",
        "client_name": "test-client",
        "type": "A",
        "status": "gravity",
        "domain": "test.example.com",
        "reply_type": "NODATA",
        "upstream": "localhost",
        "dnssec": "UNKNOWN",
        "cname": None,
        "ede_code": -1,
        "ede_text": None,
        "id": 12345,
        "list_id": None
    }
    
    # Create a test log stream in the format expected by Loki
    test_streams = [
        {
            "stream": {
                "job": "pihole_logs_exporter",
                "service": "pihole_query_log",
                "host": "test-host",
                "client_ip": "192.168.1.100",
                "client_name": "test-client",
                "type": "A",
                "status": "gravity",
                "domain": "test.example.com"
            },
            "values": [
                [now_ns, json.dumps(log_line_dict)]
            ]
        }
    ]
    
    try:
        # Call the send_to_loki method with real Loki backend
        exporter.send_to_loki(test_streams)
        print(f"✅ Successfully sent test log to Loki at: {exporter.get_loki_url()}")
        print("Payload sent to Loki:")
        payload = {"streams": test_streams}
        print(json.dumps(payload, indent=2))
        
    except Exception as e:
        print(f"❌ Failed to send to Loki: {e}")
        print("This might be because:")
        print("  - Loki is not running at the specified target")
        print("  - Network connectivity issues")
        print("  - Loki requires authentication")
        pytest.skip(f"Loki integration test failed: {e}")
    finally:
        # Logout from Pi-hole session
        exporter.logout()

def test_end_to_end_pihole_to_loki():
    """
    Full end-to-end integration test:
    1. Fetches real data from Pi-hole API.
    2. Formats it for Loki (including flattening).
    3. Sends it to a real Loki backend.
    """
    # --- 1. Setup ---
    api_token = os.getenv("PIHOLE_API_TOKEN")
    loki_target = os.getenv("LOKI_TARGET")
    pihole_host = os.getenv("PIHOLE_URL", "http://localhost:80")
    
    if not api_token:
        pytest.skip("PIHOLE_API_TOKEN environment variable not set.")
    if not loki_target:
        pytest.skip("LOKI_TARGET environment variable not set.")
        
    exporter = PiholeLogsExporter(
        host=pihole_host,
        key=api_token,
        loki_target=loki_target,
        state_file="/tmp/irrelevant"
    )

    # --- 2. Fetch real data from Pi-hole ---
    try:
        print("STEP 1: Fetching recent queries from Pi-hole...")
        # Fetch queries from the last minute to get a small, recent sample
        queries = exporter.fetch_queries(int(time.time()) - 60, int(time.time()))
        if not queries:
            pytest.skip("No queries found in Pi-hole in the last minute to test with.")
        print(f"✅ Found {len(queries)} queries. Using the first one for the test.")
        
        # Take just the first query for our test
        test_query = [queries[0]] 
        
    except Exception as e:
        pytest.skip(f"Could not fetch data from Pi-hole API: {e}")
        return

    # --- 3. Format for Loki ---
    print("\nSTEP 2: Formatting the query for Loki...")
    loki_streams, max_ts = exporter.format_for_loki(test_query)
    
    assert loki_streams, "Formatting for Loki did not produce any streams."
    print("✅ Query formatted successfully. Payload to be sent:")
    print(json.dumps({"streams": loki_streams}, indent=2))

    # --- 4. Send to Loki ---
    try:
        print("\nSTEP 3: Sending formatted log to Loki...")
        exporter.send_to_loki(loki_streams)
        print(f"✅ Successfully sent end-to-end test log to Loki at: {exporter.get_loki_url()}")

    except Exception as e:
        pytest.fail(f"End-to-end test failed while sending to Loki: {e}")
    finally:
        # Logout from Pi-hole session
        exporter.logout()

if __name__ == "__main__":
    test_get_api_call_matches_static_structure()
    test_send_to_loki_integration()
    test_end_to_end_pihole_to_loki() 