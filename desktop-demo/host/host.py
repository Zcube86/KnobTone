"""
KnobTone Desktop Demo — PC Host Script
=======================================
通过 USB Serial 与 ESP32 力反馈旋钮通信，
读取 Windows 系统音频状态，控制媒体播放。

依赖: pip install pyserial pycaw comtypes
"""

import json
import time
import sys
import threading
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

import serial
import serial.tools.list_ports

# ─── Windows Audio Control ───────────────────────────────────
try:
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from comtypes import CLSCTX_ALL
    _AUDIO_AVAILABLE = True
except ImportError:
    _AUDIO_AVAILABLE = False
    print("[WARN] pycaw 未安装，音量控制不可用。运行: pip install pycaw comtypes")


# ─── Media Control (模拟多媒体按键) ──────────────────────────
try:
    import ctypes
    from ctypes import wintypes

    # Windows 模拟按键
    VK_MEDIA_PLAY_PAUSE = 0xB3
    VK_MEDIA_NEXT_TRACK = 0xB0
    VK_MEDIA_PREV_TRACK = 0xB1
    VK_VOLUME_UP = 0xAF
    VK_VOLUME_DOWN = 0xAE
    VK_VOLUME_MUTE = 0xAD

    user32 = ctypes.windll.user32

    def send_media_key(vk_code):
        """发送 Windows 多媒体按键"""
        user32.keybd_event(vk_code, 0, 0, 0)
        time.sleep(0.02)
        user32.keybd_event(vk_code, 0, 2, 0)  # KEYEVENTF_KEYUP

    _MEDIA_AVAILABLE = True
except Exception:
    _MEDIA_AVAILABLE = False
    print("[WARN] 媒体键模拟不可用")


# ─── Data Types ──────────────────────────────────────────────

class KnobMode(Enum):
    VOLUME = auto()
    TRACKLIST = auto()
    SEEK = auto()
    EQ = auto()


@dataclass
class KnobState:
    mode: KnobMode = KnobMode.VOLUME
    position: int = 50       # 当前位置（含义取决于模式）
    max_val: int = 100       # 最大值
    last_press_time: float = 0.0
    press_count: int = 0
    is_pressed: bool = False
    press_start: float = 0.0


@dataclass
class AudioState:
    volume: float = 0.5      # 0.0 - 1.0
    muted: bool = False
    playing: bool = False
    track_title: str = ""
    track_artist: str = ""
    track_duration: float = 0.0  # 秒
    track_position: float = 0.0  # 秒


# ─── Serial Communication ───────────────────────────────────

class KnobSerial:
    """与 ESP32 力反馈旋钮的串口通信"""

    def __init__(self, port: str = None, baudrate: int = 115200):
        self.port = port
        self.baudrate = baudrate
        self.ser: Optional[serial.Serial] = None
        self._lock = threading.Lock()

    @staticmethod
    def find_port() -> Optional[str]:
        """自动查找 ESP32-S3 的串口"""
        ports = serial.tools.list_ports.comports()
        for p in ports:
            # ESP32-S3 通常显示为这些描述
            if any(kw in p.description.lower() for kw in
                   ['esp32', 'usb serial', 'ch340', 'cp210', 'usb-serial']):
                return p.device
            if any(kw in (p.manufacturer or '').lower() for kw in ['espressif', 'silicon labs']):
                return p.device
        # 没找到，返回第一个可用串口
        if ports:
            print(f"[INFO] 未识别到 ESP32，尝试第一个串口: {ports[0].device}")
            return ports[0].device
        return None

    def connect(self) -> bool:
        if self.port is None:
            self.port = self.find_port()
        if self.port is None:
            print("[ERROR] 未找到可用串口！")
            return False
        try:
            self.ser = serial.Serial(self.port, self.baudrate, timeout=0.1)
            print(f"[OK] 已连接 {self.port}")
            time.sleep(2)  # ESP32 复位后等待
            return True
        except serial.SerialException as e:
            print(f"[ERROR] 串口连接失败: {e}")
            return False

    def send(self, data: dict):
        """发送 JSON 指令到 ESP32"""
        if self.ser is None:
            return
        with self._lock:
            line = json.dumps(data, ensure_ascii=False) + '\n'
            self.ser.write(line.encode('utf-8'))

    def receive(self) -> Optional[dict]:
        """读取一行 JSON 事件"""
        if self.ser is None:
            return None
        try:
            if self.ser.in_waiting > 0:
                line = self.ser.readline().decode('utf-8').strip()
                if line:
                    return json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        return None

    def close(self):
        if self.ser:
            self.ser.close()


# ─── Audio Controller ────────────────────────────────────────

class AudioController:
    """读取和控制系统音频状态"""

    def __init__(self):
        self._volume_interface = None
        if _AUDIO_AVAILABLE:
            try:
                devices = AudioUtilities.GetSpeakers()
                interface = devices.Activate(
                    IAudioEndpointVolume._iid_, CLSCTX_ALL, None
                )
                self._volume_interface = interface.QueryInterface(IAudioEndpointVolume)
            except Exception as e:
                print(f"[WARN] 音频设备初始化失败: {e}")

    def get_volume(self) -> float:
        """获取系统音量 0.0-1.0"""
        if self._volume_interface:
            try:
                return self._volume_interface.GetMasterVolumeLevelScalar()
            except Exception:
                pass
        return 0.5

    def set_volume(self, level: float):
        """设置系统音量 0.0-1.0"""
        level = max(0.0, min(1.0, level))
        if self._volume_interface:
            try:
                self._volume_interface.SetMasterVolumeLevelScalar(level, None)
            except Exception:
                pass
        elif _MEDIA_AVAILABLE:
            # Fallback: 用媒体键微调
            current = self.get_volume()
            diff = level - current
            key = VK_VOLUME_UP if diff > 0 else VK_VOLUME_DOWN
            steps = int(abs(diff) * 50)
            for _ in range(min(steps, 20)):
                send_media_key(key)
                time.sleep(0.01)

    def get_state(self) -> AudioState:
        return AudioState(
            volume=self.get_volume(),
        )

    def play_pause(self):
        if _MEDIA_AVAILABLE:
            send_media_key(VK_MEDIA_PLAY_PAUSE)

    def next_track(self):
        if _MEDIA_AVAILABLE:
            send_media_key(VK_MEDIA_NEXT_TRACK)

    def prev_track(self):
        if _MEDIA_AVAILABLE:
            send_media_key(VK_MEDIA_PREV_TRACK)


# ─── Main Control Loop ───────────────────────────────────────

class KnobController:
    """旋钮控制器主逻辑——连接 ESP32 事件与系统音频"""

    def __init__(self, serial_conn: KnobSerial):
        self.ser = serial_conn
        self.audio = AudioController()
        self.state = KnobState()
        self.running = False

        # 防抖
        self._last_rotate_time = 0.0
        self._rotate_debounce = 0.02  # 20ms

    def _send_mode(self):
        """将当前模式状态同步给 ESP32"""
        self.ser.send({
            "cmd": "mode",
            "mode": self.state.mode.name.lower(),
            "position": self.state.position,
            "max": self.state.max_val,
        })

    def _switch_mode(self, new_mode: KnobMode):
        """切换旋钮模式"""
        old_mode = self.state.mode
        self.state.mode = new_mode

        # 根据新模式设置参数
        if new_mode == KnobMode.VOLUME:
            vol = self.audio.get_volume()
            self.state.position = int(vol * 100)
            self.state.max_val = 100
        elif new_mode == KnobMode.TRACKLIST:
            self.state.position = 0
            self.state.max_val = 50  # 假设歌单最多 50 首
        elif new_mode == KnobMode.SEEK:
            self.state.position = 0
            self.state.max_val = 240  # 假设歌曲最长 4 分钟
        elif new_mode == KnobMode.EQ:
            self.state.position = 0
            self.state.max_val = 5

        self._send_mode()
        # 切换确认振动
        self.ser.send({"cmd": "haptic", "type": "click"})
        print(f"[MODE] {old_mode.name} → {new_mode.name}")

    def _handle_rotate(self, event: dict):
        """处理旋转事件"""
        now = time.time()
        if now - self._last_rotate_time < self._rotate_debounce:
            return  # 防抖
        self._last_rotate_time = now

        direction = event.get("direction", "cw")
        steps = event.get("steps", 1)
        delta = steps if direction == "cw" else -steps
        self.state.position += delta
        self.state.position = max(0, min(self.state.max_val, self.state.position))

        mode = self.state.mode
        if mode == KnobMode.VOLUME:
            new_vol = self.state.position / 100.0
            self.audio.set_volume(new_vol)
            print(f"🔊 音量: {self.state.position}%")

        elif mode == KnobMode.TRACKLIST:
            if delta > 0:
                self.audio.next_track()
                print(f"⏭ 下一首")
            else:
                self.audio.prev_track()
                print(f"⏮ 上一首")

        elif mode == KnobMode.SEEK:
            # 快进/快退（TODO: 需要更精确的 API）
            direction_str = "快进" if delta > 0 else "快退"
            print(f"⏩ {direction_str} {abs(delta)} 秒")

        elif mode == KnobMode.EQ:
            print(f"🎚 EQ 预设: {self.state.position}")

    def _handle_press(self, event: dict):
        """处理按压事件"""
        mode = self.state.mode
        if mode == KnobMode.VOLUME:
            self.audio.play_pause()
            print("⏯ 播放/暂停")
        elif mode in (KnobMode.TRACKLIST, KnobMode.EQ):
            # 在切歌/EQ 模式下，按压 = 确认选择，返回音量模式
            self._switch_mode(KnobMode.VOLUME)
        elif mode == KnobMode.SEEK:
            # 快进模式下按压 = 确认位置
            self._switch_mode(KnobMode.VOLUME)

    def _handle_event(self, event: dict):
        """分发事件"""
        event_type = event.get("event", "")

        if event_type == "rotate":
            self._handle_rotate(event)
        elif event_type == "press":
            self._handle_press(event)
        elif event_type == "double_click":
            print("👆 双击")
            if self.state.mode != KnobMode.TRACKLIST:
                self._switch_mode(KnobMode.TRACKLIST)
        elif event_type == "long_press":
            print("👇 长按")
            if self.state.mode != KnobMode.SEEK:
                self._switch_mode(KnobMode.SEEK)
            else:
                # 在 SEEK 模式下长按 = 退出
                self._switch_mode(KnobMode.VOLUME)
        elif event_type == "triple_click":
            print("👆👆👆 三击")
            if self.state.mode != KnobMode.EQ:
                self._switch_mode(KnobMode.EQ)
        else:
            print(f"[EVENT] 未知事件: {event}")

    def run(self):
        """主循环"""
        if not self.ser.ser:
            print("[ERROR] 串口未连接")
            return

        self.running = True
        self._send_mode()  # 初始化模式
        print("\n🎛️  KnobTone Desktop Demo 已启动")
        print("    旋转 = 音量 | 按下 = 播放/暂停")
        print("    双击 = 切歌模式 | 长按 = 快进模式 | 三击 = EQ 模式")
        print("    Ctrl+C 退出\n")

        try:
            while self.running:
                event = self.ser.receive()
                if event:
                    self._handle_event(event)
                time.sleep(0.001)  # 1ms 轮询
        except KeyboardInterrupt:
            print("\n👋 退出")
        finally:
            self.ser.close()


# ─── Entry Point ─────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="KnobTone Desktop Demo Host")
    parser.add_argument("--port", "-p", type=str, help="串口号 (如 COM3)")
    parser.add_argument("--list", "-l", action="store_true", help="列出所有串口")
    args = parser.parse_args()

    if args.list:
        ports = serial.tools.list_ports.comports()
        for p in ports:
            print(f"  {p.device} — {p.description} [{p.manufacturer}]")
        return

    ser = KnobSerial(port=args.port)
    if not ser.connect():
        print("\n可用的串口：")
        ports = serial.tools.list_ports.comports()
        for p in ports:
            print(f"  {p.device} — {p.description}")
        sys.exit(1)

    controller = KnobController(ser)
    controller.run()


if __name__ == "__main__":
    main()
