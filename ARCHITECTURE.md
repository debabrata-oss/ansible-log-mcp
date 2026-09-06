# Architecture

End-to-end flow of a request through `ansible-log-mcp`, from client to
target hosts and back, including the optional RCA report + email path.

## Components

- **Clients** — Claude Desktop (stdio) or any HTTP/SSE MCP client (Bearer-token auth)
- **MCP server** — `server/ansible_mcp.py` (tool definitions) and `server/ansible_mcp_http.py` (HTTP/SSE transport + per-user auth, imports the same tools)
- **Config** — `config.yaml` (allowed groups, paths, defaults), `users.yaml` (API keys, HTTP transport only)
- **Ansible layer** — `ansible`/`ansible-playbook` CLI, `playbooks/inventory/hosts.ini`, `playbooks/logscan.yml`, `playbooks/health_check.yml`
- **Targets** — hosts in the inventory group (parallel forks, default 50)
- **Outputs** — combined per-host report files in `/tmp`, optional RCA markdown/HTML report, optional email via Resend API

## Diagram

```mermaid
flowchart TD
    subgraph Client["Client"]
        CD["Claude Desktop\n(stdio)"]
        HC["HTTP/SSE MCP client\n(Bearer token)"]
    end

    subgraph Server["MCP Server"]
        AUTH["PerUserApiKeyMiddleware\n(users.yaml)"]
        TOOLS["ansible_mcp.py tools\nscan_logs / ping_hosts / list_hosts /\ncheck_* / sar_report / full_health_check /\ncheck_pacemaker_cluster"]
        GATE["_check_group()\nallowed_groups in config.yaml"]
        RCA["generate_rca_report()"]
        MAIL["send_email_report()\n(Resend API)"]
    end

    subgraph Ansible["Ansible Layer"]
        ADHOC["ansible ad-hoc\n(ping / command / shell modules)"]
        PB1["ansible-playbook\nlogscan.yml"]
        PB2["ansible-playbook\nhealth_check.yml"]
        INV["playbooks/inventory/hosts.ini"]
    end

    subgraph Targets["Target Hosts (parallel forks)"]
        H1["host 1"]
        H2["host 2"]
        H3["host N (...200+)"]
    end

    subgraph Outputs["Outputs"]
        LOGF["/tmp/logscan_combined.txt"]
        HCF["/tmp/healthcheck_combined.txt"]
        RCAF["rca_*.md / rca_*.html"]
    end

    CD -->|stdio, direct tool calls| TOOLS
    HC -->|"Bearer <key>"| AUTH -->|"authorized, scope=user"| TOOLS

    TOOLS --> GATE
    GATE -->|"group not allowed"| TOOLS
    GATE -->|"group allowed"| ADHOC
    GATE --> PB1
    GATE --> PB2

    ADHOC --> INV
    PB1 --> INV
    PB2 --> INV
    INV --> H1
    INV --> H2
    INV --> H3

    H1 -.->|"per-host result"| PB1
    H2 -.-> PB1
    H3 -.-> PB1
    H1 -.-> PB2
    H2 -.-> PB2
    H3 -.-> PB2
    H1 -.-> ADHOC
    H2 -.-> ADHOC
    H3 -.-> ADHOC

    PB1 -->|"assemble"| LOGF
    PB2 -->|"assemble"| HCF
    LOGF --> TOOLS
    HCF --> TOOLS
    ADHOC -->|stdout/stderr| TOOLS

    TOOLS -->|"evidence gathered"| RCA
    RCA --> RCAF
    RCA -->|"tool result"| TOOLS
    RCAF -.->|"optional, attachment_path"| MAIL
    TOOLS -->|"optional"| MAIL
    MAIL -->|"HTTPS POST"| RESEND["Resend API"]

    TOOLS -->|"formatted text result"| CD
    TOOLS -->|"formatted text result"| AUTH
    AUTH --> HC
```

## Request lifecycle (example: `scan_logs`)

1. Client calls the `scan_logs` tool (Claude Desktop over stdio, or an HTTP client with a valid `Authorization: Bearer <key>` matched against `users.yaml`).
2. `_check_group()` rejects the call if `target_group` isn't in `config.yaml`'s `allowed_groups`.
3. The server shells out to `ansible-playbook playbooks/logscan.yml` with `target_group`, `search_pattern`, `log_path` as extra-vars, against `playbooks/inventory/hosts.ini`, forking up to `defaults.forks` connections in parallel.
4. Each target host greps its log file; the playbook writes one per-host file to `/tmp`, then Ansible's `assemble` module concatenates them into `/tmp/logscan_combined.txt`.
5. The server reads that combined file and returns it as the tool result, up the same path it came in.

`full_health_check` follows the same shape via `health_check.yml`, collapsing uptime/memory/disk/top/systemd/log/pacemaker/sar checks into a single remote shell call per host to save SSH round trips. `check_uptime`, `check_memory`, `ping_hosts`, etc. skip the playbook/assemble step entirely and go straight through an `ansible` ad-hoc command.

`generate_rca_report` and `send_email_report` sit downstream of evidence-gathering tools — they don't touch the inventory at all, just format/save/email whatever the model passes in.
