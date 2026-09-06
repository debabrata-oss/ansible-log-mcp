# ansible-log-mcp

MCP server that lets Claude Desktop trigger Ansible-driven log scans across
an inventory group (e.g. `patching_group`, up to 200+ hosts) and returns
combined results.

## Architecture

Claude Desktop -> MCP server (stdio) -> Ansible playbook -> target hosts
(parallel forks) -> combined result -> back up the same chain.

## Setup

```bash
git clone <this-repo>
cd ansible-log-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Edit `config.yaml`:
- `playbook_dir` / `inventory_path` — point at real paths
- `allowed_groups` — restrict to groups this server may touch

Symlink the real inventory instead of duplicating it:
```bash
ln -sf /etc/ansible/hosts playbooks/inventory/hosts.ini
```

## Test before wiring into Claude Desktop

```bash
# 1. Playbook directly, against a small test group
ansible-playbook playbooks/logscan.yml \
  -i playbooks/inventory/hosts.ini \
  -e target_group=dev_test_hosts -e search_pattern=error -f 5

# 2. Unit tests
pytest tests/

# 3. MCP server via inspector (browser UI, no Claude Desktop needed)
npx @modelcontextprotocol/inspector python3 server/ansible_mcp.py
```

## Wire into Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ansible-logs": {
      "command": "/absolute/path/to/.venv/bin/python3",
      "args": ["/absolute/path/to/ansible-log-mcp/server/ansible_mcp.py"]
    }
  }
}
```

Restart Claude Desktop. Ask: "scan patching_group for kernel panics in the
last day."

## Security notes

- Runs as a dedicated `claude-agent` SSH user, key-based auth only
- `allowed_groups` in `config.yaml` is the only guardrail on scope —
  keep it tight
- No `become: true` by default; add sudo only for specific read commands
  if needed, not blanket root
