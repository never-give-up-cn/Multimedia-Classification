# Multimedia Classification - 多媒体文件自动归类工具

照片 / 视频自动归类工具。读取 EXIF 元数据，按日期+设备+GPS 地址自动整理到分类目录。

## 功能

- **自动扫描** `E:\照片` → 归类到 `E:\照片分类`
- **EXIF 读取** — 日期、设备型号、GPS 坐标
- **GPS 逆地理编码** — 通过 Nominatim 镜像将经纬度转为**行政区划地址**（区/县/市/省）
- **同天 GPS 继承** — 没有 GPS 的照片/视频自动继承同天有 GPS 照片的位置
- **视频支持** — 通过 ffprobe 读取视频创建时间（需安装 FFmpeg）
- **RAW 支持** — 通过 ExifTool 读取 RAW 文件 EXIF（需安装 ExifTool）
- **缓存断点续传** — 边处理边写 JSON 缓存，中断后可继续
- **Windows GUI** — tkinter 原生窗口，实时显示阶段、进度、地址解析结果

## 目录结构

处理后的文件按以下结构存放：

```
E:\照片分类\
├── 2024\
│   ├── 2024-03-15\
│   │   ├── iPhone 15 Pro (思明区_厦门市)\
│   │   │   ├── 20240315_093021_IMG_0001.jpg
│   │   │   └── 20240315_143022_IMG_0002.jpg
│   │   ├── iPhone 15 Pro_视频\
│   │   │   └── 20240315_180001_VID_20240315.mp4
│   │   └── Xiaomi 14 (湖里区_厦门市)\
│   │       └── 20240315_102345_IMG_5432.jpg
│   └── 2024-03-16\
│       └── ...
└── 2025\
    └── ...
```

## 使用方法

### 直接运行

```bash
cd Multimedia-Classification
python photo_organizer_gui.py
```

或双击 `启动照片归类工具.bat`（自动加载 PATH）。

### 依赖安装

```bash
pip install pillow requests pillow-heif
```

### 可选工具（提升功能）

| 工具 | 用途 | 下载地址 |
|------|------|---------|
| **FFmpeg** (ffprobe) | 读取视频创建时间 | https://ffmpeg.org |
| **ExifTool** | 读取 RAW 格式 EXIF | https://exiftool.org |

## 配置

脚本顶部可修改以下参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `DEFAULT_SOURCE` | `E:\照片` | 源目录 |
| `DEFAULT_TARGET` | `E:\照片分类` | 目标目录 |
| `NOMINATIM_URL` | `https://api.mirror-earth.com/nominatim/reverse` | 逆地理编码 API |
| `NOMINATIM_DELAY` | `0.5` | API 调用间隔（秒，2次/秒） |

## 技术栈

- Python 3.6+
- tkinter（GUI）
- Pillow（EXIF 读取）
- pillow-heif（HEIC 支持）
- requests（Nominatim API）
- ffprobe / exiftool（可选，增强支持）
