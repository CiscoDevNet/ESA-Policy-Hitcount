#!/usr/bin/env python3
"""
Stage 1 (Pure Script) ESA Policy Health Check

Runs end-to-end without MCP:
1) Fetch ESA config text (or read local config text file)
2) Query ESA hit counts
3) Compare config inventory vs API hits
4) Save compare JSON
5) Generate DOCX via generate-health-report.js
"""

import argparse
import base64
import json
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paramiko
import requests
import urllib3


def get_time_range(days_to_query: int):
    now = datetime.now(timezone.utc)
    end_time = now.replace(minute=0, second=0, microsecond=0)
    start_time = end_time - timedelta(days=days_to_query)
    start_date = start_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_date = end_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return start_date, end_date


def build_urls(esa_ip: str, esa_port: int, start_date: str, end_date: str, top_n_policies: int):
    url_incoming = (
        f"http://{esa_ip}:{esa_port}/esa/api/v2.0/reporting/mail_policy_incoming/recipients_matched"
        f"?device_type=esa&startDate={start_date}&endDate={end_date}&top={top_n_policies}"
    )
    url_outgoing = (
        f"http://{esa_ip}:{esa_port}/esa/api/v2.0/reporting/mail_policy_outgoing/recipients_matched"
        f"?device_type=esa&startDate={start_date}&endDate={end_date}&top={top_n_policies}"
    )
    return url_incoming, url_outgoing


def get_auth_headers(api_user: str, api_pass: str):
    auth_string = f"{api_user}:{api_pass}"
    encoded_auth = base64.b64encode(auth_string.encode()).decode()
    return {
        "Authorization": f"Basic {encoded_auth}",
        "Accept": "application/json",
    }


def fetch_policy_hits(url: str, headers: dict, verify_ssl: bool):
    response = requests.get(url, headers=headers, verify=verify_ssl, timeout=60)
    response.raise_for_status()
    data = response.json()
    return data["data"]["resultSet"]["recipients_matched"]


def to_formatted_policy_hits(raw_results):
    formatted = []
    for item in raw_results:
        for policy_name, hit_count in item.items():
            formatted.append({"policy_name": policy_name, "hit_count": hit_count})
    return formatted


def aggregate_policy_hits(formatted_results) -> dict:
    totals = {}
    for item in formatted_results:
        policy_name = item.get("policy_name")
        hit_count = item.get("hit_count", 0)
        if not policy_name:
            continue
        totals[policy_name] = totals.get(policy_name, 0) + hit_count
    return totals


def normalize_policy_name(name: str) -> str:
    # Keep policy identity case-sensitive while normalizing surrounding formatting.
    return re.sub(r"\s+", " ", name.strip().strip('"').strip("'"))


def names_match(config_name: str, api_name: str) -> bool:
    normalized_config = normalize_policy_name(config_name)
    normalized_api = normalize_policy_name(api_name)
    return normalized_config == normalized_api


def resolve_api_match(config_name: str, api_names: list[str]) -> str | None:
    exact_matches = [candidate for candidate in api_names if names_match(config_name, candidate)]
    if exact_matches:
        return exact_matches[0]

    normalized_config = normalize_policy_name(config_name)
    prefix_matches = [
        candidate for candidate in api_names
        if normalize_policy_name(candidate).startswith(normalized_config)
        or normalized_config.startswith(normalize_policy_name(candidate))
    ]
    if len(prefix_matches) == 1:
        return prefix_matches[0]

    return None


def extract_policy_names_from_config(config_text: str) -> list[str]:
    patterns = [
        re.compile(r'^\s*(?:mailpolicy|policyconfig|incomingmailpolicy|outgoingmailpolicy)\s+["\']?(.+?)["\']?\s*$', re.IGNORECASE),
        re.compile(r'^\s*policy\s+name\s*[:=]\s*["\']?(.+?)["\']?\s*$', re.IGNORECASE),
        re.compile(r'^\s*name\s*[:=]\s*["\']?(.+?)["\']?\s*$', re.IGNORECASE),
    ]

    extracted = []
    seen = set()

    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        for pattern in patterns:
            match = pattern.match(line)
            if not match:
                continue

            candidate = match.group(1).strip()
            if " " in candidate and not (candidate.startswith('"') or candidate.startswith("'")):
                candidate = candidate.split(" ", 1)[0]

            normalized = normalize_policy_name(candidate)
            if normalized and normalized not in seen:
                extracted.append(candidate)
                seen.add(normalized)
            break

    return extracted


def extract_policy_inventory_from_xml(config_text: str) -> tuple[list[str], list[str], list[str]]:
    """Extract inbound/outbound mail policy names from ESA XML config export."""

    try:
        root = ET.fromstring(config_text)
    except ET.ParseError:
        return [], [], []

    def _collect_policy_names(container_path: str) -> list[str]:
        names = []
        seen = set()

        for policy_node in root.findall(container_path):
            name_node = policy_node.find("policy_name")
            if name_node is None or not name_node.text:
                continue

            candidate = name_node.text.strip()
            normalized = normalize_policy_name(candidate)
            if normalized and normalized not in seen:
                names.append(candidate)
                seen.add(normalized)

        return names

    incoming = _collect_policy_names(".//inbound_policies/policy")
    outgoing = _collect_policy_names(".//outbound_policies/policy")

    # Keep behavior consistent with SSH mode: disambiguate names present in both directions.
    incoming_normalized = {normalize_policy_name(name) for name in incoming}
    outgoing_normalized = {normalize_policy_name(name) for name in outgoing}
    shared = incoming_normalized & outgoing_normalized
    if shared:
        incoming = [f"{name}-incoming" if normalize_policy_name(name) in shared else name for name in incoming]
        outgoing = [f"{name}-outgoing" if normalize_policy_name(name) in shared else name for name in outgoing]

    combined = []
    combined_seen = set()
    for name in incoming + outgoing:
        normalized = normalize_policy_name(name)
        if normalized and normalized not in combined_seen:
            combined.append(name)
            combined_seen.add(normalized)

    return combined, incoming, outgoing


def parse_policy_inventory(config_text: str) -> tuple[list[str], list[str] | None, list[str] | None]:
    """Parse policy inventory from XML export when possible, then fall back to text parsing."""

    all_xml, incoming_xml, outgoing_xml = extract_policy_inventory_from_xml(config_text)
    if all_xml:
        return all_xml, incoming_xml, outgoing_xml

    return extract_policy_names_from_config(config_text), None, None


def read_ssh_until(shell, expected_tokens: tuple[str, ...], timeout_seconds: float = 10.0) -> str:
    deadline = time.time() + timeout_seconds
    output = ""
    while time.time() < deadline:
        if shell.recv_ready():
            chunk = shell.recv(65535).decode("utf-8", errors="ignore")
            output += chunk
            if any(token in output for token in expected_tokens):
                return output
        else:
            time.sleep(0.2)
    return output


def fetch_policyconfig_via_ssh(esa_ip: str, ssh_port: int, api_user: str, api_pass: str) -> dict:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            hostname=esa_ip,
            port=ssh_port,
            username=api_user,
            password=api_pass,
            timeout=15,
            look_for_keys=False,
            allow_agent=False,
        )
        shell = client.invoke_shell(width=200, height=2000)
        read_ssh_until(shell, (">", "#"), timeout_seconds=8)

        sections = {}
        for choice, label in (("1", "incoming"), ("2", "outgoing")):
            shell.send(b"policyconfig\n")
            read_ssh_until(shell, ("[1]>", "[2]>", "[3]>"), timeout_seconds=8)
            shell.send(f"{choice}\n".encode())
            sections[label] = read_ssh_until(shell, ("[]>",), timeout_seconds=12)
            shell.send(b"\n")
            read_ssh_until(shell, (">", "#"), timeout_seconds=8)

        return sections
    finally:
        client.close()


def extract_policy_names_from_policyconfig_output(section_text: str) -> list[str]:
    names = []
    seen = set()
    capture = False

    for raw_line in section_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            continue
        if stripped.startswith("Choose the operation you want to perform:"):
            break
        if stripped.startswith("-----"):
            capture = True
            continue
        if not capture:
            continue
        if stripped == "Default":
            continue

        if line[0].isspace():
            continue

        name_field = line[:16].strip()
        if not name_field or name_field in {"Name:", "Threat Defense", "Connector:"}:
            continue
        if stripped.isdigit():
            continue
        if "  " not in line[16:]:
            continue

        candidate = "DEFAULT" if name_field.lower() == "default" else name_field
        if candidate not in seen:  # Use exact case-sensitive match for deduplication
            names.append(candidate)
            seen.add(candidate)

    return names


def fetch_config_text(esa_ip: str, esa_port: int, api_user: str, api_pass: str, verify_ssl: bool, config_api_path: str):
    path = config_api_path if config_api_path.startswith("/") else f"/{config_api_path}"
    url = f"http://{esa_ip}:{esa_port}{path}"
    headers = get_auth_headers(api_user, api_pass)

    response = requests.get(url, headers=headers, verify=verify_ssl, timeout=60)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    if "application/json" in content_type:
        data = response.json()
        if isinstance(data, dict):
            for key in ("config", "config_text", "payload", "data"):
                value = data.get(key)
                if isinstance(value, str):
                    return value
            return json.dumps(data)
        return str(data)

    return response.text


def compare_inventory_to_hits(policy_inventory: list[str], incoming_hits: dict, outgoing_hits: dict, incoming_inventory: list[str] | None = None, outgoing_inventory: list[str] | None = None):
    """
    Compare policy inventory against incoming and outgoing hit data.
    Returns results separated by direction (incoming/outgoing).
    
    If incoming_inventory and outgoing_inventory are provided (from SSH), policies are compared
    only against their respective direction. Otherwise, all policies are compared against both directions.
    """
    # If direction-specific inventories are provided, use them; otherwise treat all as both directions
    incoming_only = set(normalize_policy_name(p) for p in (incoming_inventory or []))
    outgoing_only = set(normalize_policy_name(p) for p in (outgoing_inventory or []))

    incoming_with_hits = []
    incoming_without_hits = []
    outgoing_with_hits = []
    outgoing_without_hits = []

    # Compare inventory against both directions (sorted by original name, preserving all policies)
    for original_name in sorted(policy_inventory, key=str.lower):
        normalized_name = normalize_policy_name(original_name)
        is_incoming_only = normalized_name in incoming_only
        is_outgoing_only = normalized_name in outgoing_only
        
        # Determine which direction(s) this policy should be compared against
        should_check_incoming = (not incoming_inventory and not outgoing_inventory) or is_incoming_only or (not is_outgoing_only and not is_incoming_only)
        should_check_outgoing = (not incoming_inventory and not outgoing_inventory) or is_outgoing_only or (not is_outgoing_only and not is_incoming_only)

        incoming_api_name = None
        outgoing_api_name = None
        incoming_hit_count = 0
        outgoing_hit_count = 0

        if should_check_incoming:
            incoming_api_name = resolve_api_match(original_name, list(incoming_hits.keys()))
            incoming_hit_count = incoming_hits.get(incoming_api_name, 0) if incoming_api_name else 0

        if should_check_outgoing:
            outgoing_api_name = resolve_api_match(original_name, list(outgoing_hits.keys()))
            outgoing_hit_count = outgoing_hits.get(outgoing_api_name, 0) if outgoing_api_name else 0

        # Create entries for incoming if this is an incoming or ambiguous policy
        if should_check_incoming:
            entry = {
                "policy_name_from_config": original_name,
                "matched_policy_name_from_api": incoming_api_name,
                "hit_count": incoming_hit_count,
            }
            if incoming_hit_count > 0:
                incoming_with_hits.append(entry)
            elif not is_outgoing_only:  # Add to incoming without_hits unless it's explicitly outgoing-only
                incoming_without_hits.append(entry)

        # Create entries for outgoing only if it has outgoing hits OR is explicitly outgoing-only
        if should_check_outgoing and (outgoing_hit_count > 0 or is_outgoing_only):
            entry = {
                "policy_name_from_config": original_name,
                "matched_policy_name_from_api": outgoing_api_name,
                "hit_count": outgoing_hit_count,
            }
            if outgoing_hit_count > 0:
                outgoing_with_hits.append(entry)
            else:
                outgoing_without_hits.append(entry)

    # API-only policies for incoming
    incoming_api_only = []
    for api_name in incoming_hits:
        if not any(resolve_api_match(config_name, [api_name]) for config_name in policy_inventory):
            incoming_api_only.append({
                "policy_name_from_api": api_name,
                "hit_count": incoming_hits.get(api_name, 0),
            })

    # API-only policies for outgoing
    outgoing_api_only = []
    for api_name in outgoing_hits:
        if not any(resolve_api_match(config_name, [api_name]) for config_name in policy_inventory):
            outgoing_api_only.append({
                "policy_name_from_api": api_name,
                "hit_count": outgoing_hits.get(api_name, 0),
            })

    incoming_api_only.sort(key=lambda x: x["hit_count"], reverse=True)
    outgoing_api_only.sort(key=lambda x: x["hit_count"], reverse=True)

    return {
        "incoming": {
            "with_hits": incoming_with_hits,
            "without_hits": incoming_without_hits,
            "api_only": incoming_api_only,
        },
        "outgoing": {
            "with_hits": outgoing_with_hits,
            "without_hits": outgoing_without_hits,
            "api_only": outgoing_api_only,
        },
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Stage 1 pure-script ESA policy health check")
    parser.add_argument("--esa-ip", required=True, help="ESA IP or hostname")
    parser.add_argument("--esa-port", type=int, default=6080, help="ESA API port")
    parser.add_argument("--api-user", required=True, help="ESA API username")
    parser.add_argument("--api-pass", required=True, help="ESA API password")
    parser.add_argument("--days", type=int, default=30, help="Days to query for hit counts")
    parser.add_argument("--top", type=int, default=1000, help="Top N policies to request from API")
    parser.add_argument("--verify-ssl", action="store_true", help="Enable SSL verification")
    parser.add_argument("--ssh-port", type=int, default=22, help="ESA SSH port for CLI collection")
    parser.add_argument("--fetch-via-ssh", action="store_true", help="Collect policy inventory from ESA CLI policyconfig over SSH")

    parser.add_argument("--config-api-path", default=None, help="ESA config API path to fetch config text")
    parser.add_argument("--config-file", default=None, help="Use local ESA config text file instead of API fetch")

    parser.add_argument("--config-output", default="esa-config.txt", help="Where to save config text")
    parser.add_argument("--compare-output", default="compare-config-output.json", help="Where to save compare JSON")
    parser.add_argument("--docx-output", default="ESA-Policy-Health-Check.docx", help="Generated DOCX file path")

    return parser.parse_args()


def main():
    args = parse_args()

    if not args.verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if args.fetch_via_ssh:
        sections = fetch_policyconfig_via_ssh(args.esa_ip, args.ssh_port, args.api_user, args.api_pass)
        incoming_names = extract_policy_names_from_policyconfig_output(sections.get("incoming", ""))
        outgoing_names = extract_policy_names_from_policyconfig_output(sections.get("outgoing", ""))
        
        # Track which normalized names appear in both directions
        incoming_normalized = {normalize_policy_name(name) for name in incoming_names}
        outgoing_normalized = {normalize_policy_name(name) for name in outgoing_names}
        shared = incoming_normalized & outgoing_normalized
        
        # Rename policies that appear in both directions to make them distinctive
        if shared:
            incoming_names = [f"{name}-incoming" if normalize_policy_name(name) in shared else name for name in incoming_names]
            outgoing_names = [f"{name}-outgoing" if normalize_policy_name(name) in shared else name for name in outgoing_names]
        
        policy_inventory = incoming_names + outgoing_names
        config_text = sections.get("incoming", "") + "\n\n" + sections.get("outgoing", "")
        incoming_inventory = incoming_names
        outgoing_inventory = outgoing_names
    elif args.config_file:
        config_text = Path(args.config_file).read_text(encoding="utf-8", errors="ignore")
        policy_inventory, incoming_inventory, outgoing_inventory = parse_policy_inventory(config_text)
    else:
        if not args.config_api_path:
            raise ValueError("Provide --config-api-path or --config-file")
        config_text = fetch_config_text(
            args.esa_ip,
            args.esa_port,
            args.api_user,
            args.api_pass,
            args.verify_ssl,
            args.config_api_path,
        )
        policy_inventory, incoming_inventory, outgoing_inventory = parse_policy_inventory(config_text)

    Path(args.config_output).write_text(config_text, encoding="utf-8")

    if not policy_inventory:
        raise ValueError("No policies parsed from config text. Check config format.")

    headers = get_auth_headers(args.api_user, args.api_pass)
    start_date, end_date = get_time_range(args.days)
    url_incoming, url_outgoing = build_urls(args.esa_ip, args.esa_port, start_date, end_date, args.top)

    incoming_raw = fetch_policy_hits(url_incoming, headers, args.verify_ssl)
    outgoing_raw = fetch_policy_hits(url_outgoing, headers, args.verify_ssl)

    incoming_totals = aggregate_policy_hits(to_formatted_policy_hits(incoming_raw))
    outgoing_totals = aggregate_policy_hits(to_formatted_policy_hits(outgoing_raw))

    comparison_result = compare_inventory_to_hits(policy_inventory, incoming_totals, outgoing_totals, incoming_inventory, outgoing_inventory)

    compare_payload = {
        "analysis_time_range": {"start": start_date, "end": end_date},
        "query_parameters": {
            "days_to_query": args.days,
            "top_n_policies": args.top,
        },
        "config_inventory_source": "stage1_script",
        "summary": {
            "inventory_policy_count": len({normalize_policy_name(x) for x in policy_inventory}),
            "policies_with_hits_incoming": len(comparison_result["incoming"]["with_hits"]),
            "policies_with_hits_outgoing": len(comparison_result["outgoing"]["with_hits"]),
            "policies_without_hits": len(comparison_result["incoming"]["without_hits"]),
            "api_only_incoming": len(comparison_result["incoming"]["api_only"]),
            "api_only_outgoing": len(comparison_result["outgoing"]["api_only"]),
        },
        "incomingPolicies": comparison_result["incoming"]["with_hits"] + comparison_result["incoming"]["without_hits"],
        "outgoingPolicies": comparison_result["outgoing"]["with_hits"] + comparison_result["outgoing"]["without_hits"],
        "note": "If API top-N is too small, some hit policies may be missing from comparison. Increase --top as needed.",
    }

    compare_output_path = Path(args.compare_output)
    compare_output_path.write_text(json.dumps(compare_payload, indent=2), encoding="utf-8")

    if not compare_output_path.exists() or compare_output_path.stat().st_size == 0:
        raise RuntimeError(
            f"Comparison output file was not created correctly: {compare_output_path}"
        )

    try:
        compare_output_loaded = json.loads(compare_output_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Comparison output is not valid JSON: {compare_output_path}"
        ) from exc

    has_expected_schema = (
        isinstance(compare_output_loaded.get("incomingPolicies"), list)
        and isinstance(compare_output_loaded.get("outgoingPolicies"), list)
    )
    if not has_expected_schema:
        raise RuntimeError(
            "Comparison output JSON schema is invalid. "
            "Expected list fields: incomingPolicies and outgoingPolicies."
        )

    report_generator = Path("generate-health-report.js")
    if not report_generator.exists():
        raise RuntimeError(f"Report generator script not found: {report_generator}")

    node_executable = shutil.which("node") or "/opt/homebrew/bin/node"
    subprocess.run(
        [
            node_executable,
            "generate-health-report.js",
            "--input",
            args.compare_output,
            "--output",
            args.docx_output,
        ],
        check=True,
    )

    print("Stage 1 completed.")
    print(f"Config text saved to: {args.config_output}")
    print(f"Comparison JSON saved to: {args.compare_output}")
    print(f"DOCX report saved to: {args.docx_output}")


if __name__ == "__main__":
    main()
