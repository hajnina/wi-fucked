"""Peer registry: allocation, idempotency, persistence, exhaustion, and the
concurrency guard that stops two workers handing out the same tunnel address.
"""

from __future__ import annotations

import json
import threading

import pytest

from fabric.peers import Allocation, PeerRegistry, PoolExhausted


@pytest.fixture
def registry(tmp_path):
    return PeerRegistry(path=tmp_path / "peers.json")


def test_first_allocation_skips_the_server_address(registry):
    # .1 is the fabric itself, so the first appliance gets .2.
    alloc = registry.allocate("pubkey-A")
    assert alloc == Allocation("pubkey-A", "10.99.0.2", created=True)
    assert registry.server_address == "10.99.0.1"


def test_allocation_is_idempotent_for_a_known_key(registry):
    first = registry.allocate("pubkey-A")
    again = registry.allocate("pubkey-A")
    assert again.address == first.address
    assert again.created is False


def test_distinct_keys_get_distinct_addresses(registry):
    a = registry.allocate("pubkey-A").address
    b = registry.allocate("pubkey-B").address
    assert a == "10.99.0.2"
    assert b == "10.99.0.3"


def test_allocation_persists_across_instances(tmp_path):
    path = tmp_path / "peers.json"
    PeerRegistry(path=path).allocate("pubkey-A")
    # A fresh registry (e.g. after a container restart with a mounted volume)
    # returns the same address rather than reissuing it.
    reloaded = PeerRegistry(path=path).allocate("pubkey-A")
    assert reloaded.address == "10.99.0.2"
    assert reloaded.created is False

    stored = json.loads(path.read_text())
    assert stored["peers"]["pubkey-A"] == "10.99.0.2"


def test_pool_exhaustion_raises(tmp_path):
    # /30 -> hosts .1 and .2; .1 is the server, leaving exactly one appliance slot.
    registry = PeerRegistry(path=tmp_path / "peers.json", pool="10.99.0.0/30")
    assert registry.allocate("pubkey-A").address == "10.99.0.2"
    with pytest.raises(PoolExhausted):
        registry.allocate("pubkey-B")


def test_concurrent_allocation_never_double_assigns(tmp_path):
    """The race the flock/threading guard exists to prevent.

    Many threads register distinct keys at once; every one must get a unique
    address. Threads exercise the in-process lock; the flock covers the
    cross-process (multi-worker) case that cannot be reproduced in one process.
    """
    registry = PeerRegistry(path=tmp_path / "peers.json")
    keys = [f"pubkey-{i}" for i in range(50)]
    results: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(keys))

    def worker(key: str) -> None:
        barrier.wait()  # maximise contention
        address = registry.allocate(key).address
        with lock:
            results.append(address)

    threads = [threading.Thread(target=worker, args=(key,)) for key in keys]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == len(keys)
    assert len(set(results)) == len(keys)  # no address handed out twice
