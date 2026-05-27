#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Server Hardware Information Collector
兼容 Python 2.6+ / 3.x
自动识别厂商，获取 CPU、内存、网卡及穿透异构 RAID 卡获取物理硬盘及深层SN。
"""

import os
import re
import sys
import json
import subprocess
import argparse

try:
    import urllib2 as urllib_request
except ImportError:
    import urllib.request as urllib_request

def check_is_physical_machine():
    """
    通过底层 DMI 信息探测是否为真正的物理机。
    如果发现是 KVM、VMware、VirtualBox、Xen 等虚机环境，则抛弃采集并直接退出。
    """
    vm_signatures = ["vmware", "qemu", "kvm", "innotek", "virtualbox", "xen", "bochs", "openstack", "alibaba", "tencent", "google", "amazon"]
    
    vendor = run_cmd("cat /sys/class/dmi/id/sys_vendor 2>/dev/null").strip().lower()
    product = run_cmd("cat /sys/class/dmi/id/product_name 2>/dev/null").strip().lower()
    
    # 联合判定
    combined = vendor + " " + product
    for sig in vm_signatures:
        if sig in combined:
            print("[INFO] Virtual machine environment detected (Signature: '{0}' / '{1}').".format(vendor, product))
            print("[INFO] This script is designed for physical servers only. Exiting now.")
            sys.exit(0)
            
    # 如果 sysfs 读取失败或信息为空（极个别老物理机），则假设为物理机放行

def run_cmd(cmd):
    """
    运行 shell 命令并返回其标准输出字符串。
    使用 Popen 保障老旧机器兼容性。
    """
    try:
        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate()
        if p.returncode == 0 and out:
            # 兼容 python2 和 python3 的 decode
            try:
                res = out.decode('utf-8', 'ignore').strip()
            except AttributeError:
                res = out.strip()
            
            # RedHat 早期系统 which 找不到命令时会将 which: no xx 吐到 stdout 造成误判
            if "which: no " in res or "not found" in res:
                return ""
            return res
    except Exception:
        pass
    return ""

COLLECTION_ERRORS = []

def check_and_install_dependencies():
    packages = {
        "dmidecode": "dmidecode",
        "lscpu": "util-linux",
        "lsblk": "util-linux",
        "ip": "iproute",
        "ethtool": "ethtool",
        "lshw": "lshw",
        "lspci": "pciutils",
        "smartctl": "smartmontools",
        "sg_inq": "sg3_utils"
    }
    
    pm = None
    install_cmd = ""
    if run_cmd("which apt-get 2>/dev/null"):
        pm = "apt"
        install_cmd = "DEBIAN_FRONTEND=noninteractive apt-get install -y"
        packages["ip"] = "iproute2"
    elif run_cmd("which yum 2>/dev/null"):
        pm = "yum"
        install_cmd = "yum install -y"
        
    for cmd, pkg in packages.items():
        if not run_cmd("which {0} 2>/dev/null".format(cmd)):
            print("[INFO] Dependency '{0}' not found. Trying to auto-install package '{1}'...".format(cmd, pkg))
            if pm:
                run_cmd("{0} {1}".format(install_cmd, pkg))
                if run_cmd("which {0} 2>/dev/null".format(cmd)):
                    print("[SUCCESS] Installed '{0}' successfully.".format(pkg))
                else:
                    msg = "[ERROR] Failed to install package '{0}'.".format(pkg)
                    print(msg)
                    COLLECTION_ERRORS.append(msg)
            else:
                msg = "[Warning] No apt/yum package manager found. Cannot auto-install '{0}'.".format(pkg)
                print(msg)
                COLLECTION_ERRORS.append(msg)

def get_system_info():
    sys_info = {
        "manufacturer": "Unknown",
        "product_name": "Unknown",
        "serial_number": "Unknown",
        "uuid": "Unknown",
        "ip_address": "Unknown"
    }
    
    out = run_cmd("dmidecode -t 1")
    if out:
        manu_match = re.search(r"Manufacturer:\s*(.*)", out)
        prod_match = re.search(r"Product Name:\s*(.*)", out)
        sn_match = re.search(r"Serial Number:\s*(.*)", out)
        uuid_match = re.search(r"UUID:\s*(.*)", out)
        
        sys_info["manufacturer"] = manu_match.group(1).strip() if manu_match else "Unknown"
        sys_info["product_name"] = prod_match.group(1).strip() if prod_match else "Unknown"
        sys_info["uuid"] = uuid_match.group(1).strip() if uuid_match else "Unknown"
        
        sn = sn_match.group(1).strip() if sn_match else "Unknown"
        invalid_sn_keywords = ["unknown", "o.e.m", "oem", "default string", "system serial number", "to be filled", "reserved", "1234567"]
        
        def is_invalid_sn(s):
            sl = s.lower()
            return not s or any(kw in sl for kw in invalid_sn_keywords)
            
        if is_invalid_sn(sn):
            # 兜底探测: 当机器 (如天玥/白牌机) 系统信息未烧录时，尝试走主板 SN (dmidecode -t 2)
            out2 = run_cmd("dmidecode -t 2 2>/dev/null")
            if out2:
                sn_match2 = re.search(r"Serial Number:\s*(.*)", out2)
                if sn_match2:
                    sn2 = sn_match2.group(1).strip()
                    if not is_invalid_sn(sn2):
                        sn = sn2
        
        # 终极兜底: 如果连主板 SN 都没有，用 UUID。如果 UUID 也没有，用 MAC 地址之一(在网络收集模块无法提前拿到，所以直接用 UUID 即可)
        if is_invalid_sn(sn) and sys_info["uuid"] != "Unknown" and not is_invalid_sn(sys_info["uuid"]):
            sn = sys_info["uuid"]
            
        sys_info["serial_number"] = sn
    
    # 采集所有非回环的 IPv4 地址
    ip_list = []
    ip_out = run_cmd("ip -4 addr show 2>/dev/null")
    if ip_out:
        for m in re.finditer(r"inet\s+(\d+\.\d+\.\d+\.\d+)/\d+", ip_out):
            addr = m.group(1)
            if not addr.startswith("127."):
                ip_list.append(addr)
    
    # 兜底: 使用 hostname -I
    if not ip_list:
        hostname_out = run_cmd("hostname -I 2>/dev/null").strip()
        if hostname_out:
            for addr in hostname_out.split():
                if re.match(r"^\d+\.\d+\.\d+\.\d+$", addr) and not addr.startswith("127."):
                    ip_list.append(addr)
    
    if ip_list:
        sys_info["ip_address"] = ", ".join(ip_list)
        
    return sys_info

def auto_install_raid_tools(sys_info, server_url=None):
    """
    智能安装闭源 RAID 管理工具。
    安装源优先级:
      1. 脚本同目录下的 tools/ 子目录（无需 --server 参数）
      2. 服务端远程下载（需要 --server 参数）
    """
    manu = sys_info.get("manufacturer", "").lower()
    
    # ===== 步骤1: 探测操作系统类型和版本 =====
    is_rpm = run_cmd("which rpm 2>/dev/null") != ""
    is_dpkg = run_cmd("which dpkg 2>/dev/null") != ""
    
    if not is_rpm and not is_dpkg:
        return  # 无法识别包管理器
    
    # 探测 CPU 架构
    arch = run_cmd("uname -m").strip()  # x86_64, aarch64, ppc64le, s390x
    if not arch:
        arch = "x86_64"  # 兜底假设
    
    # 探测 Linux 发行版信息
    os_id = ""       # 如: rhel, centos, rocky, almalinux, ubuntu, debian, sles
    os_version = ""  # 如: 7, 8, 9, 20.04, 22.04
    
    # 优先读取 /etc/os-release (现代 Linux 标准)
    os_release = run_cmd("cat /etc/os-release 2>/dev/null")
    if os_release:
        id_m = re.search(r'^ID="?(\w+)"?', os_release, re.MULTILINE)
        ver_m = re.search(r'^VERSION_ID="?([^"\n]+)"?', os_release, re.MULTILINE)
        if id_m:
            os_id = id_m.group(1).lower()
        if ver_m:
            os_version = ver_m.group(1).strip().split(".")[0]  # 只取主版本号
    
    # 兜底: 老系统尝试 /etc/redhat-release
    if not os_id:
        rh_release = run_cmd("cat /etc/redhat-release 2>/dev/null")
        if rh_release:
            os_id = "rhel"
            ver_m = re.search(r'release\s+(\d+)', rh_release)
            if ver_m:
                os_version = ver_m.group(1)
    
    # 归一化 OS 家族
    os_family = "unknown"
    if os_id in ["rhel", "centos", "rocky", "almalinux", "ol", "fedora", "anolis", "openeuler", "kylin"]:
        os_family = "rhel"
    elif os_id in ["ubuntu", "debian", "linuxmint", "deepin", "uos"]:
        os_family = "debian"
    elif os_id in ["sles", "sled", "opensuse", "opensuse-leap", "opensuse-tumbleweed"]:
        os_family = "suse"
    elif is_rpm:
        os_family = "rhel"   # RPM 系兜底
    elif is_dpkg:
        os_family = "debian"  # DEB 系兜底

    print("[INFO] Detected OS: {0} {1} ({2}), Arch: {3}, Package: {4}".format(
        os_id or "unknown", os_version or "?", os_family, arch, "rpm" if is_rpm else "deb"))
    
    # ===== 步骤2: 根据系统类型构建精确的文件名 =====
    def get_ssacli_filename():
        """
        HP ssacli 包的命名规则 (由 HPE 官方发布):
        RPM:  ssacli-{ver}.x86_64.rpm  /  ssacli-{ver}.aarch64.rpm
        DEB:  ssacli-{ver}_amd64.deb   /  ssacli-{ver}_arm64.deb
        
        服务端 tools/ 目录应按以下结构存放:
          tools/ssacli-5.10-44.0.x86_64.rpm       (RHEL/CentOS x86_64)
          tools/ssacli-5.10-44.0.aarch64.rpm      (RHEL/CentOS ARM64)
          tools/ssacli-5.10-44.0_amd64.deb        (Ubuntu/Debian x86_64)
          tools/ssacli-5.10-44.0_arm64.deb        (Ubuntu/Debian ARM64)
        """
        ver = "5.10-44.0"  # 默认版本号
        
        if os_family in ["rhel", "suse"]:
            rpm_arch = arch  # x86_64 / aarch64
            return "ssacli-{0}.{1}.rpm".format(ver, rpm_arch)
        elif os_family == "debian":
            # DEB 架构名称与 uname -m 不同: x86_64 -> amd64, aarch64 -> arm64
            deb_arch_map = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "armhf"}
            deb_arch = deb_arch_map.get(arch, "amd64")
            return "ssacli-{0}_{1}.deb".format(ver, deb_arch)
        else:
            # 兜底
            return "ssacli-{0}.{1}.rpm".format(ver, arch)
    
    def get_storcli_filename():
        if os_family in ["rhel", "suse"]:
            return "storcli.rpm"
        elif os_family == "debian":
            return "storcli.deb"
        return "storcli.rpm"
    
    # ===== 步骤3: 判断需要安装的工具 =====
    tools_needed = []
    if "hp" in manu or "hewlett" in manu:
        has_hp_tool = run_cmd("which ssacli 2>/dev/null") or \
                      run_cmd("which hpssacli 2>/dev/null") or \
                      run_cmd("which hpacucli 2>/dev/null") or \
                      os.path.exists("/usr/sbin/ssacli") or \
                      os.path.exists("/usr/sbin/hpacucli") or \
                      os.path.exists("/opt/hp/ssacli/bld/ssacli") or \
                      os.path.exists("/opt/smartstorageadmin/ssacli/bin/ssacli")
        if not has_hp_tool:
            tools_needed.append(("ssacli", get_ssacli_filename()))
    elif "dell" in manu or "lenovo" in manu or "inspur" in manu or "huawei" in manu or \
         "nettrix" in manu or "sugon" in manu or "h3c" in manu or "great wall" in manu or "dawning" in manu:
        # 同时检查 storcli 和 storcli64 两种名称及常见安装路径
        has_storcli = (
            run_cmd("which storcli 2>/dev/null")   or
            run_cmd("which storcli64 2>/dev/null") or
            os.path.exists("/opt/MegaRAID/storcli/storcli64") or
            os.path.exists("/opt/MegaRAID/storcli/storcli")  or
            os.path.exists("/usr/sbin/storcli64") or
            os.path.exists("/usr/sbin/storcli")
        )
        if not has_storcli:
            tools_needed.append(("storcli", get_storcli_filename()))
        else:
            # 已安装但不在 PATH → 自动补建软链接
            if not run_cmd("which storcli64 2>/dev/null") and not run_cmd("which storcli 2>/dev/null"):
                for _bin in ["/opt/MegaRAID/storcli/storcli64", "/opt/MegaRAID/storcli/storcli",
                             "/usr/sbin/storcli64", "/usr/sbin/storcli"]:
                    if os.path.exists(_bin):
                        _sym = "/usr/local/sbin/" + os.path.basename(_bin)
                        try:
                            if os.path.lexists(_sym): os.remove(_sym)
                            os.symlink(_bin, _sym)
                            print("[INFO] storcli already installed; created symlink: {0} -> {1}".format(_sym, _bin))
                        except Exception as _se:
                            print("[WARNING] Could not create symlink: {0}".format(str(_se)))
                        break

    print("[DEBUG] Manufacturer detected: '{0}'".format(manu))
    if not tools_needed:
        print("[DEBUG] No RAID tools needed (already installed or unsupported vendor).")
        return

    # ===== 步骤4: 内部安装+验证函数 =====
    def _install_pkg(pkg_path, tool):
        """直接调用 rpm/dpkg，不经过 run_cmd（run_cmd 在 returncode!=0 时不返回任何内容）"""
        print("[INFO] Installing {0} from {1} ...".format(tool, pkg_path))
        try:
            if is_rpm:
                ret = subprocess.call(
                    ["rpm", "-ivh", "--force", pkg_path],
                    stdout=sys.stdout, stderr=sys.stderr
                )
            elif is_dpkg:
                env = os.environ.copy()
                env["DEBIAN_FRONTEND"] = "noninteractive"
                ret = subprocess.call(
                    ["dpkg", "-i", pkg_path],
                    stdout=sys.stdout, stderr=sys.stderr, env=env
                )
            else:
                ret = -1
            print("[INFO] Package manager exited with code: {0}".format(ret))
        except Exception as ex:
            print("[ERROR] Failed to run package manager: {0}".format(str(ex)))

        # 验证安装结果
        ok = False
        if tool == "ssacli":
            for p in ["/usr/sbin/ssacli", "/opt/hp/ssacli/bld/ssacli",
                      "/opt/smartstorageadmin/ssacli/bin/ssacli"]:
                if os.path.exists(p):
                    print("[SUCCESS] ssacli installed at {0}".format(p)); ok = True; break
            if not ok and run_cmd("which ssacli 2>/dev/null"):
                print("[SUCCESS] ssacli installed."); ok = True
        elif tool == "storcli":
            for p in ["/opt/MegaRAID/storcli/storcli64", "/opt/MegaRAID/storcli/storcli",
                      "/usr/sbin/storcli64", "/usr/sbin/storcli",
                      "/usr/local/sbin/storcli64", "/usr/local/sbin/storcli"]:
                if os.path.exists(p):
                    print("[SUCCESS] storcli installed at {0}".format(p)); ok = True; break
            if not ok and (run_cmd("which storcli 2>/dev/null") or run_cmd("which storcli64 2>/dev/null")):
                print("[SUCCESS] storcli installed."); ok = True
        if not ok:
            msg = "[WARNING] {0}: installation finished but binary not found. Check rpm output above.".format(tool)
            print(msg); COLLECTION_ERRORS.append(msg)
        return ok

    # ===== 步骤5: 优先本地 tools/ 目录 =====
    script_dir = os.path.dirname(os.path.realpath(__file__))
    local_search = [
        os.path.join(script_dir, "tools"),
        os.path.join(os.getcwd(), "tools"),
    ]
    still_needed = []
    for tool, filename in tools_needed:
        found_local = False
        for d in local_search:
            p = os.path.join(d, filename)
            if os.path.isfile(p):
                print("[INFO] Found local package: {0}".format(p))
                _install_pkg(p, tool)
                found_local = True
                break
        if not found_local:
            still_needed.append((tool, filename))

    # ===== 步骤6: 本地没有再尝试远程下载 =====
    if not still_needed or not server_url:
        return

    base_url = "/".join(server_url.split("/")[:3])
    for tool, actual_filename in still_needed:
        download_url = "{0}/tools/{1}".format(base_url, actual_filename)
        tmp_file = "/tmp/{0}".format(actual_filename)
        print("[INFO] Attempting to download: {0} ...".format(download_url))
        
        try:
            req = urllib_request.urlopen(download_url, timeout=30)
            code = req.getcode()
            print("[DEBUG] HTTP {0} for {1}".format(code, download_url))
            if code == 200:
                data = req.read()
                print("[INFO] Downloaded {0} bytes. Writing to {1} ...".format(len(data), tmp_file))
                with open(tmp_file, "wb") as f:
                    f.write(data)
                _install_pkg(tmp_file, tool)
                try:
                    os.remove(tmp_file)
                except:
                    pass
            else:
                msg = "[ERROR] File not found on server: {0}".format(download_url)
                print(msg)
                COLLECTION_ERRORS.append(msg)
        except Exception as e:
            msg = "[ERROR] Failed to fetch from server: {0} => {1}".format(download_url, str(e))
            print(msg)
            COLLECTION_ERRORS.append(msg)


def get_cpu_info():
    cpu_info = {
        "model": "Unknown",
        "physical_cores": 0,
        "logical_cores": 0,
        "physical_count": 0
    }
    
    # 获取详细型号和逻辑核数
    lscpu_out = run_cmd("lscpu")
    if lscpu_out:
        model_m = re.search(r"Model name:\s*(.*)", lscpu_out)
        cpu_info["model"] = model_m.group(1).strip() if model_m else "Unknown"
        
        cores_m = re.search(r"^CPU\(s\):\s*(\d+)", lscpu_out, re.MULTILINE)
        if cores_m:
            cpu_info["logical_cores"] = int(cores_m.group(1))
            
    # 从 dmidecode 获取物理颗数以及兜底查厂商型号
    dmi_cpu_out = run_cmd("dmidecode -t 4")
    if dmi_cpu_out:
        cnt = len(re.findall(r"Socket Designation", dmi_cpu_out, re.IGNORECASE))
        cpu_info["physical_count"] = cnt
        
        # ARM架构 (如华为鲲鹏) lscpu 经常没有 Model name，走 DMI 兜底
        if cpu_info["model"] == "Unknown" or not cpu_info["model"].strip():
            ver_m = re.search(r"Version:\s*(.*)", dmi_cpu_out)
            manu_m = re.search(r"Manufacturer:\s*(.*)", dmi_cpu_out)
            
            fallback_model = ""
            if manu_m and manu_m.group(1).strip():
                fallback_model += manu_m.group(1).strip() + " "
            if ver_m and ver_m.group(1).strip():
                fallback_model += ver_m.group(1).strip()
                
            if fallback_model:
                cpu_info["model"] = fallback_model.strip()
                
    # 彻底兜底：读取 /proc/cpuinfo 的 Hardware 或 Processor 字段 (也是 ARM 特供)
    if cpu_info["model"] == "Unknown" or not cpu_info["model"].strip():
        cpuinfo = run_cmd("cat /proc/cpuinfo 2>/dev/null")
        hw_m = re.search(r"^(Hardware|Processor)\s*:\s*(.*)", cpuinfo, re.MULTILINE | re.IGNORECASE)
        if hw_m:
            cpu_info["model"] = hw_m.group(2).strip()
            
    return cpu_info

def get_memory_info():
    memory_list = []
    out = run_cmd("dmidecode -t 17")
    if not out:
        return memory_list
        
    # 分割每一条内存设备
    devices = out.split("Memory Device")
    for block in devices[1:]:
        size_m = re.search(r"Size:\s*(.*)", block)
        if not size_m or "No Module Installed" in size_m.group(1):
            continue
        
        # 内存容量归一化：统一转换为 GB 显示
        raw_size = size_m.group(1).strip()
        normalized_size = raw_size
        size_val_m = re.match(r"^(\d+)\s*(MB|GB|TB|KB)", raw_size, re.IGNORECASE)
        if size_val_m:
            val = int(size_val_m.group(1))
            unit = size_val_m.group(2).upper()
            if unit == "MB" and val >= 1024:
                normalized_size = "{0} GB".format(val // 1024)
            elif unit == "KB" and val >= 1048576:
                normalized_size = "{0} GB".format(val // 1048576)
            elif unit == "TB":
                normalized_size = "{0} GB".format(val * 1024)
            else:
                normalized_size = "{0} {1}".format(val, unit)
            
        mem = {
            "size": normalized_size,
            "locator": "",
            "speed": "Unknown",
            "manufacturer": "Unknown",
            "serial_number": "Unknown",
            "part_number": "Unknown"
        }
        
        loc_m = re.search(r"Locator:\s*(.*)", block)
        if loc_m: mem["locator"] = loc_m.group(1).strip()
            
        speed_m = re.search(r"Speed:\s*(.*)", block)
        if speed_m: mem["speed"] = speed_m.group(1).strip()
            
        manu_m = re.search(r"Manufacturer:\s*(.*)", block)
        if manu_m:
            manu = manu_m.group(1).strip()
            up_manu = manu.upper()
            if "00CE" in up_manu or "80CE" in up_manu or "SAMSUNG" in up_manu: manu = "Samsung"
            elif "00AD" in up_manu or "80AD" in up_manu or "HYNIX" in up_manu: manu = "SK Hynix"
            elif "002C" in up_manu or "802C" in up_manu or "MICRON" in up_manu: manu = "Micron"
            elif "1636" in up_manu: manu = "Micron (1636)" # Frequently used by Micron/Crucial OEM
            elif "KINGSTON" in up_manu: manu = "Kingston"
            mem["manufacturer"] = manu
            
        sn_m = re.search(r"Serial Number:\s*(.*)", block)
        if sn_m: 
            sn_val = sn_m.group(1).strip()
            mem["serial_number"] = sn_val if sn_val not in ["Not Specified", "Unknown"] else "Unknown"
            
        pn_m = re.search(r"Part Number:\s*(.*)", block)
        if pn_m: 
            pn_val = pn_m.group(1).strip()
            # Dell BIOS 经常乱吐无用占位符
            if pn_val in [".+.#.", "None", "Not Specified", "Unknown"]:
                mem["part_number"] = "OEM Built-in / Unknown"
            else:
                mem["part_number"] = pn_val
            
        memory_list.append(mem)
        
    return memory_list

def get_network_info():
    net_list = []
    # 提前缓存 lshw 输出提高解析率
    lshw_out = run_cmd("lshw -class network 2>/dev/null")
    
    # 通过 ip 命令获取设备 MAC
    ip_out = run_cmd("ip link show")
    if ip_out:
        blocks = ip_out.strip().split("\n")
        current_name = None
        for line in blocks:
            name_m = re.match(r"^\d+:\s*([^:]+):", line)
            if name_m:
                current_name = name_m.group(1).split("@")[0].strip()
            elif current_name and ("link/ether" in line):
                mac_m = re.search(r"link/ether\s+([a-fA-F0-9:]+)", line)
                if mac_m and current_name != "lo":
                    # 基于名称前缀的显式黑名单过滤（直接干掉所有虚机/容器/OpenStack软路由网卡）
                    virt_prefixes = ("bond", "tun", "tap", "veth", "br-", "virbr", 
                                     "docker", "flannel", "cni", "ovs", "qbr", "qvo", "qvb", 
                                     "kube", "cali", "wg", "dummy", "tailscale", "zerotier")
                    if current_name.startswith(virt_prefixes):
                        current_name = None
                        continue

                    # 绝对过滤杀手锏：虚拟网卡在内核态是没有独立物理硬件 device 句柄的
                    if not os.path.exists("/sys/class/net/{0}/device".format(current_name)):
                        current_name = None
                        continue
                        
                    mac_addr = mac_m.group(1).strip()
                    
                    nic_info = {
                        "name": current_name,
                        "mac": mac_addr,
                        "manufacturer": "Unknown",
                        "model": "Unknown",
                        "serial_number": "Unknown",
                        "port_type": "Unknown",
                        "speed": "Unknown"
                    }
                    
                    # 1. 使用 ethtool 获取速率和光电类型
                    ethtool_out = run_cmd("ethtool {0} 2>/dev/null".format(current_name))
                    if ethtool_out:
                        speed_match = re.search(r"Speed:\s*(.*)", ethtool_out)
                        if speed_match and "Unknown" not in speed_match.group(1):
                            nic_info["speed"] = speed_match.group(1).strip()
                        
                        # 兜底1: ethtool Speed 为 Unknown 时, 从 Supported link modes 推算最大能力
                        if nic_info["speed"] == "Unknown":
                            modes = re.findall(r"(\d+)baseT|(\d+)000baseX|(\d+)000baseSR|(\d+)000baseLR|(\d+)000baseCR|(\d+)000baseKR", ethtool_out)
                            max_speed = 0
                            for m in modes:
                                for g in m:
                                    if g:
                                        val = int(g)
                                        # baseT 类为 Mb/s, 其余 *000base 已含倍率
                                        if val > max_speed:
                                            max_speed = val
                            if max_speed > 0:
                                nic_info["speed"] = "{0}Mb/s".format(max_speed)
                    
                    # 兜底2: 从内核 sysfs 读取 (单位 Mb/s, 网卡 down 时可能为 -1)
                    if nic_info["speed"] == "Unknown":
                        sysfs_speed = run_cmd("cat /sys/class/net/{0}/speed 2>/dev/null".format(current_name)).strip()
                        if sysfs_speed and sysfs_speed not in ["-1", "0", ""]:
                            try:
                                spd = int(sysfs_speed)
                                nic_info["speed"] = "{0}Mb/s".format(spd)
                            except ValueError:
                                pass
                                
                    # 速率归一化 (统一转换为 Gb/s，并干掉任何附加的描述如 Max)
                    if nic_info["speed"] != "Unknown":
                        raw_spd = nic_info["speed"]
                        m = re.search(r"(\d+)\s*(M|G)", raw_spd, re.IGNORECASE)
                        if m:
                            val = int(m.group(1))
                            unit = m.group(2).upper()
                            val_g = val / 1000.0 if unit == "M" else val
                            nic_info["speed"] = "{0:g}Gb/s".format(val_g)
                        else:
                            nic_info["speed"] = re.sub(r"\s*\(.*?\)", "", nic_info["speed"])
                    
                    # 3. 光电口类型判断 (仍从 ethtool 输出读取)
                    if ethtool_out:
                        port_match = re.search(r"Port:\s*(.*)", ethtool_out)
                        if port_match:
                            port_val = port_match.group(1).strip().upper()
                            if "FIBRE" in port_val or "FIBER" in port_val or "DA" in port_val or "DIRECT ATTACH" in port_val:
                                nic_info["port_type"] = "Optical (光口)"
                            elif "TWISTED PAIR" in port_val or "TP" in port_val or "MII" in port_val:
                                nic_info["port_type"] = "Copper (电口)"
                            else:
                                nic_info["port_type"] = port_val # 兜底显示原生标识
                                
                    # 2. 核心探测：使用 ethtool -i 获取总线号，再去调用 lspci 提取绝对真实的物理厂商
                    ethtool_i_out = run_cmd("ethtool -i {0} 2>/dev/null".format(current_name))
                    bus_info = None
                    if ethtool_i_out:
                        bus_m = re.search(r"bus-info:\s*([\w:\.]+)", ethtool_i_out)
                        if bus_m:
                            bus_info = bus_m.group(1).strip()
                            # 输出犹如 "01:00.0" "Ethernet controller" "Intel Corporation" "I350 Gigabit Network Connection"
                            lspci_bin = run_cmd("which lspci 2>/dev/null").strip()
                            if not lspci_bin:
                                lspci_bin = "/sbin/lspci" if os.path.exists("/sbin/lspci") else ("/usr/sbin/lspci" if os.path.exists("/usr/sbin/lspci") else "lspci")
                            
                            lspci_m_out = run_cmd("{0} -m -s {1} 2>/dev/null".format(lspci_bin, bus_info))
                            if lspci_m_out:
                                pci_parts = re.findall(r'"(.*?)"', lspci_m_out)
                                if len(pci_parts) >= 4:
                                    nic_info["manufacturer"] = pci_parts[2].strip()
                                    nic_info["model"] = pci_parts[3].strip()
                                    
                    # 3. 兜底探测：如果在极其罕见的情况下 lspci 没抓到，退回到使用 lshw
                    if nic_info["manufacturer"] == "Unknown" and lshw_out:
                        lshw_blocks = lshw_out.split("*-network")
                        for block in lshw_blocks:
                            if "logical name: {0}".format(current_name) in block:
                                v_match = re.search(r"vendor:\s*(.*)", block)
                                p_match = re.search(r"product:\s*(.*)", block)
                                s_match = re.search(r"serial:\s*(.*)", block)
                                
                                if v_match and nic_info["manufacturer"] == "Unknown": 
                                    nic_info["manufacturer"] = v_match.group(1).strip()
                                if p_match and nic_info["model"] == "Unknown": 
                                    nic_info["model"] = p_match.group(1).strip()
                                
                                # lshw 有时会把 MAC 当作 serial 输出
                                if s_match:
                                    s_val = s_match.group(1).strip()
                                    if s_val.lower() != mac_addr.lower():
                                        nic_info["serial_number"] = s_val
                                break
                                
                    # 4. 终极兜底探测：如果在完全割裂的裸机环境 (无 lspci, lshw)，使用内核网卡驱动名称进行启发式硬件推理
                    if nic_info["manufacturer"] == "Unknown" or nic_info["model"] == "Unknown" or "Device_ID:" in nic_info["model"]:
                        # 尝试从 ethtool 中提取 driver，或者硬读 sysfs
                        driver_name = ""
                        driver_m = re.search(r"driver:\s*(.+)", ethtool_i_out)
                        if driver_m:
                            driver_name = driver_m.group(1).strip()
                        else:
                            # 通过 sysfs /sys/class/net/{iface}/device/driver 符号链接的最后一级名字获取 driver
                            driver_path = "/sys/class/net/{0}/device/driver".format(current_name)
                            if os.path.exists(driver_path) and os.path.islink(driver_path):
                                driver_name = os.path.basename(os.readlink(driver_path))
                                
                        vendor_fs = run_cmd("cat /sys/class/net/{0}/device/vendor 2>/dev/null".format(current_name))
                        device_fs = run_cmd("cat /sys/class/net/{0}/device/device 2>/dev/null".format(current_name))
                        
                        if vendor_fs and nic_info["manufacturer"] == "Unknown":
                            vendor_id = vendor_fs.strip().lower()
                            if "8086" in vendor_id: nic_info["manufacturer"] = "Intel Corporation"
                            elif "14e4" in vendor_id: nic_info["manufacturer"] = "Broadcom (QLogic)"
                            elif "15b3" in vendor_id: nic_info["manufacturer"] = "Mellanox Technologies"
                            elif "10ec" in vendor_id: nic_info["manufacturer"] = "Realtek Semiconductor"
                            elif "10df" in vendor_id: nic_info["manufacturer"] = "Emulex Corporation"
                            elif "1077" in vendor_id: nic_info["manufacturer"] = "QLogic Corp."
                            elif "1137" in vendor_id: nic_info["manufacturer"] = "Cisco Systems Inc"
                            else: nic_info["manufacturer"] = "Vendor_ID:" + vendor_id
                        
                        if driver_name and nic_info["model"] == "Unknown":
                            # Driver-to-Model Heuristic Mapping (Smart Infer)
                            driver_map = {
                                "ixgbe": "Intel 10-Gigabit Network Connection",
                                "i40e": "Intel 40-Gigabit Ethernet",
                                "igb": "Intel Gigabit Network Connection",
                                "ice": "Intel 100-Gigabit Network Server Adapter",
                                "tg3": "Broadcom NetXtreme Gigabit Ethernet",
                                "bnx2x": "Broadcom NetXtreme II 10G",
                                "bnxt_en": "Broadcom NetXtreme-E 10/25/50/100G",
                                "mlx4_en": "Mellanox ConnectX-3",
                                "mlx5_core": "Mellanox ConnectX-4/5/6",
                                "e1000e": "Intel PRO/1000 PCIe",
                                "virtio_net": "Virtio Virtual Network Device",
                                "vmxnet3": "VMware VMXNET3 Adapter",
                                "be2net": "Emulex OneConnect 10Gbps"
                            }
                            nic_info["model"] = driver_map.get(driver_name.lower(), "Driver: " + driver_name)
                        elif nic_info["model"] == "Unknown" and device_fs:
                            nic_info["model"] = "Device_ID:" + device_fs.strip()

                    # 退一步：大多数网卡出厂时直接将 MAC 作为 SN 贴在条码上
                    if nic_info["serial_number"] == "Unknown":
                        nic_info["serial_number"] = mac_addr

                    net_list.append(nic_info)
                    current_name = None
    return net_list

def _is_wwn(sn):
    """
    判断给定字符串是否为 WWN/NAA 标识符，而非真实磁盘序列号。
    - NAA-5 (SAS 物理盘 WWN):   16 位纯十六进制 (8 字节)
    - NAA-6 (RAID 控制器 LUN): 32 位纯十六进制 (16 字节)
    真实 SN 一般为 8-16 位字母数字混合，通常包含字母且不全是 0-9a-f。
    """
    if not sn or sn in ["Unknown", ""]:
        return False
    s = sn.strip().lower().lstrip("0x")
    # 14~32 位纯十六进制字符 → 判定为 WWN/NAA 标识符
    if re.match(r'^[0-9a-f]{14,32}$', s):
        return True
    return False


def _get_real_disk_sn(dev_name, sn_candidate):
    """
    当检测到 sn_candidate 可能是 WWN 时，尝试通过多种途径获取磁盘真实序列号。
    
    优先级（从零依赖到有依赖）:
      0. sysfs VPD pg80 二进制文件 (内核直接暴露，无需任何工具)
      1. sysfs /sys/block/sdX/device/serial (内核 SCSI 层缓存)
      2. udevadm info ID_SERIAL_SHORT
      3. smartctl -i
      4. sg_inq --page=0x80
    
    :param dev_name: 磁盘设备名，如 'sda' 或 '/dev/sda'
    :param sn_candidate: 当前已有的 SN（可能是 WWN）
    :return: 真实 SN 字符串，或原始候选值（若无法改善）
    """
    if not dev_name:
        return sn_candidate
    
    # 统一设备路径和短名
    dev_path = dev_name if dev_name.startswith("/dev/") else "/dev/" + dev_name
    dev_short = dev_name.replace("/dev/", "")
    
    # ---- 方法0: 直读内核 sysfs VPD Page 0x80 二进制文件 (零依赖，最可靠) ----
    # Linux 3.6+ 在 /sys/block/<dev>/device/vpd_pg80 暴露 SCSI Unit Serial Number 页
    vpd_pg80_path = "/sys/block/{0}/device/vpd_pg80".format(dev_short)
    if os.path.exists(vpd_pg80_path):
        try:
            with open(vpd_pg80_path, "rb") as f:
                vpd_data = f.read()
            # VPD pg 0x80 格式: [devtype(1)] [0x80(1)] [length_hi(1)] [length_lo(1)] [sn_ascii...]
            if len(vpd_data) >= 4 and vpd_data[1:2] in (b'\x80', b'\x00'):
                pg_len = (vpd_data[2] << 8 | vpd_data[3]) if len(vpd_data) > 3 else 0
                # 兼容只有 2 字节头的简化实现
                if pg_len == 0 and len(vpd_data) > 4:
                    pg_len = len(vpd_data) - 4
                sn_bytes = vpd_data[4:4 + pg_len] if pg_len > 0 else vpd_data[4:]
                try:
                    real_sn = sn_bytes.decode('ascii', 'ignore').strip()
                except Exception:
                    real_sn = ""
                if real_sn and not _is_wwn(real_sn):
                    return real_sn
        except Exception:
            pass
    
    # ---- 方法1: sysfs /sys/block/sdX/device/serial (内核 SCSI inquiry 缓存) ----
    sysfs_serial_path = "/sys/block/{0}/device/serial".format(dev_short)
    if os.path.exists(sysfs_serial_path):
        try:
            with open(sysfs_serial_path, "r") as f:
                real_sn = f.read().strip()
            if real_sn and not _is_wwn(real_sn):
                return real_sn
        except Exception:
            pass
    
    # ---- 方法2: udevadm info ID_SERIAL_SHORT ----
    udev_out = run_cmd("udevadm info --query=property --name={0} 2>/dev/null".format(dev_path))
    if udev_out:
        m = re.search(r'^ID_SERIAL_SHORT=(.+)$', udev_out, re.MULTILINE)
        if m:
            real_sn = m.group(1).strip()
            if real_sn and not _is_wwn(real_sn):
                return real_sn
    
    # ---- 方法3: smartctl -i (覆盖绝大多数 ATA/SATA/SAS/NVMe 盘) ----
    smartctl_bin = run_cmd("which smartctl 2>/dev/null").strip()
    if smartctl_bin:
        smartctl_out = run_cmd("{0} -i {1} 2>/dev/null".format(smartctl_bin, dev_path))
        if smartctl_out:
            m = re.search(r'^Serial Number:\s*(\S+)', smartctl_out, re.MULTILINE | re.IGNORECASE)
            if m:
                real_sn = m.group(1).strip()
                if real_sn and not _is_wwn(real_sn):
                    return real_sn
    
    # ---- 方法4: sg_inq --page=0x80 (专为 SAS/SCSI 设备) ----
    sginq_bin = run_cmd("which sg_inq 2>/dev/null").strip()
    if sginq_bin:
        vpd_out = run_cmd("{0} --page=0x80 {1} 2>/dev/null".format(sginq_bin, dev_path))
        if vpd_out:
            m = re.search(r'Unit serial number:\s*(\S+)', vpd_out, re.IGNORECASE)
            if m:
                real_sn = m.group(1).strip()
                if real_sn and not _is_wwn(real_sn):
                    return real_sn
    
    # 所有方法均未能改善，返回原候选值
    return sn_candidate

def _probe_megaraid_physical_drives(vd_dev_name):
    """
    利用 smartctl MegaRAID pass-through 模式，透过 RAID 控制器查询背后的真实物理盘。
    支持 Broadcom/Avago/LSI MegaRAID 化容控制器（华为 2288H 上常用）。
    命令示例: smartctl -i -d megaraid,0 /dev/sdf

    :param vd_dev_name: RAID 虚拟盘设备名，如 'sdf' 或 '/dev/sdf'
    :return: list of physical drive dicts
    """
    smartctl_bin = run_cmd("which smartctl 2>/dev/null").strip()
    if not smartctl_bin:
        msg = "[WARNING] smartctl not found; cannot probe RAID physical drives via pass-through."
        print(msg)
        COLLECTION_ERRORS.append(msg)
        return []

    dev_path = vd_dev_name if vd_dev_name.startswith("/dev/") else "/dev/" + vd_dev_name
    found_drives = []
    consecutive_empty = 0  # 连续空槽位计数

    for slot in range(32):  # MegaRAID 最多支持每控制器 32 块物理盘
        # 注意: 用 subprocess 同时捕获 stdout+stderr，否则 smartctl 错误信息丢失
        try:
            import subprocess as _sp
            p = _sp.Popen(
                "{0} -i -d megaraid,{1} {2}".format(smartctl_bin, slot, dev_path),
                shell=True,
                stdout=_sp.PIPE, stderr=_sp.STDOUT
            )
            raw, _ = p.communicate()
            try:
                out = raw.decode("utf-8", "ignore")
            except AttributeError:
                out = raw
        except Exception:
            break

        out_lower = out.lower()

        # ---- 控制器完全不支持 megaraid pass-through —— 弹出并放弃 ----
        FATAL_SIGNS = [
            "no such device",
            "unable to detect device type",
            "requires '-d' option",
            "open failed",
            "controller not found",
            "unknown scsi opcode",
        ]
        if any(sig in out_lower for sig in FATAL_SIGNS):
            break

        # ---- 解析关键字段，以是否有 Serial Number 为核心判断 ----
        sn_m    = re.search(r'^Serial Number:\s*(\S+)',           out, re.MULTILINE | re.IGNORECASE)
        model_m = re.search(r'^(?:Device Model|Product):\s*(.+)',out, re.MULTILINE | re.IGNORECASE)
        cap_m   = re.search(r'^User Capacity:\s*(.+)',           out, re.MULTILINE | re.IGNORECASE)
        rpm_m   = re.search(r'^Rotation Rate:\s*(.+)',           out, re.MULTILINE | re.IGNORECASE)

        if not sn_m:
            # 没有 SN 说明该槽位为空或不可读
            consecutive_empty += 1
            if consecutive_empty >= 4:   # 连续 4 个空槽位则认为已超出范围
                break
            continue

        consecutive_empty = 0  # 成功读到一块，重置计数器

        sn        = sn_m.group(1).strip()
        model_raw = model_m.group(1).strip() if model_m else "Unknown"
        cap_raw   = cap_m.group(1).strip()   if cap_m   else ""
        rpm_raw   = rpm_m.group(1).strip()   if rpm_m   else ""

        # 容量解析: 优先从 [X.XX TB] 括号内取，其次从 bytes 计算
        size_str = "Unknown"
        bracket_m = re.search(r'\[([^\]]+(?:TB|GB|MB|KB)[^\]]*)\]', cap_raw, re.IGNORECASE)
        if bracket_m:
            size_str = bracket_m.group(1).strip()
        else:
            bytes_m = re.search(r'([\d,]+)\s+bytes', cap_raw)
            if bytes_m:
                try:
                    bv = int(bytes_m.group(1).replace(",", ""))
                    if bv >= 1024 ** 4:
                        size_str = "{0:.2f} TB".format(bv / (1024 ** 4))
                    elif bv >= 1024 ** 3:
                        size_str = "{0:.0f} GB".format(bv / (1024 ** 3))
                    else:
                        size_str = "{0:.0f} MB".format(bv / (1024 ** 2))
                except Exception:
                    pass

        # 磁盘类型
        disk_type = "Unknown"
        if rpm_raw:
            rpm_lower = rpm_raw.lower()
            if "solid state" in rpm_lower or rpm_raw.strip() == "0":
                disk_type = "SSD"
            elif re.search(r'\d+', rpm_raw):
                disk_type = "HDD"

        # 厂商 + 型号拆分
        parts = model_raw.split(None, 1)
        if len(parts) > 1:
            manufacturer, model = parts[0], parts[1]
        else:
            manufacturer, model = "Unknown", model_raw

        mfr_up = manufacturer.upper()
        mod_up = model_raw.upper()
        if "SEAGATE" in mfr_up or mod_up.startswith("ST"): manufacturer = "Seagate"
        elif mfr_up.startswith("WD") or "WESTERN" in mfr_up:  manufacturer = "Western Digital"
        elif "TOSHIBA" in mfr_up:   manufacturer = "Toshiba"
        elif "HGST" in mfr_up:      manufacturer = "HGST"
        elif "SAMSUNG" in mfr_up or mod_up.startswith("MZ"): manufacturer = "Samsung"
        elif "MICRON" in mfr_up:    manufacturer = "Micron"
        elif "INTEL" in mfr_up:     manufacturer = "Intel"
        elif "KIOXIA" in mfr_up:    manufacturer = "KIOXIA"

        found_drives.append({
            "manufacturer": manufacturer,
            "model": model,
            "serial_number": sn,
            "size": size_str,
            "type": disk_type,
            "slot": slot,
            "provider": "smartctl-megaraid",
            "via": dev_path
        })

    return found_drives


def get_disk_info():
    """
    智能下钻物理硬盘及其 SN。此环节是跨硬件管理的核心痛点。
    针对华为 2288H 等服务器，lsblk/storcli 对 SAS 盘返回的 SERIAL
    可能是 WWN 而非真实制造商序列号，需通过 udevadm/smartctl/sg_inq 修正。
    """
    disks = []
    
    # 策略 1: 检查是否存在 storcli (针对 LSI/Broadcom/Dell/Huawei)
    # 注意：新版 storcli rpm 安装后可能叫 storcli 而非 storcli64
    storcli_path = (
        run_cmd("which storcli64 2>/dev/null").strip() or
        run_cmd("which storcli 2>/dev/null").strip()
    )
    if not storcli_path:
        for _p in [
            "/opt/MegaRAID/storcli/storcli64",
            "/opt/MegaRAID/storcli/storcli",
            "/usr/sbin/storcli64",
            "/usr/sbin/storcli",
            "/usr/local/sbin/storcli64",
            "/usr/local/sbin/storcli",
        ]:
            if os.path.exists(_p):
                storcli_path = _p
                break
        
    if storcli_path:
        storcli_path = storcli_path.strip()
        try:
            # 直接用 Popen 捕获输出，不依赖 returncode（storcli 可能返回非 0 即使成功）
            _p = subprocess.Popen(
                [storcli_path, "/call", "/eall", "/sall", "show", "all", "J"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            _stdout, _ = _p.communicate()
            try:
                out = _stdout.decode("utf-8", "ignore").strip()
            except AttributeError:
                out = _stdout.strip()
        except Exception as _e:
            out = ""
            print("[WARNING] storcli execution failed: {0}".format(str(_e)))
        print("[DEBUG] storcli path={0}, output_len={1}".format(storcli_path, len(out)))
        if out and out.strip():

            try:
                # 尝试找到 JSON 开始位置（部分 storcli 版本前面有非 JSON 文字）
                json_start = out.find('{')
                if json_start > 0:
                    out = out[json_start:]
                data = json.loads(out)
                # storcli JSON 结构（Broadcom 标准格式）:
                #   "Response Data": {
                #     "Drive /c0/e252/s0": { "Model": "...", "Size": "...", "Intf": "SAS", "Med": "HDD" },
                #     "Drive /c0/e252/s0 - Detailed Information": {
                #       "Drive /c0/e252/s0 Device attributes": { "SN": "...", "Model Number": "..." },
                #       ...
                #     }
                #   }
                for ctrl in data.get("Controllers", []):
                    resp = ctrl.get("Response Data", {})
                    _resp_keys = list(resp.keys())
                    print("[DEBUG] storcli resp keys ({0}): {1}".format(len(_resp_keys), _resp_keys[:10]))
                    # 第一步：收集基本条目 和 Detailed 条目
                    basics  = {}   # "Drive /cX/eY/sZ" -> dict
                    details = {}   # "Drive /cX/eY/sZ" -> detail dict
                    for rkey, rval in resp.items():
                        if not isinstance(rval, dict):
                            continue
                        m_basic  = re.match(r'^(Drive /c\d+/e\d+/s\d+)$', rkey)
                        m_detail = re.match(r'^(Drive /c\d+/e\d+/s\d+) - Detailed Information$', rkey)
                        if m_basic:
                            basics[m_basic.group(1)] = rval
                        elif m_detail:
                            details[m_detail.group(1)] = rval

                    print("[DEBUG] storcli basics={0}, details={1}".format(len(basics),len(details)))
                    # 第二步：合并基本+详细信息
                    for drive_key, basic in basics.items():
                        model_raw = basic.get("Model", "").strip()
                        size_raw  = basic.get("Size",  "Unknown").strip()
                        intf      = basic.get("Intf",  "").strip()
                        med       = basic.get("Med",   "").strip()

                        # 从 Detailed Information 里找 SN
                        sn = "Unknown"
                        for dsval in details.get(drive_key, {}).values():
                            if isinstance(dsval, dict):
                                candidate = dsval.get("SN", "") or dsval.get("Serial Number", "")
                                if candidate:
                                    sn = str(candidate).strip()
                                    break

                        if not model_raw and sn == "Unknown":
                            continue

                        # 如果 SN 是 WWN，尝试通过 udevadm 匹配真实 SN
                        if _is_wwn(sn):
                            try:
                                import glob as _glob
                                for blk_dev in sorted(_glob.glob("/dev/sd?") + _glob.glob("/dev/sd??")):
                                    udev_out = run_cmd("udevadm info --query=property --name={0} 2>/dev/null".format(blk_dev))
                                    if udev_out:
                                        wwn_m = re.search(r'^ID_WWN=0x([0-9a-fA-F]+)$', udev_out, re.MULTILINE)
                                        wwn_cand = wwn_m.group(1).lower() if wwn_m else ""
                                        sn_strip = sn.lower().lstrip("0x")
                                        if wwn_cand and (sn_strip == wwn_cand or sn_strip in wwn_cand or wwn_cand in sn_strip):
                                            sn = _get_real_disk_sn(blk_dev, sn)
                                            break
                            except Exception:
                                pass

                        # 拆分厂商 + 型号
                        clean_model = re.sub(r'^(ATA|NVMe|SATA)\s+', '', model_raw).strip()
                        parts = clean_model.split(None, 1)
                        manufacturer = parts[0] if len(parts) > 1 else "Unknown"
                        model        = parts[1] if len(parts) > 1 else clean_model

                        # 标准化厂商名
                        mup = manufacturer.upper()
                        mf  = clean_model.upper()
                        if "SEAGATE" in mup or mf.startswith("ST"):   manufacturer = "Seagate"
                        elif mup.startswith("WD") or "WESTERN" in mup: manufacturer = "Western Digital"
                        elif "TOSHIBA" in mup: manufacturer = "Toshiba"
                        elif "HGST" in mup:    manufacturer = "HGST"
                        elif "SAMSUNG" in mup or mf.startswith("MZ"): manufacturer = "Samsung"
                        elif "MICRON" in mup or mf.startswith("MT"):  manufacturer = "Micron"
                        elif "INTEL" in mup:   manufacturer = "Intel"
                        elif "KIOXIA" in mup:  manufacturer = "KIOXIA"
                        elif "SSSTC" in mup:   manufacturer = "SSSTC"

                        disk_type = "Unknown"
                        if "ssd" in med.lower() or "flash" in med.lower(): disk_type = "SSD"
                        elif "hdd" in med.lower() or "disk" in med.lower(): disk_type = "HDD"

                        disks.append({
                            "manufacturer":  manufacturer,
                            "model":         model,
                            "serial_number": sn,
                            "size":          size_raw,
                            "type":          disk_type,
                            "interface":     intf,
                            "provider":      "LSI-storcli"
                        })
            except Exception as _je:
                print("[WARNING] storcli JSON parse error: {0}".format(str(_je)))
        if disks: return disks

    # 策略 2: 检查老旧 MegaCli
    megacli_path = run_cmd("which MegaCli 2>/dev/null") or run_cmd("which MegaCli64 2>/dev/null")
    if not megacli_path and os.path.exists("/opt/MegaRAID/MegaCli/MegaCli64"):
        megacli_path = "/opt/MegaRAID/MegaCli/MegaCli64"
        
    if megacli_path:
        out = run_cmd(megacli_path + " -PDList -aALL -NoLog")
        if out:
            current_disk = {}
            for line in out.split("\n"):
                if "Enclosure Device ID:" in line:
                    if "serial_number" in current_disk: disks.append(current_disk)
                    current_disk = {"provider": "LSI-MegaCli"}
                
                if "Inquiry Data:" in line:
                    # 'Inquiry Data:       SEAGATE ST300MM0006     LS08S0K2B5NV    '
                    parts = line.split(":", 1)[1].strip().split()
                    if len(parts) >= 2:
                        raw_model = " ".join(parts[:-1])
                        clean_model = re.sub(r'^(ATA|NVMe)\s+', '', raw_model).strip()
                        m_parts = clean_model.split(None, 1)
                        if len(m_parts) > 1:
                            current_disk["manufacturer"] = m_parts[0]
                            current_disk["model"] = m_parts[1]
                        else:
                            current_disk["manufacturer"] = "Unknown"
                            current_disk["model"] = clean_model
                        current_disk["serial_number"] = parts[-1]
                
                if "Raw Size:" in line:
                    current_disk["size"] = line.split(":", 1)[1].strip().split("[")[0].strip()
                if "Media Type:" in line:
                    current_disk["type"] = line.split(":", 1)[1].strip()
                    
            if "serial_number" in current_disk: 
                disks.append(current_disk)
        if disks: return disks

    # 策略 3: HP ssacli / hpssacli / hpacucli
    hp_cli_path = run_cmd("which ssacli 2>/dev/null") or \
                  run_cmd("which hpssacli 2>/dev/null") or \
                  run_cmd("which hpacucli 2>/dev/null")
                  
    if not hp_cli_path:
        for p in ["/usr/sbin/ssacli", "/usr/sbin/hpssacli", "/usr/sbin/hpacucli"]:
            if os.path.exists(p):
                hp_cli_path = p
                break
                
    if hp_cli_path:
        out = run_cmd(hp_cli_path + " ctrl all show config detail")
        if out:
            current_disk = {}
            in_phys_block = False
            lines = out.split("\n")
            for i, line in enumerate(lines):
                strip_line = line.strip()
                
                # 开始捕捉物理硬盘块
                if strip_line.startswith("physicaldrive"):
                    if "serial_number" in current_disk:
                        if current_disk.get("manufacturer") != "Unknown" or current_disk.get("model") != "Unknown" or current_disk.get("serial_number") != "Unknown":
                            disks.append(current_disk)
                    current_disk = {
                        "provider": "HP-hpacucli" if "hpacucli" in hp_cli_path else "HP-ssacli",
                        "serial_number": "Unknown",
                        "model": "Unknown",
                        "size": "Unknown",
                        "manufacturer": "Unknown"
                    }
                    in_phys_block = True
                    
                # 遇到非硬盘主体特征或空行时果断闭卷（防止把扩展背板 Expander、逻辑盘等混入覆写）
                elif strip_line == "" or strip_line.startswith("logicaldrive") or strip_line.startswith("Array") or strip_line.startswith("Smart") or strip_line.startswith("Enclosure") or strip_line.startswith("Expander"):
                    if in_phys_block and "serial_number" in current_disk:
                        # 过滤掉 Mirror Group 虚拟盘（往往只带小括号和OK字样，没真实详情）
                        if current_disk.get("manufacturer") != "Unknown" or current_disk.get("model") != "Unknown" or current_disk.get("serial_number") != "Unknown":
                            disks.append(current_disk)
                        current_disk = {}
                    in_phys_block = False

                elif in_phys_block and strip_line.startswith("Serial Number:"):
                    current_disk["serial_number"] = strip_line.split(":", 1)[1].strip()
                elif in_phys_block and strip_line.startswith("Model:"):
                    raw_model = strip_line.split(":", 1)[1].strip()
                    # Clean up random prefixes like 'ATA     ', 'ATA ', etc.
                    clean_model = re.sub(r'^(ATA|NVMe)\s+', '', raw_model).strip()
                    # Extract Manufacturer and Model. First word is usually Manufacturer in dirty strings
                    parts = clean_model.split(None, 1)
                    if len(parts) > 1:
                        current_disk["manufacturer"] = parts[0].strip()
                        current_disk["model"] = parts[1].strip()
                    else:
                        current_disk["model"] = clean_model
                        up_model = clean_model.upper()
                        if up_model.startswith("ST"): current_disk["manufacturer"] = "Seagate"
                        elif up_model.startswith("WD"): current_disk["manufacturer"] = "Western Digital"
                        elif up_model.startswith("MZ"): current_disk["manufacturer"] = "Samsung"
                        elif up_model.startswith("HUS") or up_model.startswith("HUC"): current_disk["manufacturer"] = "HGST"
                        elif up_model.startswith("INTEL"): current_disk["manufacturer"] = "Intel"
                        else: current_disk["manufacturer"] = "Unknown"
                elif in_phys_block and strip_line.startswith("Size:"):
                    current_disk["size"] = strip_line.split(":", 1)[1].strip()
                elif in_phys_block and strip_line.startswith("Drive Type:"):
                    current_disk["type"] = strip_line.split(":", 1)[1].strip()
            
            if in_phys_block and "serial_number" in current_disk:
                if current_disk.get("manufacturer") != "Unknown" or current_disk.get("model") != "Unknown" or current_disk.get("serial_number") != "Unknown":
                    disks.append(current_disk)
        if disks: return disks

    # 策略 4: 兜底使用 lsblk (针对 HBA/NVMe/无RAID及虚拟机)
    # 注意: lsblk MODEL 字段可能含空格(如 "SSSTC ER2-CD960A")，必须用 -J 或 -P 模式避免误切割
    
    # 优先使用 JSON 模式 (需要 lsblk >= 2.27)
    lsblk_json = run_cmd("lsblk -d -J -o NAME,TYPE,SIZE,MODEL,SERIAL,VENDOR,ROTA -b 2>/dev/null")
    if lsblk_json:
        try:
            import json as _json
            lsblk_data = _json.loads(lsblk_json)
            for dev in lsblk_data.get("blockdevices", []):
                if dev.get("type") != "disk":
                    continue
                
                model_raw = (dev.get("model") or "").strip()
                vendor_raw = (dev.get("vendor") or "").strip()
                serial = (dev.get("serial") or "Unknown").strip()
                size_bytes = dev.get("size", 0)
                rota = dev.get("rota")  # 1=HDD, 0=SSD
                
                # 智能拆分厂商和型号
                manufacturer = "Unknown"
                model = model_raw
                
                if vendor_raw:
                    manufacturer = vendor_raw
                elif model_raw:
                    # 型号第一个词可能是厂商
                    m_parts = model_raw.split(None, 1)
                    if len(m_parts) > 1:
                        manufacturer = m_parts[0]
                        model = m_parts[1]
                
                # 厂商名归一化
                mfr_upper = manufacturer.upper()
                if "SSSTC" in mfr_upper: manufacturer = "SSSTC (赛盛技诺)"
                elif mfr_upper.startswith("ATA") or mfr_upper.startswith("SATA"): manufacturer = "Unknown"
                elif "SEAGATE" in mfr_upper or mfr_upper.startswith("ST"): manufacturer = "Seagate"
                elif mfr_upper.startswith("WD") or "WESTERN" in mfr_upper: manufacturer = "Western Digital"
                elif mfr_upper.startswith("MZ") or "SAMSUNG" in mfr_upper: manufacturer = "Samsung"
                elif "HGST" in mfr_upper or mfr_upper.startswith("HUS"): manufacturer = "HGST"
                elif "INTEL" in mfr_upper: manufacturer = "Intel"
                elif "TOSHIBA" in mfr_upper: manufacturer = "Toshiba"
                elif "MICRON" in mfr_upper: manufacturer = "Micron"
                elif "KIOXIA" in mfr_upper: manufacturer = "KIOXIA"
                elif "LITEON" in mfr_upper: manufacturer = "Lite-On"
                
                # 容量格式化
                size_str = "Unknown"
                if size_bytes:
                    size_bytes = int(size_bytes)
                    if size_bytes >= 1024 ** 4:
                        size_str = "{0:.2f} TB".format(size_bytes / (1024 ** 4))
                    elif size_bytes >= 1024 ** 3:
                        size_str = "{0:.0f} GB".format(size_bytes / (1024 ** 3))
                    else:
                        size_str = "{0:.0f} MB".format(size_bytes / (1024 ** 2))
                
                # 磁盘类型推断
                disk_type = "Unknown"
                if rota is not None:
                    disk_type = "HDD" if (rota == True or rota == 1 or str(rota) == "1") else "SSD"
                
                # 华为 2288H 等服务器的 SAS 磁盘，lsblk SERIAL 字段可能返回 WWN
                # 需通过 udevadm/smartctl/sg_inq 获取真实制造商序列号
                dev_name_str = dev.get("name", "")
                if _is_wwn(serial) and dev_name_str:
                    serial = _get_real_disk_sn(dev_name_str, serial)
                
                disks.append({
                    "name": dev_name_str or "?",
                    "manufacturer": manufacturer,
                    "model": model,
                    "serial_number": serial,
                    "size": size_str,
                    "type": disk_type,
                    "provider": "OS-lsblk"
                })
        except Exception:
            pass
    
    # 如果 JSON 模式不可用或失败，使用 -P (key=value pairs) 模式兜底
    if not disks:
        lsblk_pairs = run_cmd("lsblk -d -P -o NAME,TYPE,SIZE,MODEL,SERIAL -n 2>/dev/null")
        if lsblk_pairs:
            for line in lsblk_pairs.split("\n"):
                line = line.strip()
                if not line:
                    continue
                # 解析 KEY="VALUE" 格式
                fields = {}
                for m in re.finditer(r'(\w+)="([^"]*)"', line):
                    fields[m.group(1)] = m.group(2).strip()
                
                if fields.get("TYPE") != "disk":
                    continue
                
                model_raw = fields.get("MODEL", "Unknown")
                serial = fields.get("SERIAL", "Unknown")
                size = fields.get("SIZE", "Unknown")
                
                # 拆分厂商和型号
                manufacturer = "Unknown"
                model = model_raw
                m_parts = model_raw.split(None, 1)
                if len(m_parts) > 1:
                    manufacturer = m_parts[0]
                    model = m_parts[1]
                
                # 同样检测并修正 lsblk -P 模式下返回的 WWN
                dev_name_str = fields.get("NAME", "")
                if _is_wwn(serial) and dev_name_str:
                    serial = _get_real_disk_sn(dev_name_str, serial)
                
                disks.append({
                    "name": dev_name_str or "?",
                    "manufacturer": manufacturer,
                    "model": model,
                    "serial_number": serial,
                    "size": size,
                    "type": "Unknown",
                    "provider": "OS-lsblk"
                })

    # ---- RAID 虚拟盘识别并尝试穿透查询物理盘 ----
    # 识别到 RAID VD 后，将其标记为 is_raid_virtual_drive=True，同时尝试用
    # smartctl megaraid pass-through 获取背后的物理盘并一并并入列表。
    # 返回一个普通 list，不改变输出 JSON 结构。
    RAID_VD_MODEL_KEYWORDS = [
        "hw-sas", "hw_sas",          # 华为 HW-SAS3408 / HW-SAS3416
        "avago", "broadcom", "lsi",  # Broadcom/Avago/LSI MegaRAID 控制器
        "megaraid", "mr",             # LSI MegaRAID 虚拟盘标识
        "raid", "virtual",
    ]
    all_disks = []
    for d in disks:
        model_lower = d.get("model", "").lower()
        sn_val      = d.get("serial_number", "")
        is_raid_vd  = False
        # 1. NAA-6 (28位以上hex) → RAID 控制器 LUN，必不是物理盘
        if _is_wwn(sn_val) and len(sn_val.strip().lower().lstrip("0x")) >= 28:
            is_raid_vd = True
        # 2. 型号名含已知 RAID 控制器关键字
        if not is_raid_vd:
            for kw in RAID_VD_MODEL_KEYWORDS:
                if kw in model_lower:
                    is_raid_vd = True
                    break
        if is_raid_vd:
            d["is_raid_virtual_drive"] = True
            dev_name = d.get("name", "")
            # 尝试通过 smartctl megaraid pass-through 穿透查询物理盘
            probed = _probe_megaraid_physical_drives(dev_name) if dev_name else []
            if probed:
                all_disks.extend(probed)  # 先把物理盘放入
                d["note"] = (
                    "RAID virtual drive - {0} physical drive(s) found via "
                    "smartctl megaraid pass-through.".format(len(probed))
                )
            else:
                d["note"] = (
                    "RAID virtual drive - physical disks behind this controller are not "
                    "visible to the OS. Install storcli/storcli64 and re-run to enumerate "
                    "physical drives."
                )
        all_disks.append(d)  # RAID VD 本身也保留在列表中，已标记区分

    return all_disks  # 返回普通 list，不改变 JSON 输出结构

def get_gpu_info():
    """
    采集物理显卡/GPU 加速卡信息。
    策略：
      1. 通过 lspci 扫描 PCI 总线上的 VGA / 3D / Display controller 设备
      2. 对 NVIDIA 卡额外调用 nvidia-smi 获取显存、驱动版本、序列号
      3. 过滤掉主板集成的 AST2x00 等 BMC 远控显示芯片
    """
    gpus = []
    
    # 需要过滤的 BMC/远控虚拟显示芯片 (这些不是真正的计算GPU)
    bmc_keywords = ["ast2", "ast1", "matrox", "aspeed", "mgag200", "ilo", "idrac"]
    
    lspci_bin = run_cmd("which lspci 2>/dev/null").strip()
    if not lspci_bin:
        for p in ["/sbin/lspci", "/usr/sbin/lspci", "/usr/bin/lspci"]:
            if os.path.exists(p):
                lspci_bin = p
                break
    
    if not lspci_bin:
        return gpus
    
    # lspci -mm -nn 输出格式: 
    # Slot  Class  Vendor  Device  SVendor  SDevice  PhySlot  Rev
    # "03:00.0" "3D controller" "NVIDIA Corporation" "Tesla V100" ...
    out = run_cmd("{0} -mm -nn".format(lspci_bin))
    if not out:
        return gpus
    
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        
        # 只关注 VGA / 3D / Display controller 类设备
        line_lower = line.lower()
        if "vga" not in line_lower and "3d" not in line_lower and "display" not in line_lower:
            continue
        
        parts = re.findall(r'"(.*?)"', line)
        if len(parts) < 4:
            continue
        
        pci_class = parts[0].strip()
        vendor = parts[1].strip()
        device = parts[2].strip()
        
        # 提取 PCI Bus 地址 (行首的非引号部分)
        bus_addr_m = re.match(r'^(\S+)\s', line)
        bus_addr = bus_addr_m.group(1) if bus_addr_m else "Unknown"
        
        # 过滤 BMC/远控虚拟显示芯片
        combined_lower = (vendor + " " + device).lower()
        is_bmc = any(kw in combined_lower for kw in bmc_keywords)
        if is_bmc:
            continue
        
        gpu_info = {
            "name": device,
            "manufacturer": vendor,
            "pci_address": bus_addr,
            "vram": "Unknown",
            "driver_version": "Unknown",
            "serial_number": "Unknown"
        }
        
        gpus.append(gpu_info)
    
    # 对 NVIDIA 卡进一步调用 nvidia-smi 获取详细信息
    nvidia_smi = run_cmd("which nvidia-smi 2>/dev/null").strip()
    if nvidia_smi and gpus:
        # 查询所有 GPU: 名字, 显存, 驱动版本, 序列号
        smi_out = run_cmd("{0} --query-gpu=gpu_bus_id,name,memory.total,driver_version,serial --format=csv,noheader,nounits 2>/dev/null".format(nvidia_smi))
        if smi_out:
            for smi_line in smi_out.strip().split("\n"):
                smi_parts = [p.strip() for p in smi_line.split(",")]
                if len(smi_parts) >= 5:
                    smi_bus = smi_parts[0].lower()
                    smi_name = smi_parts[1]
                    smi_vram = smi_parts[2]
                    smi_driver = smi_parts[3]
                    smi_sn = smi_parts[4]
                    
                    # 尝试匹配到 lspci 已发现的 GPU
                    matched = False
                    for gpu in gpus:
                        # nvidia-smi 的 bus_id 格式: 00000000:3B:00.0 或 3B:00.0
                        if gpu["pci_address"].lower() in smi_bus or smi_bus.endswith(gpu["pci_address"].lower()):
                            gpu["name"] = smi_name
                            gpu["vram"] = "{0} MB".format(smi_vram) if smi_vram else "Unknown"
                            gpu["driver_version"] = smi_driver
                            if smi_sn and smi_sn not in ["N/A", "[N/A]", "None", ""]:
                                gpu["serial_number"] = smi_sn
                            matched = True
                            break
                    
                    # 如果 nvidia-smi 发现了 lspci 没扫到的卡 (极罕见)
                    if not matched:
                        gpus.append({
                            "name": smi_name,
                            "manufacturer": "NVIDIA Corporation",
                            "pci_address": smi_bus,
                            "vram": "{0} MB".format(smi_vram) if smi_vram else "Unknown",
                            "driver_version": smi_driver,
                            "serial_number": smi_sn if smi_sn not in ["N/A", "[N/A]", "None", ""] else "Unknown"
                        })
    
    return gpus

def main():
    parser = argparse.ArgumentParser(description='Server Hardware Info Collector')
    parser.add_argument('--server', type=str, help='Upload information to the specified HTTP API endpoint (e.g., http://192.168.1.100:8080/api/v1/upload_hwinfo)')
    args = parser.parse_args()

    # 刚启动时，第一时间验证是否为物理实体机
    check_is_physical_machine()

    print("\n--------------------------------------------------")
    print("Checking missing system utilities...")
    check_and_install_dependencies()
    print("--------------------------------------------------\n")

    print("Collecting System Info...")
    sys_info = get_system_info()
    
    # 无论是否有 --server，始终尝试安装 RAID 工具
    # 优先本地 tools/，其次远端服务器
    print("Checking and installing RAID management tools...")
    auto_install_raid_tools(sys_info, args.server)

    print("Collecting CPU Info...")
    cpu_info = get_cpu_info()
    
    print("Collecting Memory Info...")
    memory_info = get_memory_info()
    
    print("Collecting Network Info...")
    network_info = get_network_info()
    
    print("Collecting Disk Info (Detecting RAID Controllers)...")
    disk_info = get_disk_info()
    
    print("Collecting GPU Info...")
    gpu_info = get_gpu_info()
    
    final_data = {
        "system": sys_info,
        "cpu": cpu_info,
        "memory_modules": memory_info,
        "network_interfaces": network_info,
        "physical_disks": disk_info,
        "gpu_devices": gpu_info,
        "errors": COLLECTION_ERRORS
    }
    
    print("--------------------------------------------------")
    print("              Hardware Infomation                 ")
    print("--------------------------------------------------")
    # indent=4 guarantees readable JSON in both stdout and redirect files
    json_output = json.dumps(final_data, indent=4, sort_keys=False)
    print(json_output)
    
    if args.server:
        print("\n--------------------------------------------------")
        print("Uploading data to server: {0}".format(args.server))
        try:
            req = urllib_request.Request(args.server)
            req.add_header('Content-Type', 'application/json; charset=utf-8')
            response = urllib_request.urlopen(req, data=json_output.encode('utf-8'))
            print("[SUCCESS] Upload Successful! Server replied Code: {0}".format(response.getcode()))
        except Exception as e:
            print("[ERROR] Upload Failed: {0}".format(str(e)))
            sys.exit(1)

if __name__ == "__main__":
    if os.geteuid() != 0:
        print("[Warning] This script should be run as root to access DMI and RAID info properly.")
    main()
