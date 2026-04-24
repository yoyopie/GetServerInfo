#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Lightweight Server Hardware Info Receiver
兼容 Python 2 / 3
自动接收来自 collector.py 推送过来的硬件配置，存入当前目录下 server_hw_data/ 文件夹内。
"""

import os
import json
import time

try:
    import psycopg2
    HAS_PG = True
except ImportError:
    HAS_PG = False

# ==========================================
# POSTGRESQL DATABASE CONFIGURATION
# ==========================================
PG_CONFIG = {
    "dbname": "my_database",
    "user": "postgres",
    "password": "mysecretpassword",
    "host": "127.0.0.1",
    "port": 5432
}

def get_db_conn():
    if not HAS_PG: return None
    try:
        return psycopg2.connect(**PG_CONFIG)
    except Exception as e:
        print("[DB_ERROR] Connection failed: " + str(e))
        return None

def init_db():
    conn = get_db_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS server_assets (
                        server_sn VARCHAR(100) PRIMARY KEY,
                        manufacturer VARCHAR(100),
                        product_name VARCHAR(100),
                        ip_address VARCHAR(50),
                        raw_hw_data JSONB,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    );
                """)
            conn.commit()
            print("[SUCCESS] PostgreSQL DB connected & verified table 'server_assets'.")
        except Exception as e:
            print("[DB_ERROR] Init table failed: " + str(e))
        finally:
            conn.close()
    else:
        print("[WARNING] PostgreSQL driver (psycopg2) missing or connection failed. Using ONLY local JSON file storage as fallback.")

def calc_total_mem_gb(mems):
    cur_gb = 0.0
    for m in mems:
        s = str(m.get("size", "")).upper()
        parts = s.split()
        if len(parts) >= 2 and parts[0].isdigit():
            val = float(parts[0])
            unit = parts[1]
            if "TB" in unit:
                cur_gb += val * 1024
            elif "GB" in unit:
                cur_gb += val
            elif "MB" in unit:
                cur_gb += (val / 1024.0)
            elif "KB" in unit:
                cur_gb += (val / 1048576.0)
    return round(cur_gb, 2)

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
except ImportError:
    # 兼容 Python 2
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer

PORT = 8080
SAVE_DIR = "server_hw_data"
TOOLS_DIR = "tools"

class HardwareInfoHandler(BaseHTTPRequestHandler):
    def _set_response(self, code=200, content_type='application/json'):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/tools/"):
            filename = os.path.basename(self.path)
            filepath = os.path.join(TOOLS_DIR, filename)
            
            if os.path.exists(filepath) and os.path.isfile(filepath):
                try:
                    with open(filepath, 'rb') as f:
                        self._set_response(200, 'application/octet-stream')
                        self.wfile.write(f.read())
                    print("[INFO] Served file {} to client.".format(filename))
                except Exception as e:
                    self._set_response(500, 'text/plain')
                    self.wfile.write(b"Error reading file")
            else:
                self._set_response(404, 'text/plain')
                self.wfile.write(b"File not found")
            return
            
        if self.path == "/" or self.path == "/index.html":
            filepath = "dashboard.html"
            if os.path.exists(filepath):
                with open(filepath, 'rb') as f:
                    self._set_response(200, 'text/html; charset=utf-8')
                    self.wfile.write(f.read())
            else:
                self._set_response(404, 'text/plain')
                self.wfile.write(b"Dashboard not found. Please create dashboard.html")
            return
            
        if self.path == "/api/v1/nodes":
            nodes = []
            conn = get_db_conn()
            if conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT server_sn, manufacturer, product_name, ip_address, raw_hw_data FROM server_assets ORDER BY updated_at DESC")
                        for row in cur.fetchall():
                            sn, manu, prod, ip, hw_data = row
                            
                            mems = hw_data.get("memory_modules", [])
                            total_mem_gb = calc_total_mem_gb(mems)
                            
                            nodes.append({
                                "sn": sn,
                                "manufacturer": manu,
                                "product": prod,
                                "cpu_model": hw_data.get("cpu", {}).get("model", "Unknown"),
                                "ip": ip,
                                "total_memory_gb": total_mem_gb,
                                "disk_count": len(hw_data.get("physical_disks", [])),
                                "net_count": len(hw_data.get("network_interfaces", [])),
                                "errors": len(hw_data.get("errors", []))
                            })
                except Exception as e:
                    print("[DB_ERROR] Failed to fetch nodes: " + str(e))
                finally:
                    conn.close()
            else:
                # DB Fallback: Use flat-files
                if os.path.exists(SAVE_DIR):
                    for f in os.listdir(SAVE_DIR):
                        if f.endswith('.json'):
                            try:
                                with open(os.path.join(SAVE_DIR, f), 'r') as jf:
                                    data = json.load(jf)
                                    sys_data = data.get("system", {})
                                    mems = data.get("memory_modules", [])
                                    total_mem_gb = calc_total_mem_gb(mems)
                                    nodes.append({
                                        "sn": sys_data.get("serial_number", "Unknown"),
                                        "manufacturer": sys_data.get("manufacturer", "Unknown"),
                                        "product": sys_data.get("product_name", "Unknown"),
                                        "cpu_model": data.get("cpu", {}).get("model", "Unknown"),
                                        "ip": sys_data.get("ip_address", ""),
                                        "total_memory_gb": total_mem_gb,
                                        "disk_count": len(data.get("physical_disks", [])),
                                        "net_count": len(data.get("network_interfaces", [])),
                                        "errors": len(data.get("errors", []))
                                    })
                            except Exception:
                                pass
            
            self._set_response(200, 'application/json')
            self.wfile.write(json.dumps({"status": "success", "data": nodes}).encode('utf-8'))
            return
            
        if self.path.startswith("/api/v1/node/"):
            sn_req = self.path.split("/")[-1]
            
            conn = get_db_conn()
            if conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT raw_hw_data FROM server_assets WHERE server_sn = %s", (sn_req,))
                        row = cur.fetchone()
                        if row:
                            self._set_response(200, 'application/json')
                            self.wfile.write(json.dumps({"status": "success", "data": row[0]}).encode('utf-8'))
                            return
                except Exception as e:
                    print("[DB_ERROR] Node fetch failed: " + str(e))
                finally:
                    conn.close()
            
            # DB Fallback: local file
            if os.path.exists(SAVE_DIR):
                for f in os.listdir(SAVE_DIR):
                    if f == "SN_{}.json".format(sn_req):
                        try:
                            with open(os.path.join(SAVE_DIR, f), 'r') as jf:
                                data = json.load(jf)
                            self._set_response(200, 'application/json')
                            self.wfile.write(json.dumps({"status": "success", "data": data}).encode('utf-8'))
                            return
                        except Exception:
                            pass
            self._set_response(404, 'application/json')
            self.wfile.write(b'{"status":"error", "message": "Node not found"}')
            return
            
        if self.path == "/api/v1/export_csv":
            import csv
            import io
            
            all_data = []
            conn = get_db_conn()
            if conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT server_sn, raw_hw_data FROM server_assets ORDER BY updated_at DESC")
                        for row in cur.fetchall():
                            all_data.append({"sn": row[0], "data": row[1]})
                except Exception as e:
                    print("[DB_ERROR] CSV export failed: " + str(e))
                finally:
                    conn.close()
            else:
                if os.path.exists(SAVE_DIR):
                    for f in sorted(os.listdir(SAVE_DIR)):
                        if f.endswith('.json'):
                            try:
                                with open(os.path.join(SAVE_DIR, f), 'r') as jf:
                                    data = json.load(jf)
                                    sn = data.get("system", {}).get("serial_number", "Unknown")
                                    all_data.append({"sn": sn, "data": data})
                            except Exception:
                                pass
            
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["SN", "IP", "厂商/型号", "CPU", "内存", "网卡", "磁盘", "显卡"])
            
            for item in all_data:
                sn = item["sn"]
                d = item["data"]
                sys_info = d.get("system", {})
                cpu = d.get("cpu", {})
                mems = d.get("memory_modules", [])
                nics = d.get("network_interfaces", [])
                disks = d.get("physical_disks", [])
                gpus = d.get("gpu_devices", [])
                
                # IP
                ip = sys_info.get("ip_address", "")
                
                # 厂商/型号
                mfr_model = "{} {}".format(sys_info.get("manufacturer", ""), sys_info.get("product_name", "")).strip()
                
                # CPU: Intel(R) Xeon(R) Silver 4310 CPU @ 2.10GHz *2
                cpu_model = cpu.get("model", "Unknown")
                cpu_count = cpu.get("physical_count", 1) or 1
                cpu_str = "{} *{}".format(cpu_model, cpu_count) if cpu_count > 1 else cpu_model
                
                # 内存: 按相同容量分组，如 "64 GB *4；32 GB *2"
                # 内存容量归一化：MB -> GB
                def norm_mem_size(s):
                    import re as _re
                    m = _re.match(r'^(\d+)\s*(MB|GB|TB|KB)', s, _re.IGNORECASE)
                    if m:
                        val, unit = int(m.group(1)), m.group(2).upper()
                        if unit == 'MB' and val >= 1024: return "{} GB".format(val // 1024)
                        if unit == 'KB' and val >= 1048576: return "{} GB".format(val // 1048576)
                        if unit == 'TB': return "{} GB".format(val * 1024)
                    return s

                mem_groups = {}
                for m in mems:
                    size = norm_mem_size(m.get("size", "Unknown"))
                    mem_groups[size] = mem_groups.get(size, 0) + 1
                mem_parts = []
                for size, count in mem_groups.items():
                    if count > 1:
                        mem_parts.append("{} *{}".format(size, count))
                    else:
                        mem_parts.append(size)
                mem_str = "；".join(mem_parts) if mem_parts else "Unknown"
                
                # 网卡: 按型号分组，类似 CPU 的格式 "Broadcom BCM5720 1Gb/s *2；Intel X710 10Gb/s *2"
                nic_groups = {}
                for n in nics:
                    mfr = n.get("manufacturer", "")
                    model = n.get("model", "")
                    speed = n.get("speed", "")
                    nic_desc = "{} {}".format(mfr, model).strip()
                    if speed and speed != "Unknown":
                        nic_desc += " " + speed
                    nic_groups[nic_desc] = nic_groups.get(nic_desc, 0) + 1
                nic_parts = []
                for desc, count in nic_groups.items():
                    if count > 1:
                        nic_parts.append("{} *{}".format(desc, count))
                    else:
                        nic_parts.append(desc)
                nic_str = "；".join(nic_parts) if nic_parts else ""
                
                # 磁盘: Slot0 1.1T｜Logical Volume SN；Slot1 894.3G｜SSSTC ER2-CD960A SN
                disk_parts = []
                for idx, dd in enumerate(disks):
                    size = dd.get("size", "")
                    mfr = dd.get("manufacturer", "")
                    model = dd.get("model", "")
                    serial = dd.get("serial_number", "")
                    desc = "{} {}".format(mfr, model).strip()
                    if serial and serial != "Unknown":
                        desc += " " + serial
                    disk_parts.append("Slot{} {}｜{}".format(idx, size, desc).strip())
                disk_str = "；".join(disk_parts) if disk_parts else ""
                
                # 显卡: NVIDIA Tesla V100 32510MB
                gpu_parts = []
                for g in gpus:
                    name = g.get("name", "")
                    mfr = g.get("manufacturer", "")
                    vram = g.get("vram", "")
                    gpu_desc = "{} {}".format(mfr, name).strip()
                    if vram and vram != "Unknown":
                        gpu_desc += " " + vram
                    gpu_parts.append(gpu_desc)
                gpu_str = "；".join(gpu_parts) if gpu_parts else ""
                
                writer.writerow([sn, ip, mfr_model, cpu_str, mem_str, nic_str, disk_str, gpu_str])
            
            csv_bytes = output.getvalue().encode('utf-8-sig')  # BOM 头确保 Excel 正确识别中文
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="hardware_inventory.csv"')
            self.end_headers()
            self.wfile.write(csv_bytes)
            print("[INFO] CSV export: {} records".format(len(all_data)))
            return
            
        self._set_response(404)
        self.wfile.write(b'{"status":"error", "message": "Not Found"}')

    def do_POST(self):
        if self.path != "/api/v1/upload_hwinfo":
            self._set_response(404)
            self.wfile.write(b'{"status":"error", "message": "Not Found"}')
            return

        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self._set_response(400)
            self.wfile.write(b'{"status":"error", "message": "Empty body"}')
            return

        post_data = self.rfile.read(content_length)
        
        try:
            # 兼容 python2 和 3 的 JSON load
            payload = json.loads(post_data.decode('utf-8'))
        except Exception as e:
            print("Failed to decode JSON: " + str(e))
            self._set_response(400)
            self.wfile.write(b'{"status":"error", "message": "Invalid JSON format"}')
            return

        # 提取序列号和数据
        sn = payload.get("system", {}).get("serial_number", "Unknown_SN")
        
        # 处理可能的非法文件名字符
        sn_safe = "".join(c for c in sn if c.isalnum() or c in ('-', '_'))
        if not sn_safe:
            sn_safe = "Unknown_SN"
            
        # 确保保存目录存在
        if not os.path.exists(SAVE_DIR):
            os.makedirs(SAVE_DIR)

        filepath = os.path.join(SAVE_DIR, "SN_{}.json".format(sn_safe))
        
        try:
            # 1. Fallback / Resilient save to File System
            with open(filepath, "w") as f:
                json.dump(payload, f, indent=4, sort_keys=False)
            print("[INFO] Local JSON Backup: {} -> {}".format(sn_safe, filepath))
            
            # 2. Main save to PostgreSQL
            conn = get_db_conn()
            if conn:
                try:
                    sys_data = payload.get("system", {})
                    manu = sys_data.get("manufacturer", "Unknown")
                    prod = sys_data.get("product_name", "Unknown")
                    ip = sys_data.get("ip_address", "")
                    raw_hw = json.dumps(payload)
                    
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO server_assets (server_sn, manufacturer, product_name, ip_address, raw_hw_data)
                            VALUES (%s, %s, %s, %s, %s::jsonb)
                            ON CONFLICT (server_sn) DO UPDATE SET
                                manufacturer = EXCLUDED.manufacturer,
                                product_name = EXCLUDED.product_name,
                                ip_address = EXCLUDED.ip_address,
                                raw_hw_data = EXCLUDED.raw_hw_data,
                                updated_at = NOW();
                        """, (sn_safe, manu, prod, ip, raw_hw))
                    conn.commit()
                    print("[SUCCESS] Upserted node '{}' to PostgreSQL.".format(sn_safe))
                except Exception as db_err:
                    print("[DB_ERROR] Failed to save to PostgreSQL: " + str(db_err))
                finally:
                    conn.close()
            
            self._set_response(200)
            self.wfile.write(b'{"status":"success"}')
        except Exception as e:
            print("[ERROR] Failed to save file: " + str(e))
            self._set_response(500)
            self.wfile.write(b'{"status":"error", "message": "Failed to save file"}')

def run_server():
            
    # Initialize DB (if psycopg2 available and DB reachable)
    init_db()

    if not os.path.exists(TOOLS_DIR):
        os.makedirs(TOOLS_DIR)
        print("[INIT] Created empty tools directory. Put RPM/DEB files here.")

    server_address = ('', PORT)
    httpd = HTTPServer(server_address, HardwareInfoHandler)
    print("=======================================")
    print("Receiver Server Started on port {}".format(PORT))
    print("Waiting for data from collector.py...")
    print("Save Directory: ./{}".format(SAVE_DIR))
    print("API Endpoint: POST /api/v1/upload_hwinfo")
    print("Tools Repo: GET /tools/<filename>")
    print("=======================================")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Server...")
    finally:
        httpd.server_close()

if __name__ == '__main__':
    run_server()
