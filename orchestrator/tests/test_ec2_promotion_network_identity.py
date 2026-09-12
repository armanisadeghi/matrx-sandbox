"""Regression contract for overlapping EC2 writable-layer replacement."""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from orchestrator.hosted_migration import HostedMigrationStateError
from orchestrator.hosted_runtime import _disconnect_paused_source, _endpoint_matches, _hold_runtime_config, _pin_helper_image, _reconnect_paused_source, _source_reconnect_kwargs, migrate_hosted, replacement_config, source_endpoint_identity


def _old(*, explicit_mac: str = "", binds=None, ipam_config=None):
    return SimpleNamespace(
        id="a" * 64,
        attrs={
            "Config": {"Env": ["OLD=1"], "Volumes": {}, "MacAddress": explicit_mac},
            "HostConfig": {"Binds": binds if binds is not None else ["home:/home/agent:rw"]},
            "Mounts": [{"Type": "volume", "Name": "home", "Destination": "/home/agent", "RW": True}],
            "NetworkSettings": {"Networks": {"bridge": {
                "Aliases": ["sbx-123456789abc", "service-alias", "a" * 12],
                "MacAddress": "02:42:ac:11:00:02",
                "IPAddress": "172.17.0.22/16", "GlobalIPv6Address": "",
                "IPAMConfig": {} if ipam_config is None else ipam_config,
            }}},
        },
    )


def test_replacement_preserves_intended_aliases_but_not_generated_endpoint_mac():
    """Regression: a paused source must not make the target reuse its Docker MAC."""
    old = _old()
    config = replacement_config(old, image="sha256:" + "b" * 64, environment=["NEW=1"], operation="proof")
    endpoint = config["NetworkingConfig"]["EndpointsConfig"]["bridge"]
    assert endpoint["Aliases"] == ["sbx-123456789abc", "service-alias"]
    assert "MacAddress" not in endpoint


def test_hosted_target_create_contract_is_held_even_without_ec2_promotion():
    """Break caught: hosted migration started an image that chowned shared home before CAS."""
    runtime = replacement_config(_old(), image="sha256:" + "b" * 64,
                                 environment=["OLD=1", "MATRX_MIGRATION_HOLD=0"], operation="hosted")
    _hold_runtime_config(runtime)
    assert runtime["Env"] == ["OLD=1", "MATRX_MIGRATION_HOLD=1"]


def test_reconnect_identity_accepts_reassigned_auto_ip_but_requires_aliases():
    """Docker-generated IP/MAC may change; requested aliases remain durable."""
    old = _old()
    identity = source_endpoint_identity(old, SimpleNamespace(id="network-id", attrs={"IPAM": {"Config": []}}))
    exact = {"Aliases": ["sbx-123456789abc", "service-alias", "a" * 12],
             "IPAddress": "172.17.0.22/16", "GlobalIPv6Address": "",
             "IPPrefixLen": 16, "MacAddress": "02:42:ac:11:00:02"}
    assert _endpoint_matches(exact, identity)
    assert _endpoint_matches({**exact, "IPAddress": "172.17.0.23/16"}, identity)
    assert _endpoint_matches({**exact, "MacAddress": "02:42:ac:11:00:03"}, identity)
    assert _endpoint_matches({**exact, "IPPrefixLen": 24}, identity)
    assert not _endpoint_matches({**exact, "Aliases": ["wrong"]}, identity)
    assert not _endpoint_matches({**exact, "IPAddress": ""}, identity)
    assert not _endpoint_matches({**exact, "IPAddress": "not-an-ip"}, identity)
    assert not _endpoint_matches({**exact, "IPPrefixLen": 99}, identity)


def test_reconnect_identity_requires_explicit_ip_and_prefix_exactly():
    old = _old(ipam_config={"IPv4Address": "172.17.0.22"})
    identity = source_endpoint_identity(old, SimpleNamespace(id="network-id", attrs={"IPAM": {"Config": []}}))
    exact = {"Aliases": ["sbx-123456789abc", "service-alias", "a" * 12],
             "IPAddress": "172.17.0.22/16", "GlobalIPv6Address": "",
             "IPPrefixLen": 16, "MacAddress": "02:42:ac:11:00:02"}
    assert _endpoint_matches(exact, identity)
    assert not _endpoint_matches({**exact, "IPAddress": "172.17.0.23/16"}, identity)
    assert not _endpoint_matches({**exact, "IPPrefixLen": 24}, identity)


def test_rollback_retry_accepts_docker_reconnected_ipam_representation_without_mac_reuse():
    """Break caught: an exact-IP rollback could never migrate again after Docker added IPAMConfig."""
    old = _old(ipam_config={"IPv4Address": "172.17.0.22"})
    identity = source_endpoint_identity(old, SimpleNamespace(id="network-id", attrs={"IPAM": {"Config": []}}))
    config = replacement_config(old, image="sha256:" + "b" * 64, environment=["NEW=1"], operation="retry")
    endpoint = config["NetworkingConfig"]["EndpointsConfig"]["bridge"]
    assert identity["endpoint_ipam_config"] == {"IPv4Address": "172.17.0.22"}
    assert endpoint["IPAMConfig"] == {"IPv4Address": "172.17.0.22"}
    assert "MacAddress" not in endpoint


def test_default_auto_ipam_reconnect_never_submits_an_illegal_requested_address():
    """Break caught: rollback stranded a paused source by requesting IP on Docker auto-IPAM."""
    dynamic = source_endpoint_identity(_old(), SimpleNamespace(id="network-id", attrs={"IPAM": {"Config": [{"Subnet": "172.17.0.0/16"}]}}))
    explicit = source_endpoint_identity(_old(ipam_config={"IPv4Address": "172.17.0.22"}), SimpleNamespace(id="network-id", attrs={"IPAM": {"Config": [{"Subnet": "172.17.0.0/16"}]}}))
    assert dynamic["ipv4_explicit_ipam"] is False
    assert _source_reconnect_kwargs(dynamic) == {"aliases": ["sbx-123456789abc", "service-alias"]}
    assert explicit["ipv4_explicit_ipam"] is True
    assert _source_reconnect_kwargs(explicit)["ipv4_address"] == "172.17.0.22"


def test_ipv6_endpoint_ipam_never_turns_a_dynamic_ipv4_into_an_illegal_requested_address():
    """Break caught: IPv6-only endpoint pin caused Docker to reject auto-IPv4 reconnect."""
    old = _old(ipam_config={"IPv6Address": "fd00::22"})
    old.attrs["NetworkSettings"]["Networks"]["bridge"].update(
        {"GlobalIPv6Address": "fd00::22/64", "GlobalIPv6PrefixLen": 64}
    )
    identity = source_endpoint_identity(old, SimpleNamespace(id="network-id", attrs={"IPAM": {"Config": []}}))
    kwargs = _source_reconnect_kwargs(identity)
    assert "ipv4_address" not in kwargs
    assert kwargs["ipv6_address"] == "fd00::22"


@pytest.mark.asyncio
async def test_network_name_reuse_cannot_disconnect_the_wrong_immutable_network():
    """Break caught: a same-name recreated Docker network accepted an old journal receipt."""
    old = _old()
    old.reload = lambda: None
    network = SimpleNamespace(id="replacement-network", disconnect=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not disconnect")))
    client = SimpleNamespace(networks=SimpleNamespace(get=lambda _name: network))
    with pytest.raises(HostedMigrationStateError, match="network ID"):
        await _disconnect_paused_source({"source_endpoint": {"network": "bridge", "network_id": "original-network"}}, old, client)


@pytest.mark.asyncio
async def test_reconnect_retry_accepts_already_valid_dynamic_endpoint_without_duplicate_connect():
    """Crash after Docker connect must not make recovery connect the endpoint twice."""
    old = _old()
    old.reload = lambda: None
    identity = source_endpoint_identity(old, SimpleNamespace(id="network-id", attrs={"IPAM": {"Config": []}}))
    old.attrs["NetworkSettings"]["Networks"]["bridge"]["IPAddress"] = "172.17.0.23/16"
    old.attrs["NetworkSettings"]["Networks"]["bridge"]["IPPrefixLen"] = 16
    connect_calls = []
    network = SimpleNamespace(id="network-id", connect=lambda *_args, **_kwargs: connect_calls.append(True))
    client = SimpleNamespace(networks=SimpleNamespace(get=lambda _name: network))
    record = {"source_endpoint": identity,
              "network_disconnect_receipt": {"old_id": old.id, "absent": True}}

    await _reconnect_paused_source(record, old, client)
    assert connect_calls == []


@pytest.mark.asyncio
async def test_reconnect_retry_refuses_already_attached_wrong_aliases():
    old = _old()
    old.reload = lambda: None
    identity = source_endpoint_identity(old, SimpleNamespace(id="network-id", attrs={"IPAM": {"Config": []}}))
    old.attrs["NetworkSettings"]["Networks"]["bridge"]["Aliases"] = ["wrong"]
    old.attrs["NetworkSettings"]["Networks"]["bridge"]["IPPrefixLen"] = 16
    network = SimpleNamespace(id="network-id", connect=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not reconnect")))
    client = SimpleNamespace(networks=SimpleNamespace(get=lambda _name: network))
    record = {"source_endpoint": identity,
              "network_disconnect_receipt": {"old_id": old.id, "absent": True}}

    with pytest.raises(HostedMigrationStateError, match="disagrees"):
        await _reconnect_paused_source(record, old, client)


def test_replacement_refuses_explicit_mac_that_cannot_survive_overlap():
    with pytest.raises(HostedMigrationStateError, match="explicit container MAC"):
        replacement_config(_old(explicit_mac="02:42:ac:11:00:99"), image="sha256:" + "b" * 64,
                           environment=["NEW=1"], operation="proof")


def test_replacement_accepts_captured_docker_null_binds_shape():
    """Regression: Docker's real null Binds field must not crash EC2 promotion setup."""
    old = _old()
    old.attrs["HostConfig"]["Binds"] = None
    old.attrs["Config"]["Volumes"] = {}
    old.attrs["Mounts"] = []
    config = replacement_config(old, image="sha256:" + "b" * 64,
                                environment=["NEW=1"], operation="proof")
    assert config["HostConfig"]["Binds"] is None


def test_ec2_target_create_is_after_durable_paused_source_disconnect():
    """Regression: aliases must be released without booting the old image."""
    source = inspect.getsource(migrate_hosted)
    assert source.index('transition(record, "backup_verified"') < source.index('transition(record, "network_disconnect_intent"')
    assert source.index("_disconnect_paused_source") < source.index("create_container_from_config")
    assert "await _docker(old.stop" not in source


def test_helper_pin_is_not_created_until_after_ec2_preflight():
    """Regression: a failing helper preflight must not orphan an unjournalled operation tag."""
    source = inspect.getsource(migrate_hosted)
    assert source.index("await _finish(preflight_helper())") < source.index("helper_image_pin = await _pin_helper_image")


def test_pending_operation_pins_exact_helper_digest_before_journal_admission():
    """Regression: deploy tag pruning must not make a pending restore helper dangling."""
    digest = "sha256:" + "f" * 64
    image = SimpleNamespace(id=digest, tags=[])

    def tag(repository, operation):
        image.tags.append(f"{repository}:{operation}")

    image.tag = tag

    class Images:
        def get(self, identity):
            assert identity in {digest, "matrx-migration-helper:op-123"}
            return image

    pin = asyncio.run(_pin_helper_image(SimpleNamespace(images=Images()), digest, "op-123"))
    assert pin == "matrx-migration-helper:op-123"
    assert image.tags == [pin]
