# Roaming Panda Mesh Provisioner

A desktop CLI tool for provisioning ESP-BLE-MESH devices (Roaming Panda smart lights) using BlueZ's bluetooth-meshd daemon on Linux.

## Features

- Scan for unprovisioned BLE Mesh devices
- Provision devices into a mesh network
- Send Generic OnOff commands to provisioned devices
- Automatic detection of Roaming Panda devices (UUID prefix `0xDDDD`)
- Persistent network configuration

## Requirements

### System Requirements

- **Linux** with BlueZ 5.47 or later
- `bluetooth-meshd` daemon (part of BlueZ)
- D-Bus system bus access
- Python 3.10+

### Installing BlueZ bluetooth-meshd

On Ubuntu/Debian:

```bash
# Install BlueZ (usually pre-installed)
sudo apt install bluez

# Check version (needs 5.47+)
bluetoothctl --version

# Enable and start bluetooth-meshd
sudo systemctl enable bluetooth-mesh
sudo systemctl start bluetooth-mesh
```

On Arch Linux:

```bash
sudo pacman -S bluez bluez-utils
sudo systemctl enable bluetooth-mesh
sudo systemctl start bluetooth-mesh
```

### Verify bluetooth-meshd is Running

```bash
# Check service status
sudo systemctl status bluetooth-mesh

# Check D-Bus interface is available
busctl introspect org.bluez.mesh /org/bluez/mesh
```

## Installation

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Make script executable
chmod +x mesh_provisioner.py
```

## Usage

### Scan for Devices

Scan for unprovisioned Roaming Panda devices:

```bash
./mesh_provisioner.py scan
```

Scan for all BLE Mesh devices:

```bash
./mesh_provisioner.py scan --all
```

Specify scan duration (default 10 seconds):

```bash
./mesh_provisioner.py scan --duration 30
```

### Provision a Device

Provision a device using its UUID from the scan:

```bash
./mesh_provisioner.py provision DDDD1234567890ABCDEF0123456789AB
```

Provision with a friendly name:

```bash
./mesh_provisioner.py provision DDDD1234567890ABCDEF0123456789AB --name "Kitchen Light"
```

### Control Devices

Turn on a device:

```bash
./mesh_provisioner.py onoff 0x0002 on
```

Turn off a device:

```bash
./mesh_provisioner.py onoff 0x0002 off
```

Query device state:

```bash
./mesh_provisioner.py onoff 0x0002 get
```

### List Provisioned Nodes

```bash
./mesh_provisioner.py list
```

### Options

```
-c, --config PATH    Config file path (default: ~/.config/mesh-provisioner/mesh_network.json)
-v, --verbose        Enable verbose logging
```

## Network Configuration

The tool stores mesh network configuration in `~/.config/mesh-provisioner/mesh_network.json`, including:

- Network key and application key
- Key indices
- IV index
- Provisioned nodes with their addresses

To reset the network, delete this file:

```bash
rm ~/.config/mesh-provisioner/mesh_network.json
```

## Roaming Panda Device Details

### Device Identification

- UUID prefix: `0xDDDD`
- Company ID: `0x02E5` (Espressif)
- OOB authentication: None (no out-of-band)

### Device Elements

Each Roaming Panda light has two elements:

| Element | LED Ring | Models |
|---------|----------|--------|
| 0 | WWA (Warm/Cool/Amber) | Generic OnOff, Light CTL |
| 1 | RGB | Generic OnOff, Light HSL |

### Supported Models

- **Generic OnOff Server** (0x1000) - On/off control
- **Generic Level Server** (0x1002) - Brightness via level
- **Light Lightness Server** (0x1300) - Brightness via lightness
- **Light CTL Server** (0x1303) - Color temperature (Element 0)
- **Light HSL Server** (0x1307) - Hue/Saturation/Lightness (Element 1)
- **Vendor Model** (0x02E5:0x0001) - Light mode selection

## Troubleshooting

### "Cannot connect to D-Bus"

Ensure bluetooth-meshd is running:

```bash
sudo systemctl status bluetooth-mesh
```

If not installed:

```bash
# On Ubuntu/Debian
sudo apt install bluez

# Enable the mesh daemon
sudo systemctl enable bluetooth-mesh
sudo systemctl start bluetooth-mesh
```

### "Permission denied"

The tool needs access to the system D-Bus. Options:

1. Run as root (not recommended):
   ```bash
   sudo ./mesh_provisioner.py scan
   ```

2. Add user to bluetooth group:
   ```bash
   sudo usermod -aG bluetooth $USER
   # Log out and back in
   ```

3. Configure D-Bus permissions (recommended):
   ```bash
   sudo cp mesh-provisioner-dbus.conf /etc/dbus-1/system.d/
   sudo systemctl restart dbus
   ```

### "No devices found"

1. Ensure the device is in unprovisioned mode (may need factory reset)
2. Check Bluetooth is enabled: `bluetoothctl show`
3. Try scanning for longer: `./mesh_provisioner.py scan -d 30`
4. Check device is powered on and in range

### "Provisioning failed"

1. Device may already be provisioned - factory reset it
2. Check bluetooth-meshd logs: `journalctl -u bluetooth-mesh -f`
3. Try moving device closer to the computer

## macOS Support (Future Work)

This tool currently requires Linux with BlueZ. macOS support would require:

- CoreBluetooth framework integration
- Custom mesh protocol implementation (no macOS equivalent to bluetooth-meshd)
- Alternatively, using a Nordic nRF52 USB dongle with serial interface

See the Linear ticket for discussion of macOS approaches.

## Related Projects

- [roaming_panda_app](https://github.com/roaming-panda-llc/roaming_panda_app) - Flutter mobile app with BLE Mesh
- [roaming_panda_esp_idf_light](https://github.com/roaming-panda-llc/roaming_panda_esp_idf_light) - ESP32 firmware
- [python-bluetooth-mesh](https://github.com/SilvairGit/python-bluetooth-mesh) - BlueZ Python SDK

## License

MIT License - Copyright (c) 2026 Roaming Panda
