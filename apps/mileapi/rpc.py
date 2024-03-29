import asyncio
import atexit
import json
from random import choice

import aiohttp

from core.config import WEB_WALLET_URL, API_VERIFY_SSL


class config:
    version = "1"
    nodes_url = WEB_WALLET_URL + "/v1/nodes.json"


class Rpc:

    headers = {'content-type': 'application/json'}
    __urls = None
    __current_url_index = 0
    __path = "/v" + config.version + "/api"

    def __init__(self, method, params):
        self.id = 0
        self.__payload = {
            "method": method,
            "params": params,
            "jsonrpc": "2.0",
            "id": self.id,
        }

    @classmethod
    async def get_url(cls):

        if not cls.__urls:
            cls.__session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(verify_ssl=API_VERIFY_SSL))
            atexit.register(lambda: asyncio.get_event_loop().run_until_complete(cls.__session.close()))

            cls.__urls = await (await cls.__session.get(config.nodes_url)).json()

        return choice(cls.__urls) + cls.__path
        # cls.__current_url_index += 1
        # return cls.__urls[cls.__current_url_index % len(cls.__urls)] + cls.__path

    async def exec(self, url=None) -> (dict, str):
        self.__payload["id"] = self.id
        data = json.dumps(self.__payload)
        self.id += 1

        if not url:
            url = await self.get_url()
        response = await (await self.__session.post(
            url,
            data=data,
            headers=self.headers
        )).json()

        error = response.get('error', None)
        if error:
            raise Exception(error['message'])

        return response['result'], url
