#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
QER Web UI Server - 基于 Python 内置 http.server 的 Web 操作与可视化服务
不依赖 flask；前端使用 plotly.js CDN 绘图。

用法：
    python web_ui.py [port]    # 默认端口 8080

路由：
    GET  /                → 操作页面（index.html）
    GET  /visualize       → 可视化页面（visualize.html）
    GET  /api/results     → 解析 out_fun3.txt + out_fun4.txt，返回 JSON
    GET  /api/ephemeris   → 解析 out_fun3.eph，返回 JSON
    POST /api/run         → 运行 QER.exe（接收 JSON 配置）
"""

import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


# MJD 历元：1858-11-17 00:00:00 UTC
_MJD_EPOCH = datetime(1858, 11, 17, 0, 0, 0)


def fortran_date_to_mjd(date_str):
    """将 Fortran 输出的日期字符串转为 MJD。
    支持格式（单数字月/日/时/分/秒可能带前导空格）：
        '2021- 4-10T10: 4: 0.82'
        '2021- 4- 9T14:56: 8.09'
        '2021- 4-10T12:40:18.32'
        '2021-04-10T10:04:00.82'
    """
    if not date_str:
        return None
    m = re.match(
        r"(\d{4})-\s*(\d{1,2})-\s*(\d{1,2})T\s*(\d{1,2}):\s*(\d{1,2}):\s*(\d+(?:\.\d+)?)",
        date_str.strip()
    )
    if not m:
        return None
    try:
        year = int(m.group(1))
        month = int(m.group(2))
        day = int(m.group(3))
        hour = int(m.group(4))
        minute = int(m.group(5))
        second_float = float(m.group(6))
        second = int(second_float)
        micro = int(round((second_float - second) * 1e6))
        dt = datetime(year, month, day, hour, minute, second, micro)
        return (dt - _MJD_EPOCH).total_seconds() / 86400.0
    except (ValueError, OverflowError):
        return None

# QER 项目根目录（web_ui.py 所在目录即系统根目录）
QER_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(QER_DIR, "web")
OUTPUT_DIR = os.path.join(QER_DIR, "output")
QER_EXE = os.path.join(QER_DIR, "QER.exe")

# 默认结果文件路径
FUN3_OUT = os.path.join(OUTPUT_DIR, "out_fun3.txt")
FUN4_OUT = os.path.join(OUTPUT_DIR, "out_fun4.txt")
FUN3_EPH = os.path.join(OUTPUT_DIR, "out_fun3.eph")

# ReentryProgram 结果文件
REENTRY_B = os.path.join(QER_DIR, "TleReentry", "TleReentry_retimes-last.txt")


def parse_fun3_results(filepath):
    """解析 Function3 输出文件，返回 list[dict]"""
    results = []
    if not os.path.exists(filepath):
        return results, "文件不存在: " + filepath
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 8:
                    results.append({
                        "objId": parts[0],
                        "mjdEpoch": float(parts[1]),
                        "mjdReentry": float(parts[2]),
                        "reentryDate": parts[3],
                        "lifeDays": float(parts[4]),
                        "lat": float(parts[5]),
                        "lon": float(parts[6]),
                        "alt": float(parts[7]),
                    })
        return results, None
    except Exception as e:
        return results, str(e)


def parse_reentry_program_file(filepath):
    """解析 ReentryProgram 输出文件（21retimes-last.txt 格式），返回 list[dict]
    文件格式：每个 TLE 历元以 'epoch date YYYY- M-DDTHH: MM: SS.SS' 行起始，
    后跟 10 行 'BC mjdReentry dateStr lat lon alt' 数据（dateStr 可能含空格）。
    """
    results = []
    current_epoch_date = None
    current_epoch_mjd = None
    epoch_index = -1
    if not os.path.exists(filepath):
        return results, "文件不存在: " + filepath
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("epoch date"):
                    current_epoch_date = line[len("epoch date"):].strip()
                    current_epoch_mjd = fortran_date_to_mjd(current_epoch_date)
                    epoch_index += 1
                    continue
                parts = line.split()
                if len(parts) >= 6:
                    try:
                        bc = float(parts[0])
                        mjdReentry = float(parts[1])
                        # dateStr 可能含空格（'2021- 4-10T12:40:18.32'），用 rsplit 取最后3个为 lat/lon/alt
                        alt = float(parts[-1])
                        lon = float(parts[-2])
                        lat = float(parts[-3])
                        dateStr = " ".join(parts[2:-3])
                        if 50000 < mjdReentry < 80000:
                            life_days = None
                            if current_epoch_mjd is not None:
                                life_days = mjdReentry - current_epoch_mjd
                            results.append({
                                "bc": bc,
                                "mjdReentry": mjdReentry,
                                "dateStr": dateStr,
                                "lat": lat,
                                "lon": lon,
                                "alt": alt,
                                "epochDate": current_epoch_date or "",
                                "mjdEpoch": current_epoch_mjd,
                                "epochIndex": epoch_index,
                                "lifeDays": life_days,
                            })
                    except (ValueError, IndexError):
                        continue
        return results, None
    except Exception as e:
        return results, str(e)


def parse_fun4_results(filepath):
    """解析 Function4 输出文件，返回 dict"""
    result = {
        "fileA": "", "fileB": "",
        "countA": 0, "countB": 0,
        "bStats": {},
        "comparison": [],
        "diffStats": {},
        "conclusion": ""
    }
    if not os.path.exists(filepath):
        return result, "文件不存在: " + filepath
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()

        section = ""
        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Section 切换检测（注释行中的关键字）
            if "来源B 统计摘要" in line:
                section = "bStats"
                continue
            elif "逐目标对比" in line:
                section = "comparison"
                continue
            elif "对比统计" in line:
                section = "diffStats"
                continue
            elif "总体评估" in line:
                section = "conclusion"
                continue

            is_comment = line.startswith("#")

            if is_comment:
                if line.startswith("# 文件A"):
                    result["fileA"] = line
                elif line.startswith("# 文件B"):
                    result["fileB"] = line
                elif section == "bStats":
                    result["bStats"].setdefault("_lines", []).append(line)
                elif section == "diffStats":
                    result["diffStats"].setdefault("_lines", []).append(line)
                elif section == "conclusion":
                    result["conclusion"] += line + "\n"
            else:
                # 数据行
                if section == "comparison":
                    parts = line.split()
                    if len(parts) >= 8:
                        try:
                            result["comparison"].append({
                                "objId": parts[0],
                                "mjdA": float(parts[1]),
                                "mjdB": float(parts[2]),
                                "diff": float(parts[3]),
                                "absDiff": float(parts[4]),
                                "duration": " ".join(parts[5:8]),
                                "dateA": parts[8] if len(parts) > 8 else "",
                                "dateB": parts[9] if len(parts) > 9 else "",
                            })
                        except (ValueError, IndexError):
                            pass
        return result, None
    except Exception as e:
        return result, str(e)


def parse_ephemeris(filepath):
    """解析星历文件，返回 list[dict]，每个 dict 含 objId, mjdEpoch, mjdReentry, lifeDays, points"""
    targets = []
    if not os.path.exists(filepath):
        return targets, "文件不存在: " + filepath
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()

        current = None
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                if line.startswith("# objID:"):
                    # 新目标开始
                    if current is not None:
                        targets.append(current)
                    # 解析 "# objID: 44240  mjdEpoch: 59313.2  mjdReentry: 59317.7  lifeDays: 4.46528"
                    parts = line.split()
                    current = {
                        "objId": parts[2] if len(parts) > 2 else "",
                        "mjdEpoch": float(parts[4]) if len(parts) > 4 else 0,
                        "mjdReentry": float(parts[6]) if len(parts) > 6 and parts[6] != "NaN" else None,
                        "lifeDays": float(parts[8]) if len(parts) > 8 and parts[8] != "NaN" else None,
                        "points": []
                    }
                continue
            parts = line.split()
            if len(parts) >= 3 and current is not None:
                current["points"].append({
                    "mjd": float(parts[0]),
                    "alt": float(parts[1]),
                    "phase": parts[2]
                })
        if current is not None:
            targets.append(current)
        return targets, None
    except Exception as e:
        return targets, str(e)


def run_qer(config_path):
    """运行 QER.exe，返回 (returncode, stdout, stderr)"""
    if not os.path.exists(QER_EXE):
        return -1, "", "QER.exe 不存在: " + QER_EXE
    try:
        proc = subprocess.run(
            [QER_EXE, config_path],
            capture_output=True, text=True, timeout=300,
            cwd=QER_DIR
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "运行超时（300秒）"
    except Exception as e:
        return -1, "", str(e)


def _mean(values):
    """计算均值，空列表返回 None"""
    if not values:
        return None
    return sum(values) / len(values)


def _std(values):
    """计算样本标准差，空列表返回 None"""
    if len(values) < 2:
        return None
    m = sum(values) / len(values)
    var = sum((v - m) ** 2 for v in values) / (len(values) - 1)
    return var ** 0.5


def build_unified_comparison(fun3_data, reentry_b_data):
    """构建按 TLE 历元匹配的统一对比表。
    将 Function3（单 TLE SGP4 外推）与 ReentryProgram（多 BC + NRLMSISE-00）
    按同一 TLE 历元配对，输出每个历元下两种方法的陨落时刻与轨道寿命。
    匹配策略：按 mjdEpoch 最近邻配对（容差 0.01 天 ≈ 14.4 分钟）。
    """
    if not fun3_data and not reentry_b_data:
        return []

    # 将 ReentryProgram 按 epochIndex 分组
    b_groups = {}
    for rec in reentry_b_data:
        idx = rec.get("epochIndex", 0)
        b_groups.setdefault(idx, []).append(rec)

    # 为每个分组计算统计量
    b_summary = []
    for idx, group in sorted(b_groups.items()):
        if not group:
            continue
        mjd_epochs = [g["mjdEpoch"] for g in group if g.get("mjdEpoch") is not None]
        mjd_reentries = [g["mjdReentry"] for g in group]
        life_days = [g["lifeDays"] for g in group if g.get("lifeDays") is not None]
        bcs = [g["bc"] for g in group]
        b_summary.append({
            "epochIndex": idx,
            "epochDate": group[0].get("epochDate", ""),
            "mjdEpoch": _mean(mjd_epochs),
            "mjdReentryMean": _mean(mjd_reentries),
            "mjdReentryMin": min(mjd_reentries) if mjd_reentries else None,
            "mjdReentryMax": max(mjd_reentries) if mjd_reentries else None,
            "mjdReentryStd": _std(mjd_reentries),
            "lifeDaysMean": _mean(life_days),
            "lifeDaysMin": min(life_days) if life_days else None,
            "lifeDaysMax": max(life_days) if life_days else None,
            "bcMin": min(bcs) if bcs else None,
            "bcMax": max(bcs) if bcs else None,
            "bcCount": len(group),
        })

    # 按 mjdEpoch 最近邻匹配 Function3 记录
    tolerance = 0.01  # 天
    used_a = set()
    rows = []
    for b in b_summary:
        best_a = None
        best_diff = tolerance + 1
        for i, a in enumerate(fun3_data):
            if i in used_a:
                continue
            if a.get("mjdEpoch") is None or b["mjdEpoch"] is None:
                continue
            diff = abs(a["mjdEpoch"] - b["mjdEpoch"])
            if diff < best_diff:
                best_diff = diff
                best_a = i
        if best_a is not None:
            used_a.add(best_a)
            a = fun3_data[best_a]
            diff_days = None
            if a.get("mjdReentry") is not None and b["mjdReentryMean"] is not None:
                diff_days = a["mjdReentry"] - b["mjdReentryMean"]
            rows.append({
                "epochIndex": b["epochIndex"],
                "objId": a.get("objId", ""),
                "mjdEpoch": a.get("mjdEpoch"),
                "epochDate": b.get("epochDate", ""),
                # 方法A：Function3（单TLE SGP4）
                "methodA_name": "Function3 (单TLE SGP4)",
                "methodA_mjdReentry": a.get("mjdReentry"),
                "methodA_reentryDate": a.get("reentryDate", ""),
                "methodA_lifeDays": a.get("lifeDays"),
                # 方法B：ReentryProgram（多BC + NRLMSISE-00）
                "methodB_name": "ReentryProgram (多BC+NRLMSISE-00)",
                "methodB_mjdReentryMean": b["mjdReentryMean"],
                "methodB_mjdReentryMin": b["mjdReentryMin"],
                "methodB_mjdReentryMax": b["mjdReentryMax"],
                "methodB_mjdReentryStd": b["mjdReentryStd"],
                "methodB_lifeDaysMean": b["lifeDaysMean"],
                "methodB_bcRange": "{0:.6f}~{1:.6f}".format(b["bcMin"], b["bcMax"]) if b["bcMin"] is not None else "",
                "methodB_bcCount": b["bcCount"],
                # 差异
                "diffDays": diff_days,
                "absDiffDays": abs(diff_days) if diff_days is not None else None,
            })
        else:
            # 无匹配的 ReentryProgram 历元
            rows.append({
                "epochIndex": b["epochIndex"],
                "objId": "",
                "mjdEpoch": b["mjdEpoch"],
                "epochDate": b.get("epochDate", ""),
                "methodA_name": "Function3 (单TLE SGP4)",
                "methodA_mjdReentry": None,
                "methodA_reentryDate": "（无匹配）",
                "methodA_lifeDays": None,
                "methodB_name": "ReentryProgram (多BC+NRLMSISE-00)",
                "methodB_mjdReentryMean": b["mjdReentryMean"],
                "methodB_mjdReentryMin": b["mjdReentryMin"],
                "methodB_mjdReentryMax": b["mjdReentryMax"],
                "methodB_mjdReentryStd": b["mjdReentryStd"],
                "methodB_lifeDaysMean": b["lifeDaysMean"],
                "methodB_bcRange": "{0:.6f}~{1:.6f}".format(b["bcMin"], b["bcMax"]) if b["bcMin"] is not None else "",
                "methodB_bcCount": b["bcCount"],
                "diffDays": None,
                "absDiffDays": None,
            })

    # 处理未匹配的 Function3 记录
    for i, a in enumerate(fun3_data):
        if i not in used_a:
            rows.append({
                "epochIndex": -1,
                "objId": a.get("objId", ""),
                "mjdEpoch": a.get("mjdEpoch"),
                "epochDate": "",
                "methodA_name": "Function3 (单TLE SGP4)",
                "methodA_mjdReentry": a.get("mjdReentry"),
                "methodA_reentryDate": a.get("reentryDate", ""),
                "methodA_lifeDays": a.get("lifeDays"),
                "methodB_name": "ReentryProgram (多BC+NRLMSISE-00)",
                "methodB_mjdReentryMean": None,
                "methodB_mjdReentryMin": None,
                "methodB_mjdReentryMax": None,
                "methodB_mjdReentryStd": None,
                "methodB_lifeDaysMean": None,
                "methodB_bcRange": "",
                "methodB_bcCount": 0,
                "diffDays": None,
                "absDiffDays": None,
            })

    # 按 mjdEpoch 升序排序
    rows.sort(key=lambda r: r.get("mjdEpoch") or 0)
    return rows


class QERHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    def log_message(self, format, *args):
        # 简化日志输出
        sys.stdout.write("[Web] %s - %s\n" % (self.command, self.path))

    def _send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, filepath, code=200):
        if not os.path.exists(filepath):
            self._send_json({"error": "文件不存在: " + filepath}, 404)
            return
        with open(filepath, "rb") as f:
            body = f.read()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, filepath, content_type="application/octet-stream"):
        if not os.path.exists(filepath):
            self._send_json({"error": "文件不存在: " + filepath}, 404)
            return
        with open(filepath, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._send_html(os.path.join(WEB_DIR, "index.html"))
        elif path == "/visualize":
            self._send_html(os.path.join(WEB_DIR, "visualize.html"))
        elif path == "/api/results":
            fun3_data, err3 = parse_fun3_results(FUN3_OUT)
            fun4_data, err4 = parse_fun4_results(FUN4_OUT)
            reentry_b, err_b = parse_reentry_program_file(REENTRY_B)
            unified = build_unified_comparison(fun3_data, reentry_b)
            self._send_json({
                "fun3": fun3_data,
                "fun4": fun4_data,
                "reentryB": reentry_b,
                "unified": unified,
                "errors": {"fun3": err3, "fun4": err4, "reentryB": err_b}
            })
        elif path == "/api/ephemeris":
            eph_data, err = parse_ephemeris(FUN3_EPH)
            self._send_json({"targets": eph_data, "error": err})
        elif path == "/api/status":
            self._send_json({
                "qerExeExists": os.path.exists(QER_EXE),
                "qerDir": QER_DIR,
                "fun3OutExists": os.path.exists(FUN3_OUT),
                "fun4OutExists": os.path.exists(FUN4_OUT),
                "fun3EphExists": os.path.exists(FUN3_EPH),
            })
        else:
            # 尝试提供静态文件
            safe_path = path.lstrip("/")
            static_file = os.path.join(WEB_DIR, safe_path)
            if os.path.isfile(static_file):
                ct = "text/css; charset=utf-8" if safe_path.endswith(".css") else \
                     "application/javascript; charset=utf-8" if safe_path.endswith(".js") else \
                     "text/html; charset=utf-8"
                self._send_file(static_file, ct)
            else:
                self._send_json({"error": "未知路径: " + path}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/run":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                params = json.loads(body)
            except json.JSONDecodeError:
                self._send_json({"error": "无效的 JSON 请求"}, 400)
                return

            # 生成临时配置文件
            config_lines = [
                "# Web UI 临时配置文件",
                "function = " + str(params.get("function", "3")),
                "",
                "# Function 0/2",
                "tleFile0 = " + str(params.get("tleFile0", "catalog-20250311-3line.tle")),
                "PerigeeHeightThreshold = " + str(params.get("PerigeeHeightThreshold", "300.0")),
                "outFile0 = " + str(params.get("outFile0", "output/out_fun0.txt")),
                "objectType = " + str(params.get("objectType", "all")),
                "",
                "# Function 1 (TLE 质量评估)",
                "tleFile1 = " + str(params.get("tleFile1", "5.tle")),
                "forecastDays = " + str(params.get("forecastDays", "3.0")),
                "intervalDays = " + str(params.get("intervalDays", "0.5")),
                "outFile1 = " + str(params.get("outFile1", "output/Fun1out.txt")),
                "",
                "# Function 2 (陨落预报新算法)",
                "tleFile2 = " + str(params.get("tleFile2", "input/test.tle")),
                "tleReentryDir = " + str(params.get("tleReentryDir", "TleReentry")),
                "outFile2 = " + str(params.get("outFile2", "output/out_fun2.txt")),
                "",
                "# Function 3",
                "tleFile3 = " + str(params.get("tleFile3", "input/test.tle")),
                "forecastDays3 = " + str(params.get("forecastDays3", "30.0")),
                "stepSec3 = " + str(params.get("stepSec3", "60.0")),
                "reentryHeightKm = " + str(params.get("reentryHeightKm", "80.0")),
                "outFile3 = " + str(params.get("outFile3", "output/out_fun3.txt")),
                "outEphFile3 = " + str(params.get("outEphFile3", "output/out_fun3.eph")),
                "",
                "# Function 4",
                "reentryFileA = " + str(params.get("reentryFileA", "output/out_fun3.txt")),
                "reentryFileB = " + str(params.get("reentryFileB", "TleReentry/TleReentry_retimes-last.txt")),
                "outFile4 = " + str(params.get("outFile4", "output/out_fun4.txt")),
            ]
            config_content = "\n".join(config_lines) + "\n"
            config_path = os.path.join(OUTPUT_DIR, "web_temp.conf")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(config_content)

            # 运行 QER.exe
            code, stdout, stderr = run_qer(config_path)
            self._send_json({
                "returnCode": code,
                "stdout": stdout,
                "stderr": stderr,
                "configPath": config_path
            })
        else:
            self._send_json({"error": "未知 POST 路径: " + path}, 404)


def main():
    port = 8080
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print("无效端口: " + sys.argv[1] + "，使用默认 8080")

    server = HTTPServer(("0.0.0.0", port), QERHandler)
    print("=" * 60)
    print("QER Web UI Server")
    print("=" * 60)
    print("QER 目录: " + QER_DIR)
    print("QER.exe: " + ("存在" if os.path.exists(QER_EXE) else "不存在"))
    print("服务地址: http://localhost:" + str(port))
    print("操作页面: http://localhost:" + str(port) + "/")
    print("可视化页: http://localhost:" + str(port) + "/visualize")
    print("=" * 60)
    print("按 Ctrl+C 停止服务")
    print()

    # 自动打开浏览器
    threading.Timer(1.0, lambda: webbrowser.open("http://localhost:" + str(port))).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Web] 服务已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
