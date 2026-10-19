"""Read-only Codex app-server capability probe; never starts a call."""
import asyncio
import json


async def main():
    process = await asyncio.create_subprocess_exec(
        "codex", "app-server", "--listen", "stdio://", "--enable", "apps",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=16 * 1024 * 1024,
    )
    counter = 0
    async def rpc(method, params):
        nonlocal counter
        counter += 1
        wanted = counter
        process.stdin.write((json.dumps({"id": wanted, "method": method, "params": params}) + "\n").encode())
        await process.stdin.drain()
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), 45)
            if not line:
                raise RuntimeError("app_server_closed")
            message = json.loads(line)
            if message.get("id") == wanted:
                return message
            if "id" in message and "method" in message:
                process.stdin.write((json.dumps({"id": message["id"], "error": {"code": -32000, "message": "Read-only capability probe cannot approve actions"}}) + "\n").encode())
                await process.stdin.drain()
    try:
        result = await rpc("initialize", {"clientInfo": {"name": "dots_phone_capability_probe", "version": "0.1.0"}, "capabilities": {"experimentalApi": True}})
        print(json.dumps({"initialize_ok": "result" in result}), flush=True)
        process.stdin.write(b'{"method":"initialized"}\n')
        await process.stdin.drain()
        account = await rpc("account/read", {"refreshToken": False})
        print(json.dumps({"account_type": account.get("result", {}).get("account", {}).get("type"), "error": account.get("error")}), flush=True)
        apps = await rpc("app/installed", {"forceRefresh": True})
        print(json.dumps({"apps": apps.get("result", {}).get("apps"), "error": apps.get("error")}), flush=True)
        thread = await rpc("thread/start", {"cwd": str(__import__("pathlib").Path.cwd()), "ephemeral": True, "approvalPolicy": "never", "sandbox": "read-only", "serviceName": "dots_phone_owned_worker"})
        if "error" in thread:
            print(json.dumps({"thread_error": thread["error"]}), flush=True)
            return
        tid = thread["result"]["thread"]["id"]
        print(json.dumps({"ephemeral": thread["result"]["thread"]["ephemeral"]}), flush=True)
        inventory = await rpc("mcpServerStatus/list", {"threadId": tid, "detail": "toolsAndAuthOnly", "limit": 100})
        servers = inventory.get("result", {}).get("data", [])
        for server in servers:
            tools = server.get("tools", {})
            if isinstance(tools, list):
                tools = {x["name"]: x for x in tools}
            selected = [n for n in tools if n in {"gmail.get_profile", "dots_phone.get_call", "yahoo_dot.yahoo_status"}]
            print(json.dumps({"server": server.get("name"), "selected_tools": selected, "tool_count": len(tools)}), flush=True)
            for tool in selected:
                if "get_call_result" in tool:
                    continue
                args = {"call_id": "f584862f895547239f29400367eba385"} if "get_call" in tool else {}
                response = await rpc("mcpServer/tool/call", {"threadId": tid, "server": server["name"], "tool": tool, "arguments": args})
                print(json.dumps({"called": tool, "rpc_ok": "result" in response, "error": response.get("error"), "result_keys": list(response.get("result", {})), "is_error": response.get("result", {}).get("isError")}), flush=True)
    finally:
        process.terminate()
        await process.wait()


if __name__ == "__main__":
    asyncio.run(main())
