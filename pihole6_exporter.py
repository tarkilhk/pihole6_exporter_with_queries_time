#!/usr/bin/env python3

import os
import time
import requests
import urllib3
import logging
import argparse
import psutil
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily, REGISTRY
from prometheus_client.registry import Collector
from prometheus_client import start_http_server
import socket

class PiholeCollector(Collector):

    CACHE_TTL = 3600


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
        self.hostname_cache = {}

        # DNS error tracking
        self.error_cnt = {}  # Track DNS errors by rcode
        self.total_queries_processed = 0  # Track total queries for rate calculation

        # DNS latency tracking - manually track histogram data
        self.latency_buckets = [0.0001, 0.0005, 0.001, 0.01, 0.1, 0.5, 1.0, 2.5]
        self.latency_counts = {}  # {status: {bucket: count}}
        self.latency_sums = {}    # {status: total_sum}
        self.latency_total_counts = {}  # {status: total_count}
        logging.info("DNS latency tracking initialized with manual buckets")


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

        # Reset error tracking
        self.error_cnt = {}
        self.total_queries_processed = 0
        
        # Reset latency tracking
        self.latency_counts = {}
        self.latency_sums = {}
        self.latency_total_counts = {}

    def _get_raspberry_pi_temperature(self):
        """Return Raspberry Pi CPU temperature in Celsius or None if unavailable."""
        try:
            import subprocess
            result = subprocess.run(['vcgencmd', 'measure_temp'], 
                                  capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                # Parse output like "temp=45.1'C"
                temp_str = result.stdout.strip()
                if temp_str.startswith('temp=') and temp_str.endswith("'C"):
                    temp_value = float(temp_str[5:-2])  # Extract the numeric part
                    return temp_value
                else:
                    logging.debug(f"Unexpected temperature output format: {temp_str}")
            else:
                logging.debug(f"vcgencmd failed with return code {result.returncode}: {result.stderr}")
        except subprocess.TimeoutExpired:
            logging.debug("vcgencmd measure_temp timed out")
        except FileNotFoundError:
            logging.debug("vcgencmd not found - not running on Raspberry Pi")
        except Exception as e:
            logging.debug(f"Error getting temperature: {e}")
        return None

    def _collect_system_metrics(self):
        mem = psutil.virtual_memory()
        metrics = [
            GaugeMetricFamily(
                "system_memory_usage_bytes",
                "Pihole used memory in bytes",
                value=mem.used,
            ),
            GaugeMetricFamily(
                "system_memory_total_bytes",
                "Pihole total memory in bytes",
                value=mem.total,
            ),
            GaugeMetricFamily(
                "system_cpu_usage_percent",
                "Pihole CPU usage percentage",
                value=psutil.cpu_percent(interval=None),
            ),
        ]

        # Cross-platform load average
        if hasattr(os, "getloadavg"):
            try:
                load1, load5, load15 = os.getloadavg()
                metrics.extend(
                    [
                        GaugeMetricFamily("system_load1", "1 minute load average", value=load1),
                        GaugeMetricFamily("system_load5", "5 minute load average", value=load5),
                        GaugeMetricFamily("system_load15", "15 minute load average", value=load15),
                    ]
                )
            except OSError as e:
                logging.debug(f"Load average not available: {e}")
        else:
            logging.debug("Load average not available on this OS.")

        disk = psutil.disk_usage("/")
        metrics.extend(
            [
                GaugeMetricFamily(
                    "system_disk_usage_bytes",
                    "Pihole used disk space on root filesystem in bytes",
                    value=disk.used,
                ),
                GaugeMetricFamily(
                    "system_disk_total_bytes",
                    "Pihole total disk space on root filesystem in bytes",
                    value=disk.total,
                ),
            ]
        )

        net = psutil.net_io_counters()
        metrics.extend(
            [
                CounterMetricFamily(
                    "system_network_receive_bytes",
                    "Pihole total bytes received on all interfaces",
                    value=net.bytes_recv,
                ),
                CounterMetricFamily(
                    "system_network_transmit_bytes",
                    "Pihole total bytes transmitted on all interfaces",
                    value=net.bytes_sent,
                ),
            ]
        )

        # Raspberry Pi temperature
        temp = self._get_raspberry_pi_temperature()
        if temp is not None:
            metrics.append(
                GaugeMetricFamily(
                    "system_temperature_celsius",
                    "Pihole CPU temperature in Celsius",
                    value=temp,
                )
            )
            logging.debug(f"Temperature: {temp}°C")
        else:
            logging.debug("Temperature not available")

        return metrics

    def resolve_hostname(self, ip):
        """Resolve an IP address to a hostname with caching. Returns only the short hostname."""
        now = time.time()
        cached = self.hostname_cache.get(ip)
        if cached and now - cached[1] < self.CACHE_TTL:
            return cached[0]
        try:
            fqdn = socket.gethostbyaddr(ip)[0]
            hostname = fqdn.split('.')[0] if fqdn else ip
        except Exception:
            hostname = ip
        self.hostname_cache[ip] = (hostname, now)
        return hostname


    def _process_query(self, q):
        query_type = q["type"]
        status = q["status"]
        replytype = q["reply"]["type"]
        client = self.resolve_hostname(q["client"]["ip"])
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
            # Categorize Pi-hole status into meaningful groups for latency tracking
            if status in ("CACHE", "CACHE_STALE"):
                status_label = "cache"
            elif status == "FORWARDED":
                status_label = "forwarded"
            elif status in ("GRAVITY", "REGEX", "DENYLIST", "GRAVITY_CNAME", "REGEX_CNAME", "DENYLIST_CNAME",
                          "EXTERNAL_BLOCKED_IP", "EXTERNAL_BLOCKED_NULL", "EXTERNAL_BLOCKED_NXRA", 
                          "EXTERNAL_BLOCKED_EDE15", "SPECIAL_DOMAIN"):
                status_label = "blocked"
            elif status in ("RETRIED", "RETRIED_DNSSEC"):
                status_label = "retried"
            elif status == "IN_PROGRESS":
                status_label = "in_progress"
            elif status in ("DBBUSY", "UNKNOWN"):
                status_label = "other"
            else:
                # Fallback for unknown statuses - log them for debugging
                status_label = "unknown"
                logging.warning(f"Unknown Pi-hole status '{status}' encountered, categorizing as 'unknown'")
            
            # Manually track histogram data
            if status_label not in self.latency_counts:
                self.latency_counts[status_label] = {str(bucket): 0 for bucket in self.latency_buckets}
                self.latency_sums[status_label] = 0.0
                self.latency_total_counts[status_label] = 0
            
            # Update sum and total count
            self.latency_sums[status_label] += reply_time
            self.latency_total_counts[status_label] += 1
            
            # Update bucket counts (cumulative)
            for bucket in self.latency_buckets:
                if reply_time <= bucket:
                    self.latency_counts[status_label][str(bucket)] += 1
            
            logging.debug(f"Tracked latency: {reply_time}s for status '{status}' (labeled as '{status_label}')")
        else:
            logging.debug(f"No valid latency data: field={latency_field_found}, value={reply_time}, type={type(reply_time) if reply_time is not None else 'None'}")

        # Track DNS timeouts
        rcode = q.get("rcode", "")
        if rcode == "TIMEOUT" or status == "TIMEOUT":
            self.timeout_cnt += 1
            logging.info(f"Timeout detected: rcode={rcode}, status={status}")

        # Track DNS errors by rcode
        self.total_queries_processed += 1
        
        # Count errors (excluding NOERROR which is success)
        if rcode and rcode != "NOERROR":
            if rcode in self.error_cnt:
                self.error_cnt[rcode] += 1
            else:
                self.error_cnt[rcode] = 1
            logging.debug(f"DNS error detected: rcode={rcode}, status={status}")


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



            # DNS timeouts gauge (from last minute data)
            dns_timeouts_gauge = GaugeMetricFamily("pihole_dns_timeouts_1m", 
                                                 "Total DNS timeout queries (last whole 1m)")
            dns_timeouts_gauge.add_metric([], self.timeout_cnt, last_min)
            logging.info(f"DNS timeouts in last minute: {self.timeout_cnt}")
            
            yield dns_timeouts_gauge

            # DNS error gauges - export raw counts, let Prometheus compute rates
            dns_errors_gauge = GaugeMetricFamily("pihole_dns_errors_1m", 
                                               "Total DNS errors by rcode (last whole 1m)", 
                                               labels=["rcode"])
            
            # Define common DNS error codes to always export (even with zero values)
            # Based on standard DNS response codes (rcode) that represent actual errors
            common_error_codes = ["SERVFAIL", "NXDOMAIN", "REFUSED", "FORMERR", "NOTIMP"]
            
            # Create a complete error count dict with all common codes initialized to 0
            complete_error_counts = {rcode: 0 for rcode in common_error_codes}
            # Update with actual error counts
            complete_error_counts.update(self.error_cnt)
            
            # Always export all error codes (including zeros)
            for rcode, count in complete_error_counts.items():
                dns_errors_gauge.add_metric([rcode], count, last_min)
                if count > 0:
                    logging.info(f"DNS errors for {rcode}: {count} in last minute")
            
            # Log if no errors occurred
            if sum(complete_error_counts.values()) == 0:
                logging.info("No DNS errors in last minute")
            
            # Also add total queries processed as a gauge for rate calculations
            total_queries_processed_gauge = GaugeMetricFamily("pihole_dns_queries_processed_1m",
                                                            "Total DNS queries processed (last whole 1m)")
            total_queries_processed_gauge.add_metric([], self.total_queries_processed, last_min)
            logging.info(f"Total queries processed in last minute: {self.total_queries_processed}")
            
            yield dns_errors_gauge
            yield total_queries_processed_gauge

            # Create HistogramMetricFamily from manually tracked data
            if self.latency_counts:
                from prometheus_client.core import HistogramMetricFamily
                latency_histogram = HistogramMetricFamily(
                    "pihole_dns_latency_seconds_1m",
                    "DNS query latency in seconds (1m)",
                    labels=["status"]
                )
                
                for status_label in self.latency_counts:
                    buckets = []
                    for bucket_str in [str(b) for b in self.latency_buckets]:
                        buckets.append((bucket_str, self.latency_counts[status_label][bucket_str]))
                    
                    # Add the +Inf bucket
                    buckets.append(("+Inf", self.latency_total_counts[status_label]))
                    
                    latency_histogram.add_metric(
                        [status_label],
                        buckets,
                        self.latency_sums[status_label]
                    )
                    logging.info(f"Created latency histogram for {status_label}: {self.latency_total_counts[status_label]} observations, sum={self.latency_sums[status_label]:.3f}s")
                
                yield latency_histogram
            else:
                logging.info("No latency data to export")

            # System resource metrics
            for metric in self._collect_system_metrics():
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

