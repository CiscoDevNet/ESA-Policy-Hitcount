#!/usr/bin/env python3
"""
SMA Mail Policy Hitcount Reporter (Policy Name Version)
-------------------------------------------------------
Queries Cisco SMA v2 API endpoints for incoming and outgoing mail policies
and prints a summary of policy hit counts over a configurable time range.

Author: ciscoketcheon
Date: 2026-05-30
"""

import requests
from datetime import datetime, timedelta, timezone
import urllib3
import base64

# ---------------- USER PARAMETERS ---------------- #
SMA_IP = "x.x.x.x"                # SMA IP or hostname
SMA_PORT = 6080                    # SMA API port, this is default API HTTP
API_USER = "xxxxx"              # API username
API_PASS = "xxxxx"          # API password
DAYS_TO_QUERY = 1                  # Number of days to query, default 1 day
TOP_N_POLICIES = 10                # Top N policies to retrieve, default Top 10
VERIFY_SSL = False                 # True if SMA has valid SSL cert, SSL cert is not supported yet, to be added in next release.
# ------------------------------------------------- #

# Disable SSL warnings if VERIFY_SSL=False
if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Time calculation
now = datetime.now(timezone.utc)
end_time = now.replace(minute=0, second=0, microsecond=0)
start_time = end_time - timedelta(days=DAYS_TO_QUERY)

startDate = start_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
endDate = end_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")

# Construct SMA API URLs
url_incoming = (
    f"http://{SMA_IP}:{SMA_PORT}/sma/api/v2.0/reporting/mail_policy_incoming/recipients_matched"
    f"?device_type=esa&startDate={startDate}&endDate={endDate}&top={TOP_N_POLICIES}"
)

url_outgoing = (
    f"http://{SMA_IP}:{SMA_PORT}/sma/api/v2.0/reporting/mail_policy_outgoing/recipients_matched"
    f"?device_type=esa&startDate={startDate}&endDate={endDate}&top={TOP_N_POLICIES}"
)

# Basic Auth header
auth_string = f"{API_USER}:{API_PASS}"
encoded_auth = base64.b64encode(auth_string.encode()).decode()
headers = {
    "Authorization": f"Basic {encoded_auth}",
    "Accept": "application/json"
}

def fetch_policy_hits(url, policy_type="Incoming"):
    """Fetch SMA API data and print hit counts per policy name"""
    try:
        response = requests.get(url, headers=headers, verify=VERIFY_SSL, timeout=30)
        response.raise_for_status()
        data = response.json()

        # SMA v2 API returns a dict of policy_name -> hit_count
        results = data['data']['resultSet']['recipients_matched']

        print(f"\nSMA Report: {policy_type} Policy Hitcounts Matched - last {DAYS_TO_QUERY} day(s)")
        print(f"Time Range: {startDate} -> {endDate}\n")

        if not results:
            print("No policy hits found.")
            return

        # Print each policy and its hit count
        for item in results:
            for policy_name, hit_count in item.items():
                print(f"{policy_name:30} {hit_count}")

    except requests.exceptions.RequestException as e:
        print(f"Error fetching {policy_type} policy data: {e}")
    except KeyError:
        print(f"Unexpected data format received for {policy_type} policy.")

# Fetch incoming and outgoing policy hits
fetch_policy_hits(url_incoming, "Incoming")
fetch_policy_hits(url_outgoing, "Outgoing")
