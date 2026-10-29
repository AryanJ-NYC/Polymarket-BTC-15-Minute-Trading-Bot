import asyncio
from contextlib import contextmanager
from decimal import Decimal
from queue import Queue
from threading import Event, Thread

import pytest
from stream_fakes import FakeSocket, wait_for

from execution.order_notifications import OrderNotifications
from execution.polymarket_client import parse_market


def test_explicit_snapshot_subscription_is_required_and_quiet_accounts_stay_ready(monkeypatch):
    import execution.order_notifications as module

    elapsed = [100.0]
    monkeypatch.setattr(module, "monotonic", lambda: elapsed[0])
    with receiver() as (notifications, factory):
        socket = factory.ready()
        wait_for(lambda: len(socket.subscriptions) == 2)
        assert socket.subscriptions == [
            ("orders-1", None),
            ("snapshot-1", "SUBSCRIPTION_TYPE_ORDER_SNAPSHOT"),
        ]
        socket.deliver(snapshot(socket, True, "SUBSCRIPTION_TYPE_ORDER_SNAPSHOT"))
        elapsed[0] = 200.0
        # An account can legitimately have no order traffic for minutes. This is
        # readiness for advisory hints, not freshness of cached accounting data.
        assert notifications.health()["ready"]
        assert notifications.health()["generation"] == 1


@pytest.mark.parametrize("automatic_first", [False, True])
def test_automatic_snapshots_do_not_conflict_with_explicit_readiness(automatic_first):
    with receiver() as (notifications, factory):
        socket = factory.ready()
        automatic = snapshot(socket, True, "SUBSCRIPTION_TYPE_ORDER")
        if automatic_first:
            socket.deliver(automatic)
            assert not notifications.health()["ready"]
        socket.deliver(snapshot(socket, True))
        if not automatic_first:
            socket.deliver(automatic)
        assert notifications.health()["ready"]
        revision = notifications.revision("known")
        socket.deliver(update(socket, "known", "ORDER_STATE_FILLED"))
        assert notifications.wait("known", revision, 0)


def test_snapshot_requires_eof_before_advisory_updates():
    subscription_type = "SUBSCRIPTION_TYPE_ORDER_SNAPSHOT"
    with receiver() as (notifications, factory):
        socket = factory.ready()
        revision = notifications.revision("known")
        socket.deliver(snapshot(socket, False, subscription_type))
        socket.deliver(update(socket, "known", "ORDER_STATE_FILLED"))
        assert not notifications.health()["ready"]
        assert not notifications.wait("known", revision, 0)
        socket.deliver(snapshot(socket, True, subscription_type))
        assert notifications.health()["ready"]
        assert not notifications.wait("known", revision, 0)
        socket.deliver(update(socket, "known", "ORDER_STATE_PARTIALLY_FILLED"))
        assert notifications.wait("known", revision, 0)
        revision = notifications.revision("known")
        socket.deliver(update(socket, "known", "ORDER_STATE_FILLED"))
        assert notifications.wait("known", revision, 0)
        notifications.forget("known")
        assert notifications.health()["watched"] == 0


@pytest.mark.parametrize("payload", [{"orders": []}, {"orders": [{"id": None}], "eof": True}])
def test_malformed_snapshot_cannot_enable_notification_shortcuts(payload):
    with receiver() as (notifications, factory):
        socket = factory.ready()
        revision = notifications.revision("known")
        message = snapshot(socket, True)
        message["orderSubscriptionSnapshot"] = payload
        socket.deliver(message)
        socket.deliver(update(socket, "known", "ORDER_STATE_FILLED"))
        assert not notifications.health()["ready"]
        assert not notifications.wait("known", revision, 0)


def test_order_update_during_failed_rest_read_is_not_lost(venue, monkeypatch):
    with receiver() as (notifications, factory):
        socket = factory.ready()
        socket.deliver(snapshot(socket, True))
        market = parse_market(venue.market)
        plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("5"))
        order_id = venue.client.submit(plan)
        venue.client.order_notifications = notifications
        venue.failures[("GET", f"/v1/order/{order_id}")] = [404]
        reads = []

        def during_read(request):
            if request.url.path == f"/v1/order/{order_id}":
                reads.append(request)
                if len(reads) == 1:
                    socket.deliver(update(socket, order_id, "ORDER_STATE_FILLED"))

        venue.on_request = during_read
        original_wait = notifications.wait
        wakeups = []

        def wait_for_hint(*args):
            result = original_wait(*args)
            wakeups.append(result)
            return result

        monkeypatch.setattr(notifications, "wait", wait_for_hint)
        try:
            assert venue.client.order(order_id)["id"] == order_id
            assert len(reads) == 2
            assert wakeups == [True]
            assert notifications.health()["watched"] == 0
        finally:
            venue.client.order_notifications = None


def test_reconnect_requires_new_snapshot_and_ignores_old_generation():
    with receiver() as (notifications, factory):
        old = factory.ready()
        old.deliver(snapshot(old, True))
        old_revision = notifications.revision("known")
        old.disconnect()
        wait_for(lambda: not notifications.health()["ready"])
        new = factory.ready()
        revision = notifications.revision("known")
        old.deliver(snapshot(old, True))
        old.deliver(update(old, "known", "ORDER_STATE_FILLED"))
        assert not notifications.health()["ready"]
        new.deliver(snapshot(new, True))
        assert not notifications.wait("known", revision, 0)
        new.deliver(update(new, "known", "ORDER_STATE_FILLED"))
        assert notifications.wait("known", revision, 0)
        assert not notifications.wait("known", old_revision, 0)


def test_shutdown_releases_waiter_and_closes_socket():
    factory = PrivateFactory()
    notifications = OrderNotifications(factory)
    notifications.start()
    socket = factory.ready()
    revision = notifications.revision("known")
    finished = Event()
    waiter = Thread(target=lambda: (notifications.wait("known", revision, 30), finished.set()))
    waiter.start()
    notifications.close()
    waiter.join(timeout=1)
    assert finished.is_set()
    assert socket.closed
    assert not notifications.health()["ready"]


def test_shutdown_during_connect_skips_subscription_and_closes_receiver():
    socket = HeldConnectSocket()
    notifications = OrderNotifications(lambda: socket)
    notifications.start()
    errors = []

    def close_receiver():
        try:
            notifications.close()
        except Exception as exc:
            errors.append(exc)

    closer = Thread(target=close_receiver)
    try:
        assert socket.connect_started.wait(1)
        revision = notifications.revision("known")
        closer.start()
        # With no snapshot or updates, this can return true only once close
        # has published its stop signal. Release connect strictly afterward.
        assert notifications.wait("known", revision, 1)
        socket.loop.call_soon_threadsafe(socket.release_connect.set)
        closer.join(timeout=1)
        assert not closer.is_alive()
        assert not errors
        assert socket.closed
        assert socket.subscriptions == []
        assert not notifications.health()["ready"]
    finally:
        if socket.connect_started.is_set() and not socket.closed:
            socket.loop.call_soon_threadsafe(socket.release_connect.set)
        if closer.ident is not None:
            closer.join(timeout=2)
        notifications.close()


def test_notification_watch_map_stays_bounded_and_unknown_updates_do_not_grow_it():
    with receiver() as (notifications, factory):
        socket = factory.ready()
        socket.deliver(snapshot(socket, True))
        for index in range(100):
            notifications.revision(str(index))
            socket.deliver(update(socket, f"unknown-{index}", "ORDER_STATE_FILLED"))
        assert notifications.health()["watched"] == 64
        notifications.forget("99")
        assert notifications.health()["watched"] == 63


@contextmanager
def receiver():
    factory = PrivateFactory()
    notifications = OrderNotifications(factory)
    notifications.start()
    try:
        yield notifications, factory
    finally:
        notifications.close()


class PrivateFactory:
    def __init__(self):
        self.created = Queue()

    def __call__(self):
        socket = PrivateSocket()
        self.created.put(socket)
        return socket

    def ready(self):
        socket = self.created.get(timeout=5)
        wait_for(lambda: len(socket.subscriptions) == 2)
        return socket


class PrivateSocket(FakeSocket):
    async def subscribe_orders(self, request_id):
        self.subscriptions.append((request_id, None))

    def deliver(self, message):
        delivered = Event()

        def dispatch():
            self.listeners["message"](message)
            delivered.set()

        self.loop.call_soon_threadsafe(dispatch)
        assert delivered.wait(1), "Receiver did not process the test message"


class HeldConnectSocket(PrivateSocket):
    def __init__(self):
        super().__init__()
        self.connect_started = Event()

    async def connect(self):
        self.loop = asyncio.get_running_loop()
        self.release_connect = asyncio.Event()
        self.connect_started.set()
        await self.release_connect.wait()
        self.listeners["open"]()


def snapshot(socket, eof, subscription_type="SUBSCRIPTION_TYPE_ORDER_SNAPSHOT"):
    return {
        "requestId": (
            socket.subscriptions[1][0]
            if subscription_type == "SUBSCRIPTION_TYPE_ORDER_SNAPSHOT"
            else socket.subscriptions[0][0]
        ),
        "subscriptionType": subscription_type,
        "orderSubscriptionSnapshot": {"orders": [], "eof": eof},
    }


def update(socket, order_id, state):
    return {
        "requestId": socket.subscriptions[0][0],
        "subscriptionType": "SUBSCRIPTION_TYPE_ORDER",
        "orderSubscriptionUpdate": {"execution": {"order": {"id": order_id, "state": state}}},
    }
