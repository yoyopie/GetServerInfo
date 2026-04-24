# 硬件资产智能采集与管理系统 (GetServerInfo)

这是一个专为物理机、虚拟机及 ESXi 宿主机设计的**无侵入式硬件配置采集与可视化平台**。系统由集中式服务端负责接收展示，并能自动下发必备的 RAID 阵列卡检测工具，精准抓取最深度的硬件资产数据。

---

## 🎯 核心能力

- **深度物理层探测**：跨过 OS 虚拟层获取真实的内存条、显卡（GPU/算力卡）、不同阵列模式下的每块物理硬盘（SN/容量/厂商等）。
- **自动化阵列工具部署**：根据受控机的**型号（HP、Dell、浪潮、宁畅、曙光等）**和**系统类型（Debian/RHEL 系）**，探针会自动从服务端下载并安装 `storcli` 或 `ssacli`，无需人工准备。
- **ESXi 无代理直连**：对接 vSphere API 和 CIM/WBEM 底层接口，无需在 ESXi 内核装任何插件即可抓取内存拓扑和直通硬件。
- **动态大屏展示与导出**：内置实时 Web 数据监控大屏，并支持一键导出高度格式化的归一化 CSV 表格。

---

## 🏗️ 架构与文件结构

- `receiver.py`：核心服务端。提供数据接收API、工具下载伺服、数据库连通，以及承载前端大屏。
- `collector.py`：Linux 物理机采集探针。自动嗅探硬件并 POST 数据给服务端。
- `esxi_scanner.py`：ESXi 远程扫描器。通过 vCenter/ESXi API 远程提取硬件数据。
- `dashboard.html`：前端大屏界面文件。
- `worker.sh`：批量执行辅助脚本（可通过 SSH 结合多线程自动推拉数据）。
- `tools/` 目录：存放各种自动化部署的 rpm/deb 阵列驱动。

---

## 🚀 部署与使用指南

### 1. 启动服务端
服务端可以独立运行（会使用本地 JSON 文件兜底保存），或者如果有 PostgreSQL 数据库，它会自动使用 Postgres 进行结构化存储。
```bash
# 启动服务端 (默认监听 8080 端口)
python3 receiver.py
```
> **注意**：大屏页面地址为 `http://您的服务器IP:8080/`。

### 2. Linux 物理机/虚拟机采集 (探针模式)
**单台机器直接执行：**
```bash
# 自动从服务端拉取脚本并执行采集
curl -so /tmp/collector.py http://<YOUR_SERVER_IP>:8080/tools/collector.py && sudo python /tmp/collector.py --server http://<YOUR_SERVER_IP>:8080/api/v1/upload_hwinfo
```

**大批量执行 (使用 worker.sh)：**
通过准备 IP 列表文件，配合多线程 `xargs` 可以快速扫过数百台机器。
```bash
# 准备一个包含要扫描的 IP 列表文件 ip_list.txt
cat ip_list.txt | xargs -n1 -P50 ./worker.sh
```

### 3. VMware ESXi 扫描 (无代理模式)
由于 ESXi 闭源且缺少标准工具包，我们提供了独立的远程提取方案：
```bash
# 启动扫描 (按提示输入密码或传参)
python3 esxi_scanner.py <ESXi管理IP> <账号> <密码>
```
> ESXi **必须开启了 CIM 服务** 才能捕获精确的内存拓扑。你可以通过 vSphere 开启 `sfcbd-watchdog` 服务。

---

## 🔧 前置要求与工具包说明

为了让 `collector.py` 能自动下载 RAID 工具，你需要确保与 `receiver.py` 同级的 `tools/` 目录下放置好以下核心解析文件（系统会自动映射）：

- `ssacli-5.10-44.0.x86_64.rpm` (惠普系 CentOS)
- `ssacli-5.10-44.0_amd64.deb` (惠普系 Ubuntu)
- `storcli.rpm` (浪潮、戴尔、联想、宁畅 等可用 CentOS)
- `storcli.deb` (此等品牌系 Ubuntu 可用)
- **`collector.py`** 探针自身的副本（必须复制一份进来，保证 `curl` 时能下到最新版）

*更新探针的快速命令:* `cp collector.py tools/collector.py`

---

## 📊 数据导出 (导出 CMDB)

您可以在前端大屏右上角点击【📥 导出 CSV】按钮，或直接通过接口下载：
```bash
wget http://<YOUR_SERVER_IP>:8080/api/v1/export_csv
```

导出格式经过大量归一化清洗：
1. 内存大小自适应转化为 **GB**。
2. 网卡同型号合并计算，过滤出真实物理网卡，并提取真实速率（如 **1Gb/s**, **10Gb/s**）。
3. 磁盘呈现 **Slot0/Slot1** 格式供即读。
