#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ANTIY CMDB - VMware ESXi Agentless Hardware Scanner (v2.0 - PCI Passthrough Edition)

通过 pyVmomi API + PCI 设备表穿透 VMware 抽象层，获取底层真实物理硬件信息。
输出与 collector.py 完全兼容的 JSON 格式，可直接投递给 receiver.py。

依赖: pip install pyvmomi requests
"""
import sys
import ssl
import json
import re
import argparse

try:
    import requests
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim
except ImportError:
    print("[ERROR] Missing core dependencies.")
    print("Please install them using: pip install pyvmomi requests")
    sys.exit(1)

# CIM/WBEM 支持 (用于采集单根内存条的物理信息)
HAS_PYWBEM = False
try:
    import pywbem
    HAS_PYWBEM = True
except ImportError:
    pass


# ========================================================================================
# PCI 设备表构建 —— 穿透 VMware 抽象层的核心武器
# ========================================================================================
def build_pci_device_map(host):
    """
    从 ESXi 的 host.hardware.pciDevice 中构建 PCI 总线地址 -> 真实硬件信息 的映射表。
    这是 VMware 对外暴露的最底层硬件级数据，包含物理 PCI 设备的真实厂商名和芯片型号。
    返回: {  "0000:04:00.0": {"vendor": "Broadcom Inc.", "device": "BCM5720 ..."}, ... }
    """
    pci_map = {}
    try:
        for dev in host.hardware.pciDevice:
            # pci 地址格式: "0000:04:00.0" 与 pnic.pci 一致
            addr = dev.id
            pci_map[addr] = {
                "vendor": (dev.vendorName or "").strip(),
                "device": (dev.deviceName or "").strip(),
                "vendorId": hex(dev.vendorId & 0xFFFF) if dev.vendorId else "",
                "deviceId": hex(dev.deviceId & 0xFFFF) if dev.deviceId else "",
                "subVendor": (getattr(dev, 'subVendorId', 0) or 0),
            }
    except Exception as e:
        print(f"[WARN] Failed to build PCI device map: {e}")
    return pci_map


# ========================================================================================
# 数据采集函数
# ========================================================================================
def get_system_info(host):
    sys_info = {
        "manufacturer": "Unknown",
        "product_name": "Unknown",
        "serial_number": "Unknown",
        "uuid": "Unknown",
        "ip_address": "Unknown"
    }

    hw_info = host.hardware.systemInfo
    if hw_info:
        sys_info["manufacturer"] = hw_info.vendor or "Unknown"
        sys_info["product_name"] = hw_info.model or "Unknown"
        sys_info["uuid"] = hw_info.uuid or "Unknown"

        # 原厂 ESXi 物理机的SN序列号深埋在 otherIdentifyingInfo 数组中
        if hw_info.otherIdentifyingInfo:
            for info in hw_info.otherIdentifyingInfo:
                key = getattr(info.identifierType, 'key', "")
                if key in ["ServiceTag", "EnclosureSerialNumber", "OemSpecificString"]:
                    val = (info.identifierValue or "").strip()
                    if val:
                        sys_info["serial_number"] = val
                        break

    if sys_info["serial_number"] == "Unknown":
        sys_info["serial_number"] = sys_info["uuid"]

    # 尝试提取真实的 ESXi 管理 IP
    try:
        sys_info["ip_address"] = host.config.network.vnic[0].spec.ip.ipAddress
    except:
        sys_info["ip_address"] = host.name

    return sys_info


def get_cpu_info(host):
    cpu_info = {
        "model": "Unknown",
        "physical_cores": 0,
        "logical_cores": 0,
        "physical_count": 0,
        "architecture": "x86_64"
    }

    summary = host.hardware.cpuInfo
    if summary:
        cpu_info["physical_count"] = summary.numCpuPackages
        cpu_info["physical_cores"] = summary.numCpuCores
        cpu_info["logical_cores"] = summary.numCpuThreads

    hw_cpu = host.hardware.cpuPkg
    if hw_cpu and len(hw_cpu) > 0:
        cpu_info["model"] = hw_cpu[0].description

    return cpu_info


def get_memory_info_cim(esxi_host, username, password):
    """
    通过 CIM/WBEM 接口直接查询 ESXi 底层的 CIM_PhysicalMemory 类，
    获取每一根物理内存条的插槽、厂商、SN、Part Number、容量和速率。
    这是穿透 VMware 抽象层获取真实 DIMM 信息的唯一 API 通道。
    """
    if not HAS_PYWBEM:
        return None

    try:
        # ESXi 的 CIM/WBEM 服务监听在 5989 端口 (HTTPS)
        url = f'https://{esxi_host}:5989'
        conn = pywbem.WBEMConnection(
            url,
            (username, password),
            no_verification=True
        )

        # 查询物理内存实体 (CIM 标准类名)
        instances = conn.EnumerateInstances('CIM_PhysicalMemory', namespace='root/cimv2')
        mem_list = []

        for inst in instances:
            capacity = inst.get('Capacity', 0)
            if not capacity or int(capacity) == 0:
                continue

            size_bytes = int(capacity)
            if size_bytes >= 1024 ** 3:
                size_str = f"{round(size_bytes / (1024 ** 3))} GB"
            else:
                size_str = f"{round(size_bytes / (1024 ** 2))} MB"

            # 尝试多种 CIM 标准属性来获取速率 (不同品牌厂商的 CIM Provider 映射不一样)
            speed = inst.get('ConfiguredMemoryClockSpeed') or inst.get('ConfiguredClockSpeed') or inst.get('MaxMemorySpeed') or inst.get('Speed') or 0
            if isinstance(speed, str) and not speed.isdigit():
                speed_str = speed # 有些直接返回了 '3200 MT/s'
            else:
                speed_str = f"{speed} MT/s" if speed and int(speed) > 0 else "Unknown"
            manufacturer = (inst.get('Manufacturer', '') or 'Unknown').strip()
            serial_number = (inst.get('SerialNumber', '') or 'Unknown').strip()
            part_number = (inst.get('PartNumber', '') or 'Unknown').strip()
            bank_label = (inst.get('BankLabel', '') or '').strip()
            tag = (inst.get('Tag', '') or '').strip()
            locator = bank_label if bank_label else tag if tag else 'Unknown'

            # 清理厂商名中的 JEDEC 编码
            manu_upper = manufacturer.upper()
            if 'SAMSUNG' in manu_upper or '00CE' in manu_upper:
                manufacturer = 'Samsung'
            elif 'HYNIX' in manu_upper or '00AD' in manu_upper:
                manufacturer = 'SK Hynix'
            elif 'MICRON' in manu_upper or '002C' in manu_upper:
                manufacturer = 'Micron'
            elif 'KINGSTON' in manu_upper:
                manufacturer = 'Kingston'

            mem_list.append({
                "size": size_str,
                "locator": locator,
                "speed": speed_str,
                "manufacturer": manufacturer,
                "serial_number": serial_number if serial_number != '' else 'Unknown',
                "part_number": part_number if part_number != '' else 'N/A'
            })

        if mem_list:
            return mem_list
        return None
    except Exception as e:
        print(f"    [WARN] CIM memory query failed: {e}")
        print(f"    [HINT] ESXi 的 CIM 服务可能未启用。请在 ESXi Shell 中执行: esxcli system wbem set --enable true")
        return None


def get_memory_info(host, esxi_host=None, username=None, password=None):
    """
    获取内存信息。优先通过 CIM 获取每根 DIMM 的详细数据，
    如果 CIM 不可用则回退到 vSphere API 聚合总容量。
    """
    # ===== 优先尝试 CIM 穿透获取每根内存条 =====
    if esxi_host and username and password:
        cim_result = get_memory_info_cim(esxi_host, username, password)
        if cim_result:
            print(f"    [CIM] Successfully retrieved {len(cim_result)} physical DIMM module(s).")
            return cim_result
        else:
            if HAS_PYWBEM:
                print(f"    [CIM] WBEM/CIM query returned no data, falling back to vSphere API aggregation.")
            else:
                print(f"    [INFO] pywbem not installed. Install it for per-DIMM details: pip install pywbem")

    # ===== 回退：vSphere API 聚合总容量 =====
    mem_list = []
    total_bytes = host.hardware.memorySize or 0
    total_gb = round(total_bytes / (1024 ** 3), 2) if total_bytes else 0

    mem_list.append({
        "size": f"{total_gb} GB",
        "locator": "Aggregated Total (ESXi Host)",
        "speed": "Unknown",
        "manufacturer": "Physical Server RAM",
        "serial_number": "Unknown",
        "part_number": "N/A (Install pywbem for per-DIMM details)"
    })

    return mem_list


def get_network_info(host, pci_map):
    """
    通过 PCI 设备表穿透获取真实物理网卡的厂商名和芯片型号。
    """
    net_list = []
    try:
        pnics = host.config.network.pnic
        for pnic in pnics:
            speed = "Unknown"
            if pnic.linkSpeed:
                speed_mb = pnic.linkSpeed.speedMb
                if speed_mb >= 1000:
                    speed = f"{speed_mb // 1000}Gb/s"
                else:
                    speed = f"{speed_mb}Mb/s"

            driver = pnic.driver or "Unknown"
            pci_addr = pnic.pci or ""

            # ===== 核心穿透：从 PCI 设备表中提取真实物理硬件信息 =====
            manufacturer = "Unknown"
            model = "Unknown"

            if pci_addr and pci_addr in pci_map:
                pci_dev = pci_map[pci_addr]
                manufacturer = pci_dev["vendor"] if pci_dev["vendor"] else "Unknown"
                model = pci_dev["device"] if pci_dev["device"] else "Unknown"

            # 如果 PCI 表没有命中（极端情况），回退到驱动名启发式推断
            if manufacturer == "Unknown" or model == "Unknown":
                from_driver = ESXI_DRIVER_MAP.get(driver.lower(), None)
                if from_driver:
                    if manufacturer == "Unknown":
                        manufacturer = from_driver["manufacturer"]
                    if model == "Unknown":
                        model = from_driver["model"]
                else:
                    if model == "Unknown":
                        model = f"Driver: {driver}"

            # 推断光口/电口
            port_type = "Unknown"
            model_lower = model.lower()
            if any(kw in model_lower for kw in ["10gb", "25gb", "40gb", "100gb", "10g", "25g", "sfp", "rdma", "fibre", "optical"]):
                port_type = "Optical (光口)"
            elif any(kw in model_lower for kw in ["gigabit", "1gb", "1000base", "copper", "rj45", "tp"]):
                port_type = "Copper (电口)"
                
            # 速率兜底：如果由于网线没插导致 ESXi 获取不到活跃 speed，依据刚才 PCI 穿透出来的型号名字进行推算
            if speed == "Unknown":
                if any(x in model_lower for x in ["100gb", "100g"]): speed = "100Gb/s"
                elif any(x in model_lower for x in ["40gb", "40g"]): speed = "40Gb/s"
                elif any(x in model_lower for x in ["25gb", "25g"]): speed = "25Gb/s"
                elif any(x in model_lower for x in ["10gb", "10g"]): speed = "10Gb/s"
                elif any(x in model_lower for x in ["gigabit", "1gb", "1g", "1000m"]): speed = "1Gb/s"

            net_list.append({
                "name": pnic.device,
                "mac": pnic.mac,
                "manufacturer": manufacturer,
                "model": model,
                "serial_number": pnic.mac,  # ESXi 不暴露网卡独立 SN
                "port_type": port_type,
                "speed": speed
            })
    except Exception as e:
        print(f"[WARN] Network info collection error: {e}")
    return net_list


def get_disk_info(host, pci_map):
    """
    获取物理硬盘信息。通过 RAID 控制器的 PCI 信息标识阵列卡型号。
    """
    disk_list = []

    # 先提取 RAID 控制器的真实型号 (从 PCI 表中筛选存储控制器)
    raid_controllers = []
    for addr, dev in pci_map.items():
        dev_name = (dev.get("device", "") or "").lower()
        vendor_name = (dev.get("vendor", "") or "").lower()
        # 常见 RAID/存储控制器关键词
        if any(kw in dev_name for kw in ["raid", "perc", "megaraid", "sas", "sata", "nvme", "scsi", "storage"]):
            raid_controllers.append(f"{dev.get('vendor', '')} {dev.get('device', '')}")
        elif any(kw in vendor_name for kw in ["lsi", "avago", "broadcom"]) and "raid" not in dev_name:
            if any(kw in dev_name for kw in ["sas", "mega", "fusion"]):
                raid_controllers.append(f"{dev.get('vendor', '')} {dev.get('device', '')}")

    raid_info_str = raid_controllers[0] if raid_controllers else "Unknown RAID Controller"

    try:
        luns = host.config.storageDevice.scsiLun
        for lun in luns:
            if lun.lunType == "disk":
                size_gb = "Unknown"
                if hasattr(lun, 'capacity') and lun.capacity:
                    if hasattr(lun.capacity, 'block') and hasattr(lun.capacity, 'blockSize'):
                        blocks = lun.capacity.block
                        block_size = lun.capacity.blockSize
                        raw_gb = (blocks * block_size) / (1024 ** 3)
                        size_gb = f"{round(raw_gb, 2)} GB"

                vendor = (getattr(lun, 'vendor', "") or "").strip()
                model = (getattr(lun, 'model', "") or "").strip()
                sn = ""

                # 多级串号提取策略
                for attr in ['serialNumber', 'serial']:
                    val = getattr(lun, attr, "") or ""
                    val = val.strip()
                    if val and val.lower() not in ["unavailable", "unknown", "n/a", ""]:
                        sn = val
                        break

                # 尝试从 alternateName 提取（某些 RAID 卡会把 SN 藏在这里面）
                if not sn:
                    alt_names = getattr(lun, 'alternateName', []) or []
                    for alt in alt_names:
                        ns = getattr(alt, 'namespace', '')
                        data = getattr(alt, 'data', None)
                        if ns == 'SERIALNUM' and data:
                            try:
                                sn = ''.join([chr(b) for b in data if 32 <= b <= 126]).strip()
                            except:
                                pass
                            if sn:
                                break

                # 兜底：使用 canonicalName
                if not sn:
                    sn = getattr(lun, 'canonicalName', "") or getattr(lun, 'uuid', "Unknown")

                # 如果厂商和型号都是 RAID 控制器名 (PERC H745)，标注清楚
                display_model = model
                if model:
                    display_model = f"{model} (via {raid_info_str})"

                # 尝试推断磁盘类型
                disk_type = "Unknown"
                display_name = getattr(lun, 'displayName', "") or ""
                model_lower = model.lower()
                if any(kw in model_lower for kw in ["ssd", "solid", "flash", "nand"]):
                    disk_type = "SSD"
                elif any(kw in model_lower for kw in ["hdd", "sas", "sata", "spinpoint", "barracuda"]):
                    disk_type = "HDD"

                disk_list.append({
                    "provider": "VMware-ESXi-API",
                    "name": getattr(lun, 'canonicalName', "") or display_name,
                    "size": size_gb,
                    "manufacturer": vendor if vendor else "Unknown",
                    "model": display_model,
                    "serial_number": sn,
                    "type": disk_type
                })
    except Exception as e:
        print(f"[WARN] Disk info collection error: {e}")

    return disk_list


# ESXi 驱动名 -> 真实硬件型号 启发式映射引擎（PCI 表获取失败时的兜底）
ESXI_DRIVER_MAP = {
    "ntg3":     {"manufacturer": "Broadcom",              "model": "NetXtreme BCM5720 Gigabit Ethernet"},
    "tg3":      {"manufacturer": "Broadcom",              "model": "NetXtreme BCM5720 Gigabit Ethernet"},
    "bnxtnet":  {"manufacturer": "Broadcom",              "model": "NetXtreme-E BCM57412 10/25G RDMA Ethernet"},
    "bnx2x":    {"manufacturer": "Broadcom",              "model": "NetXtreme II 10GbE"},
    "ixgbe":    {"manufacturer": "Intel Corporation",     "model": "10-Gigabit Network Connection (X520/X540)"},
    "ixgben":   {"manufacturer": "Intel Corporation",     "model": "10-Gigabit Network Connection (X520/X540)"},
    "i40en":    {"manufacturer": "Intel Corporation",     "model": "Ethernet Controller X710/XL710 25/40GbE"},
    "igbn":     {"manufacturer": "Intel Corporation",     "model": "I350 Gigabit Network Connection"},
    "icen":     {"manufacturer": "Intel Corporation",     "model": "Ethernet 800 Series (E810) 100GbE"},
    "e1000e":   {"manufacturer": "Intel Corporation",     "model": "PRO/1000 PCIe Gigabit Ethernet"},
    "nmlx5_core": {"manufacturer": "Mellanox (NVIDIA)",   "model": "ConnectX-4/5/6 25/50/100GbE"},
    "mlx5_core":  {"manufacturer": "Mellanox (NVIDIA)",   "model": "ConnectX-4/5/6 25/50/100GbE"},
}


# ========================================================================================
# 主逻辑
# ========================================================================================
def main():
    parser = argparse.ArgumentParser(description="VMware ESXi / vCenter Agentless Hardware Scanner (PCI Passthrough Edition)")
    parser.add_argument("--host", required=True, help="ESXi or vCenter IP/Hostname")
    parser.add_argument("--user", required=True, help="Username")
    parser.add_argument("--password", required=True, help="Password")
    parser.add_argument("--server", required=True, help="Target API endpoint e.g., http://127.0.0.1:8080/api/v1/upload_hwinfo")

    args = parser.parse_args()

    print("=" * 60)
    print("ANTIY CMDB - VMware ESXi Agentless Scanner v2.0")
    print("          [ PCI Passthrough Edition ]")
    print("=" * 60)
    print(f"[*] Connecting to VMware API at {args.host}...")

    try:
        si = SmartConnect(host=args.host, user=args.user, pwd=args.password, disableSslCertValidation=True)
    except Exception as e:
        print(f"[ERROR] Connection to {args.host} failed: {e}")
        sys.exit(1)

    content = si.RetrieveContent()
    obj_view = content.viewManager.CreateContainerView(content.rootFolder, [vim.HostSystem], True)
    hosts = obj_view.view

    if not hosts:
        print("[WARNING] No physical HostSystem found under this endpoint.")
        Disconnect(si)
        sys.exit(1)

    print(f"[*] Found {len(hosts)} physical host(s).\n")

    for host_obj in hosts:
        print(f">>> Processing: {host_obj.name}")
        print(f"    Building PCI Device Map (hardware passthrough)...")

        # ===== 核心：构建 PCI 设备映射表 =====
        pci_map = build_pci_device_map(host_obj)
        print(f"    PCI Devices Found: {len(pci_map)}")

        print(f"    Extracting System Info...")
        sys_info = get_system_info(host_obj)

        print(f"    Extracting CPU Info...")
        cpu_info = get_cpu_info(host_obj)

        print(f"    Extracting Memory Info (CIM Passthrough)...")
        mem_info = get_memory_info(host_obj, esxi_host=args.host, username=args.user, password=args.password)

        print(f"    Extracting Network Info (PCI Passthrough)...")
        net_info = get_network_info(host_obj, pci_map)

        print(f"    Extracting Disk Info...")
        disk_info = get_disk_info(host_obj, pci_map)

        final_data = {
            "system": sys_info,
            "cpu": cpu_info,
            "memory_modules": mem_info,
            "network_interfaces": net_info,
            "physical_disks": disk_info,
            "errors": []
        }

        sn = sys_info.get("serial_number", "Unknown_SN")
        print(f"\n    [*] Assembly Complete. SN: [{sn}]")
        print(f"        Manufacturer: {sys_info['manufacturer']}")
        print(f"        Model:        {sys_info['product_name']}")
        print(f"        CPU:          {cpu_info['model']} x{cpu_info['physical_count']}")
        print(f"        Memory:       {sum(float(m['size'].split()[0]) for m in mem_info):.0f} GB")
        print(f"        NICs:         {len(net_info)} physical ports")
        print(f"        Disks:        {len(disk_info)} drives")

        # 投递到中央 CMDB
        print(f"\n    [*] Posting to CMDB [{args.server}]...")
        try:
            resp = requests.post(args.server, json=final_data, timeout=15)
            if resp.status_code == 200:
                print(f"    [SUCCESS] Data ingested successfully.")
            else:
                print(f"    [ERROR] Server rejected: HTTP {resp.status_code}: {resp.text}")
        except Exception as e:
            print(f"    [ERROR] Transmission failed: {e}")

        # 同时保存一份本地 JSON 备份
        local_file = f"esxi_{sn}.json"
        with open(local_file, 'w') as f:
            json.dump(final_data, f, indent=4, ensure_ascii=False)
        print(f"    [*] Local backup saved: {local_file}")
        print()

    Disconnect(si)
    print("[+] All hosts scanned. Done.")


if __name__ == "__main__":
    main()
