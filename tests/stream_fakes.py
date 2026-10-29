import asyncio
import threading
import time

from execution.market_stream import MarketStream


class SocketFactory:
    def __init__(self):
        self.sockets = []

    def __call__(self):
        socket = FakeSocket()
        self.sockets.append(socket)
        return socket

    def ready(self, index=0):
        wait_for(lambda: len(self.sockets) > index and self.sockets[index].subscriptions)
        return self.sockets[index]


class FakeSocket:
    def __init__(self):
        self.listeners = {}
        self.subscriptions = []
        self.unsubscriptions = []
        self.closed = False

    def on(self, event, callback):
        self.listeners[event] = callback
        return self

    async def connect(self):
        self.loop = asyncio.get_running_loop()
        self.listeners["open"]()

    async def subscribe_orders(self, request_id):
        self.subscriptions.append((request_id, []))

    async def subscribe_all(self, request_id, subscription_type):
        self.subscriptions.append((request_id, subscription_type))

    async def subscribe_market_data(self, request_id, slugs):
        self.subscriptions.append((request_id, slugs))

    async def unsubscribe(self, request_id):
        self.unsubscriptions.append(request_id)

    async def close(self):
        self.closed = True

    def publish(self, message):
        self.loop.call_soon_threadsafe(self.listeners["message"], message)

    def disconnect(self):
        self.loop.call_soon_threadsafe(self.listeners["close"])


def snapshot(socket, market_row, timestamp, **changes):
    return {
        "requestId": socket.subscriptions[-1][0],
        "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
        "marketData": {
            "marketSlug": market_row["slug"],
            "state": "MARKET_STATE_OPEN",
            "transactTime": timestamp,
            "bids": [{"px": {"value": "0.63", "currency": "USD"}, "qty": "100"}],
            "offers": [{"px": {"value": "0.65", "currency": "USD"}, "qty": "40"}],
            **changes,
        },
    }


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for stream event")
        threading.Event().wait(0.002)


class started_stream:
    def __init__(self, factory, markets):
        self.stream = MarketStream(factory)
        self.stream.set_markets(markets)

    def __enter__(self):
        self.stream.start()
        return self.stream

    def __exit__(self, *args):
        self.stream.close()
