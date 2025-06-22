#!/usr/bin/env python3

import os
import time
import requests
import urllib3
import logging
import argparse
import json
import socket

class PiholeLogsExporter:
    """
    Exports Pi-hole query logs to a Loki-compatible endpoint (like Grafana Alloy).
    """
    CACHE_TTL = 3600

    def __init__(self, host, key, loki_target, state_file, initial_history_minutes=5):
        self.host = host
        self.loki_target = loki_target
        self.state_file = state_file
        self.initial_history_minutes = initial_history_minutes
        self.using_auth = False
        self.hostname_cache = {}
        
        # Disable if you've actually got a good cert set up.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        # Try to get API key from environment variable if not provided
        if key is None:
            key = os.getenv('PIHOLE_API_TOKEN')
            
        if key is not None and host is not None:
            self.using_auth = True
            self.sid = self.get_sid(key)
        else:
            logging.warning("No host or API token provided. Some information may not be available.")

    def get_sid(self, key):
        """Authenticates with the Pi-hole API and returns a session ID."""
        auth_url = f"https://{self.host}:443/api/auth"
        headers = {"accept": "application/json", "content-type": "application/json"}
        json_data = {"password": key}
        try:
            req = requests.post(auth_url, verify=False, headers=headers, json=json_data, timeout=10)
            req.raise_for_status()
            reply = req.json()
            logging.info("Successfully authenticated with Pi-hole API.")
            return reply['session']['sid']
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to authenticate with Pi-hole API: {e}")
            raise

    def logout(self):
        """Logs out from the Pi-hole API session."""
        if not self.using_auth or not hasattr(self, 'sid'):
            return
        
        logout_url = f"https://{self.host}:443/api/logout"
        headers = {"accept": "application/json", "sid": self.sid}
        
        try:
            req = requests.post(logout_url, verify=False, headers=headers, timeout=10)
            req.raise_for_status()
            logging.info("Successfully logged out from Pi-hole API session.")
        except requests.exceptions.RequestException as e:
            logging.warning(f"Failed to log out from Pi-hole API: {e}")
        finally:
            # Clear the session ID regardless of logout success
            self.sid = None
            self.using_auth = False

    def get_api_call(self, api_path):
        """Makes a GET request to the Pi-hole API."""
        url = f"https://{self.host}:443/api/{api_path}"
        headers = {"accept": "application/json"}
        if self.using_auth:
            headers["sid"] = self.sid
        
        try:
            req = requests.get(url, verify=False, headers=headers, timeout=30)
            req.raise_for_status()
            reply = req.json()
            logging.debug(f"API response for {api_path}: {reply}")
            return reply
        except requests.exceptions.RequestException as e:
            logging.error(f"API call failed: {e}")
            raise
        except json.JSONDecodeError:
            logging.error(f"Failed to parse JSON response from {api_path}. Raw response: {req.text}")
            raise

    def read_last_timestamp(self):
        """Reads the last successfully processed timestamp from the state file."""
        try:
            with open(self.state_file, 'r') as f:
                return int(f.read().strip())
        except FileNotFoundError:
            logging.info(f"State file not found at {self.state_file}. Starting from beginning (timestamp 0).")
            return 0
        except (ValueError, TypeError) as e:
            logging.error(f"Invalid timestamp in state file: {e}. Starting fresh.")
            return int(time.time()) - (self.initial_history_minutes * 60)

    def write_last_timestamp(self, timestamp):
        """Writes the latest timestamp to the state file."""
        try:
            with open(self.state_file, 'w') as f:
                f.write(str(timestamp))
        except IOError as e:
            logging.error(f"Error writing to state file {self.state_file}: {e}")

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

    def _flatten_dict(self, d: dict, parent_key: str = '', sep: str = '_') -> dict:
        """
        Flattens a nested dictionary.
        Example: {'a': {'b': 1}} -> {'a_b': 1}
        """
        items = []
        for k, v in d.items():
            new_key = parent_key + sep + k if parent_key else k
            if isinstance(v, dict):
                items.extend(self._flatten_dict(v, new_key, sep=sep).items())
            else:
                items.append((new_key, v))
        return dict(items)

    def fetch_queries(self, from_ts, until_ts):
        """Fetches queries from the Pi-hole API within a given time range."""
        logging.info(f"Fetching queries from {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(from_ts + 1))} to {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(until_ts))}")
        api_path = f"queries?from={from_ts + 1}&until={until_ts}&length=1000000"
        reply = self.get_api_call(api_path)
        queries = reply.get("queries", [])
        logging.info(f"Fetched {len(queries)} new queries.")
        return queries

    def format_for_loki(self, queries):
        """Formats Pi-hole queries into Loki stream format."""
        streams = {}
        max_timestamp = 0
        debug_logged = False

        for q in queries:
            if not debug_logged:
                logging.info("--- DEBUG: First query structure ---")
                logging.info(json.dumps(q, indent=2))
                logging.info("------------------------------------")
                debug_logged = True
            
            # The query time from Pi-hole v6 API is in the 'time' field as a Unix timestamp.
            q_ts = q.get('time')
            if not q_ts:
                logging.warning(f"Query missing 'time' field, skipping: {q}")
                continue
            
            q_ts = int(float(q_ts))
            max_timestamp = max(max_timestamp, q_ts)
            ts_ns = str(q_ts * 1_000_000_000)

            client_ip = q.get('client', {}).get('ip', 'unknown')
            client_name = self.resolve_hostname(client_ip)

            # Update the original query dict with the short hostname so it's reflected in the flattened log line.
            if 'client' in q and isinstance(q['client'], dict):
                q['client']['name'] = client_name

            # Create stream labels for Loki.
            stream_labels = {
                "job": "pihole_logs_exporter",
                "service": "pihole_query_log",
                "host": self.host,
                "client_ip": client_ip,
                "client_name": client_name,
                "type": q.get('type', 'unknown'),
                "status": q.get('status', 'unknown'),
                "domain": q.get('domain', 'unknown')
            }
            
            labels_tuple = tuple(sorted(stream_labels.items()))
            
            if labels_tuple not in streams:
                streams[labels_tuple] = {
                    "stream": stream_labels,
                    "values": []
                }
            
            flat_q = self._flatten_dict(q)
            log_line = json.dumps(flat_q)
            streams[labels_tuple]["values"].append([ts_ns, log_line])
        
        return list(streams.values()), max_timestamp

    def send_to_loki(self, streams):
        """Sends a batch of log streams to the Loki endpoint."""
        if not streams:
            logging.info("No new logs to send to Loki.")
            return

        payload = {"streams": streams}
        headers = {"Content-Type": "application/json"}
        
        try:
            response = requests.post(self.get_loki_url(), data=json.dumps(payload), headers=headers, timeout=15)
            response.raise_for_status()
            log_count = sum(len(s['values']) for s in streams)
            logging.info(f"Successfully sent {log_count} log entries to Loki.")
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to send logs to Loki: {e}")
            raise

    def get_loki_url(self):
        """Generate the full Loki URL from the target."""
        return f"{self.loki_target}/loki/api/v1/push"

    def run(self):
        """Main execution logic."""
        logging.info("Starting Pi-hole log export run.")
        try:
            last_ts = self.read_last_timestamp()
            current_ts = int(time.time())

            if last_ts >= current_ts:
                logging.info("Last timestamp is current, no new logs to fetch.")
                return

            queries = self.fetch_queries(last_ts, current_ts)

            if not queries:
                self.write_last_timestamp(current_ts)
                logging.info("No new queries found in the time range.")
                return
            
            loki_streams, max_ts = self.format_for_loki(queries)

            if loki_streams:
                self.send_to_loki(loki_streams)
                self.write_last_timestamp(max_ts)
            else:
                # No valid queries were formatted, but we should still advance the timestamp
                self.write_last_timestamp(current_ts)

            logging.info("Pi-hole log export run finished.")

        except Exception as e:
            logging.error(f"An unexpected error occurred during the run: {e}", exc_info=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Pi-hole v6 Log Exporter for Loki.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("-H", "--host", dest="host", type=str, required=False, default=os.getenv("PIHOLE_HOST", "localhost"),
                        help="Hostname or IP address of the Pi-hole instance.")
    parser.add_argument("-k", "--key", dest="key", type=str, required=False, default=None,
                        help="Pi-hole API token. Can also be set via PIHOLE_API_TOKEN env var.")
    parser.add_argument("-u", "--loki-target", dest="loki_target", type=str, required=True,
                        help="URL of the Loki/Alloy push API endpoint (e.g., http://localhost:3100).")
    parser.add_argument("-s", "--state-file", dest="state_file", type=str, required=False, default="/var/tmp/pihole_logs_exporter.state",
                        help="Path to the state file for storing the last timestamp.")
    parser.add_argument("-i", "--initial-minutes", dest="initial_minutes", type=int, required=False, default=5,
                        help="On first run, how many minutes of history to fetch.")
    parser.add_argument("-l", "--log-level", dest="log_level", type=str, required=False, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Set the logging level.")

    args = parser.parse_args()

    # Set logging level
    log_level = getattr(logging, args.log_level.upper())
    logging.basicConfig(format='time="%(asctime)s" level="%(levelname)s" message="%(message)s"', level=log_level)

    try:
        exporter = PiholeLogsExporter(
            host=args.host,
            key=args.key,
            loki_target=args.loki_target,
            state_file=args.state_file,
            initial_history_minutes=args.initial_minutes
        )
        exporter.run()
    except Exception as e:
        logging.critical(f"Exporter failed to initialize or run: {e}", exc_info=True)
        exit(1) 