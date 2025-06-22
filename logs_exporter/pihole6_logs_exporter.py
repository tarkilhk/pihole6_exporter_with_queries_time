#!/usr/bin/env python3

import os
import time
import requests
import urllib3
import logging
import argparse
import json
import socket
from logging.handlers import RotatingFileHandler

class PiholeLogsExporter:
    """
    Exports Pi-hole query logs to a Loki-compatible endpoint (like Grafana Alloy).
    """
    CACHE_TTL = 3600

    def __init__(self, host, key, loki_target, state_file):
        self.host = host
        self.key = key
        self.loki_target = loki_target
        self.state_file = state_file
        self.using_auth = False
        self.sid = None
        self.hostname_cache = {}
        
        # Disable SSL warnings for self-signed certificates
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        logging.info(f"Initializing Pi-hole Logs Exporter with host: {host}, loki_target: {loki_target}, state_file: {state_file}")
        
        if key is not None and host is not None:
            self.using_auth = True
            logging.info("Authentication enabled - will attempt to get session ID")
            self.sid = self.get_sid(key)
        else:
            logging.warning("No host or API token provided. Some information may not be available.")

    def get_sid(self, key):
        """Authenticates with the Pi-hole API and returns a session ID."""
        auth_url = f"https://{self.host}:443/api/auth"
        headers = {"accept": "application/json", "content-type": "application/json"}
        json_data = {"password": key}
        logging.info(f"Attempting to authenticate with Pi-hole API at {auth_url}")
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
        
        logging.info(f"Making API call to: {url}")
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
                content = f.read().strip()
                if not content:
                    ts = int(time.time()) - 1800
                    logging.info(f"State file {self.state_file} is empty. Starting from 1 hour ago (timestamp {ts}).")
                    return ts
                timestamp = int(content)
                logging.info(f"Read last timestamp from state file: {timestamp} ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp))})")
                return timestamp
        except FileNotFoundError:
            ts = int(time.time()) - 1800
            logging.info(f"State file not found at {self.state_file}. Starting from 1 hour ago (timestamp {ts}).")
            return ts
        except (ValueError, TypeError) as e:
            logging.error(f"Invalid timestamp in state file: {e}. Starting fresh.")
            return int(time.time()) - 1800  # Start from 30 minutes ago

    def write_last_timestamp(self, timestamp):
        """Writes the latest timestamp to the state file."""
        try:
            with open(self.state_file, 'w') as f:
                f.write(str(timestamp))
            logging.info(f"Wrote timestamp to state file: {timestamp} ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp))})")
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

        logging.info(f"Formatting {len(queries)} queries for Loki")

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
        
        logging.info(f"Formatted {len(streams)} unique streams for Loki")
        return list(streams.values()), max_timestamp

    def send_to_loki(self, streams):
        """Sends a batch of log streams to the Loki endpoint."""
        if not streams:
            logging.info("No new logs to send to Loki.")
            return

        payload = {"streams": streams}
        headers = {"Content-Type": "application/json"}
        loki_url = self.get_loki_url()
        
        logging.info(f"Sending {len(streams)} streams to Loki at {loki_url}")
        log_count = sum(len(s['values']) for s in streams)
        logging.info(f"Total log entries to send: {log_count}")
        
        try:
            response = requests.post(loki_url, data=json.dumps(payload), headers=headers, timeout=15)
            response.raise_for_status()
            logging.info(f"Successfully sent {log_count} log entries to Loki.")
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to send logs to Loki: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logging.error(f"Loki response status: {e.response.status_code}")
                logging.error(f"Loki response body: {e.response.text}")
            raise

    def get_loki_url(self):
        """Generate the full Loki URL from the target."""
        # Validate that loki_target is provided and not empty
        if not self.loki_target or self.loki_target.strip() == "":
            raise ValueError("LOKI_URL environment variable is not set or is empty")
        
        # Check if the target is just a path (missing scheme and hostname)
        if self.loki_target.startswith('/'):
            raise ValueError(f"LOKI_URL is just a path '{self.loki_target}'. Please provide a complete URL with scheme and hostname (e.g., http://localhost:3100)")
        
        # Check if the target has a scheme
        if not self.loki_target.startswith(('http://', 'https://')):
            raise ValueError(f"LOKI_URL missing scheme '{self.loki_target}'. Please provide a complete URL with http:// or https://")
        
        return f"{self.loki_target.rstrip('/')}/loki/api/v1/push"

    def run(self):
        """Main execution logic."""
        logging.info("Starting Pi-hole log export run.")
        try:
            # Validate Loki target before proceeding
            try:
                loki_url = self.get_loki_url()
                logging.info(f"Loki target validated: {loki_url}")
            except ValueError as e:
                logging.error(f"Invalid Loki configuration: {e}")
                logging.error("Please set LOKI_URL to a complete URL (e.g., http://localhost:3100) and restart the service.")
                return
            
            last_ts = self.read_last_timestamp()
            current_ts = int(time.time())
            
            logging.info(f"Current timestamp: {current_ts} ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(current_ts))})")

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
        finally:
            # Always logout to free up API sessions
            logging.info("Logging out from Pi-hole API session")
            self.logout()

def setup_logging(log_level, log_file=None):
    """Setup logging with both console and file handlers."""
    # Create formatter
    formatter = logging.Formatter('time="%(asctime)s" level="%(levelname)s" message="%(message)s"')
    
    # Create logger
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, log_level.upper()))
    
    # Clear any existing handlers
    logger.handlers.clear()
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # File handler (if specified)
    if log_file:
        # Ensure log directory exists
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            try:
                os.makedirs(log_dir, exist_ok=True)
                logging.info(f"Created log directory: {log_dir}")
            except Exception as e:
                logging.error(f"Failed to create log directory {log_dir}: {e}")
        
        # Create rotating file handler
        file_handler = RotatingFileHandler(
            log_file, 
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logging.info(f"Logging to file: {log_file}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Pi-hole v6 Log Exporter for Loki.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("-H", "--host", dest="host", type=str, required=False, default=os.getenv("PIHOLE_HOST", "localhost"),
                        help="Hostname or IP address of the Pi-hole instance.")
    parser.add_argument("-k", "--key", dest="key", type=str, required=False, default=None,
                        help="Pi-hole API token. Can also be set via PIHOLE_API_TOKEN env var.")
    parser.add_argument("-t", "--loki-target", dest="loki_target", type=str, required=True,
                        help="URL of the Loki/Alloy push API endpoint (e.g., http://localhost:3100).")
    parser.add_argument("-s", "--state-file", dest="state_file", type=str, required=False, default="/var/tmp/pihole_logs_exporter.state",
                        help="Path to the state file for storing the last timestamp.")
    parser.add_argument("-l", "--log-level", dest="log_level", type=str, required=False, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Set the logging level.")
    parser.add_argument("--log-file", dest="log_file", type=str, required=False, default="/var/log/pihole6_exporter/pihole_logs_exporter.log",
                        help="Path to the log file for detailed logging.")

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.log_level, args.log_file)

    exporter = None
    try:
        exporter = PiholeLogsExporter(
            host=args.host,
            key=args.key,
            loki_target=args.loki_target,
            state_file=args.state_file
        )
        exporter.run()
    except Exception as e:
        logging.critical(f"Exporter failed to initialize or run: {e}", exc_info=True)
        exit(1)
    finally:
        # Ensure logout happens even if initialization fails
        if exporter:
            logging.info("Ensuring logout from Pi-hole API session")
            exporter.logout() 