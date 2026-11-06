#!/usr/bin/env python3
"""
Roaming Panda BLE Mesh Provisioner

A desktop CLI tool for provisioning ESP-BLE-MESH devices using BlueZ's
bluetooth-meshd daemon on Linux.

This tool enables:
- Scanning for unprovisioned BLE Mesh devices
- Provisioning devices into a mesh network
- Sending Generic OnOff commands to provisioned devices

Requirements:
- Linux with BlueZ 5.47+ (bluetooth-meshd support)
- bluetooth-meshd daemon running
- D-Bus system bus access
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import struct
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from dbus_fast import BusType, Message, MessageType
    from dbus_fast.aio import MessageBus
except ImportError:
    print("Error: dbus-fast package required. Install with: pip install dbus-fast")
    sys.exit(1)

# BlueZ Mesh D-Bus constants
BLUEZ_MESH_SERVICE = "org.bluez.mesh"
BLUEZ_MESH_PATH = "/org/bluez/mesh"
MESH_NETWORK_IFACE = "org.bluez.mesh.Network1"
MESH_NODE_IFACE = "org.bluez.mesh.Node1"
MESH_MANAGEMENT_IFACE = "org.bluez.mesh.Management1"
MESH_ELEMENT_IFACE = "org.bluez.mesh.Element1"
MESH_PROVISIONER_IFACE = "org.bluez.mesh.Provisioner1"
MESH_APPLICATION_IFACE = "org.bluez.mesh.Application1"

# Roaming Panda device constants
ROAMING_PANDA_UUID_PREFIX = bytes([0xDD, 0xDD])
ESPRESSIF_COMPANY_ID = 0x02E5

# Mesh constants
GENERIC_ONOFF_GET = 0x8201
GENERIC_ONOFF_SET = 0x8202
GENERIC_ONOFF_SET_UNACK = 0x8203
GENERIC_ONOFF_STATUS = 0x8204

# Default paths
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "mesh-provisioner"
DEFAULT_MESH_CONFIG = DEFAULT_CONFIG_DIR / "mesh_network.json"

# Logger setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class UnprovisionedDevice:
    """Represents an unprovisioned BLE Mesh device."""

    uuid: bytes
    rssi: int
    oob_info: int = 0

    @property
    def uuid_str(self) -> str:
        """Return UUID as hex string."""
        return self.uuid.hex().upper()

    @property
    def is_roaming_panda(self) -> bool:
        """Check if device is a Roaming Panda light."""
        return self.uuid[:2] == ROAMING_PANDA_UUID_PREFIX

    def __str__(self) -> str:
        rp_marker = " [Roaming Panda]" if self.is_roaming_panda else ""
        return f"UUID: {self.uuid_str}, RSSI: {self.rssi} dBm{rp_marker}"


@dataclass
class ProvisionedNode:
    """Represents a provisioned mesh node."""

    uuid: bytes
    unicast_address: int
    element_count: int = 1
    name: str = ""

    @property
    def uuid_str(self) -> str:
        """Return UUID as hex string."""
        return self.uuid.hex().upper()

    def __str__(self) -> str:
        name_part = f" ({self.name})" if self.name else ""
        return (
            f"Address: 0x{self.unicast_address:04X}{name_part}, UUID: {self.uuid_str}"
        )


@dataclass
class MeshNetwork:
    """Mesh network configuration."""

    network_key: bytes = field(default_factory=lambda: os.urandom(16))
    app_key: bytes = field(default_factory=lambda: os.urandom(16))
    net_key_index: int = 0
    app_key_index: int = 0
    iv_index: int = 0
    next_unicast: int = 0x0002  # 0x0001 is provisioner
    nodes: dict[str, ProvisionedNode] = field(default_factory=dict)

    def allocate_address(self, element_count: int = 1) -> int:
        """Allocate unicast address for a new node."""
        addr = self.next_unicast
        self.next_unicast += element_count
        return addr

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "network_key": self.network_key.hex(),
            "app_key": self.app_key.hex(),
            "net_key_index": self.net_key_index,
            "app_key_index": self.app_key_index,
            "iv_index": self.iv_index,
            "next_unicast": self.next_unicast,
            "nodes": {
                k: {
                    "uuid": v.uuid.hex(),
                    "unicast_address": v.unicast_address,
                    "element_count": v.element_count,
                    "name": v.name,
                }
                for k, v in self.nodes.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> MeshNetwork:
        """Create from dictionary."""
        nodes = {}
        for uuid_str, node_data in data.get("nodes", {}).items():
            nodes[uuid_str] = ProvisionedNode(
                uuid=bytes.fromhex(node_data["uuid"]),
                unicast_address=node_data["unicast_address"],
                element_count=node_data.get("element_count", 1),
                name=node_data.get("name", ""),
            )

        return cls(
            network_key=bytes.fromhex(data["network_key"]),
            app_key=bytes.fromhex(data["app_key"]),
            net_key_index=data.get("net_key_index", 0),
            app_key_index=data.get("app_key_index", 0),
            iv_index=data.get("iv_index", 0),
            next_unicast=data.get("next_unicast", 0x0002),
            nodes=nodes,
        )

    def save(self, path: Path) -> None:
        """Save network config to file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        logger.info(f"Saved network config to {path}")

    @classmethod
    def load(cls, path: Path) -> MeshNetwork:
        """Load network config from file."""
        if path.exists():
            with open(path) as f:
                return cls.from_dict(json.load(f))
        return cls()


class MeshProvisionerApp:
    """
    D-Bus application that implements the mesh provisioner interfaces.

    This class registers with bluetooth-meshd and handles provisioning
    callbacks.
    """

    def __init__(self, bus: MessageBus, network: MeshNetwork):
        self.bus = bus
        self.network = network
        self.app_path = f"/com/roamingpanda/mesh/{uuid.uuid4().hex[:8]}"
        self.element_path = f"{self.app_path}/element0"
        self.discovered_devices: dict[str, UnprovisionedDevice] = {}
        self.provisioning_complete = asyncio.Event()
        self.scan_complete = asyncio.Event()
        self._node_path: str | None = None
        self._token: int | None = None

    async def register(self) -> None:
        """Register application interfaces on D-Bus."""
        # Export application object
        self.bus.export(self.app_path, self)
        self.bus.export(self.element_path, self)
        logger.debug(f"Exported app at {self.app_path}")

    async def unregister(self) -> None:
        """Unregister from D-Bus."""
        try:
            self.bus.unexport(self.app_path)
            self.bus.unexport(self.element_path)
        except Exception:
            pass

    # =========================================================================
    # org.bluez.mesh.Application1 interface
    # =========================================================================

    def JoinComplete(self, token: int) -> None:
        """Called when join/attach completes successfully."""
        logger.info(f"Join complete, token: {token}")
        self._token = token

    def JoinFailed(self, reason: str) -> None:
        """Called when join/attach fails."""
        logger.error(f"Join failed: {reason}")

    @property
    def CompanyID(self) -> int:
        """Return company ID (Espressif for Roaming Panda)."""
        return ESPRESSIF_COMPANY_ID

    @property
    def ProductID(self) -> int:
        """Return product ID."""
        return 0x0001

    @property
    def VersionID(self) -> int:
        """Return version ID."""
        return 0x0001

    @property
    def CRPL(self) -> int:
        """Return replay protection list size."""
        return 32768

    # =========================================================================
    # org.bluez.mesh.Provisioner1 interface
    # =========================================================================

    def ScanResult(self, rssi: int, data: bytes, options: dict[str, Any]) -> None:
        """Called when an unprovisioned device is discovered."""
        if len(data) < 16:
            logger.warning(f"Invalid scan result data length: {len(data)}")
            return

        device_uuid = bytes(data[:16])
        oob_info = struct.unpack("<H", bytes(data[16:18]))[0] if len(data) >= 18 else 0

        device = UnprovisionedDevice(
            uuid=device_uuid,
            rssi=rssi,
            oob_info=oob_info,
        )

        uuid_str = device.uuid_str
        if uuid_str not in self.discovered_devices:
            self.discovered_devices[uuid_str] = device
            logger.info(f"Discovered: {device}")

    def RequestProvData(self, count: int) -> tuple[int, int]:
        """
        Called during provisioning to get network index and unicast address.

        Args:
            count: Number of elements on the device

        Returns:
            Tuple of (net_key_index, unicast_address)
        """
        address = self.network.allocate_address(count)
        logger.info(f"Allocating address 0x{address:04X} for {count} element(s)")
        return (self.network.net_key_index, address)

    def AddNodeComplete(
        self,
        uuid: bytes,
        unicast: int,
        count: int,
    ) -> None:
        """Called when a node is successfully provisioned."""
        device_uuid = bytes(uuid)
        uuid_str = device_uuid.hex().upper()

        node = ProvisionedNode(
            uuid=device_uuid,
            unicast_address=unicast,
            element_count=count,
        )
        self.network.nodes[uuid_str] = node

        logger.info(f"Provisioning complete: {node}")
        self.provisioning_complete.set()

    def AddNodeFailed(self, uuid: bytes, reason: str) -> None:
        """Called when provisioning fails."""
        uuid_str = bytes(uuid).hex().upper()
        logger.error(f"Provisioning failed for {uuid_str}: {reason}")
        self.provisioning_complete.set()

    # =========================================================================
    # org.bluez.mesh.Element1 interface (element 0)
    # =========================================================================

    @property
    def Index(self) -> int:
        """Element index."""
        return 0

    @property
    def Models(self) -> list[tuple[int, dict]]:
        """
        Return list of models on this element.

        Format: [(model_id, options), ...]
        For SIG models, model_id is 16-bit.
        For vendor models, model_id is 32-bit (company_id << 16 | model_id).
        """
        return [
            # Configuration Client (required for provisioner)
            (0x0001, {}),
            # Generic OnOff Client
            (0x1001, {}),
            # Generic Level Client
            (0x1003, {}),
            # Light Lightness Client
            (0x1302, {}),
            # Light CTL Client
            (0x1305, {}),
            # Light HSL Client
            (0x1309, {}),
        ]

    def MessageReceived(
        self,
        source: int,
        key_index: int,
        destination: int,
        data: bytes,
    ) -> None:
        """Handle incoming mesh message."""
        if len(data) < 1:
            return

        # Parse opcode
        opcode = data[0]
        if opcode & 0x80:
            if len(data) < 2:
                return
            opcode = (data[0] << 8) | data[1]
            payload = data[2:]
        else:
            payload = data[1:]

        logger.debug(
            f"Message from 0x{source:04X}: opcode=0x{opcode:04X}, "
            f"payload={payload.hex()}"
        )

        if opcode == GENERIC_ONOFF_STATUS:
            state = payload[0] if payload else 0
            logger.info(f"OnOff Status from 0x{source:04X}: {'ON' if state else 'OFF'}")

    def DevKeyMessageReceived(
        self,
        source: int,
        remote: bool,
        net_index: int,
        data: bytes,
    ) -> None:
        """Handle incoming device key encrypted message."""
        logger.debug(f"DevKey message from 0x{source:04X}: {data.hex()}")

    def UpdateModelConfiguration(
        self,
        model_id: int,
        config: dict[str, Any],
    ) -> None:
        """Handle model configuration update."""
        logger.debug(f"Model 0x{model_id:04X} config updated: {config}")


class MeshProvisioner:
    """
    Main provisioner class that coordinates mesh operations.
    """

    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or DEFAULT_MESH_CONFIG
        self.network = MeshNetwork.load(self.config_path)
        self.bus: MessageBus | None = None
        self.app: MeshProvisionerApp | None = None
        self._node_path: str | None = None

    async def connect(self) -> None:
        """Connect to D-Bus and register application."""
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.app = MeshProvisionerApp(self.bus, self.network)
        await self.app.register()
        logger.info("Connected to D-Bus")

    async def disconnect(self) -> None:
        """Disconnect from D-Bus."""
        if self.app:
            await self.app.unregister()
        if self.bus:
            self.bus.disconnect()
        logger.info("Disconnected from D-Bus")

    async def create_network(self) -> None:
        """
        Create a new mesh network and register as provisioner.

        This creates the network keys and registers our application
        as the provisioner node.
        """
        if not self.bus or not self.app:
            raise RuntimeError("Not connected")

        # Call CreateNetwork on org.bluez.mesh.Network1
        msg = Message(
            destination=BLUEZ_MESH_SERVICE,
            path=BLUEZ_MESH_PATH,
            interface=MESH_NETWORK_IFACE,
            member="CreateNetwork",
            signature="oa{sv}",
            body=[
                self.app.app_path,
                {
                    "IvIndex": ("u", self.network.iv_index),
                },
            ],
        )

        reply = await self.bus.call(msg)

        if reply.message_type == MessageType.ERROR:
            if "AlreadyExists" in str(reply.error_name):
                logger.info("Network already exists, attaching instead")
                await self.attach_network()
                return
            raise RuntimeError(f"CreateNetwork failed: {reply.body}")

        logger.info("Network created successfully")

    async def attach_network(self) -> None:
        """Attach to an existing mesh network."""
        if not self.bus or not self.app:
            raise RuntimeError("Not connected")

        # For attach, we need to provide our UUID token
        # This is simplified - a real implementation would store the token
        msg = Message(
            destination=BLUEZ_MESH_SERVICE,
            path=BLUEZ_MESH_PATH,
            interface=MESH_NETWORK_IFACE,
            member="Attach",
            signature="ot",
            body=[
                self.app.app_path,
                self.app._token or 0,
            ],
        )

        reply = await self.bus.call(msg)

        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"Attach failed: {reply.body}")

        self._node_path = reply.body[0]
        logger.info(f"Attached to network, node path: {self._node_path}")

    async def scan(
        self,
        duration: int = 10,
        roaming_panda_only: bool = True,
    ) -> list[UnprovisionedDevice]:
        """
        Scan for unprovisioned devices.

        Args:
            duration: Scan duration in seconds
            roaming_panda_only: Only return Roaming Panda devices

        Returns:
            List of discovered devices
        """
        if not self.bus or not self.app or not self._node_path:
            raise RuntimeError("Not connected or no network")

        self.app.discovered_devices.clear()

        # Start unprovisioned scan
        options: dict[str, Any] = {"Seconds": ("q", duration)}
        if roaming_panda_only:
            # Filter by UUID prefix
            options["UUID"] = ("ay", list(ROAMING_PANDA_UUID_PREFIX))

        msg = Message(
            destination=BLUEZ_MESH_SERVICE,
            path=self._node_path,
            interface=MESH_MANAGEMENT_IFACE,
            member="UnprovisionedScan",
            signature="a{sv}",
            body=[options],
        )

        reply = await self.bus.call(msg)

        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"Scan failed: {reply.body}")

        logger.info(f"Scanning for {duration} seconds...")
        await asyncio.sleep(duration + 1)

        # Stop scan
        msg = Message(
            destination=BLUEZ_MESH_SERVICE,
            path=self._node_path,
            interface=MESH_MANAGEMENT_IFACE,
            member="UnprovisionedScanCancel",
        )
        await self.bus.call(msg)

        devices = list(self.app.discovered_devices.values())
        if roaming_panda_only:
            devices = [d for d in devices if d.is_roaming_panda]

        return devices

    async def provision(
        self,
        device_uuid: str | bytes,
        name: str = "",
    ) -> ProvisionedNode | None:
        """
        Provision a device into the mesh network.

        Args:
            device_uuid: UUID of the device (hex string or bytes)
            name: Optional friendly name for the device

        Returns:
            ProvisionedNode if successful, None otherwise
        """
        if not self.bus or not self.app or not self._node_path:
            raise RuntimeError("Not connected or no network")

        if isinstance(device_uuid, str):
            device_uuid = bytes.fromhex(device_uuid.replace("-", ""))

        uuid_array = list(device_uuid)
        self.app.provisioning_complete.clear()

        # AddNode to start provisioning
        msg = Message(
            destination=BLUEZ_MESH_SERVICE,
            path=self._node_path,
            interface=MESH_MANAGEMENT_IFACE,
            member="AddNode",
            signature="aya{sv}",
            body=[
                uuid_array,
                {},  # Options (empty for no OOB)
            ],
        )

        reply = await self.bus.call(msg)

        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"AddNode failed: {reply.body}")

        logger.info(f"Provisioning started for {device_uuid.hex().upper()}")

        # Wait for provisioning to complete
        try:
            await asyncio.wait_for(
                self.app.provisioning_complete.wait(),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            logger.error("Provisioning timed out")
            return None

        uuid_str = device_uuid.hex().upper()
        node = self.app.network.nodes.get(uuid_str)
        if node and name:
            node.name = name

        # Save updated network config
        self.network.save(self.config_path)

        return node

    async def send_onoff(
        self,
        address: int,
        state: bool,
        acknowledged: bool = True,
    ) -> None:
        """
        Send Generic OnOff Set message.

        Args:
            address: Destination unicast address
            state: True for ON, False for OFF
            acknowledged: Whether to request acknowledgment
        """
        if not self.bus or not self.app or not self._node_path:
            raise RuntimeError("Not connected or no network")

        opcode = GENERIC_ONOFF_SET if acknowledged else GENERIC_ONOFF_SET_UNACK

        # Build message: [opcode_high, opcode_low, on_off, tid]
        tid = random.randint(0, 255)
        payload = bytes(
            [
                (opcode >> 8) & 0xFF,
                opcode & 0xFF,
                1 if state else 0,
                tid,
            ]
        )

        msg = Message(
            destination=BLUEZ_MESH_SERVICE,
            path=self._node_path,
            interface=MESH_NODE_IFACE,
            member="Send",
            signature="qqa{sv}ay",
            body=[
                self.app.element_path,  # Element path
                address,  # Destination
                self.network.app_key_index,  # App key index
                {},  # Options
                list(payload),  # Data
            ],
        )

        reply = await self.bus.call(msg)

        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"Send failed: {reply.body}")

        state_str = "ON" if state else "OFF"
        logger.info(f"Sent OnOff {state_str} to 0x{address:04X}")

    async def get_onoff(self, address: int) -> None:
        """
        Send Generic OnOff Get message.

        Args:
            address: Destination unicast address
        """
        if not self.bus or not self.app or not self._node_path:
            raise RuntimeError("Not connected or no network")

        # Build message: [opcode_high, opcode_low]
        payload = bytes(
            [
                (GENERIC_ONOFF_GET >> 8) & 0xFF,
                GENERIC_ONOFF_GET & 0xFF,
            ]
        )

        msg = Message(
            destination=BLUEZ_MESH_SERVICE,
            path=self._node_path,
            interface=MESH_NODE_IFACE,
            member="Send",
            signature="qqa{sv}ay",
            body=[
                self.app.element_path,
                address,
                self.network.app_key_index,
                {},
                list(payload),
            ],
        )

        reply = await self.bus.call(msg)

        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"Send failed: {reply.body}")

        logger.info(f"Sent OnOff Get to 0x{address:04X}")

    def list_nodes(self) -> list[ProvisionedNode]:
        """Return list of provisioned nodes."""
        return list(self.network.nodes.values())


# =============================================================================
# CLI Commands
# =============================================================================


async def cmd_scan(args: argparse.Namespace) -> int:
    """Scan for unprovisioned devices."""
    provisioner = MeshProvisioner(args.config)

    try:
        await provisioner.connect()
        await provisioner.create_network()

        devices = await provisioner.scan(
            duration=args.duration,
            roaming_panda_only=not args.all_devices,
        )

        if devices:
            print(f"\nFound {len(devices)} device(s):")
            for device in devices:
                print(f"  {device}")
        else:
            print("\nNo devices found")

        return 0
    except Exception as e:
        logger.error(f"Scan failed: {e}")
        return 1
    finally:
        await provisioner.disconnect()


async def cmd_provision(args: argparse.Namespace) -> int:
    """Provision a device."""
    provisioner = MeshProvisioner(args.config)

    try:
        await provisioner.connect()
        await provisioner.create_network()

        node = await provisioner.provision(args.uuid, name=args.name)

        if node:
            print(f"\nProvisioned: {node}")
            return 0
        else:
            print("\nProvisioning failed")
            return 1
    except Exception as e:
        logger.error(f"Provision failed: {e}")
        return 1
    finally:
        await provisioner.disconnect()


async def cmd_onoff(args: argparse.Namespace) -> int:
    """Send OnOff command."""
    provisioner = MeshProvisioner(args.config)

    try:
        await provisioner.connect()
        await provisioner.attach_network()

        address = (
            int(args.address, 16)
            if args.address.startswith("0x")
            else int(args.address)
        )

        if args.state.lower() in ("on", "1", "true"):
            await provisioner.send_onoff(address, True, not args.unacknowledged)
        elif args.state.lower() in ("off", "0", "false"):
            await provisioner.send_onoff(address, False, not args.unacknowledged)
        elif args.state.lower() == "get":
            await provisioner.get_onoff(address)
        else:
            print(f"Invalid state: {args.state}")
            return 1

        return 0
    except Exception as e:
        logger.error(f"Command failed: {e}")
        return 1
    finally:
        await provisioner.disconnect()


async def cmd_list(args: argparse.Namespace) -> int:
    """List provisioned nodes."""
    provisioner = MeshProvisioner(args.config)

    nodes = provisioner.list_nodes()

    if nodes:
        print(f"Provisioned nodes ({len(nodes)}):")
        for node in nodes:
            print(f"  {node}")
    else:
        print("No provisioned nodes")

    return 0


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Roaming Panda BLE Mesh Provisioner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scan for Roaming Panda devices
  %(prog)s scan

  # Scan for all BLE Mesh devices
  %(prog)s scan --all

  # Provision a device
  %(prog)s provision DDDD1234567890ABCDEF --name "Living Room Light"

  # Turn on a device
  %(prog)s onoff 0x0002 on

  # Turn off a device
  %(prog)s onoff 0x0002 off

  # Get device state
  %(prog)s onoff 0x0002 get

  # List provisioned nodes
  %(prog)s list
""",
    )

    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_MESH_CONFIG,
        help=f"Config file path (default: {DEFAULT_MESH_CONFIG})",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Scan command
    scan_parser = subparsers.add_parser("scan", help="Scan for unprovisioned devices")
    scan_parser.add_argument(
        "-d",
        "--duration",
        type=int,
        default=10,
        help="Scan duration in seconds (default: 10)",
    )
    scan_parser.add_argument(
        "-a",
        "--all",
        dest="all_devices",
        action="store_true",
        help="Show all devices, not just Roaming Panda",
    )

    # Provision command
    provision_parser = subparsers.add_parser("provision", help="Provision a device")
    provision_parser.add_argument(
        "uuid",
        help="Device UUID (32 hex characters)",
    )
    provision_parser.add_argument(
        "-n",
        "--name",
        default="",
        help="Friendly name for the device",
    )

    # OnOff command
    onoff_parser = subparsers.add_parser("onoff", help="Send Generic OnOff command")
    onoff_parser.add_argument(
        "address",
        help="Destination address (e.g., 0x0002 or 2)",
    )
    onoff_parser.add_argument(
        "state",
        choices=["on", "off", "get", "1", "0"],
        help="State to set or 'get' to query",
    )
    onoff_parser.add_argument(
        "-u",
        "--unacknowledged",
        action="store_true",
        help="Send unacknowledged message",
    )

    # List command
    subparsers.add_parser("list", help="List provisioned nodes")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Dispatch to command handler
    if args.command == "scan":
        return asyncio.run(cmd_scan(args))
    elif args.command == "provision":
        return asyncio.run(cmd_provision(args))
    elif args.command == "onoff":
        return asyncio.run(cmd_onoff(args))
    elif args.command == "list":
        return asyncio.run(cmd_list(args))

    return 1


if __name__ == "__main__":
    sys.exit(main())
