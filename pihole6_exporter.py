#!/usr/bin/env python3

import os
import time
import requests
import urllib3
import logging
import argparse
from prometheus_client import Histogram, Counter
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily, REGISTRY
from prometheus_client.registry import Collector
from prometheus_client import start_http_server

class PiholeCollector(Collector):


    def __init__(self, host="localhost", key=None):

        self.using_auth = False
        # Disable if you've actually got a good cert set up.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self.host = host

        # Try to get API key from environment variable if not provided
        if key is None:
            key = os.getenv('PIHOLE_API_TOKEN')
            
        # Try to get API key from file if still not available
        if key is None and os.path.exists('/etc/pihole6_exporter/api_token'):
            try:
                with open('/etc/pihole6_exporter/api_token', 'r') as f:
                    key = f.read().strip()
            except Exception as e:
                logging.error(f"Failed to read API token from file: {e}")

        if key is not None:
            self.using_auth = True
            self.sid = self.get_sid(key)
        else:
            logging.warning("No API token provided. Some metrics may not be available.")

        self.type_cnt = {}
        self.status_cnt = {}
        self.reply_cnt = {}
        self.client_cnt = {}
        self.upstream_cnt = {}
        self.timeout_cnt = 0  # Track DNS timeouts
        self.debug_logged = False  # Only log query structure once per run

        # DNS latency histogram
        self.dns_latency = Histogram(
            name='pihole_dns_latency_seconds',
            documentation='DNS query latency in seconds',
            registry=None,  # Don't auto-register to avoid conflicts
            buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0],
            labelnames=['status']
        )
        logging.info("DNS latency histogram initialized")


    def get_sid(self, key):
        auth_url = "https://" + self.host + ":443/api/auth"
        headers = {"accept": "application/json", "content_type": "application/json"}
        json_data = {"password": key}
        req = requests.post(auth_url, verify = False, headers = headers, json = json_data)
        
        reply = req.json()
        return reply['session']['sid']


    def get_api_call(self, api_path):
        url = "https://" + self.host + ":443/api/" + api_path
        if self.using_auth:
            headers = {"accept": "application/json", "sid": self.sid}
        else:
            headers = {"accept": "application/json"}
        req = requests.get(url, verify = False, headers = headers)
        
        if req.status_code != 200:
            logging.error(f"API call failed with status {req.status_code}: {req.text}")
            raise Exception(f"API call failed with status {req.status_code}")
            
        try:
            reply = req.json()
            logging.debug(f"API response for {api_path}: {reply}")
            return reply
        except Exception as e:
            logging.error(f"Failed to parse JSON response: {e}")
            logging.error(f"Raw response: {req.text}")
            raise


    def clear_cnts(self):
        keys = self.type_cnt.keys()
        for k in list(keys):
            if self.type_cnt[k] != 0:
                self.type_cnt[k] = 0
            else:
                del self.type_cnt[k]

        keys = self.status_cnt.keys()
        for k in list(keys):
            if self.status_cnt[k] != 0:
                self.status_cnt[k] = 0
            else:
                del self.status_cnt[k]

        keys = self.reply_cnt.keys()
        for k in list(keys):
            if self.reply_cnt[k] != 0:
                self.reply_cnt[k] = 0
            else:
                del self.reply_cnt[k]

        keys = self.client_cnt.keys()
        for k in list(keys):
            if self.client_cnt[k] != 0:
                self.client_cnt[k] = 0
            else:
                del self.client_cnt[k]

        keys = self.upstream_cnt.keys()
        for k in list(keys):
            if self.upstream_cnt[k] != 0:
                self.upstream_cnt[k] = 0
            else:
                del self.upstream_cnt[k]

        # Reset timeout counter
        self.timeout_cnt = 0


    def _process_query(self, q):
        query_type = q["type"]
        status = q["status"]
        replytype = q["reply"]["type"]
        client = q["client"]["ip"]
        upstream = q["upstream"]
        
        # DEBUG: Log the structure of the first query to understand available fields
        if not self.debug_logged:
            logging.info("=== DEBUG: First query structure ===")
            for key, value in q.items():
                logging.info(f"  {key}: {value} ({type(value).__name__})")
            
            # Look for potential latency fields
            latency_candidates = []
            for key in q.keys():
                if any(term in key.lower() for term in ['time', 'latency', 'duration', 'response', 'reply']):
                    latency_candidates.append(key)
            
            if latency_candidates:
                logging.info(f"=== Potential latency fields: {latency_candidates} ===")
                for field in latency_candidates:
                    logging.info(f"  {field}: {q.get(field)}")
            else:
                logging.info("=== No obvious latency fields found ===")
            
            self.debug_logged = True
        if upstream is None:
            if status in ("GRAVITY", "CACHE", "SPECIAL_DOMAIN"):
                upstream = f"None-{status}"
            else:
                upstream = "None-OTHER"

        if query_type in self.type_cnt:
            self.type_cnt[query_type] += 1
        else:
            self.type_cnt[query_type] = 1

        if status in self.status_cnt:
            self.status_cnt[status] += 1
        else:
            self.status_cnt[status] = 1

        if replytype in self.reply_cnt:
            self.reply_cnt[replytype] += 1
        else:
            self.reply_cnt[replytype] = 1

        if client in self.client_cnt:
            self.client_cnt[client] += 1
        else:
            self.client_cnt[client] = 1

        if upstream in self.upstream_cnt:
            self.upstream_cnt[upstream] += 1
        else:
            self.upstream_cnt[upstream] = 1

        # Track DNS latency - try multiple possible field names
        reply_time = None
        latency_field_found = None
        
        reply_time = q['reply']['time']
        latency_field_found = 'reply.time'

        if latency_field_found:
            logging.debug(f"Found latency field '{latency_field_found}': {reply_time}")
        
        if reply_time is not None and isinstance(reply_time, (int, float)) and reply_time >= 0:
            status_label = "cache" if status == "CACHED" else "forwarded"
            self.dns_latency.labels(status=status_label).observe(reply_time)
            logging.debug(f"Observed latency: {reply_time}s for {status_label}")
        else:
            logging.debug(f"No valid latency data: field={latency_field_found}, value={reply_time}, type={type(reply_time) if reply_time is not None else 'None'}")

        # Track DNS timeouts
        rcode = q.get("rcode", "")
        if rcode == "TIMEOUT" or status == "TIMEOUT":
            self.timeout_cnt += 1
            logging.info(f"Timeout detected: rcode={rcode}, status={status}")


    def collect(self):
        logging.info("beginning scrape...")

        try:
            reply = self.get_api_call("stats/summary")
            
            if not isinstance(reply, dict):
                logging.error(f"Expected dict response, got {type(reply)}")
                raise Exception("Invalid API response format")
                
            if "queries" not in reply:
                logging.error(f"Missing 'queries' in response: {reply}")
                raise Exception("Missing 'queries' in API response")
                
            if "types" not in reply["queries"]:
                logging.error(f"Missing 'types' in queries: {reply['queries']}")
                raise Exception("Missing 'types' in queries")

            query_types = GaugeMetricFamily("pihole_query_by_type",
                    "Count of queries by type (24h)", labels=["query_type"])

            for item in reply["queries"]["types"].items():
                labels = [item[0]]
                value = item[1]
                query_types.add_metric(labels, value)

            yield query_types

            status_types = GaugeMetricFamily("pihole_query_by_status",
                    "Count of queries by status over 24h", labels=["query_status"])

            for item in reply["queries"]["status"].items():
                labels = [item[0]]
                value = item[1]
                status_types.add_metric(labels, value)

            yield status_types

            reply_types = GaugeMetricFamily("pihole_query_replies",
                    "Count of replies by type over 24h", labels=["reply_type"])

            for item in reply["queries"]["replies"].items():
                labels = [item[0]]
                value = item[1]
                reply_types.add_metric(labels, value)

            yield reply_types

            total_counts = GaugeMetricFamily("pihole_query_count",
                    "Query counts by category, 24h", labels=["category"])
            
            total_counts.add_metric(["total"], reply["queries"]["total"])
            total_counts.add_metric(["blocked"], reply["queries"]["blocked"])
            total_counts.add_metric(["unique"], reply["queries"]["unique_domains"])
            total_counts.add_metric(["forwarded"], reply["queries"]["forwarded"])
            total_counts.add_metric(["cached"], reply["queries"]["cached"])

            yield total_counts

            # Yes, I skipped percent_blocked.  We can calculate that in Grafana.
            # Better to not provide data that can be derived from other data.

            clients = GaugeMetricFamily("pihole_client_count",
                    "Total/active client counts", labels=["category"])
            
            clients.add_metric(["active"], reply["clients"]["active"])
            clients.add_metric(["total"], reply["clients"]["total"])

            yield clients

            gravity = GaugeMetricFamily("pihole_domains_being_blocked",
                    "Number of domains on current blocklist", labels=[])
            
            gravity.add_metric([], reply["gravity"]["domains_being_blocked"])

            yield gravity

            reply = self.get_api_call("stats/upstreams")
            
            upstreams = GaugeMetricFamily("pihole_query_upstream_count",
                    "Total query upstream counts (24h)", labels=["ip", "name", "port"])

            for item in reply["upstreams"]:
                labels = [item["ip"], item["name"], str(item["port"])]
                value = item["count"]
                upstreams.add_metric(labels, value)

            yield upstreams

            now = int(time.time())
            last_min = now // 60 * 60
            min_before = last_min - 60

            reply = self.get_api_call("queries?from=" + str(min_before) + "&until=" + str(last_min) + "&length=1000000")

            self.clear_cnts()

            query_count = len(reply.get("queries", []))
            logging.info(f"Processing {query_count} queries from last minute")

            for q in reply["queries"]:
                self._process_query(q)

            q_type = GaugeMetricFamily("pihole_query_type_1m", "Count of query types (last whole 1m)",
                                       labels=["query_type"])
            for t in self.type_cnt.items():
                q_type.add_metric([t[0]], t[1], last_min)

            yield q_type

            q_status = GaugeMetricFamily("pihole_query_status_1m", "Count of query status (last whole 1m)",
                                       labels=["query_status"])
            for s in self.status_cnt.items():
                q_status.add_metric([s[0]], s[1], last_min)

            yield q_status

            q_reply = GaugeMetricFamily("pihole_query_reply_1m", "Count of query reply types (last whole 1m)",
                                       labels=["query_reply"])
            for r in self.reply_cnt.items():
                q_reply.add_metric([r[0]], r[1], last_min)

            yield q_reply

            q_client = GaugeMetricFamily("pihole_query_client_1m", "Count of query clients (last whole 1m)",
                                       labels=["query_client"])
            for c in self.client_cnt.items():
                q_client.add_metric([c[0]], c[1], last_min)

            yield q_client

            q_up = GaugeMetricFamily("pihole_query_upstream_1m", "Count of query upstream destinations (last whole 1m)",
                                       labels=["query_upstream"])
            for u in self.upstream_cnt.items():
                q_up.add_metric([str(u[0])], u[1], last_min)

            yield q_up

            # Cache hit ratio gauge (from 24h summary data)
            # Note: We need to get the summary data again since 'reply' now contains query data
            summary_reply = self.get_api_call("stats/summary")
            total_queries = summary_reply["queries"]["total"] if "queries" in summary_reply and "total" in summary_reply["queries"] else 0
            cached_queries = summary_reply["queries"]["cached"] if "queries" in summary_reply and "cached" in summary_reply["queries"] else 0
            
            cache_hit_ratio = GaugeMetricFamily("pihole_cache_hit_ratio_percent", 
                                              "Cache hit ratio as percentage (24h)")
            if total_queries > 0:
                ratio = (cached_queries / total_queries) * 100
                cache_hit_ratio.add_metric([], ratio)
                logging.info(f"Cache hit ratio: {ratio:.2f}% ({cached_queries}/{total_queries})")
            else:
                cache_hit_ratio.add_metric([], 0)
                logging.info("Cache hit ratio: 0% (no queries)")
            
            yield cache_hit_ratio

            # DNS timeouts counter (from last minute data)
            dns_timeouts = CounterMetricFamily("pihole_dns_timeouts", 
                                             "Total DNS timeout queries (last whole 1m)")
            dns_timeouts.add_metric([], self.timeout_cnt, last_min)
            logging.info(f"DNS timeouts in last minute: {self.timeout_cnt}")
            
            yield dns_timeouts

            # Add latency histogram metrics
            histogram_metrics = list(self.dns_latency.collect())
            logging.info(f"Yielding {len(histogram_metrics)} latency histogram metrics")
            for metric in histogram_metrics:
                yield metric

            logging.info("scrape completed")
        except Exception as e:
            logging.error(f"Error during collection: {e}")
            logging.error("Scrape aborted")


if __name__ == '__main__':
    
    parser = argparse.ArgumentParser(description="Prometheus exporter for Pi-hole version 6+")

    parser.add_argument("-H", "--host", dest="host", type=str, required=False, help="hostname/ip of pihole instance (default localhost)", default="localhost")
    parser.add_argument("-p", "--port", dest="port", type=int, required=False, help="port to expose for scraping (default 9666)", default=9666)
    parser.add_argument("-k", "--key", dest="key", type=str, required=False, help="authentication token (if required)", default=None)
    parser.add_argument("-l", "--log-level", dest="log_level", type=str, required=False, help="logging level (DEBUG, INFO, WARNING, ERROR)", default="INFO")

    args = parser.parse_args()

    # Set logging level based on argument
    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    logging.basicConfig(format='level="%(levelname)s" message="%(message)s"', level=log_level)

    start_http_server(args.port)
    logging.info("Exporter HTTP endpoint started")

    REGISTRY.register(PiholeCollector(args.host, args.key))
    logging.info("Ready to collect data")
    while True:
        time.sleep(1)

