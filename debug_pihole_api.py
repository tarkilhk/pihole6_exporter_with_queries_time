#!/usr/bin/env python3

import requests
import urllib3
import json
import time
from datetime import datetime

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def get_api_call(host, api_path, sid=None):
    url = f"https://{host}:443/api/{api_path}"
    headers = {"accept": "application/json"}
    if sid:
        headers["sid"] = sid
    
    try:
        req = requests.get(url, verify=False, headers=headers, timeout=10)
        if req.status_code == 200:
            return req.json()
        else:
            print(f"API call failed with status {req.status_code}: {req.text}")
            return None
    except Exception as e:
        print(f"Error calling API: {e}")
        return None

def main():
    host = "localhost"  # Change this if needed
    
    print("=== Pi-hole API Debug Tool ===")
    print(f"Testing API calls to: {host}")
    print()
    
    # Test basic connectivity
    print("1. Testing basic summary API...")
    summary = get_api_call(host, "stats/summary")
    if summary:
        print("✅ Summary API working")
        print(f"   Total queries (24h): {summary.get('queries', {}).get('total', 'N/A')}")
    else:
        print("❌ Summary API failed")
        return
    
    print()
    
    # Test queries API with recent data
    print("2. Testing queries API (last 5 minutes)...")
    now = int(time.time())
    five_min_ago = now - 300
    
    queries_response = get_api_call(host, f"queries?from={five_min_ago}&until={now}&length=10")
    
    if queries_response and "queries" in queries_response:
        queries = queries_response["queries"]
        print(f"✅ Queries API working - found {len(queries)} queries in last 5 minutes")
        
        if len(queries) > 0:
            print("\n3. Analyzing first query structure...")
            first_query = queries[0]
            print("   Available fields:")
            for key, value in first_query.items():
                print(f"     {key}: {value} ({type(value).__name__})")
            
            print("\n4. Checking for latency data...")
            latency_fields = []
            for query in queries[:5]:  # Check first 5 queries
                for key in query.keys():
                    if 'time' in key.lower() or 'latency' in key.lower() or 'duration' in key.lower():
                        if key not in latency_fields:
                            latency_fields.append(key)
            
            if latency_fields:
                print(f"   Found potential latency fields: {latency_fields}")
                for field in latency_fields:
                    values = [q.get(field) for q in queries[:5] if q.get(field) is not None]
                    print(f"     {field} sample values: {values}")
            else:
                print("   ❌ No obvious latency/timing fields found")
                
            print("\n5. Sample query data (first query):")
            print(json.dumps(first_query, indent=2))
            
        else:
            print("   ⚠️  No queries found in last 5 minutes")
            print("   Try making some DNS queries and run this script again")
    else:
        print("❌ Queries API failed or returned no data")
        
    print("\n=== Debug Complete ===")

if __name__ == "__main__":
    main() 