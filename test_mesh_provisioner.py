"""
Tests for the Mesh Provisioner tool.

These tests focus on the data structures and serialization logic.
Integration tests with actual bluetooth-meshd require a Linux system
with BlueZ configured.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from mesh_provisioner import (
    MeshNetwork,
    ProvisionedNode,
    UnprovisionedDevice,
    ROAMING_PANDA_UUID_PREFIX,
)


class DescribeUnprovisionedDevice:
    """Tests for UnprovisionedDevice dataclass."""

    def it_creates_device_with_uuid_and_rssi(self):
        uuid = bytes.fromhex("DDDD1234567890ABCDEF0123456789AB")
        device = UnprovisionedDevice(uuid=uuid, rssi=-45)

        assert device.uuid == uuid
        assert device.rssi == -45
        assert device.oob_info == 0

    def it_formats_uuid_as_uppercase_hex(self):
        uuid = bytes.fromhex("dddd1234567890abcdef0123456789ab")
        device = UnprovisionedDevice(uuid=uuid, rssi=-50)

        assert device.uuid_str == "DDDD1234567890ABCDEF0123456789AB"

    def it_identifies_roaming_panda_device(self):
        rp_uuid = bytes.fromhex("DDDD1234567890ABCDEF0123456789AB")
        other_uuid = bytes.fromhex("ABCD1234567890ABCDEF0123456789AB")

        rp_device = UnprovisionedDevice(uuid=rp_uuid, rssi=-40)
        other_device = UnprovisionedDevice(uuid=other_uuid, rssi=-40)

        assert rp_device.is_roaming_panda is True
        assert other_device.is_roaming_panda is False

    def it_has_string_representation(self):
        uuid = bytes.fromhex("DDDD1234567890ABCDEF0123456789AB")
        device = UnprovisionedDevice(uuid=uuid, rssi=-55)

        str_repr = str(device)

        assert "DDDD1234567890ABCDEF0123456789AB" in str_repr
        assert "-55 dBm" in str_repr
        assert "[Roaming Panda]" in str_repr


class DescribeProvisionedNode:
    """Tests for ProvisionedNode dataclass."""

    def it_creates_node_with_required_fields(self):
        uuid = bytes.fromhex("DDDD1234567890ABCDEF0123456789AB")
        node = ProvisionedNode(uuid=uuid, unicast_address=0x0002)

        assert node.uuid == uuid
        assert node.unicast_address == 0x0002
        assert node.element_count == 1
        assert node.name == ""

    def it_creates_node_with_optional_fields(self):
        uuid = bytes.fromhex("DDDD1234567890ABCDEF0123456789AB")
        node = ProvisionedNode(
            uuid=uuid,
            unicast_address=0x0005,
            element_count=2,
            name="Kitchen Light",
        )

        assert node.element_count == 2
        assert node.name == "Kitchen Light"

    def it_has_string_representation(self):
        uuid = bytes.fromhex("DDDD1234567890ABCDEF0123456789AB")
        node = ProvisionedNode(
            uuid=uuid,
            unicast_address=0x0002,
            name="Test Light",
        )

        str_repr = str(node)

        assert "0x0002" in str_repr
        assert "Test Light" in str_repr
        assert "DDDD1234567890ABCDEF0123456789AB" in str_repr


class DescribeMeshNetwork:
    """Tests for MeshNetwork dataclass."""

    def it_creates_network_with_default_values(self):
        network = MeshNetwork()

        assert len(network.network_key) == 16
        assert len(network.app_key) == 16
        assert network.net_key_index == 0
        assert network.app_key_index == 0
        assert network.iv_index == 0
        assert network.next_unicast == 0x0002
        assert network.nodes == {}

    def it_allocates_addresses_sequentially(self):
        network = MeshNetwork()

        addr1 = network.allocate_address(1)
        addr2 = network.allocate_address(2)
        addr3 = network.allocate_address(1)

        assert addr1 == 0x0002
        assert addr2 == 0x0003  # 1 element
        assert addr3 == 0x0005  # 2 elements from previous

    def it_serializes_to_dict(self):
        network = MeshNetwork(
            network_key=bytes.fromhex("00112233445566778899AABBCCDDEEFF"),
            app_key=bytes.fromhex("FFEEDDCCBBAA99887766554433221100"),
            net_key_index=1,
            app_key_index=2,
            iv_index=5,
            next_unicast=0x0010,
        )
        node = ProvisionedNode(
            uuid=bytes.fromhex("DDDD1234567890ABCDEF0123456789AB"),
            unicast_address=0x0002,
            element_count=2,
            name="Test",
        )
        network.nodes["DDDD1234567890ABCDEF0123456789AB"] = node

        data = network.to_dict()

        assert data["network_key"] == "00112233445566778899aabbccddeeff"
        assert data["app_key"] == "ffeeddccbbaa99887766554433221100"
        assert data["net_key_index"] == 1
        assert data["app_key_index"] == 2
        assert data["iv_index"] == 5
        assert data["next_unicast"] == 0x0010
        assert "DDDD1234567890ABCDEF0123456789AB" in data["nodes"]

    def it_deserializes_from_dict(self):
        data = {
            "network_key": "00112233445566778899aabbccddeeff",
            "app_key": "ffeeddccbbaa99887766554433221100",
            "net_key_index": 1,
            "app_key_index": 2,
            "iv_index": 5,
            "next_unicast": 16,
            "nodes": {
                "DDDD1234567890ABCDEF0123456789AB": {
                    "uuid": "dddd1234567890abcdef0123456789ab",
                    "unicast_address": 2,
                    "element_count": 2,
                    "name": "Test",
                }
            },
        }

        network = MeshNetwork.from_dict(data)

        assert network.network_key == bytes.fromhex("00112233445566778899aabbccddeeff")
        assert network.app_key == bytes.fromhex("ffeeddccbbaa99887766554433221100")
        assert network.net_key_index == 1
        assert network.app_key_index == 2
        assert network.iv_index == 5
        assert network.next_unicast == 16
        assert len(network.nodes) == 1
        assert network.nodes["DDDD1234567890ABCDEF0123456789AB"].name == "Test"

    def it_saves_and_loads_from_file(self):
        network = MeshNetwork()
        node = ProvisionedNode(
            uuid=bytes.fromhex("DDDD1234567890ABCDEF0123456789AB"),
            unicast_address=0x0002,
            name="Saved Node",
        )
        network.nodes["DDDD1234567890ABCDEF0123456789AB"] = node

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test_config.json"

            network.save(path)
            loaded = MeshNetwork.load(path)

            assert loaded.network_key == network.network_key
            assert loaded.app_key == network.app_key
            assert len(loaded.nodes) == 1
            assert loaded.nodes["DDDD1234567890ABCDEF0123456789AB"].name == "Saved Node"

    def it_creates_new_network_if_file_missing(self):
        path = Path("/nonexistent/path/config.json")

        network = MeshNetwork.load(path)

        assert len(network.network_key) == 16
        assert network.nodes == {}


class DescribeRoamingPandaPrefix:
    """Tests for Roaming Panda UUID prefix detection."""

    def it_defines_correct_prefix(self):
        assert ROAMING_PANDA_UUID_PREFIX == bytes([0xDD, 0xDD])

    def it_matches_roaming_panda_uuids(self):
        rp_uuid = bytes.fromhex("DDDD0000000000000000000000000000")

        assert rp_uuid[:2] == ROAMING_PANDA_UUID_PREFIX

    def it_does_not_match_other_uuids(self):
        other_uuid = bytes.fromhex("ABCD0000000000000000000000000000")

        assert other_uuid[:2] != ROAMING_PANDA_UUID_PREFIX


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
