"""Private order notifications accelerate REST retries; they never prove fills."""

import asyncio
from threading import Condition, Thread
from time import monotonic

from loguru import logger


class OrderNotifications:
    """Bounded known-ID hints with connection-local snapshot readiness.

    No account cache, fill ledger or replay assumptions. Only an update received
    after a failed read starts can shorten its wait; REST still proves the result.
    """

    def __init__(self, socket_factory):
        self._factory = socket_factory
        self._condition = Condition()
        self._thread = None
        self._stopped = False
        self._ready = False
        self._broken = False
        self._generation = 0
        self._revision = 0
        self._watched = {}
        self._snapshot_count = 0

    def start(self):
        with self._condition:
            if self._thread is not None or self._stopped:
                raise RuntimeError("Order notifications already started or closed")
            self._thread = Thread(
                target=lambda: asyncio.run(self._run()), name="order-notifications"
            )
            self._thread.start()

    def revision(self, order_id):
        with self._condition:
            if order_id not in self._watched:
                if len(self._watched) >= 64:
                    self._watched.pop(next(iter(self._watched)))
                self._watched[order_id] = self._revision
            return (self._generation, self._watched[order_id])

    def wait(self, order_id, revision, timeout):
        with self._condition:
            return self._condition.wait_for(
                lambda: (
                    self._stopped
                    or (
                        self._ready
                        and self._generation == revision[0]
                        and self._watched.get(order_id, revision[1]) > revision[1]
                    )
                ),
                timeout=timeout,
            )

    def forget(self, order_id):
        with self._condition:
            self._watched.pop(order_id, None)

    def health(self):
        with self._condition:
            return {
                "generation": self._generation,
                "ready": self._ready,
                "watched": len(self._watched),
            }

    def close(self):
        with self._condition:
            self._stopped = True
            self._ready = False
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout=25)
            if self._thread.is_alive():
                raise RuntimeError("Order notification receiver did not stop")

    async def _run(self):
        failures = 0
        while not self._stopped:
            socket = None
            with self._condition:
                self._generation += 1
                generation = self._generation
                self._broken = False
                self._ready = False
                self._snapshot_count = 0
                snapshot_deadline = monotonic() + 35
            request = f"orders-{generation}"
            try:
                socket = self._factory()
                socket.on("open", lambda: None)
                socket.on("message", lambda msg, g=generation: self._message(g, msg))
                socket.on("close", lambda g=generation: self._disconnect(g))
                socket.on("error", lambda *_, g=generation: self._disconnect(g))
                await asyncio.wait_for(socket.connect(), timeout=10)
                if self._stopped:
                    continue
                await asyncio.wait_for(socket.subscribe_orders(request), timeout=10)
                if self._stopped:
                    continue
                await asyncio.wait_for(
                    socket.subscribe_all(
                        f"snapshot-{generation}", "SUBSCRIPTION_TYPE_ORDER_SNAPSHOT"
                    ),
                    timeout=10,
                )
                while not self._stopped:
                    with self._condition:
                        if self._broken or (not self._ready and monotonic() >= snapshot_deadline):
                            break
                        if self._ready:
                            failures = 0
                    await asyncio.sleep(0.1)
            except Exception:
                pass  # Never expose authenticated exception bodies.
            finally:
                self._disconnect(generation)
                if socket:
                    try:
                        await asyncio.wait_for(socket.close(), timeout=10)
                    except Exception:
                        pass
            if not self._stopped:
                failures = min(failures + 1, 5)
                delay = min(30, 2**failures)
                logger.warning(
                    "Order notifications unavailable; REST reconciliation remains active"
                )
                deadline = monotonic() + delay
                while not self._stopped and monotonic() < deadline:
                    await asyncio.sleep(min(0.1, max(0, deadline - monotonic())))

    def _message(self, generation, message):
        with self._condition:
            if generation != self._generation or self._broken or self._stopped:
                return
            try:
                if not isinstance(message, dict) or "error" in message:
                    raise ValueError("Invalid envelope")
                if "heartbeat" in message:
                    return
                if message.get("requestId") not in {
                    f"orders-{generation}",
                    f"snapshot-{generation}",
                } or message.get("subscriptionType") not in {
                    "SUBSCRIPTION_TYPE_ORDER",
                    "SUBSCRIPTION_TYPE_ORDER_SNAPSHOT",
                }:
                    return
                if "orderSubscriptionSnapshot" in message:
                    # Some documented variants also send automatic snapshots on
                    # the update subscription. Only our explicit request owns
                    # readiness, so duplicate automatic responses cannot reset it.
                    if (
                        message.get("requestId") != f"snapshot-{generation}"
                        or message.get("subscriptionType") != "SUBSCRIPTION_TYPE_ORDER_SNAPSHOT"
                    ):
                        return
                    if self._ready:
                        raise ValueError("Unexpected new snapshot")
                    snapshot = message["orderSubscriptionSnapshot"]
                    orders = snapshot["orders"]
                    if not isinstance(orders, list) or type(snapshot.get("eof")) is not bool:
                        raise ValueError("Incomplete snapshot envelope")
                    self._snapshot_count += len(orders)
                    if self._snapshot_count > 10000:
                        raise ValueError("Snapshot bound exceeded")
                    for order in orders:
                        _order_id(order)
                    self._ready = snapshot["eof"]
                elif (
                    "orderSubscriptionUpdate" in message
                    and message.get("subscriptionType") == "SUBSCRIPTION_TYPE_ORDER"
                    and message.get("requestId") == f"orders-{generation}"
                ):
                    order = message["orderSubscriptionUpdate"]["execution"]["order"]
                    order_id = _order_id(order)
                    # Partial and terminal updates both prompt a fresh REST read.
                    # Missing fields, duplicate/out-of-order delivery cannot mutate
                    # accounting; initial snapshot completion is still mandatory.
                    if self._ready and order_id in self._watched:
                        self._revision += 1
                        self._watched[order_id] = self._revision
                        self._condition.notify_all()
                else:
                    raise ValueError("Unknown order envelope")
            except (KeyError, TypeError, ValueError):
                self._ready = False
                self._broken = True

    def _disconnect(self, generation):
        with self._condition:
            if generation == self._generation:
                self._ready = False
                self._broken = True


def _order_id(order):
    if not isinstance(order, dict) or not isinstance(order.get("id"), str) or not order["id"]:
        raise ValueError("Missing order identity")
    return order["id"]
