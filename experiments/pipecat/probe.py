"""Exercise actual MCP HTTP discovery/tools from inside the Compose network."""

import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    async with streamable_http_client("http://phone-app:8000/mcp") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("get_baseline_status", {})
            print(
                json.dumps(
                    {"tools": [t.name for t in tools.tools], "result": result.structured_content}
                )
            )


if __name__ == "__main__":
    asyncio.run(main())
