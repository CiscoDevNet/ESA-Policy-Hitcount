# Cisco ESA Mail Policy Hitcount Reporter (Policy Name)

This Python script queries **Cisco ESA (Email Security Appliance) v2 API** endpoints for **incoming and outgoing mail policies** and prints a summary of **policy hit counts** over a configurable time range.  

It is designed for administrators who want a **quick overview of which mail policies were triggered** in a given period.

While ESA natively provides hit count reporting at the **content filter** and **message filter** levels, it does not offer visibility at the **mail policy level**. This script is designed to bridge that gap by aggregating and presenting policy-level hit count data.

In addition, the tool is useful for **policy housekeeping**. Policies with **zero hit counts** are automatically excluded from the output, making it easier to identify unused or redundant policies that may be candidates for cleanup or removal.

---

## Features

- Query **incoming** and **outgoing** mail policies.
- Summarizes **policy name → hit count**.
- Configurable **time range** (number of days to query).
- Configurable **number of top policies** to retrieve.
- Handles **Basic Authentication**.
- Clean, easy-to-read tabular output.
- All user parameters are centralized at the top of the script.

---

## Requirements

- Python 3.8+
- `requests` library

Install dependencies via pip:

```
pip install requests
```

## Requirements on ESA

- Enable API (from Network -> IP Interfaces -> AsyncOS API)
- Create api user credential with read permission. 


---

## Configuration

Edit the user parameters at the top of `esa_policy_hitcount.py`:

```
ESA_IP = "x.x.x.x"                 # ESA IP or hostname
ESA_PORT = 6080                    # ESA API port, this is default API HTTP
API_USER = "xxxxx"                 # API username
API_PASS = "xxxxx"                 # API password
DAYS_TO_QUERY = 1                  # Number of days to query, default 1 day
TOP_N_POLICIES = 10                # Top N policies to retrieve, default Top 10
VERIFY_SSL = False                 # True if ESA has valid SSL cert, SSL cert is not supported yet, to be added in next release.
```

---

## Usage

Run the script:

```
python esa_policy_hitcount.py
```

Sample output:

```
ESA Report: Incoming Policy Hitcounts Matched - last 1 day(s)
Time Range: 2026-03-30T18:00:00.000Z -> 2026-03-31T18:00:00.000Z

Policy1                           129
Policy2	                            7
Quarantine Policy                   6
Test-Policy                         5
postmaster-forwarding               4
Spam-FalsePositive                  2
spoofing-allowed                    2

ESA Report: Outgoing Policy Hitcounts Matched - last 1 day(s)
Time Range: 2026-03-30T18:00:00.000Z -> 2026-03-31T18:00:00.000Z

Default-out                         9
```

---

## How it Works

1. The script calculates the **start and end time** based on `DAYS_TO_QUERY`.
2. Constructs ESA API URLs for incoming and outgoing mail policies.
3. Uses **Basic Authentication** to query ESA v2 API.
4. Processes the returned JSON:
   - ESA returns a mapping: `{policy_name: hit_count}`.
5. Prints each policy and the number of hits in a clean table format.

---

## Notes

- The **hit count** represents how many times a policy was triggered (per recipient) in the selected time range.
- ESA API limits results with `top=N`. Increase `TOP_N_POLICIES` if needed.
- Set `VERIFY_SSL = True` if using HTTPS with a valid certificate. Not fully working yet, suggest to use API HTTP now.
- The script uses **UTC time** for ESA API queries.

---


## Author

ciscoketcheon - ketcheon@cisco.com

---

## Suggested Improvements

- Export results to **CSV or JSON** for reporting or dashboards.
- Include **automatic pagination** for more than `top=N` results.
- Add **authentication via token** instead of basic auth for better security.
- Add SSL support, currently is not fully working yet


**Author:** ketcheon@cisco.com  


