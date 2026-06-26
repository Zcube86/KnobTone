# KnobTone 桌面版 Demo（纯控制器）

> **目的**：在 Windows 上验证旋钮力反馈 + 交互逻辑，音频走电脑，设备只做控制。

## 与最终产品的区别

| | 桌面 Demo | 最终产品 (KnobTone) |
|---|---|---|
| **形态** | 桌面旋钮 + USB 连电脑 | USB-C 小尾巴连手机 |
| **音频** | 走电脑扬声器/耳机 | 走内置 DAC → 3.5mm 耳机 |
| **数据来源** | Windows 系统 API（丰富） | 手机 HID 协议（有限） |
| **连接** | USB Serial (CDC) | USB HID + UAC |
| **屏幕** | 电脑显示器（调试方便） | 无屏幕，靠触觉 |
| **力反馈** | ✅ 完整支持 | ✅ 完整支持 |

## 架构

```
┌──────────────────────────────────────────────┐
│                 Windows PC                     │
│                                                │
│  ┌─────────────────┐    ┌──────────────────┐  │
│  │ host.py          │    │ 音频播放          │  │
│  │ (Python 主控)    │    │ (Spotify/网易云)  │  │
│  │                 │    │                  │  │
│  │ • 读取系统音量   │    │ • 音乐播放       │  │
│  │ • 读取播放信息   │    │ • 系统音量       │  │
│  │ • 发送力反馈参数 │    │ • 媒体控制       │  │
│  │ • 接收旋钮事件   │    │                  │  │
│  └────────┬────────┘    └──────────────────┘  │
│           │ Serial (CDC)                       │
└───────────┼───────────────────────────────────┘
            │ USB-C
            ▼
┌──────────────────────────────────────────────┐
│           ESP32-S3 + 力反馈旋钮                │
│                                                │
│  ┌────────────┐    ┌────────────────────┐     │
│  │ 旋钮事件    │    │ 力反馈执行          │     │
│  │ • 旋转角度  │    │ • 力矩控制          │     │
│  │ • 按压/双击 │    │ • Detent 生成       │     │
│  │ • 长按/三击│    │ • 边界阻力          │     │
│  └─────┬──────┘    └────────┬───────────┘     │
│        │                    │                  │
│        ▼                    ▼                  │
│  ┌──────────────────────────────────────┐     │
│  │        SimpleFOC 闭环控制              │     │
│  │   AS5600 编码器 ←→ TMC6300 → BLDC    │     │
│  └──────────────────────────────────────┘     │
└──────────────────────────────────────────────┘
```

## 通信协议

ESP32 与 PC 通过 USB Serial 通信，JSON 格式，每行一条消息。

### PC → ESP32（力反馈指令）

```json
{"cmd": "mode", "mode": "volume", "position": 50, "max": 100}
{"cmd": "mode", "mode": "seek", "position": 120, "max": 240}
{"cmd": "mode", "mode": "tracklist", "position": 3, "max": 42}
{"cmd": "haptic", "type": "click"}
{"cmd": "haptic", "type": "bump"}
```

### ESP32 → PC（旋钮事件）

```json
{"event": "rotate", "direction": "cw", "steps": 3, "position": 52}
{"event": "rotate", "direction": "ccw", "steps": 1, "position": 51}
{"event": "press", "duration_ms": 120}
{"event": "double_click"}
{"event": "long_press", "duration_ms": 800}
{"event": "triple_click"}
```

## 快速开始

### 1. 硬件准备

| 组件 | 型号 | 备注 |
|------|------|------|
| MCU | ESP32-S3-DevKitC | USB Serial 原生支持 |
| 电机驱动 | TMC6300 模块 | 或 DRV8313 |
| BLDC 电机 | 2204 云台电机 | 小尺寸即可 |
| 编码器 | AS5600 模块 | I2C 接口 |
| 旋钮帽 | 铝合金 20mm | 手感重要 |

### 2. 固件烧录

```bash
cd desktop-demo/firmware
pio run -t upload
```

### 3. PC 端运行

```bash
cd desktop-demo/host
pip install pyserial pycaw comtypes
python host.py
```

### 4. 验证

- 旋转旋钮 → 系统音量变化 + 旋钮有刻度反馈
- 按下旋钮 → 播放/暂停
- 双击 → 切歌模式（每切一首一个 Detent）
- 长按 → 快进模式（无阻尼飞轮）

## 文件夹结构

```
desktop-demo/
├── README.md              ← 你在这里
├── firmware/
│   ├── platformio.ini     ← PlatformIO 配置
│   └── src/
│       └── main.cpp       ← 固件主程序
├── host/
│   └── host.py            ← PC 端 Python 主控脚本
└── hardware/
    └── wiring.md          ← 接线说明
```
