#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
照片/视频归类工具 - Windows GUI 版
功能：扫描 E:\照片，读取 EXIF/元数据，按日期+设备+GPS地址归类复制到 E:\照片分类
"""

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
import threading
import queue
import os
import json
import shutil
import time
import re
import subprocess
import math
import sys
from datetime import datetime
from pathlib import Path

# ============================================================
# 配置
# ============================================================

DEFAULT_SOURCE = r"E:\照片"
DEFAULT_TARGET = r"E:\照片分类"
CACHE_FILENAME = "_cache.json"

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp',
              '.heic', '.heif', '.cr2', '.cr3', '.arw', '.nef',
              '.dng', '.orf', '.rw2'}
VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.mts', '.m2ts',
              '.m4v', '.3gp', '.wmv', '.flv', '.webm'}
ALL_EXTS = IMAGE_EXTS | VIDEO_EXTS

NOMINATIM_URL = "https://api.mirror-earth.com/nominatim/reverse"
NOMINATIM_DELAY = 0.1  # 秒（10次/秒）

# ============================================================
# 处理引擎（后台线程运行）
# ============================================================

class ProcessingEngine:
    """文件处理引擎，通过回调上报进度，通过 stop_event 控制停止"""

    def __init__(self, source_dir, target_dir):
        self.source_dir = source_dir
        self.target_dir = target_dir
        self.cache_path = os.path.join(target_dir, CACHE_FILENAME)
        self.cache = self._load_cache()
        self.stop_event = threading.Event()
        self.last_geo_time = time.time() - NOMINATIM_DELAY

        # GUI 轮询用的共享状态（线程安全，GIL 保护）
        self.read_file = ""
        self.read_pct = 0           # 累计读取百分比
        self.write_file = ""
        self.write_pct = 0          # 累计写入百分比
        self.file_read_pct = 0      # 单文件读取进度 0-100
        self.file_write_pct = 0     # 单文件写入进度 0-100
        self.file_cur = 0
        self.file_tot = 0

    # ---------- 缓存 ----------

    def _load_cache(self):
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {"processed": {}, "geocode": {}}

    def _save_cache(self):
        try:
            os.makedirs(self.target_dir, exist_ok=True)
            tmp = self.cache_path + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)
            shutil.move(tmp, self.cache_path)
        except Exception:
            pass

    @staticmethod
    def _fmt_size(size_bytes):
        for unit in ('B', 'KB', 'MB', 'GB'):
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} TB"

    # ---------- EXIF 读取 ----------

    def _pil_exif(self, path):
        try:
            from PIL import Image
            with Image.open(path) as img:
                return img._getexif()
        except Exception:
            return None

    def _get_exif_value(self, exif, tag, default=None):
        if exif and tag in exif:
            v = exif[tag]
            if isinstance(v, bytes):
                v = v.decode('utf-8', errors='replace')
            return v.strip() if v else default
        return default

    def _read_image_date(self, path):
        """读取图片 EXIF 日期，优先 exiftool（最通用），其次 Pillow"""
        ext = os.path.splitext(path)[1].lower()

        # 1) 有 exiftool 时所有格式都优先用它（更准确，支持 CreateDate 等字段）
        if ext in IMAGE_EXTS:
            dt = self._exiftool_date(path)
            if dt:
                return dt

        # 2) 退回到 Pillow
        if ext in {'.jpg', '.jpeg', '.tif', '.tiff', '.png', '.bmp'}:
            exif = self._pil_exif(path)
            if exif:
                for tag in (0x9003, 0x9004, 0x0132):
                    dt = self._parse_exif_date(self._get_exif_value(exif, tag))
                    if dt:
                        return dt
        elif ext in {'.heic', '.heif'}:
            try:
                import pillow_heif
                from PIL import Image as PILImage
                import io
                hf = pillow_heif.open_heif(path)
                exif_raw = hf.info.get('exif')
                if exif_raw:
                    img = PILImage.open(io.BytesIO(exif_raw))
                    exif = img._getexif()
                    if exif:
                        for tag in (0x9003, 0x9004, 0x0132):
                            dt = self._parse_exif_date(self._get_exif_value(exif, tag))
                            if dt:
                                return dt
            except Exception:
                pass
        return None

    def _read_video_date(self, path):
        try:
            result = subprocess.run(
                ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', path],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                info = json.loads(result.stdout)
                tags = info.get('format', {}).get('tags', {})
                for key in ('creation_time', 'date', 'DATE'):
                    if key in tags:
                        dt = self._parse_exif_date(tags[key])
                        if dt:
                            return dt
        except Exception:
            pass
        return None

    def _parse_exif_date(self, s):
        if not s:
            return None
        s = s.strip().replace('Z', '').replace('T', ' ')
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                    "%Y:%m:%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(s[:len(datetime.now().strftime(fmt))], fmt)
            except:
                continue
        return None

    def _exiftool_date(self, path):
        """用 exiftool 读取 RAW 日期，尝试多个字段"""
        try:
            # 尝试 DateTimeOriginal → CreateDate → 通用日期
            r = subprocess.run(
                ['exiftool', '-DateTimeOriginal', '-CreateDate',
                 '-d', '%Y:%m:%d %H:%M:%S', path],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0:
                for line in r.stdout.strip().split('\n'):
                    if ': ' in line:
                        dt = self._parse_exif_date(line.split(': ', 1)[1].strip())
                        if dt:
                            return dt
        except Exception:
            pass
        return None

    # ---------- 设备名 ----------

    def _read_device(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext in {'.jpg', '.jpeg', '.tif', '.tiff', '.png', '.bmp'}:
            exif = self._pil_exif(path)
            if exif:
                make = self._get_exif_value(exif, 0x010F)
                model = self._get_exif_value(exif, 0x0110)
                return self._fmt_device(make, model)
        elif ext in {'.heic', '.heif'}:
            try:
                import pillow_heif, io
                from PIL import Image as PILImage
                hf = pillow_heif.open_heif(path)
                exif_raw = hf.info.get('exif')
                if exif_raw:
                    img = PILImage.open(io.BytesIO(exif_raw))
                    exif = img._getexif()
                    if exif:
                        make = self._get_exif_value(exif, 0x010F)
                        model = self._get_exif_value(exif, 0x0110)
                        return self._fmt_device(make, model)
            except:
                pass
        elif ext in IMAGE_EXTS:
            try:
                r = subprocess.run(['exiftool', '-Make', '-Model', path],
                                   capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    make = model = ""
                    for line in r.stdout.strip().split('\n'):
                        if ': ' in line:
                            k, v = line.split(': ', 1)
                            if k == 'Make': make = v.strip()
                            elif k == 'Model': model = v.strip()
                    return self._fmt_device(make, model)
            except:
                pass
        elif ext in VIDEO_EXTS:
            try:
                r = subprocess.run(
                    ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', path],
                    capture_output=True, text=True, timeout=30
                )
                if r.returncode == 0:
                    tags = json.loads(r.stdout).get('format', {}).get('tags', {})
                    make = (tags.get('make') or tags.get('Make') or
                            tags.get('com.apple.quicktime.make', ''))
                    model = (tags.get('model') or tags.get('Model') or
                             tags.get('com.apple.quicktime.model', ''))
                    return self._fmt_device(make, model)
            except:
                pass
        return None

    def _fmt_device(self, make, model):
        make = (make or '').strip()
        model = (model or '').strip()
        if not make and not model:
            return None
        if make and model:
            if model.lower().startswith(make.lower()):
                return model
            return f"{make} {model}".strip()
        return make or model

    # ---------- GPS ----------

    def _read_gps(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext in {'.jpg', '.jpeg', '.tif', '.tiff', '.png', '.bmp'}:
            exif = self._pil_exif(path)
            if exif and 0x8825 in exif:
                g = exif[0x8825]
                if 2 in g and 4 in g:
                    lat = self._dms2dec(g[2], g.get(1, 'N'))
                    lon = self._dms2dec(g[4], g.get(3, 'E'))
                    if lat is not None and lon is not None:
                        return (round(lat, 6), round(lon, 6))
        elif ext in {'.heic', '.heif'}:
            try:
                import pillow_heif, io
                from PIL import Image as PILImage
                hf = pillow_heif.open_heif(path)
                exif_raw = hf.info.get('exif')
                if exif_raw:
                    img = PILImage.open(io.BytesIO(exif_raw))
                    exif = img._getexif()
                    if exif and 0x8825 in exif:
                        g = exif[0x8825]
                        if 2 in g and 4 in g:
                            lat = self._dms2dec(g[2], g.get(1, 'N'))
                            lon = self._dms2dec(g[4], g.get(3, 'E'))
                            if lat and lon:
                                return (round(lat, 6), round(lon, 6))
            except:
                pass
        elif ext in IMAGE_EXTS:
            try:
                r = subprocess.run(
                    ['exiftool', '-GPSLatitude', '-GPSLatitudeRef',
                     '-GPSLongitude', '-GPSLongitudeRef', path],
                    capture_output=True, text=True, timeout=10
                )
                if r.returncode == 0:
                    d = {}
                    for line in r.stdout.strip().split('\n'):
                        if ': ' in line:
                            k, v = line.split(': ', 1)
                            d[k] = v.strip()
                    if 'GPS Latitude' in d and 'GPS Longitude' in d:
                        try:
                            lat = float(d['GPS Latitude'])
                            lon = float(d['GPS Longitude'])
                            if d.get('GPS Latitude Ref') == 'S': lat = -lat
                            if d.get('GPS Longitude Ref') == 'W': lon = -lon
                            return (round(lat, 6), round(lon, 6))
                        except:
                            pass
            except:
                pass
        elif ext in VIDEO_EXTS:
            try:
                r = subprocess.run(
                    ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', path],
                    capture_output=True, text=True, timeout=30
                )
                if r.returncode == 0:
                    tags = json.loads(r.stdout).get('format', {}).get('tags', {})
                    loc = tags.get('com.apple.quicktime.location.ISO6709', '')
                    if loc:
                        m = re.match(r'([+-]\d+\.?\d*)([+-]\d+\.?\d*)', loc)
                        if m:
                            return (round(float(m.group(1)), 6),
                                    round(float(m.group(2)), 6))
            except:
                pass
        return None

    def _dms2dec(self, dms, ref):
        try:
            d = float(dms[0]) + float(dms[1]) / 60 + float(dms[2]) / 3600
            if ref in ('S', 'W'):
                d = -d
            return d
        except:
            return None

    # ---------- 逆地理编码 ----------

    def _geocode(self, lat, lon):
        key = f"{lat},{lon}"
        if key in self.cache["geocode"]:
            return self.cache["geocode"][key]

        # 限速（2次/秒）
        now = time.time()
        gap = now - self.last_geo_time
        if gap < NOMINATIM_DELAY:
            time.sleep(NOMINATIM_DELAY - gap)
        self.last_geo_time = time.time()

        try:
            import requests
            resp = requests.get(NOMINATIM_URL,
                                params={'lat': lat, 'lon': lon, 'format': 'json'},
                                headers={'User-Agent': 'PhotoOrganizer/1.0'},
                                timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if 'address' in data:
                    addr = data['address']
                    parts = []
                    for k in ('district', 'county', 'city', 'state'):
                        if addr.get(k):
                            parts.append(addr[k])
                    short = '_'.join(parts) if parts else '未知位置'
                    result = {
                        "display": data.get('display_name', short),
                        "short": short,
                        "address": addr,
                    }
                    self.cache["geocode"][key] = result
                    self._save_cache()
                    return result
        except Exception as e:
            return {"display": str(e), "short": "API错误", "address": {}}
        return None

    # ---------- 主入口：按日期分组处理 ----------

    def _collect_files(self):
        """收集所有符合条件的文件，排除目标目录和缓存"""
        all_files = []
        for root, dirs, files in os.walk(self.source_dir):
            dirs[:] = [d for d in dirs if d != '照片分类' and not d.startswith('_')]
            for f in files:
                if os.path.splitext(f)[1].lower() in ALL_EXTS:
                    full = os.path.join(root, f)
                    if (full != self.cache_path and
                            not full.startswith(os.path.join(self.target_dir, ''))):
                        all_files.append(full)
        return all_files

    def run(self, report, total_count):
        """两阶段处理：先扫日期分组，再逐天处理（支持同天GPS继承）"""

        # === 阶段一：扫描文件 ===
        report("phase", "📂 扫描文件")
        report("status", "正在扫描文件...")

        all_files = self._collect_files()
        total = len(all_files)
        report("total", total)
        report("status", f"共找到 {total} 个文件")

        # 过滤已处理
        remaining = [f for f in all_files if f not in self.cache["processed"]]
        done_count = total - len(remaining)
        if done_count > 0:
            report("log", f"⏭ {done_count} 个已在缓存中，跳过")
        if not remaining:
            report("status", "所有文件已处理完毕！")
            report("phase", "✅ 完成")
            report("done", True)
            return

        rem_total = len(remaining)
        report("log", f"▶ 开始处理 {rem_total} 个文件")
        self.read_file = "等待开始"
        self.file_cur = 0
        self.file_tot = rem_total

        # === 阶段二：读取全部文件元数据（一次遍历，读完所有EXIF） ===
        report("phase", "📖 读取全部文件元数据")
        all_metas = []
        date_groups = {}
        for idx, fp in enumerate(remaining):
            if self.stop_event.is_set():
                report("log", "⏹ 已暂停"); report("done", False); return

            rel = os.path.relpath(fp, self.source_dir)
            fname = os.path.basename(fp)
            report("current", rel)
            report("sub_progress", (idx + 1, rem_total))
            self.read_file = fname
            self.file_read_pct = 100
            report("file_read", (100, fname))

            ext = os.path.splitext(fp)[1].lower()
            is_video = ext in VIDEO_EXTS

            # 读取：日期 + 设备 + GPS
            dt = None
            date_source = "文件创建时间"
            if is_video:
                dt = self._read_video_date(fp)
            else:
                dt = self._read_image_date(fp)
            if dt:
                date_source = "EXIF"
            else:
                try:
                    dt = datetime.fromtimestamp(os.path.getctime(fp))
                except:
                    dt = datetime.now()

            device = self._read_device(fp) or "未知设备"
            gps = self._read_gps(fp)

            self.read_pct = math.ceil((idx + 1) / rem_total * 100)  # 累计

            meta = {"path": fp, "device": device, "gps": gps,
                    "dt": dt, "ext": ext, "is_video": is_video,
                    "filename": fname,
                    "name_no_ext": os.path.splitext(fname)[0]}

            all_metas.append(meta)
            ds = dt.strftime("%Y-%m-%d")
            date_groups.setdefault(ds, []).append(meta)

            # 文件统计
            self.file_cur = idx + 1
            self.file_tot = rem_total
            gps_str = f"{gps[0]:.6f}, {gps[1]:.6f}" if gps else "无GPS"
            report("file_info",
                f"📄 {fname}  |  📅 {dt.strftime('%Y-%m-%d %H:%M:%S')} [{date_source}]"
                f"  |  📱 {device}  |  📍 {gps_str}  |  💾 {self._fmt_size(os.path.getsize(fp))}")

        report("log", f"📅 元数据读取完毕，共 {len(date_groups)} 个日期")

        # === 阶段三：逐天处理（地理编码+复制） ===
        success = errors = 0
        write_so_far = 0
        self.write_pct = 0
        sorted_dates = sorted(date_groups.keys())

        for date_idx, date_str in enumerate(sorted_dates):
            if self.stop_event.is_set():
                report("log", "⏹ 已暂停"); report("done", False); return

            entries = date_groups[date_str]
            n = len(entries)
            report("log", f"\n── [{date_idx+1}/{len(sorted_dates)}] {date_str} ({n}个文件) ──")
            report("status", f"处理 {date_str}")

            # 收集本日不重复 GPS
            unique_gps = set()
            for meta in entries:
                if meta["gps"]:
                    unique_gps.add(meta["gps"])

            # ── B) 逆地理编码 ──
            location_map = {}
            if unique_gps:
                report("phase", f"🌐 地址解析 {date_str}")
                for gi, gps in enumerate(sorted(unique_gps)):
                    if self.stop_event.is_set():
                        report("log", "⏹ 已暂停"); report("done", False); return
                    report("sub_progress", (gi + 1, len(unique_gps)))
                    r = self._geocode(gps[0], gps[1])
                    if r:
                        k = f"{gps[0]},{gps[1]}"
                        location_map[k] = r["short"]
                        report("geocode", (gps, r["display"], r["short"]))
                report("log", f"  🌐 {len(location_map)} 个位置已解析")

            # ── C) 降级位置 ──
            fallback_location = None
            if location_map:
                from collections import Counter
                locs = [v for v in location_map.values() if v and v != "API错误"]
                if locs:
                    fallback_location = Counter(locs).most_common(1)[0][0]
                    report("log", f"  📌 降级位置: {fallback_location}")

            # ── D1) 复制有GPS的文件 ──
            gps_m = [m for m in entries if m["gps"]]
            if gps_m:
                report("phase", f"📝 写入 {len(gps_m)}个带GPS文件")
                for fi, meta in enumerate(gps_m):
                    if self.stop_event.is_set():
                        report("log", "⏹ 已暂停"); report("done", False); return
                    self.write_file = meta["filename"]
                    report("file_write", (0, meta["filename"]))
                    self._copy_one(meta, date_str, location_map, report)
                    write_so_far += 1
                    self.write_pct = math.ceil(write_so_far / rem_total * 100) if rem_total else 0  # 累计
                    self.file_write_pct = 100
                    report("file_write", (100, meta["filename"]))
                    self.file_cur = write_so_far
                    self.file_tot = rem_total
                    report("sub_progress", (fi + 1, len(gps_m)))
                    success += 1

            # ── D2) 复制无GPS的文件 ──
            nogps_m = [m for m in entries if not m["gps"]]
            if nogps_m:
                lbl = f"📝 写入 {len(nogps_m)}个无GPS文件"
                if fallback_location:
                    lbl += f" ←{fallback_location}"
                report("phase", lbl)
                for fi, meta in enumerate(nogps_m):
                    if self.stop_event.is_set():
                        report("log", "⏹ 已暂停"); report("done", False); return
                    self.write_file = meta["filename"]
                    report("file_write", (0, meta["filename"]))
                    self._copy_one(meta, date_str, {}, report, fallback_location)
                    write_so_far += 1
                    self.write_pct = math.ceil(write_so_far / rem_total * 100) if rem_total else 0  # 累计
                    self.file_write_pct = 100
                    report("file_write", (100, meta["filename"]))
                    self.file_cur = write_so_far
                    self.file_tot = rem_total
                    report("sub_progress", (fi + 1, len(nogps_m)))
                    success += 1

            processed_so_far = done_count + success + errors
            report("pct", round(processed_so_far / total * 100 if total else 0, 1))

        report("log", f"\n✅ 完成！成功 {success} / 失败 {errors}")
        report("status", f"完成！成功 {success} / 失败 {errors}")
        report("phase", "✅ 全部完成")
        report("done", True)

    # ---------- 复制单个文件 ----------

    def _copy_one(self, meta, date_str, location_map, report, fallback_location=None):
        """复制单个文件到目标目录，写入阶段进度"""
        location = None
        inherited = False
        if meta["gps"]:
            k = f"{meta['gps'][0]},{meta['gps'][1]}"
            location = location_map.get(k) if location_map else None
        elif fallback_location:
            location = fallback_location
            inherited = True

        device = meta["device"]
        if meta["is_video"] and not meta["gps"]:
            device_dir = f"{device}_视频"
        elif device and location:
            device_dir = f"{device} ({location})"
        elif device:
            device_dir = device
        else:
            device_dir = "未知位置"

        y = meta["dt"].strftime("%Y")
        tdir = os.path.join(self.target_dir, y, date_str, device_dir)
        tp = meta["dt"].strftime("%Y%m%d_%H%M%S")
        new = f"{tp}_{meta['name_no_ext']}{meta['ext']}"
        dst = os.path.join(tdir, new)
        c = 1
        while os.path.exists(dst):
            new = f"{tp}_{meta['name_no_ext']}_{c}{meta['ext']}"
            dst = os.path.join(tdir, new); c += 1

        try:
            os.makedirs(tdir, exist_ok=True)
            shutil.copy2(meta["path"], dst)
        except Exception as e:
            report("log", f"✗ {meta['filename']}: {e}")
            self.cache["processed"][meta["path"]] = {"error": str(e), "copied": False,
                "processed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            self._save_cache()
            return

        tag = " ↩" if inherited else ""
        report("log", f"✓ {meta['filename']} → {date_str}/{device_dir}/{new}{tag}")
        self.cache["processed"][meta["path"]] = {"copied": True, "target": dst,
            "date": date_str, "year": y, "device": device, "gps": meta["gps"],
            "location": location, "inherited_gps": inherited, "is_video": meta["is_video"],
            "processed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        self._save_cache()
        report("progress", 1)


# ============================================================
# GUI
# ============================================================

class PhotoOrganizerGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("照片归类工具 v1.0")
        self.root.geometry("1000x750")
        self.root.minsize(880, 650)

        # 变量
        self.src_var = tk.StringVar(value=DEFAULT_SOURCE)
        self.dst_var = tk.StringVar(value=DEFAULT_TARGET)
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_text = tk.StringVar(value="等待开始")
        self.current_file = tk.StringVar(value="—")
        self.status_var = tk.StringVar(value="就绪")
        self.stats_total = tk.StringVar(value="—")
        self.stats_done = tk.StringVar(value="0")
        self.stats_success = tk.StringVar(value="0")
        self.stats_fail = tk.StringVar(value="0")
        self.stats_cache = tk.StringVar(value="0")
        self.stats_gps = tk.StringVar(value="0")
        self.geo_total = tk.StringVar(value="0")
        self.geo_current = tk.StringVar(value="—")
        self.phase_var = tk.StringVar(value="等待开始")
        self.sub_progress = tk.StringVar(value="")

        # 累计进度
        self.read_progress_var = tk.DoubleVar(value=0)
        self.read_progress_text = tk.StringVar(value="—")
        self.write_progress_var = tk.DoubleVar(value=0)
        self.write_progress_text = tk.StringVar(value="—")
        # 单文件进度
        self.file_read_var = tk.DoubleVar(value=0)
        self.file_read_text = tk.StringVar(value="—")
        self.file_write_var = tk.DoubleVar(value=0)
        self.file_write_text = tk.StringVar(value="—")

        # 文件信息
        self.file_info_var = tk.StringVar(value="等待开始...")

        self.engine = None
        self.worker_thread = None
        self.msg_queue = queue.Queue()
        self.running = False
        self._total_files = 0
        self._done_files = 0
        self._success_count = 0
        self._error_count = 0
        self._read_count = 0
        self._write_count = 0

        # 环境检查
        self._tool_ffprobe = self._check_tool('ffprobe')
        self._tool_exiftool = self._check_tool('exiftool')

        # 构建界面
        self._build_ui()

        # 定时检查队列
        self.root.after(30, self._poll_queue)

    def _build_ui(self):
        # ---------- 样式 ----------
        style = ttk.Style()
        style.theme_use('vista' if 'vista' in style.theme_names() else 'clam')
        style.configure('TButton', padding=6)
        style.configure('Header.TLabel', font=('Microsoft YaHei UI', 10, 'bold'))

        # ---------- 顶部标题 ----------
        title = ttk.Label(self.root, text="📸 照片 / 视频 归类工具",
                          font=('Microsoft YaHei UI', 14, 'bold'))
        title.pack(pady=(10, 2))

        # 环境提示
        env_warnings = []
        if not self._tool_ffprobe:
            env_warnings.append("⚠ FFmpeg (ffprobe) 未安装 → 视频日期将使用文件创建时间")
        if not self._tool_exiftool:
            env_warnings.append("⚠ ExifTool 未安装 → RAW 文件将使用文件创建时间")

        if env_warnings:
            warn_frame = ttk.Frame(self.root)
            warn_frame.pack(fill=tk.X, padx=20)
            for w in env_warnings:
                lbl = ttk.Label(warn_frame, text=w,
                                foreground='#cc8800', font=('Microsoft YaHei UI', 9))
                lbl.pack(anchor='w')

        # ---------- 主内容 ----------
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # === 设置区域 ===
        self._add_setting_row(main_frame, 0, "📂 源目录:", self.src_var, self._browse_src)
        self._add_setting_row(main_frame, 1, "📂 目标目录:", self.dst_var, self._browse_dst)

        # === 按钮 ===
        btn_frame = ttk.Frame(main_frame)
        btn_frame.grid(row=2, column=0, columnspan=3, pady=8, sticky='ew')

        self.start_btn = ttk.Button(btn_frame, text="▶  开始处理",
                                    command=self._start)
        self.start_btn.pack(side=tk.LEFT, padx=4)

        self.pause_btn = ttk.Button(btn_frame, text="⏹  暂停",
                                    command=self._stop, state=tk.DISABLED)
        self.pause_btn.pack(side=tk.LEFT, padx=4)

        ttk.Button(btn_frame, text="📋 打开缓存目录", command=self._open_target).pack(side=tk.LEFT, padx=4)

        # === 分隔线 ===
        ttk.Separator(main_frame, orient='horizontal').grid(row=3, column=0, columnspan=3,
                                                             sticky='ew', pady=8)

        # === 进度区域 ===
        prog_frame = ttk.LabelFrame(main_frame, text=" 进度 ", padding=8)
        prog_frame.grid(row=4, column=0, columnspan=3, sticky='ew', pady=(0, 4))

        # 阶段指示
        phase_row = ttk.Frame(prog_frame)
        phase_row.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(phase_row, text="📌 阶段:", font=('Microsoft YaHei UI', 9)).pack(side=tk.LEFT)
        ttk.Label(phase_row, textvariable=self.phase_var,
                  font=('Consolas', 9, 'bold'), foreground='#cc6600').pack(side=tk.LEFT, padx=4)
        ttk.Label(phase_row, textvariable=self.sub_progress,
                  font=('Consolas', 8), foreground='#888888').pack(side=tk.LEFT, padx=(10, 0))

        ttk.Label(prog_frame, textvariable=self.current_file,
                  font=('Consolas', 9)).pack(anchor='w', pady=(2, 2))

        # ── 读取进度 ──
        read_row = ttk.Frame(prog_frame)
        read_row.pack(fill=tk.X, pady=1)
        ttk.Label(read_row, text="📖 读取:", font=('Microsoft YaHei UI', 8)).pack(side=tk.LEFT)
        self.read_bar = ttk.Progressbar(read_row, variable=self.read_progress_var,
                                         length=400, mode='determinate')
        self.read_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        ttk.Label(read_row, textvariable=self.read_progress_text,
                  font=('Consolas', 8)).pack(side=tk.LEFT, padx=(2, 0))

        # ── 写入进度 ──
        write_row = ttk.Frame(prog_frame)
        write_row.pack(fill=tk.X, pady=1)
        ttk.Label(write_row, text="📝 写入:", font=('Microsoft YaHei UI', 8)).pack(side=tk.LEFT)
        write_bar = ttk.Progressbar(write_row, variable=self.write_progress_var,
                                     length=400, mode='determinate')
        write_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        ttk.Label(write_row, textvariable=self.write_progress_text,
                  font=('Consolas', 8)).pack(side=tk.LEFT, padx=(2, 0))

        # ── 单文件读取 ──
        fr_row = ttk.Frame(prog_frame)
        fr_row.pack(fill=tk.X, pady=1)
        ttk.Label(fr_row, text="📄 文件读:", font=('Microsoft YaHei UI', 8)).pack(side=tk.LEFT)
        fr_bar = ttk.Progressbar(fr_row, variable=self.file_read_var,
                                  length=400, mode='determinate')
        fr_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        ttk.Label(fr_row, textvariable=self.file_read_text,
                  font=('Consolas', 8)).pack(side=tk.LEFT, padx=(2, 0))

        # ── 单文件写入 ──
        fw_row = ttk.Frame(prog_frame)
        fw_row.pack(fill=tk.X, pady=1)
        ttk.Label(fw_row, text="📄 文件写:", font=('Microsoft YaHei UI', 8)).pack(side=tk.LEFT)
        fw_bar = ttk.Progressbar(fw_row, variable=self.file_write_var,
                                  length=400, mode='determinate')
        fw_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        ttk.Label(fw_row, textvariable=self.file_write_text,
                  font=('Consolas', 8)).pack(side=tk.LEFT, padx=(2, 0))

        # ── 总进度 ──
        total_row = ttk.Frame(prog_frame)
        total_row.pack(fill=tk.X, pady=1)
        ttk.Label(total_row, text="📊 总进度:", font=('Microsoft YaHei UI', 8)).pack(side=tk.LEFT)
        self.progress_bar = ttk.Progressbar(total_row, variable=self.progress_var,
                                            length=400, mode='determinate')
        self.progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        ttk.Label(total_row, textvariable=self.progress_text,
                  font=('Consolas', 8), width=16, anchor='e').pack(side=tk.LEFT)

        # === 文件信息区域 ===
        info_frame = ttk.LabelFrame(main_frame, text=" 📄 当前文件信息 ", padding=6)
        info_frame.grid(row=5, column=0, columnspan=3, sticky='ew', pady=(0, 4))

        self.info_label = ttk.Label(info_frame, textvariable=self.file_info_var,
                                     font=('Consolas', 9),
                                     wraplength=920, anchor='w', justify='left')
        self.info_label.pack(fill=tk.X, anchor='w')

        # === 统计 + 地址解析 + 日志（三栏） ===
        mid_frame = ttk.Frame(main_frame)
        mid_frame.grid(row=6, column=0, columnspan=3, sticky='nsew', pady=4)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(6, weight=1)

        # -- 统计（窄－左） --
        stats_frame = ttk.LabelFrame(mid_frame, text=" 📊 统计 ", padding=8, width=180)
        stats_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=False, padx=(0, 4))
        stats_frame.pack_propagate(False)

        for label, var in [
            ("总文件数:", self.stats_total),
            ("已处理:", self.stats_done),
            ("✓ 成功:", self.stats_success),
            ("✗ 失败:", self.stats_fail),
            ("文件缓存:", self.stats_cache),
            ("GPS缓存:", self.stats_gps),
        ]:
            row = ttk.Frame(stats_frame)
            row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=label, width=10, anchor='e').pack(side=tk.LEFT)
            ttk.Label(row, textvariable=var, font=('Consolas', 10)).pack(side=tk.LEFT, padx=4)

        # -- 地址解析结果（中） --
        geo_frame = ttk.LabelFrame(mid_frame, text=" 🌐 地址解析结果 ", padding=4)
        geo_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))

        # 顶部统计行
        geo_stat_row = ttk.Frame(geo_frame)
        geo_stat_row.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(geo_stat_row, text="已解析:", font=('Microsoft YaHei UI', 8)).pack(side=tk.LEFT)
        ttk.Label(geo_stat_row, textvariable=self.geo_total,
                  font=('Consolas', 9, 'bold'), foreground='#0066cc').pack(side=tk.LEFT, padx=2)
        ttk.Label(geo_stat_row, text="  当前:", font=('Microsoft YaHei UI', 8)).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(geo_stat_row, textvariable=self.geo_current,
                  font=('Consolas', 8), foreground='#333333').pack(side=tk.LEFT, padx=2)

        self.geo_text = scrolledtext.ScrolledText(
            geo_frame, height=10, font=('Consolas', 9),
            bg='#f5faf5', fg='#1a3a1a', insertbackground='black',
            wrap=tk.WORD, state=tk.DISABLED
        )
        self.geo_text.pack(fill=tk.BOTH, expand=True)

        # -- 日志（右） --
        log_frame = ttk.LabelFrame(mid_frame, text=" 📋 实时日志 ", padding=4)
        log_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=12, font=('Consolas', 9),
            bg='#1e1e1e', fg='#d4d4d4', insertbackground='white',
            wrap=tk.WORD, state=tk.DISABLED
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # 绑定右键清空
        self.log_text.bind("<Button-3>", lambda e: self._clear_log())

        # === 底部状态栏 ===
        ttk.Separator(main_frame, orient='horizontal').grid(row=6, column=0, columnspan=3,
                                                             sticky='ew', pady=(6, 2))
        status_bar = ttk.Frame(main_frame)
        status_bar.grid(row=7, column=0, columnspan=3, sticky='ew')
        ttk.Label(status_bar, textvariable=self.status_var,
                  font=('Microsoft YaHei UI', 9)).pack(side=tk.LEFT)

        # 退出处理
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _add_setting_row(self, parent, row, label, var, cmd):
        f = ttk.Frame(parent)
        f.grid(row=row, column=0, columnspan=3, sticky='ew', pady=3)
        ttk.Label(f, text=label, width=12).pack(side=tk.LEFT)
        e = ttk.Entry(f, textvariable=var, font=('Consolas', 9))
        e.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        ttk.Button(f, text="浏览", command=cmd, width=6).pack(side=tk.LEFT)

    # ---------- 回调 ----------

    def _browse_src(self):
        d = filedialog.askdirectory(title="选择源目录", initialdir=self.src_var.get())
        if d:
            self.src_var.set(d)

    def _browse_dst(self):
        d = filedialog.askdirectory(title="选择目标目录", initialdir=self.dst_var.get())
        if d:
            self.dst_var.set(d)

    def _open_target(self):
        path = self.dst_var.get()
        if os.path.isdir(path):
            os.startfile(path)
        else:
            messagebox.showinfo("提示", f"目录不存在:\n{path}")

    def _clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)

    # ---------- 启停 ----------

    def _start(self):
        src = self.src_var.get().strip()
        dst = self.dst_var.get().strip()
        if not os.path.isdir(src):
            messagebox.showerror("错误", f"源目录不存在:\n{src}")
            return

        self.running = True
        self._total_files = 0
        self._done_files = 0
        self._success_count = 0
        self._error_count = 0

        self.start_btn.config(state=tk.DISABLED)
        self.pause_btn.config(state=tk.NORMAL)

        self.progress_var.set(0)
        self.progress_text.set("正在启动...")
        self.current_file.set("—")
        self.phase_var.set("启动中...")
        self.sub_progress.set("")
        self.read_progress_var.set(0)
        self.read_progress_text.set("—")
        self.write_progress_var.set(0)
        self.write_progress_text.set("—")
        self.file_read_var.set(0)
        self.file_read_text.set("—")
        self.file_write_var.set(0)
        self.file_write_text.set("—")
        self.file_info_var.set("等待开始...")

        self._clear_geo()
        self._log("▶ 开始处理任务")
        self._log(f"  源目录: {src}")
        self._log(f"  目标目录: {dst}")

        # 清空统计
        self.stats_done.set("0")
        self.stats_success.set("0")
        self.stats_fail.set("0")

        # 创建引擎并启动线程
        self.engine = ProcessingEngine(src, dst)
        self.engine.stop_event.clear()

        # 更新缓存统计
        self.stats_cache.set(str(len(self.engine.cache["processed"])))
        self.stats_gps.set(str(len(self.engine.cache["geocode"])))



        self.worker_thread = threading.Thread(
            target=self.engine.run,
            args=(self._report, self._total_files),
            daemon=True
        )
        self.worker_thread.start()

    def _stop(self):
        if self.engine:
            self.engine.stop_event.set()
        self.pause_btn.config(state=tk.DISABLED)
        self._log("⏹ 正在暂停（等待当前文件处理完）...")

    def _check_tool(self, name):
        """检查外部工具是否可用"""
        try:
            subprocess.run([name, '-version'], capture_output=True, timeout=5)
            return True
        except Exception:
            return False

    def _on_close(self):
        if self.running:
            if not messagebox.askyesno("确认退出",
                                       "处理正在进行中，确定要退出吗？\n（进度已保存，下次可继续）"):
                return
            if self.engine:
                self.engine.stop_event.set()
        self.root.destroy()

    # ---------- 消息系统 ----------

    def _report(self, msg_type, data):
        """由后台线程调用，往队列里放消息"""
        self.msg_queue.put((msg_type, data))

    def _poll_queue(self):
        """主线程定时轮询消息队列 + 引擎共享状态"""
        try:
            while True:
                msg_type, data = self.msg_queue.get_nowait()
                self._handle_message(msg_type, data)
        except queue.Empty:
            pass

        # 从引擎直接读取进度状态
        if self.engine:
            # 累计进度
            self.read_progress_var.set(self.engine.read_pct)
            self.read_progress_text.set(f"{self.engine.read_file}  {self.engine.read_pct}%")
            self.write_progress_var.set(self.engine.write_pct)
            self.write_progress_text.set(f"{self.engine.write_file}  {self.engine.write_pct}%")
            cur, tot = self.engine.file_cur, self.engine.file_tot
            self.progress_bar.configure(maximum=max(tot, 1))
            self.progress_var.set(min(cur, tot))
            pct = math.ceil(cur / max(tot, 1) * 100)
            self.progress_text.set(f"文件 {cur} / {tot}  ({pct}%)")
            self.stats_done.set(str(cur))
            if tot:
                self.stats_total.set(str(tot))

        self.root.after(30, self._poll_queue)

    def _handle_message(self, msg_type, data):
        if msg_type == "log":
            self._log(data)
        elif msg_type == "current":
            self.current_file.set(str(data))
        elif msg_type == "phase":
            self.phase_var.set(str(data))
        elif msg_type == "sub_progress":
            cur, tot = data
            self.sub_progress.set(f"[{cur}/{tot}]")
        elif msg_type == "file_read":
            pct, fname = data
            self.file_read_var.set(pct)
            self.file_read_text.set(f"{fname}  ✅")
        elif msg_type == "file_write":
            pct, fname = data
            self.file_write_var.set(pct)
            self.file_write_text.set(f"{fname}  {pct}%")
        elif msg_type == "file_info":
            self.file_info_var.set(str(data))
        elif msg_type == "status":
            self.status_var.set(str(data))
        elif msg_type == "geocode":
            # data = (gps_tuple, display_name, short_name)
            gps, display, short = data
            self._log_geo(f"{gps[0]:.6f}, {gps[1]:.6f}  →  {short}")
            self.geo_current.set(f"{gps[0]:.5f}, {gps[1]:.5f}  →  {short}")
            self.geo_total.set(str(len(self.engine.cache.get("geocode", {}))) if self.engine else "0")
        elif msg_type == "done":
            self.running = False
            self.start_btn.config(state=tk.NORMAL)
            self.pause_btn.config(state=tk.DISABLED)
            self.current_file.set("—")
            # 更新缓存统计
            if self.engine:
                self.stats_cache.set(str(len(self.engine.cache["processed"])))
                self.stats_gps.set(str(len(self.engine.cache["geocode"])))

        # 更新统计
        if self.engine:
            self.stats_success.set(str(sum(
                1 for v in self.engine.cache["processed"].values()
                if v.get("copied")
            )))
            self.stats_fail.set(str(sum(
                1 for v in self.engine.cache["processed"].values()
                if v.get("error")
            )))
            done = len([v for v in self.engine.cache["processed"].values()
                        if v.get("copied") or v.get("error")])
            self.stats_done.set(str(done))
            total = self._total_files
            if total > 0:
                self.progress_var.set(min(done, total))
                pct = done / total * 100
                self.progress_text.set(f"{done} / {total}  ({pct:.1f}%)")

    def _log(self, msg):
        self.log_text.config(state=tk.NORMAL)
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{ts}] {msg}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _log_geo(self, msg):
        """向地址解析结果面板追加内容"""
        self.geo_text.config(state=tk.NORMAL)
        self.geo_text.insert(tk.END, f"  {msg}\n")
        self.geo_text.see(tk.END)
        self.geo_text.config(state=tk.DISABLED)

    def _clear_geo(self):
        self.geo_text.config(state=tk.NORMAL)
        self.geo_text.delete(1.0, tk.END)
        self.geo_text.config(state=tk.DISABLED)
        self.geo_total.set("0")
        self.geo_current.set("—")

    def run(self):
        self.root.mainloop()


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    app = PhotoOrganizerGUI()
    app.run()
