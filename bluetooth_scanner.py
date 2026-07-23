"""
bluetooth_scanner.py — Bluetooth proximity scanner for FaceAuth 2FA.

Two-layer device discovery strategy
-------------------------------------
1. Windows Registry  — instantly returns ALL paired Bluetooth devices
   (classic BT + BLE) regardless of whether they are currently advertising.
2. BLE Advertisement Scan (bleak) — catches devices that are nearby but
   not yet paired, sorted by RSSI signal strength.

Both sources are merged and de-duplicated (registry devices get priority
for the name, BLE scan fills in real-time RSSI).

RSSI thresholds
---------------
    -50 dBm  : Very close   (~1 m)
    -65 dBm  : Close        (~2–3 m)
    -70 dBm  : Nearby       (~3–5 m)  ← default
    -80 dBm  : Medium range (~5–8 m)
    -90 dBm  : Far          (>8 m)

iOS note: iOS 14+ randomises BLE advertisement MACs for privacy.
Paired iPhones still appear via the registry under their stable hardware MAC.
"""

import asyncio
import logging
import platform
import re
import subprocess
import json
import concurrent.futures
from typing import Optional, Set

logger = logging.getLogger(__name__)

# ── Windows asyncio policy fix ─────────────────────────────────────────────────
# bleak requires SelectorEventLoop on Windows (ProactorEventLoop is the default
# on Python 3.8+ and is incompatible with bleak's BLE backend).
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ── Optional bleak import ──────────────────────────────────────────────────────
try:
    from bleak import BleakScanner                            # type: ignore[import-untyped]
    from bleak.backends.device import BLEDevice               # type: ignore[import-untyped]
    from bleak.backends.scanner import AdvertisementData      # type: ignore[import-untyped]
    BLEAK_AVAILABLE = True
except ImportError:
    BLEAK_AVAILABLE = False
    logger.warning(
        "bleak is not installed. BLE scanning unavailable. "
        "Run: pip install bleak"
    )


# ── Constants ──────────────────────────────────────────────────────────────────

DEFAULT_RSSI_THRESHOLD: int   = -50    # dBm
DEFAULT_SCAN_DURATION:  float =  5.0  # seconds


# ── Windows Registry — paired device list ─────────────────────────────────────

def get_windows_paired_devices() -> list:
    """
    Read ALL paired Bluetooth devices from the Windows registry.

    Works for both classic BT and BLE devices, connected or not.
    Devices appear here as soon as they have ever been paired with the laptop.

    Returns list of dicts:
        [{"name": str, "mac": str, "rssi": None, "source": "paired"}, …]
    """
    if platform.system() != "Windows":
        return []

    devices = []
    try:
        import winreg
        key_path = r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices"
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path)
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(key, i)
                raw = sub.upper().replace("-", "")
                if len(raw) == 12:
                    mac = ":".join(raw[j:j+2] for j in range(0, 12, 2))
                    name = "Unknown Device"
                    try:
                        sk = winreg.OpenKey(key, sub)
                        name_val, _ = winreg.QueryValueEx(sk, "Name")
                        if isinstance(name_val, (bytes, bytearray)):
                            name = name_val.decode("utf-8", errors="replace").rstrip("\x00").strip()
                        elif isinstance(name_val, str):
                            name = name_val.strip()
                    except OSError:
                        pass
                    if name:
                        devices.append({
                            "name":   name,
                            "mac":    mac,
                            "rssi":   None,
                            "source": "paired",
                        })
                i += 1
            except OSError:
                break
    except Exception as exc:
        logger.debug(f"Registry paired-device lookup failed: {exc}")

    return devices


def get_windows_connected_devices() -> Set[str]:
    """
    Use PowerShell to enumerate currently active (connected/OK) Bluetooth
    devices and extract the MAC addresses from their DeviceID strings.

    Returns a set of MAC address strings (upper-case, colon-separated).
    """
    if platform.system() != "Windows":
        return set()

    connected_macs: Set[str] = set()
    try:
        ps_cmd = (
            "Get-PnpDevice -Class Bluetooth | "
            "Select-Object FriendlyName,Status,DeviceID | "
            "ConvertTo-Json -Depth 2"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=12
        )
        if result.returncode == 0 and result.stdout.strip():
            items = json.loads(result.stdout)
            if isinstance(items, dict):
                items = [items]
            for item in items:
                status = (item.get("Status") or "").strip()
                if status != "OK":
                    continue
                dev_id = (item.get("DeviceID") or "")
                # Extract MAC from patterns like:
                #   BTHENUM\DEV_A0FBC52D5981\...
                #   BTHLE\DEV_A0FBC52D5981\...
                m = re.search(r"DEV_([0-9A-F]{12})", dev_id, re.IGNORECASE)
                if m:
                    raw = m.group(1).upper()
                    mac = ":".join(raw[j:j+2] for j in range(0, 12, 2))
                    connected_macs.add(mac)
    except Exception as exc:
        logger.debug(f"PowerShell connected-device check failed: {exc}")

    return connected_macs


# ── BLE advertisement scan ────────────────────────────────────────────────────

async def _async_scan(mac_address: str, rssi_threshold: int, scan_duration: float) -> dict:
    """Async BLE scan for a single known MAC address."""
    target_mac = mac_address.upper().strip()
    best_rssi: Optional[int] = None
    found_event = asyncio.Event()

    def detection_callback(device: "BLEDevice", adv: "AdvertisementData"):
        nonlocal best_rssi
        if device.address.upper() == target_mac:
            rssi = adv.rssi if adv.rssi is not None else getattr(device, "rssi", None)
            if rssi is not None:
                if best_rssi is None or rssi > best_rssi:
                    best_rssi = rssi
                if rssi >= rssi_threshold:
                    found_event.set()

    async with BleakScanner(detection_callback=detection_callback):
        try:
            await asyncio.wait_for(found_event.wait(), timeout=scan_duration)
        except asyncio.TimeoutError:
            pass

    if best_rssi is not None and best_rssi >= rssi_threshold:
        return {"found": True,  "rssi": best_rssi, "mac": target_mac,
                "message": f"Device found (RSSI {best_rssi} dBm)"}
    elif best_rssi is not None:
        return {"found": False, "rssi": best_rssi, "mac": target_mac,
                "message": f"Device too far (RSSI {best_rssi} dBm, threshold {rssi_threshold} dBm)"}
    else:
        return {"found": False, "rssi": None, "mac": target_mac,
                "message": f"Device not detected in {scan_duration}s scan"}


async def _async_discover(scan_duration: float) -> list:
    """Discover ALL nearby BLE-advertising devices."""
    seen: dict = {}

    def cb(device: "BLEDevice", adv: "AdvertisementData"):
        mac  = device.address.upper()
        name = (device.name
                or (adv.local_name if hasattr(adv, "local_name") else None)
                or "Unknown Device")
        rssi = adv.rssi if adv.rssi is not None else -100
        if mac not in seen or rssi > seen[mac]["rssi"]:
            seen[mac] = {"name": name, "mac": mac, "rssi": rssi, "source": "ble"}

    async with BleakScanner(detection_callback=cb):
        await asyncio.sleep(scan_duration)

    return list(seen.values())


def _run_async(coro):
    """
    Run an async coroutine from a synchronous Flask context (Windows-safe).

    Runs the coroutine in a dedicated daemon thread with its own SelectorEventLoop
    so that:
      - Multiple sequential Flask requests never share a closed event loop.
      - The ProactorEventLoop used by the main thread doesn't interfere with bleak.
    """
    result_holder = [None]
    exception_holder = [None]

    def thread_target():
        # Each thread gets a brand-new SelectorEventLoop (required for bleak on Windows)
        loop = asyncio.new_event_loop()
        if platform.system() == "Windows":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.set_event_loop(loop)
        try:
            result_holder[0] = loop.run_until_complete(coro)
        except Exception as exc:
            exception_holder[0] = exc
        finally:
            loop.close()

    t = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = t.submit(thread_target)
    future.result()  # block until done, propagates exceptions
    t.shutdown(wait=False)

    if exception_holder[0] is not None:
        raise exception_holder[0]
    return result_holder[0]


# ── Public API ─────────────────────────────────────────────────────────────────

def scan_nearby_devices(scan_duration: float = 8.0) -> dict:
    """
    Combined device discovery:
      1. Reads paired devices from Windows registry  (instant, always works)
      2. Runs a BLE advertisement scan               (finds advertising devices)
      3. Merges both, marking which are connected

    Returns:
        {
            "success": bool,
            "devices": [
                {
                  "name":      str,
                  "mac":       str,
                  "rssi":      int | null,
                  "source":    "paired" | "ble",
                  "connected": bool,
                },
                …
            ],
            "message": str,
        }
    """
    # Step 1: Paired devices from registry (instant)
    paired = get_windows_paired_devices()
    paired_by_mac = {d["mac"]: d for d in paired}

    # Step 2: Connected status from PowerShell (fast)
    connected_macs = get_windows_connected_devices()

    # Step 3: BLE advertisement scan (may find additional devices)
    ble_devices = []
    if BLEAK_AVAILABLE:
        try:
            ble_devices = _run_async(_async_discover(scan_duration))
        except Exception as exc:
            logger.exception("BLE discover error")

    # Step 4: Merge — BLE scan updates RSSI for known paired devices
    merged: dict = {}

    # Start with paired registry devices
    for d in paired:
        merged[d["mac"]] = {
            "name":      d["name"],
            "mac":       d["mac"],
            "rssi":      None,
            "source":    "paired",
            "connected": d["mac"] in connected_macs,
        }

    # Overlay BLE results (update RSSI if device already known, or add new)
    for d in ble_devices:
        mac = d["mac"]
        if mac in merged:
            merged[mac]["rssi"]   = d["rssi"]          # real-time RSSI
            merged[mac]["source"] = "paired+ble"
        else:
            merged[mac] = {
                "name":      d["name"],
                "mac":       mac,
                "rssi":      d["rssi"],
                "source":    "ble",
                "connected": mac in connected_macs,
            }

    # Sort: connected first, then by RSSI (None treated as -999 for sorting)
    result = sorted(
        merged.values(),
        key=lambda x: (not x["connected"], -(x["rssi"] or -999)),
    )

    total = len(result)
    return {
        "success": True,
        "devices": result,
        "message": f"Found {total} device{'s' if total != 1 else ''} ({len(paired)} paired, {len(ble_devices)} via BLE scan)",
    }


def scan_for_device(
    mac_address: str,
    rssi_threshold: int = DEFAULT_RSSI_THRESHOLD,
    scan_duration:  float = DEFAULT_SCAN_DURATION,
) -> dict:
    """
    Proximity check for a single MAC address.

    Attempts to locate the device using BLE:
      1. Resolves the MAC address to its friendly name from paired registry devices.
      2. Performs a BLE advertisement scan to find nearby broadcasting devices.
      3. Matches the target by MAC address, registry-mapped name, or BLE advertised name.
      4. If detected in the BLE scan and RSSI is >= threshold, returns found=True.
      5. Fallback: if BLE scan doesn't find it, check if it's connected via Windows PnP status.
    """
    if not BLEAK_AVAILABLE:
        return {
            "found": False, "rssi": None,
            "mac": mac_address.upper().strip(),
            "message": "bleak library not installed. Run: pip install bleak",
            "bleak_available": False,
        }

    if not mac_address or len(mac_address.strip()) < 17:
        return {
            "found": False, "rssi": None,
            "mac": mac_address,
            "message": "Invalid or empty MAC address",
            "bleak_available": True,
        }

    target_mac = mac_address.upper().strip()

    try:
        # 1. Resolve friendly name from paired devices registry
        paired_devices = get_windows_paired_devices()
        paired_by_mac = {dev["mac"].upper(): dev["name"] for dev in paired_devices}
        friendly_name = paired_by_mac.get(target_mac)

        # 2. Run a general BLE scan to discover all advertising devices
        logger.info(f"Scanning for device {target_mac} (Name: {friendly_name}) over BLE...")
        ble_devices = _run_async(_async_discover(scan_duration))

        # 3. Search for a matching device in the BLE scan results
        matched_device = None
        for dev in ble_devices:
            dev_mac = dev["mac"].upper()
            
            # Match by MAC address
            if dev_mac == target_mac:
                matched_device = dev
                break
                
            # Match by friendly name if available
            if friendly_name:
                # A. Match by looking up the BLE device's MAC in the registry to get its paired name
                reg_name = paired_by_mac.get(dev_mac)
                if reg_name and reg_name.lower() == friendly_name.lower():
                    matched_device = dev
                    break
                
                # B. Match by BLE advertisement name
                dev_name = dev["name"]
                if dev_name and dev_name != "Unknown Device" and dev_name.lower() == friendly_name.lower():
                    matched_device = dev
                    break

        if matched_device:
            rssi = matched_device["rssi"]
            if rssi is not None:
                # We have a real RSSI reading — apply the threshold
                if rssi >= rssi_threshold:
                    return {
                        "found": True,
                        "rssi": rssi,
                        "mac": target_mac,
                        "message": f"Device in range (Name: {friendly_name or matched_device['name']}, RSSI: {rssi} dBm, threshold: {rssi_threshold} dBm)",
                        "bleak_available": True
                    }
                else:
                    return {
                        "found": False,
                        "rssi": rssi,
                        "mac": target_mac,
                        "message": f"Device too far (Name: {friendly_name or matched_device['name']}, RSSI: {rssi} dBm, threshold: {rssi_threshold} dBm)",
                        "bleak_available": True
                    }
            # rssi is None for this BLE match — fall through to PnP connection check

        # Fallback: if BLE scan didn't find it (or RSSI was None), check if
        # the device is actively connected via Windows PnP/BT stack
        connected = get_windows_connected_devices()
        if target_mac in connected:
            return {
                "found": True,
                "rssi": None,
                "mac": target_mac,
                "message": "Device is connected (detected via Windows connection status)",
                "bleak_available": True
            }

        return {
            "found": False,
            "rssi": None,
            "mac": target_mac,
            "message": f"Device not detected in {scan_duration}s BLE scan (Connection check also failed)",
            "bleak_available": True
        }

    except Exception as exc:
        logger.exception("BLE scan error")
        return {
            "found": False, "rssi": None,
            "mac": target_mac,
            "message": f"Scan error: {exc}",
            "bleak_available": True,
        }


def validate_mac_address(mac: str) -> bool:
    """Return True if mac is a valid Bluetooth MAC address (XX:XX:XX:XX:XX:XX)."""
    return bool(re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac.strip()))


# ── CLI test ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) >= 2 and sys.argv[1] == "discover":
        duration = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
        print(f"\nDiscovering all Bluetooth devices (scan {duration}s)…\n")
        res = scan_nearby_devices(scan_duration=duration)
        for d in res["devices"]:
            conn  = "[CONNECTED]" if d["connected"] else "[paired]   "
            rssi  = f"{d['rssi']} dBm" if d['rssi'] is not None else "—"
            print(f"  {conn}  {d['name']:<30}  {d['mac']}  RSSI: {rssi}  [{d['source']}]")
        print(f"\n{res['message']}")
    else:
        mac       = sys.argv[1] if len(sys.argv) > 1 else "AA:BB:CC:DD:EE:FF"
        threshold = int(sys.argv[2])   if len(sys.argv) > 2 else DEFAULT_RSSI_THRESHOLD
        duration  = float(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_SCAN_DURATION
        print(f"\nScanning for {mac} (threshold {threshold} dBm, duration {duration}s)…\n")
        result = scan_for_device(mac, threshold, duration)
        print(json.dumps(result, indent=2))
