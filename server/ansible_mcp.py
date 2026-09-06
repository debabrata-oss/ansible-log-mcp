"""
ansible-log-mcp: MCP server exposing Ansible-driven operations against
a controlled set of inventory groups — log scanning, connectivity checks,
host listing, and Linux health-check / RCA reporting.
"""

import subprocess
import yaml
from pathlib import Path

from mcp.server.fastmcp import FastMCP

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


CONFIG = load_config()
mcp = FastMCP("ansible-logs")


def _check_group(target_group: str):
    """Return an error string if the group isn't allowed, else None."""
    allowed = set(CONFIG["allowed_groups"])
    if target_group not in allowed:
        return f"Error: '{target_group}' is not in the allowed groups: {sorted(allowed)}"
    return None


def _run(cmd, timeout: int):
    """
    Run a subprocess and return (returncode, stdout, stderr).
    Returns a formatted error string instead if the process couldn't
    be launched or timed out.
    """
    try:
        result = subprocess.run(
            cmd, cwd=CONFIG["playbook_dir"],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s\nCommand: {' '.join(cmd)}"
    except FileNotFoundError as e:
        return (
            f"Error: could not launch command — {e}\n"
            f"Check the binary is installed and on PATH for this Python environment.\n"
            f"Command attempted: {' '.join(cmd)}"
        )
    except Exception as e:
        return f"Error: unexpected exception: {type(e).__name__}: {e}"


def _adhoc(target_group: str, module: str, args: str = "", become: bool = False) -> str:
    """
    Run a single Ansible ad-hoc module against a group and return raw
    combined stdout/stderr, or a detailed error string on failure.
    """
    err = _check_group(target_group)
    if err:
        return err

    forks = str(CONFIG["defaults"]["forks"])
    cmd = [
        "ansible", target_group,
        "-i", CONFIG["inventory_path"],
        "-m", module,
        "-f", forks,
    ]
    if args:
        cmd += ["-a", args]
    if become:
        cmd += ["--become"]

    outcome = _run(cmd, CONFIG["defaults"]["timeout_seconds"])
    if isinstance(outcome, str):
        return outcome
    returncode, stdout, stderr = outcome

    if returncode != 0:
        detail = (stdout[-2000:] + "\n" + stderr[-500:]).strip()
        return f"Command failed (exit code {returncode})\n---\n{detail}"

    return stdout.strip() or "(no output)"


# ---------------------------------------------------------------------
# Log scanning
# ---------------------------------------------------------------------

@mcp.tool()
def scan_logs(target_group: str, pattern: str, log_path: str = "") -> str:
    """
    Search logs across every host in an Ansible inventory group for a
    regex pattern, and return the combined per-host results.

    Args:
        target_group: Inventory group name. Must be in the allowlist —
            call list_allowed_groups to see valid options.
        pattern: Extended regex (grep -E syntax), e.g. "error|failed|critical".
            Use "." to match every non-empty line.
        log_path: Absolute log file path on target hosts. Defaults to
            /var/log/messages (varies by OS — Amazon Linux/RHEL/Ubuntu differ).

    Returns:
        Combined grep output labeled per host, "No matches found.", or
        a detailed error message (exit code + Ansible stdout/stderr).
    """
    err = _check_group(target_group)
    if err:
        return err

    log_path = log_path or CONFIG["defaults"]["log_path"]
    playbook = str(Path(CONFIG["playbook_dir"]) / CONFIG["playbook_file"])
    forks = str(CONFIG["defaults"]["forks"])
    timeout = CONFIG["defaults"]["timeout_seconds"]
    combined_file = CONFIG["output"]["combined_file"]

    cmd = [
        "ansible-playbook", playbook,
        "-i", CONFIG["inventory_path"],
        "-e", f"target_group={target_group}",
        "-e", f"search_pattern={pattern}",
        "-e", f"log_path={log_path}",
        "-f", forks,
    ]

    outcome = _run(cmd, timeout)
    if isinstance(outcome, str):
        return outcome
    returncode, stdout, stderr = outcome

    if returncode != 0:
        detail = (stdout[-2000:] + "\n" + stderr[-500:]).strip()
        return f"Ansible run failed (exit code {returncode})\nCommand: {' '.join(cmd)}\n---\n{detail}"

    try:
        with open(combined_file) as f:
            output = f.read()
    except FileNotFoundError:
        return f"Error: no combined output file at {combined_file} — run reported success but produced no report.\nStdout tail:\n{stdout[-1000:]}"
    except Exception as e:
        return f"Error reading combined output file: {type(e).__name__}: {e}"

    return output or "No matches found."


# ---------------------------------------------------------------------
# Connectivity / inventory
# ---------------------------------------------------------------------

@mcp.tool()
def ping_hosts(target_group: str) -> str:
    """
    Check SSH/connectivity to every host in an inventory group using
    Ansible's ping module (a real connection test, not ICMP).

    Args:
        target_group: Inventory group name (must be in the allowlist).

    Returns:
        Per-host SUCCESS/UNREACHABLE/FAILED status, or a detailed error.
    """
    err = _check_group(target_group)
    if err:
        return err
    return _adhoc(target_group, "ping")


@mcp.tool()
def list_hosts(target_group: str) -> str:
    """
    List every hostname/IP that belongs to an inventory group.

    Args:
        target_group: Inventory group name (must be in the allowlist).

    Returns:
        One hostname/IP per line, or an error if the group is empty
        or missing from inventory.
    """
    err = _check_group(target_group)
    if err:
        return err

    cmd = ["ansible", target_group, "-i", CONFIG["inventory_path"], "--list-hosts"]
    outcome = _run(cmd, CONFIG["defaults"]["timeout_seconds"])
    if isinstance(outcome, str):
        return outcome
    returncode, stdout, stderr = outcome

    if returncode != 0:
        detail = (stdout[-1000:] + "\n" + stderr[-500:]).strip()
        return f"Failed to list hosts (exit code {returncode})\n---\n{detail}"

    return stdout.strip() or "Group is empty or not defined in inventory."


@mcp.tool()
def list_allowed_groups() -> str:
    """
    List every inventory group this MCP server is permitted to run
    tools against. Fixed allowlist from config.yaml.

    Returns:
        Comma-separated, sorted list of allowed group names.
    """
    return ", ".join(sorted(CONFIG["allowed_groups"]))


# ---------------------------------------------------------------------
# Individual health-check tools (quick, single-purpose)
# ---------------------------------------------------------------------

@mcp.tool()
def check_uptime(target_group: str) -> str:
    """
    Get uptime and load average (1/5/15 min) for every host in a group.
    Good first check for RCA — high load average is often the first sign
    of a problem.

    Args:
        target_group: Inventory group name (must be in the allowlist).
    """
    return _adhoc(target_group, "command", "uptime")


@mcp.tool()
def check_memory(target_group: str) -> str:
    """
    Get memory and swap usage (free -h equivalent) for every host in a group.
    Use this to check for memory exhaustion or heavy swap usage during RCA.

    Args:
        target_group: Inventory group name (must be in the allowlist).
    """
    return _adhoc(target_group, "command", "free -h")


@mcp.tool()
def check_disk(target_group: str) -> str:
    """
    Get disk usage per mounted filesystem (df -h equivalent) for every
    host in a group. Use this to check for full disks — a very common
    root cause of service failures.

    Args:
        target_group: Inventory group name (must be in the allowlist).
    """
    return _adhoc(target_group, "command", "df -h")


@mcp.tool()
def check_load_top(target_group: str, sort_by: str = "cpu", count: int = 10) -> str:
    """
    Get a live top-style snapshot of the highest resource-consuming
    processes on every host in a group (equivalent to `top`/`ps aux --sort`).

    Args:
        target_group: Inventory group name (must be in the allowlist).
        sort_by: "cpu" or "mem" — which resource to sort processes by.
            Defaults to "cpu".
        count: Number of top processes to return per host. Defaults to 10.

    Returns:
        Process list (PID, user, %CPU, %MEM, command) per host, sorted
        by the requested resource, or an error message.
    """
    if sort_by not in ("cpu", "mem"):
        return "Error: sort_by must be 'cpu' or 'mem'"
    field = "%cpu" if sort_by == "cpu" else "%mem"
    n = count + 1  # +1 to account for the header line
    return _adhoc(target_group, "shell", f"ps aux --sort=-{field} | head -{n}")


@mcp.tool()
def check_failed_services(target_group: str) -> str:
    """
    List all failed systemd units on every host in a group
    (systemctl --failed). A key first check for RCA — shows which
    services have crashed or failed to start.

    Args:
        target_group: Inventory group name (must be in the allowlist).

    Returns:
        Per-host list of failed units, or "(none)" if nothing is failed.
    """
    return _adhoc(target_group, "shell", "systemctl --failed --no-legend 2>/dev/null || echo '(none)'")


@mcp.tool()
def check_service_status(target_group: str, service_name: str) -> str:
    """
    Get the detailed status of a specific systemd service on every host
    in a group (systemctl status <service>). Use this once
    check_failed_services or a scan_logs result points to a specific
    service name.

    Args:
        target_group: Inventory group name (must be in the allowlist).
        service_name: Exact systemd unit name, e.g. "nginx", "sshd",
            "docker".

    Returns:
        Per-host systemctl status output.
    """
    return _adhoc(target_group, "shell", f"systemctl status {service_name} --no-pager 2>&1 | head -20")


@mcp.tool()
def sar_report(target_group: str, metric: str = "cpu") -> str:
    """
    Get a historical sar (sysstat) report for a resource metric on
    every host in a group — useful for RCA to see how CPU/memory/IO
    trended over the day, not just right now. Requires the `sysstat`
    package to be installed on target hosts; if it isn't, the tool
    reports that per host rather than failing.

    Args:
        target_group: Inventory group name (must be in the allowlist).
        metric: Which resource history to pull. One of:
            "cpu" (sar -u), "memory" (sar -r), "disk" (sar -b, I/O),
            "network" (sar -n DEV, per-interface throughput),
            "load" (sar -q, load average history),
            "all" (every metric above, one labeled section each —
            use this for a full RCA snapshot in one call).
            Defaults to "cpu".

    Returns:
        Per-host sar output for today, or a note that sysstat is not
        installed/has no data for that host.
    """
    flag_map = {
        "cpu": "-u",
        "memory": "-r",
        "disk": "-b",
        "network": "-n DEV",
        "load": "-q",
    }
    if metric not in flag_map and metric != "all":
        return f"Error: metric must be one of {list(flag_map)} or 'all'"

    if metric == "all":
        cmd = (
            "for label_flag in "
            "'CPU:-u' 'Memory:-r' 'Disk I/O:-b' 'Network:-n DEV' 'Load Average:-q'; do "
            "label=\"${label_flag%%:*}\"; flag=\"${label_flag#*:}\"; "
            "echo \"--- SAR $label ---\"; "
            "sar $flag 2>/dev/null || echo 'sysstat not installed or no data available for today'; "
            "echo; "
            "done"
        )
    else:
        flag = flag_map[metric]
        cmd = f"sar {flag} 2>/dev/null || echo 'sysstat not installed or no data available for today'"

    return _adhoc(target_group, "shell", cmd)


@mcp.tool()
def check_cluster_logs(target_group: str, lines: int = 50) -> str:
    """
    Tail Pacemaker and Corosync cluster logs on every host in a group,
    filtered for errors/warnings where possible. Use this alongside
    check_pacemaker_cluster when a clustered service has an issue and
    you need to see what the cluster stack itself logged, not just its
    current status.

    Args:
        target_group: Inventory group name (must be in the allowlist).
        lines: How many recent lines to pull per log file. Defaults to 50.

    Returns:
        Per-host tail of pacemaker.log and corosync.log (or journalctl
        output if the log files don't exist on that distro), or a note
        if neither is found.
    """
    cmd = (
        f"echo '--- pacemaker.log (last {lines} lines) ---'; "
        f"sudo tail -n {lines} /var/log/pacemaker/pacemaker.log 2>/dev/null "
        f"|| sudo journalctl -u pacemaker -n {lines} --no-pager 2>/dev/null "
        f"|| echo 'pacemaker.log not found and journalctl unit unavailable'; "
        f"echo; "
        f"echo '--- corosync.log (last {lines} lines) ---'; "
        f"sudo tail -n {lines} /var/log/cluster/corosync.log 2>/dev/null "
        f"|| sudo journalctl -u corosync -n {lines} --no-pager 2>/dev/null "
        f"|| echo 'corosync.log not found and journalctl unit unavailable'"
    )
    return _adhoc(target_group, "shell", cmd)


@mcp.tool()
def check_recent_reboots(target_group: str) -> str:
    """
    Show recent reboot/shutdown history for every host in a group
    (last reboot equivalent). Useful in RCA to confirm whether an
    unexpected reboot correlates with the incident timeline.

    Args:
        target_group: Inventory group name (must be in the allowlist).
    """
    return _adhoc(target_group, "shell", "last reboot -F | head -10")


# ---------------------------------------------------------------------
# Combined RCA report
# ---------------------------------------------------------------------

@mcp.tool()
def full_health_check(target_group: str, log_path: str = "") -> str:
    """
    Run a complete health snapshot across every host in a group and
    return one combined, RCA-ready report. Covers: uptime/load, memory,
    disk, top CPU/memory processes, failed systemd units, recent
    error/critical log lines, and a sar CPU snapshot.

    This is the tool to reach for when investigating "why did X break" —
    it gathers everything check_uptime, check_memory, check_disk,
    check_load_top, check_failed_services, and sar_report would return
    individually, in one pass, formatted as a single per-host report.

    Args:
        target_group: Inventory group name (must be in the allowlist).
        log_path: Log file to scan for recent errors. Defaults to
            /var/log/messages.

    Returns:
        Combined multi-section report, one block per host, or a
        detailed error message if the run failed.
    """
    err = _check_group(target_group)
    if err:
        return err

    log_path = log_path or CONFIG["defaults"]["log_path"]
    playbook = str(Path(CONFIG["playbook_dir"]) / CONFIG["health_playbook_file"])
    forks = str(CONFIG["defaults"]["forks"])
    timeout = CONFIG["defaults"]["timeout_seconds"]
    combined_file = CONFIG["output"]["health_combined_file"]

    cmd = [
        "ansible-playbook", playbook,
        "-i", CONFIG["inventory_path"],
        "-e", f"target_group={target_group}",
        "-e", f"log_path={log_path}",
        "-f", forks,
    ]

    outcome = _run(cmd, timeout)
    if isinstance(outcome, str):
        return outcome
    returncode, stdout, stderr = outcome

    if returncode != 0:
        detail = (stdout[-2000:] + "\n" + stderr[-500:]).strip()
        return f"Health check failed (exit code {returncode})\nCommand: {' '.join(cmd)}\n---\n{detail}"

    try:
        with open(combined_file) as f:
            output = f.read()
    except FileNotFoundError:
        return f"Error: no combined report at {combined_file} — run reported success but produced no report.\nStdout tail:\n{stdout[-1000:]}"
    except Exception as e:
        return f"Error reading combined report: {type(e).__name__}: {e}"

    return output


# ---------------------------------------------------------------------
# Pacemaker/HA cluster check
# ---------------------------------------------------------------------

@mcp.tool()
def check_pacemaker_cluster(target_group: str) -> str:
    """
    Check Pacemaker/Corosync cluster status on every host in a group
    (equivalent to `pcs status` or `crm_mon -1`, whichever is available).
    Shows node status, resource state, and any failed actions — the
    first thing to check when a clustered service (e.g. HA database,
    VIP failover) isn't behaving as expected.

    Args:
        target_group: Inventory group name (must be in the allowlist).

    Returns:
        Per-host cluster status output, or a note if neither pcs nor
        crm_mon is installed on that host.
    """
    cmd = (
        "sudo pcs status 2>/dev/null "
        "|| sudo crm_mon -1 -A 2>/dev/null "
        "|| echo 'Neither pcs nor crm_mon found — Pacemaker may not be installed on this host'"
    )
    return _adhoc(target_group, "shell", cmd, become=False)


# ---------------------------------------------------------------------
# RCA report generation
# ---------------------------------------------------------------------

@mcp.tool()
def generate_rca_report(
    incident_title: str,
    summary: str,
    events: list[dict],
    root_cause: str,
    resolution: str,
    affected_hosts: str = "",
) -> str:
    """
    Generate a formatted Root Cause Analysis (RCA) report from a
    timeline of events, in canonical incident-report structure with
    events sorted into chronological order. Saves the report to disk
    and returns both the file path and the full formatted text.

    Use this after gathering evidence (e.g. via full_health_check,
    scan_logs, check_pacemaker_cluster) to produce a client-ready
    document — don't write the report yourself in prose; call this
    tool with the structured facts instead.

    Args:
        incident_title: Short name for the incident, e.g. "API latency
            spike on webservers".
        summary: 2-4 sentence high-level summary of what happened and impact.
        events: List of {"time": "<ISO 8601 or human timestamp>",
            "description": "<what happened>"} dicts. Need not be
            pre-sorted — this tool sorts them chronologically. Example:
            [{"time": "2026-09-06T11:29:00Z", "description": "Disk usage crossed 90% on web01"},
             {"time": "2026-09-06T11:31:00Z", "description": "nginx service failed"}]
        root_cause: The identified root cause, stated plainly.
        resolution: What was done to resolve it, and current status.
        affected_hosts: Optional comma-separated list of affected hostnames/IPs.

    Returns:
        The full formatted RCA report text, plus the path it was saved to.
    """
    from datetime import datetime, timezone

    def _sort_key(e):
        return e.get("time", "")

    sorted_events = sorted(events, key=_sort_key)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        f"# RCA Report: {incident_title}",
        "",
        f"**Generated:** {generated_at}",
    ]
    if affected_hosts:
        lines.append(f"**Affected hosts:** {affected_hosts}")
    lines += [
        "",
        "## Summary",
        summary,
        "",
        "## Timeline",
    ]
    for e in sorted_events:
        t = e.get("time", "unknown time")
        desc = e.get("description", "")
        lines.append(f"- **{t}** — {desc}")
    lines += [
        "",
        "## Root Cause",
        root_cause,
        "",
        "## Resolution",
        resolution,
    ]

    report_text = "\n".join(lines)

    def _esc(s: str) -> str:
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    timeline_rows = "\n".join(
        f'<tr><td style="padding:6px 12px;border-bottom:1px solid #e5e7eb;'
        f'font-family:monospace;color:#374151;white-space:nowrap;">{_esc(e.get("time", "unknown time"))}</td>'
        f'<td style="padding:6px 12px;border-bottom:1px solid #e5e7eb;color:#111827;">{_esc(e.get("description", ""))}</td></tr>'
        for e in sorted_events
    )

    affected_hosts_html = (
        f'<p style="margin:4px 0;"><strong>Affected hosts:</strong> '
        f'<span style="font-family:monospace;">{_esc(affected_hosts)}</span></p>'
        if affected_hosts else ""
    )

    report_html = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:720px;margin:0 auto;color:#111827;">
  <div style="background:#1e293b;color:#ffffff;padding:20px 24px;border-radius:8px 8px 0 0;">
    <h1 style="margin:0;font-size:20px;">RCA Report: {_esc(incident_title)}</h1>
    <p style="margin:6px 0 0;font-size:13px;color:#cbd5e1;">Generated: {generated_at}</p>
  </div>
  <div style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;padding:20px 24px;">
    {affected_hosts_html}

    <h2 style="font-size:15px;color:#1e293b;border-bottom:2px solid #3b82f6;padding-bottom:6px;margin-top:20px;">Summary</h2>
    <p style="line-height:1.5;">{_esc(summary)}</p>

    <h2 style="font-size:15px;color:#1e293b;border-bottom:2px solid #3b82f6;padding-bottom:6px;margin-top:20px;">Timeline</h2>
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:8px;">
      {timeline_rows}
    </table>

    <h2 style="font-size:15px;color:#b91c1c;border-bottom:2px solid #ef4444;padding-bottom:6px;margin-top:20px;">Root Cause</h2>
    <p style="line-height:1.5;background:#fef2f2;padding:10px 14px;border-left:3px solid #ef4444;border-radius:4px;">{_esc(root_cause)}</p>

    <h2 style="font-size:15px;color:#15803d;border-bottom:2px solid #22c55e;padding-bottom:6px;margin-top:20px;">Resolution</h2>
    <p style="line-height:1.5;background:#f0fdf4;padding:10px 14px;border-left:3px solid #22c55e;border-radius:4px;">{_esc(resolution)}</p>
  </div>
</div>
"""

    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in incident_title)[:60]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(CONFIG.get("output", {}).get("tmp_dir", "/tmp"))
    md_path = out_dir / f"rca_{safe_name}_{timestamp}.md"
    html_path = out_dir / f"rca_{safe_name}_{timestamp}.html"

    try:
        md_path.write_text(report_text)
        html_path.write_text(report_html)
    except Exception as e:
        return f"Report generated but could not be saved to disk ({type(e).__name__}: {e}):\n\n{report_text}"

    return (
        f"RCA report saved to:\n"
        f"  Markdown: {md_path}\n"
        f"  HTML: {html_path}\n\n"
        f"To email this report with full HTML formatting, call send_email_report with "
        f"html_body set to the contents of {html_path}, and optionally attachment_path={html_path} "
        f"to also attach it as a file.\n\n"
        f"{report_text}"
    )


# ---------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------

@mcp.tool()
def send_email_report(
    to_address: str,
    subject: str,
    body: str,
    html_body: str = "",
    attachment_path: str = "",
) -> str:
    """
    Send an email (e.g. a completed RCA report or health-check summary)
    to a client or team address via the Resend API. Requires
    RESEND_API_KEY to be set as an environment variable, and a
    'from_address' set in config.yaml under 'email' (must be a domain
    verified in your Resend account, or Resend's shared test address
    for initial testing).

    Args:
        to_address: Recipient email address.
        subject: Email subject line.
        body: Plain-text fallback body — always required, shown by
            clients that can't render HTML.
        html_body: Optional HTML version of the email for full color
            formatting. When generate_rca_report is used, pass the
            contents of its saved .html file here for a styled,
            color-coded report in the email body itself.
        attachment_path: Optional absolute path to a file to attach
            (e.g. the .html or .md file generate_rca_report saved).
            Attaches it as-is; the recipient can open it directly.

    Returns:
        Confirmation the email was sent (with Resend's message ID),
        or a detailed error if the API key is missing or the send failed.
    """
    import base64
    import json
    import os
    import urllib.request
    import urllib.error

    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        return (
            "Error: RESEND_API_KEY environment variable is not set. "
            "Sign up at https://resend.com, create an API key, and set it "
            "as an environment variable for this service."
        )

    email_cfg = CONFIG.get("email", {})
    from_address = email_cfg.get("from_address")
    if not from_address:
        return (
            "Error: config.yaml is missing an 'email.from_address' entry. "
            "For initial testing without a verified domain, use "
            "'onboarding@resend.dev' (Resend's shared test sender)."
        )

    payload_dict = {
        "from": from_address,
        "to": [to_address],
        "subject": subject,
        "text": body,
    }
    if html_body:
        payload_dict["html"] = html_body

    if attachment_path:
        try:
            with open(attachment_path, "rb") as f:
                file_bytes = f.read()
        except Exception as e:
            return f"Error: could not read attachment_path '{attachment_path}': {type(e).__name__}: {e}"
        payload_dict["attachments"] = [{
            "filename": Path(attachment_path).name,
            "content": base64.b64encode(file_bytes).decode("ascii"),
        }]

    payload = json.dumps(payload_dict).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Resend's API is behind Cloudflare, which blocks the default
            # urllib User-Agent as bot-like traffic (HTTP 403, error 1010)
            # before the request ever reaches Resend. A normal-looking
            # User-Agent avoids that.
            "User-Agent": "ansible-log-mcp/1.0 (+https://resend.com/docs/api-reference/emails/send-email)",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return f"Email sent to {to_address}. Resend message ID: {result.get('id', 'unknown')}"
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        return f"Error sending email (HTTP {e.code}): {error_body}"
    except Exception as e:
        return f"Error sending email: {type(e).__name__}: {e}"


if __name__ == "__main__":
    mcp.run()
