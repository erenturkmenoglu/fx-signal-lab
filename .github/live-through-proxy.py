"""The live stream through the proxy of a domain: refused without a session, flowing with one.

Run inside the application's container, which has the WebSocket library:

    python - <name> <host of the proxy> <session token>   (ROOT_PEM: the authority that signed the certificate)

The name is used for the certificate and the request; the connection itself goes to the host.
"""

import asyncio
import json
import os
import ssl
import sys

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

NAME, HOST, TOKEN = sys.argv[1], sys.argv[2], sys.argv[3]
context = ssl.create_default_context(cadata=os.environ["ROOT_PEM"])


async def main() -> None:
    url = f"wss://{NAME}/ws/live"
    try:
        async with connect(url, host=HOST, port=443, ssl=context, proxy=None):
            raise SystemExit("the live stream opened without a session")
    except InvalidStatus as refused:
        assert refused.response.status_code == 403, refused.response.status_code
    cookie = {"Cookie": f"fxlab_session={TOKEN}"}
    async with connect(url, host=HOST, port=443, ssl=context, proxy=None, additional_headers=cookie) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
        assert hello["type"] == "hello" and hello["status"]["provider"]["label"] == "MOCK", hello["type"]
        seconds = 0
        while seconds < 3:
            message = json.loads(await asyncio.wait_for(ws.recv(), 10))
            seconds += message["type"] == "obs"
        print("ok: the live stream passes the proxy,", seconds, "new seconds after the snapshot")


asyncio.run(main())
