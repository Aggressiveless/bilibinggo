"""Standard anonymous-pipe transport for Windows hosts restricting named pipes.

Framing and message validation still use the official MCP SDK. No socket or
protocol shortcut is used; only subprocess pipe IO runs on worker threads.
"""
from __future__ import annotations
import asyncio
import os
import subprocess
from contextlib import asynccontextmanager
import anyio
from mcp.types import jsonrpc_message_adapter
from mcp.shared.message import SessionMessage


@asynccontextmanager
async def threaded_stdio(parameters):
    environment = os.environ.copy()
    environment.update(parameters.env or {})
    process = subprocess.Popen([parameters.command, *parameters.args], cwd=parameters.cwd, env=environment,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    incoming_writer, incoming = anyio.create_memory_object_stream(0)
    outgoing, outgoing_reader = anyio.create_memory_object_stream(0)

    async def read():
        async with incoming_writer:
            while True:
                line = await asyncio.to_thread(process.stdout.readline)
                if not line:
                    return
                try:
                    message = SessionMessage(jsonrpc_message_adapter.validate_json(line))
                except ValueError as exc:
                    message = exc
                await incoming_writer.send(message)

    async def write():
        async with outgoing_reader:
            async for message in outgoing_reader:
                data = message.message.model_dump_json(by_alias=True, exclude_unset=True).encode("utf-8")+b"\n"
                await asyncio.to_thread(process.stdin.write, data)
                await asyncio.to_thread(process.stdin.flush)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(read)
        tasks.start_soon(write)
        try:
            yield incoming, outgoing
        finally:
            with anyio.CancelScope(shield=True):
                await outgoing.aclose()
                process.stdin.close()
                try:
                    await asyncio.to_thread(process.wait, 15)
                except subprocess.TimeoutExpired:
                    # Only the exact child created by this transport is terminated.
                    process.terminate()
                    await asyncio.to_thread(process.wait, 5)
                await incoming.aclose()
                process.stdout.close()
            tasks.cancel_scope.cancel()
