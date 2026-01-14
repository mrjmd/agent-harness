#!/usr/bin/env python3
"""
MCP Server Manager

Handles lifecycle management of MCP (Model Context Protocol) servers.
Spins up servers defined in .claude/config.json, gets their tool definitions,
and routes tool calls to the appropriate server.

This is what makes the agent actually able to DO things instead of just
talking about what it would do.
"""

import json
import subprocess
import sys
import asyncio
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class MCPServer:
    """Represents a running MCP server."""
    name: str
    process: subprocess.Popen
    tools: list[dict]
    tool_names: set[str]


class MCPManager:
    """
    Manages MCP server lifecycle and tool routing.

    Usage:
        mcp = MCPManager.from_config()
        tools = mcp.get_all_tools()  # Pass to Claude API

        # When Claude returns tool_use:
        result = mcp.execute_tool(tool_name, tool_input)

        # Clean up:
        mcp.shutdown()
    """

    def __init__(self):
        self.servers: dict[str, MCPServer] = {}
        self._tool_to_server: dict[str, str] = {}

    @classmethod
    def from_config(cls, config_path: Path = None) -> "MCPManager":
        """Create MCPManager from .claude/config.json."""
        if config_path is None:
            config_path = Path(".claude/config.json")

        if not config_path.exists():
            raise FileNotFoundError(f"MCP config not found: {config_path}")

        try:
            config = json.loads(config_path.read_text())
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in MCP config {config_path}: {e}")

        manager = cls()

        mcp_servers = config.get("mcpServers", {})
        if not mcp_servers:
            print("Warning: No MCP servers defined in config.json")
            return manager

        for name, server_config in mcp_servers.items():
            try:
                manager._start_server(name, server_config)
            except Exception as e:
                print(f"Warning: Failed to start MCP server '{name}': {e}")

        return manager

    def _start_server(self, name: str, config: dict) -> None:
        """
        Start an MCP server process and get its tool definitions.

        MCP servers communicate via stdio using JSON-RPC.
        """
        command = config.get("command")
        args = config.get("args", [])
        env = config.get("env", {})

        if not command:
            raise ValueError(f"Server '{name}' missing 'command' in config")

        # Merge environment variables
        full_env = {**dict(subprocess.os.environ), **env}

        # Start the server process
        print(f"Starting MCP server: {name} ({command})")

        process = subprocess.Popen(
            [command] + args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
            text=False  # Binary mode for JSON-RPC
        )

        # Initialize the server and get tools
        tools = self._initialize_server(process, name)

        server = MCPServer(
            name=name,
            process=process,
            tools=tools,
            tool_names={t["name"] for t in tools}
        )

        self.servers[name] = server

        # Map tool names to servers for routing
        for tool in tools:
            self._tool_to_server[tool["name"]] = name

        print(f"  Server '{name}' provides {len(tools)} tools")

    def _initialize_server(self, process: subprocess.Popen, name: str) -> list[dict]:
        """
        Initialize MCP server and retrieve tool definitions.

        Uses MCP JSON-RPC protocol:
        1. Send 'initialize' request
        2. Send 'initialized' notification
        3. Send 'tools/list' request to get available tools
        """
        try:
            # Send initialize request
            init_request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "agent-harness",
                        "version": "1.0.0"
                    }
                }
            }
            self._send_message(process, init_request)
            init_response = self._read_message(process, timeout=10)

            if "error" in init_response:
                raise RuntimeError(f"Initialize failed: {init_response['error']}")

            # Send initialized notification
            initialized_notification = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized"
            }
            self._send_message(process, initialized_notification)

            # Request tool list
            tools_request = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {}
            }
            self._send_message(process, tools_request)
            tools_response = self._read_message(process, timeout=10)

            if "error" in tools_response:
                raise RuntimeError(f"Tools list failed: {tools_response['error']}")

            tools = tools_response.get("result", {}).get("tools", [])

            # Convert MCP tool format to Anthropic tool format
            return [self._convert_tool_schema(t) for t in tools]

        except Exception as e:
            print(f"  Warning: Failed to initialize server '{name}': {e}")
            return []

    def _convert_tool_schema(self, mcp_tool: dict) -> dict:
        """Convert MCP tool schema to Anthropic API format."""
        return {
            "name": mcp_tool.get("name"),
            "description": mcp_tool.get("description", ""),
            "input_schema": mcp_tool.get("inputSchema", {"type": "object", "properties": {}})
        }

    def _send_message(self, process: subprocess.Popen, message: dict) -> None:
        """Send a JSON-RPC message to the server."""
        content = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(content)}\r\n\r\n".encode("utf-8")
        process.stdin.write(header + content)
        process.stdin.flush()

    def _read_message(self, process: subprocess.Popen, timeout: float = 30) -> dict:
        """Read a JSON-RPC message from the server."""
        import select

        # Read headers
        headers = {}
        while True:
            # Check if data is available
            ready, _, _ = select.select([process.stdout], [], [], timeout)
            if not ready:
                raise TimeoutError("Timeout waiting for server response")

            line = b""
            while True:
                char = process.stdout.read(1)
                if char == b"\n":
                    break
                line += char

            line = line.strip()
            if not line:
                break

            if b":" in line:
                key, value = line.split(b":", 1)
                headers[key.decode().lower()] = value.strip().decode()

        # Read content
        content_length = int(headers.get("content-length", 0))
        if content_length == 0:
            return {}

        content = process.stdout.read(content_length)
        try:
            return json.loads(content.decode("utf-8"))
        except json.JSONDecodeError as e:
            print(f"Warning: Invalid JSON from MCP server: {content[:200]}...")
            return {"error": {"code": -32700, "message": f"Parse error: {e}"}}
        except UnicodeDecodeError as e:
            return {"error": {"code": -32700, "message": f"Encoding error: {e}"}}

    def get_all_tools(self) -> list[dict]:
        """Get all tool definitions for the Anthropic API."""
        tools = []
        for server in self.servers.values():
            tools.extend(server.tools)
        return tools

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """
        Execute a tool call by routing to the appropriate server.

        Returns the result as a string.
        """
        server_name = self._tool_to_server.get(tool_name)

        if not server_name:
            return json.dumps({
                "error": f"Unknown tool: {tool_name}",
                "available_tools": list(self._tool_to_server.keys())
            })

        server = self.servers.get(server_name)
        if not server or server.process.poll() is not None:
            return json.dumps({"error": f"Server '{server_name}' is not running"})

        try:
            # Send tool call request
            request = {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": tool_input
                }
            }
            self._send_message(server.process, request)
            response = self._read_message(server.process, timeout=120)

            if "error" in response:
                return json.dumps({"error": response["error"]})

            # Extract content from response
            result = response.get("result", {})
            content = result.get("content", [])

            # MCP returns content as array of {type, text} objects
            if isinstance(content, list):
                text_parts = [
                    c.get("text", str(c))
                    for c in content
                    if isinstance(c, dict)
                ]
                return "\n".join(text_parts) if text_parts else json.dumps(result)

            return json.dumps(result)

        except TimeoutError:
            return json.dumps({"error": f"Timeout executing tool '{tool_name}'"})
        except Exception as e:
            return json.dumps({"error": f"Error executing tool '{tool_name}': {str(e)}"})

    def shutdown(self) -> None:
        """Shut down all MCP servers."""
        for name, server in self.servers.items():
            try:
                if server.process.poll() is None:
                    server.process.terminate()
                    server.process.wait(timeout=5)
            except Exception as e:
                print(f"Warning: Error shutting down server '{name}': {e}")
                try:
                    server.process.kill()
                except:
                    pass

        self.servers.clear()
        self._tool_to_server.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
        return False


# Standalone test
if __name__ == "__main__":
    print("Testing MCP Manager...")

    try:
        with MCPManager.from_config() as mcp:
            tools = mcp.get_all_tools()
            print(f"\nAvailable tools ({len(tools)}):")
            for tool in tools:
                print(f"  - {tool['name']}: {tool.get('description', '')[:60]}...")
    except FileNotFoundError as e:
        print(f"Config not found: {e}")
    except Exception as e:
        print(f"Error: {e}")
