"""
Standalone test client for ansible-logs over HTTP/SSE. Calls any tool
with any JSON arguments — use this to test the real Ansible tools
(ping_hosts, scan_logs, full_health_check, etc.) through the persistent
systemd service, not just the connection handshake.

Usage:
    python3 test_http_client.py <url> <api_key> <tool_name> [json_args]

Examples:
    # List tools only (no tool_name given)
    python3 test_http_client.py http://127.0.0.1:8787/sse <key>

    # Call a tool with no arguments
    python3 test_http_client.py http://127.0.0.1:8787/sse <key> list_allowed_groups

    # Call a tool with arguments (JSON)
    python3 test_http_client.py http://127.0.0.1:8787/sse <key> ping_hosts '{"target_group": "dev_test_hosts"}'

    python3 test_http_client.py http://127.0.0.1:8787/sse <key> full_health_check '{"target_group": "dev_test_hosts"}'
"""

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.sse import sse_client


async def main(url: str, api_key: str, tool_name: str = None, args_json: str = "{}"):
    headers = {"Authorization": f"Bearer {api_key}"}

    print(f"Connecting to {url} ...")
    async with sse_client(url, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("Connected and initialized.\n")

            tools = await session.list_tools()
            print(f"Available tools ({len(tools.tools)}):")
            for t in tools.tools:
                print(f"  - {t.name}")
            print()

            if not tool_name:
                return

            try:
                args = json.loads(args_json)
            except json.JSONDecodeError as e:
                print(f"Error: invalid JSON in arguments: {e}")
                return

            print(f"Calling {tool_name} with args={args} ...")
            print("(This may take a while on high-latency links — each remote host adds real SSH round-trip time.)\n")
            result = await session.call_tool(tool_name, args)
            for block in result.content:
                if hasattr(block, "text"):
                    print(block.text)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    url = sys.argv[1]
    api_key = sys.argv[2]
    tool_name = sys.argv[3] if len(sys.argv) > 3 else None
    args_json = sys.argv[4] if len(sys.argv) > 4 else "{}"
    asyncio.run(main(url, api_key, tool_name, args_json))
