"""Ad-hoc MCP stdio integration check (run on a box with the SDK + a live monitor).

Spawns server.py over stdio, lists tools/resources, and calls get_snapshot +
reads the changelog resource. Not part of the stdlib unit suite (needs the `mcp`
SDK / py3.10+); used to verify the FastMCP wiring against a real monitor.
"""
import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    env = dict(os.environ)
    env["HOMELAB_MONITOR_URL"] = os.environ.get("HOMELAB_MONITOR_URL", "http://localhost:9800")
    env["MCP_TRANSPORT"] = "stdio"
    params = StdioServerParameters(command=sys.executable, args=["server.py"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print("TOOLS:", sorted(t.name for t in tools))
            res = (await session.list_resources()).resources
            print("RESOURCES:", sorted(str(r.uri) for r in res))
            snap = await session.call_tool("get_snapshot", {})
            txt = snap.content[0].text if snap.content else ""
            print("get_snapshot ok, len=", len(txt), "version-in-payload:", '"version"' in txt)
            models = await session.call_tool("get_ai_models", {"range": "24h"})
            print("get_ai_models ok, len=", len(models.content[0].text))
            for name, args in (("get_containers", {}), ("get_services", {}),
                               ("get_memory", {"range": "24h"}), ("get_gpu", {"range": "24h"}),
                               ("get_history", {"range": "24h"}), ("scan_disk", {"path": "/"})):
                r = await session.call_tool(name, args)
                print("%s ok, len=%d" % (name, len(r.content[0].text)))
            cl = await session.read_resource("homelab://changelog")
            body = cl.contents[0].text
            print("changelog resource ok, starts:", body.splitlines()[0][:40])
            print("ALL INTEGRATION CHECKS OK")


if __name__ == "__main__":
    asyncio.run(main())
