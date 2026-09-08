"""Entry point: `python -m mcp_server`. Serves over stdio.

`stdio_server()` diverts the process's real fd 1 to stderr for the duration
of the connection and drives the JSON-RPC wire through a private duplicated
descriptor, so even a stray `print()` in our code or a dependency lands on
stderr, not the wire — see `mcp.server.stdio` for the mechanism. We still
never use bare `print()` and route all logging through `logging` (configured
onto stderr in `mcp_server.server`), both as defense in depth and so the
convention holds even outside this transport.
"""

from __future__ import annotations

import anyio
from mcp.server.stdio import stdio_server

from mcp_server.server import logger, server


async def _main() -> None:
    logger.info("starting mcp_server on stdio transport")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    anyio.run(_main)


if __name__ == "__main__":
    main()
