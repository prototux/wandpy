#!/usr/bin/env python3
"""
Wand Data Visualizer - Uses wandpy library with orientation projections

Works with the Kano Coding Wand and the Magic Caster Wand (the Fused view
stays empty on the Magic Caster, which has no on-device fusion).

Usage:
    python wandview.py                      # Scan and pick a wand
    python wandview.py AA:BB:CC:DD:EE:FF
"""

import asyncio
import math
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import pygame
from pygame.locals import *

from wandpy import ButtonEvent, Color, VibrationPattern, Wand
from wandpy.imu import QuaternionData, RawData, FusedData


# ============== DATA STRUCTURES ==============


@dataclass
class DataPacket:
    timestamp: float
    data_type: str
    data: Any


class DataBuffer:
    def __init__(self, maxlen: int = 1000):
        self.packets: deque = deque(maxlen=maxlen)
        self._paused = False
        self._paused_snapshot: Optional[list] = None

    def append(self, packet: DataPacket):
        if not self._paused:
            self.packets.append(packet)

    def get_recent(self, seconds: float = 5.0) -> List[DataPacket]:
        if self._paused and self._paused_snapshot:
            return self._paused_snapshot
        cutoff = time.time() - seconds
        return [p for p in self.packets if p.timestamp >= cutoff]

    def take_snapshot(self):
        self._paused_snapshot = list(self.packets)

    def clear_snapshot(self):
        self._paused_snapshot = None


@dataclass
class GraphSeries:
    name: str
    color: Tuple[int, int, int]
    extractor: Callable[[Any], Optional[float]]


@dataclass
class GraphView:
    title: str
    series: List[GraphSeries]
    y_range: Optional[Tuple[float, float]] = None


# ============== 3D MATH HELPERS ==============


def quat_to_rot_matrix(q1: float, q2: float, q3: float, q4: float) -> List[List[float]]:
    """Quaternion (w,x,y,z) to 3x3 rotation matrix."""
    length = math.sqrt(q1*q1 + q2*q2 + q3*q3 + q4*q4)
    if length == 0:
        return [[1,0,0],[0,1,0],[0,0,1]]
    w, x, y, z = q1/length, q2/length, q3/length, q4/length
    return [
        [1-2*(y*y+z*z), 2*(x*y-w*z), 2*(x*z+w*y)],
        [2*(x*y+w*z), 1-2*(x*x+z*z), 2*(y*z-w*x)],
        [2*(x*z-w*y), 2*(y*z+w*x), 1-2*(x*x+y*y)],
    ]


def euler_to_rot_matrix(yaw: float, pitch: float, roll: float) -> List[List[float]]:
    """Euler angles (degrees) to 3x3 rotation matrix."""
    yr, pr, rr = math.radians(yaw), math.radians(pitch), math.radians(roll)
    cy, sy = math.cos(yr), math.sin(yr)
    cp, sp = math.cos(pr), math.sin(pr)
    cr, sr = math.cos(rr), math.sin(rr)
    return [
        [cy*cp, cy*sp*sr-sy*cr, cy*sp*cr+sy*sr],
        [sy*cp, sy*sp*sr+cy*cr, sy*sp*cr-cy*sr],
        [-sp, cp*sr, cp*cr],
    ]


def mat_vec_mul(m: List[List[float]], v: List[float]) -> List[float]:
    return [m[0][0]*v[0]+m[0][1]*v[1]+m[0][2]*v[2],
            m[1][0]*v[0]+m[1][1]*v[1]+m[1][2]*v[2],
            m[2][0]*v[0]+m[2][1]*v[1]+m[2][2]*v[2]]


def rotate_point(p: List[float], rot_matrix: List[List[float]]) -> List[float]:
    return mat_vec_mul(rot_matrix, p)


def project_point(p: List[float], cx: int, cy: int, scale: float = 200, focal: float = 3.0) -> Tuple[int, int]:
    """Perspective projection. Camera at (0,0,-focal), looking at +Z."""
    z = max(p[2] + focal, 0.1)
    x = p[0] * scale * focal / z
    y = -p[1] * scale * focal / z  # flip Y for screen
    return (int(cx + x), int(cy + y))


def project_point_ortho(p: List[float], cx: int, cy: int, scale: float = 120) -> Tuple[int, int]:
    """Orthographic projection (no perspective)."""
    return (int(cx + p[0] * scale), int(cy - p[1] * scale))


# ============== MAIN VISUALIZER ==============


class WandVisualizer:
    def __init__(self, mac_address: str = None, width: int = 1920, height: int = 1080):
        self.width = width
        self.height = height
        self.mac_address = mac_address
        self.wand = Wand(mac_address=mac_address, auto_reconnect=True)
        self.running = True
        self.paused = False
        self.current_view = 0

        self.buffers = {
            "quaternions": DataBuffer(maxlen=1000),
            "raw": DataBuffer(maxlen=1000),
            "fused": DataBuffer(maxlen=1000),
        }
        self.stats = {
            "quat_count": 0, "raw_count": 0, "fused_count": 0,
            "start_time": time.time(), "last_packet_time": 0,
        }
        self.latest = {"quaternions": None, "raw": None, "fused": None}

        self.screen = None
        self.clock = None
        self.fonts = {}

        self.COLORS = {
            "bg": (25, 25, 30), "panel": (35, 35, 40), "border": (60, 60, 70),
            "text": (220, 220, 220), "text_dim": (150, 150, 150),
            "accent": (100, 150, 255), "red": (255, 100, 100),
            "green": (100, 255, 100), "blue": (100, 150, 255),
            "yellow": (255, 255, 100), "purple": (200, 100, 255),
            "cyan": (100, 255, 255), "orange": (255, 165, 0),
            "white": (255, 255, 255), "quat_color": (0, 200, 255),
            "fused_color": (255, 140, 0), "fused_color2": (255, 80, 80),
        }

        # Simple wand model: vertices of a rectangular prism (wand pointing along -Z)
        # Tip at z=-2, base at z=2
        self.wand_verts = [
            [-0.2, -0.2, -2.0], [0.2, -0.2, -2.0], [0.2, 0.2, -2.0], [-0.2, 0.2, -2.0],  # tip
            [-0.3, -0.3, 2.0], [0.3, -0.3, 2.0], [0.3, 0.3, 2.0], [-0.3, 0.3, 2.0],      # base
        ]
        self.wand_edges = [
            (0,1), (1,2), (2,3), (3,0),   # tip face
            (4,5), (5,6), (6,7), (7,4),   # base face
            (0,4), (1,5), (2,6), (3,7),   # connecting edges
        ]
        self.wand_faces = [
            (0, 1, 2, 3),  # tip (front)
            (4, 7, 6, 5),  # base (back)
            (0, 4, 7, 3),  # left
            (1, 5, 6, 2),  # right
            (0, 1, 5, 4),  # bottom
            (3, 2, 6, 7),  # top
        ]
        self.wand_face_colors = [
            (255, 80, 80),    # tip - red
            (80, 255, 80),    # base - green
            (100, 100, 120),  # left
            (100, 100, 120),  # right
            (100, 100, 120),  # bottom
            (100, 100, 120),  # top
        ]

        self._init_graph_views()
        self.button_rects = {}

    def _init_graph_views(self):
        self.graph_views = {
            "quaternion_raw": GraphView("Quaternion Raw Components", [
                GraphSeries("q1", self.COLORS["red"], lambda d: d.raw.get("q1") if d.raw else None),
                GraphSeries("q2", self.COLORS["green"], lambda d: d.raw.get("q2") if d.raw else None),
                GraphSeries("q3", self.COLORS["blue"], lambda d: d.raw.get("q3") if d.raw else None),
                GraphSeries("q4", self.COLORS["yellow"], lambda d: d.raw.get("q4") if d.raw else None),
            ], (-1200, 1200)),
            "quaternion_euler": GraphView("Quaternion Euler Angles", [
                GraphSeries("yaw", self.COLORS["red"], lambda d: d.yaw),
                GraphSeries("pitch", self.COLORS["green"], lambda d: d.pitch),
                GraphSeries("roll", self.COLORS["blue"], lambda d: d.roll),
            ], (-180, 180)),
            "raw_accel": GraphView("Raw Accelerometer", [
                GraphSeries("x", self.COLORS["red"], lambda d: d.accel.get("x") if d.accel else None),
                GraphSeries("y", self.COLORS["green"], lambda d: d.accel.get("y") if d.accel else None),
                GraphSeries("z", self.COLORS["blue"], lambda d: d.accel.get("z") if d.accel else None),
            ], (-2048, 2048)),
            "raw_gyro": GraphView("Raw Gyroscope", [
                GraphSeries("0", self.COLORS["red"], lambda d: d.gyro_xyz[0] if d.gyro_xyz else None),
                GraphSeries("1", self.COLORS["green"], lambda d: d.gyro_xyz[1] if d.gyro_xyz else None),
                GraphSeries("2", self.COLORS["blue"], lambda d: d.gyro_xyz[2] if d.gyro_xyz else None),
            ], (-32768, 32767)),
            "raw_mag": GraphView("Raw Magnetometer", [
                GraphSeries("x", self.COLORS["red"], lambda d: d.mag.get("x") if d.mag else None),
                GraphSeries("y", self.COLORS["green"], lambda d: d.mag.get("y") if d.mag else None),
                GraphSeries("z", self.COLORS["blue"], lambda d: d.mag.get("z") if d.mag else None),
            ], (-1800, 1800)),
            "fused_accel": GraphView("Fused Acceleration", [
                GraphSeries("x", self.COLORS["red"], lambda d: d.filtered_accel.get("x") if d.filtered_accel else None),
                GraphSeries("y", self.COLORS["green"], lambda d: d.filtered_accel.get("y") if d.filtered_accel else None),
                GraphSeries("z", self.COLORS["blue"], lambda d: d.filtered_accel.get("z") if d.filtered_accel else None),
            ], (-32768, 32767)),
            "fused_orientation": GraphView("Fused Orientation", [
                GraphSeries("pitch", self.COLORS["red"], lambda d: d.pitch),
                GraphSeries("roll_sin", self.COLORS["green"], lambda d: d.roll_sin),
                GraphSeries("roll_cos", self.COLORS["blue"], lambda d: d.roll_cos),
            ], (-1000, 1000)),
            "fused_heading": GraphView("Fused Heading", [
                GraphSeries("compass", self.COLORS["red"], lambda d: d.compass),
                GraphSeries("abs_pitch", self.COLORS["green"], lambda d: d.absolute_pitch),
                GraphSeries("roll", self.COLORS["blue"], lambda d: d.roll),
            ], (-1800, 1800)),
        }

    # ============== CALLBACKS ==============

    def _on_imu_quaternions(self, data: QuaternionData):
        self.stats["quat_count"] += 1
        self.stats["last_packet_time"] = time.time()
        packet = DataPacket(time.time(), "quaternion", data)
        if not self.paused:
            self.buffers["quaternions"].append(packet)
            self.latest["quaternions"] = packet

    def _on_imu_raw(self, data: RawData):
        self.stats["raw_count"] += 1
        self.stats["last_packet_time"] = time.time()
        packet = DataPacket(time.time(), "raw", data)
        if not self.paused:
            self.buffers["raw"].append(packet)
            self.latest["raw"] = packet

    def _on_imu_fused(self, data: FusedData):
        self.stats["fused_count"] += 1
        self.stats["last_packet_time"] = time.time()
        packet = DataPacket(time.time(), "fused", data)
        if not self.paused:
            self.buffers["fused"].append(packet)
            self.latest["fused"] = packet

    def _on_button(self, event: ButtonEvent, state):
        if event == ButtonEvent.PRESSED:
            asyncio.create_task(self.wand.set_led(Color.PURPLE))
        elif event == ButtonEvent.RELEASED:
            asyncio.create_task(self.wand.set_led(Color.TEAL))
            if state.was_double_press(window_ms=500):
                asyncio.create_task(self.wand.vibrate(VibrationPattern.BURST))

    def _on_disconnect(self):
        print("Wand disconnected!")

    # ============== CONNECTION ==============

    async def connect(self, mac_address: Optional[str] = None):
        target = mac_address or self.mac_address
        if not target:
            print("Scanning for wands...")
            devices = await self.wand.scan(timeout=5.0)
            if not devices:
                print("No wands found!")
                return False
            for i, device in enumerate(devices):
                print(f"  [{i}] {device}")
            choice = int(input("Select wand: ")) if len(devices) > 1 else 0
            target = devices[choice]

        self.wand.on_imu_quaternions = self._on_imu_quaternions
        self.wand.on_imu_raw = self._on_imu_raw
        self.wand.on_imu_fused = self._on_imu_fused
        self.wand.on_button_event = self._on_button
        self.wand.on_disconnect = self._on_disconnect

        success = await self.wand.connect(target)
        if success:
            print(f"Connected to {self.wand.address} ({self.wand.wand_type.value})")
            await self.wand.set_led(Color.TEAL)
        return success

    async def disconnect(self):
        await self.wand.disconnect()

    # ============== PYGAME ==============

    def init_pygame(self):
        pygame.init()
        pygame.display.set_caption("Wand Visualizer (wandpy)")
        self.screen = pygame.display.set_mode((self.width, self.height), DOUBLEBUF)
        self.clock = pygame.time.Clock()
        self.fonts = {
            "small": pygame.font.SysFont("monospace", 10),
            "normal": pygame.font.SysFont("monospace", 12),
            "title": pygame.font.SysFont("monospace", 14, bold=True),
            "big": pygame.font.SysFont("monospace", 20, bold=True),
        }

    def toggle_pause(self):
        self.paused = not self.paused
        if self.paused:
            for buf in self.buffers.values():
                buf.take_snapshot()
        else:
            for buf in self.buffers.values():
                buf.clear_snapshot()

    # ============== MAIN DRAW ==============

    def draw(self):
        self.screen.fill(self.COLORS["bg"])
        self._draw_header()
        content_rect = pygame.Rect(10, 70, self.width - 20, self.height - 130)

        if self.current_view == 0:
            self._draw_quaternions_view(content_rect)
        elif self.current_view == 1:
            self._draw_raw_view(content_rect)
        elif self.current_view == 2:
            self._draw_fused_view(content_rect)
        elif self.current_view == 3:
            self._draw_projections_view(content_rect)
        elif self.current_view == 4:
            self._draw_3d_orientation_view(content_rect)

        self._draw_stats_bar()
        self._draw_instructions()
        pygame.display.flip()

    def _draw_header(self):
        title = self.fonts["big"].render("Wand Visualizer (wandpy)", True, self.COLORS["accent"])
        self.screen.blit(title, (10, 5))
        status = "CONNECTED" if self.wand.is_connected else "DISCONNECTED"
        color = self.COLORS["green"] if self.wand.is_connected else self.COLORS["red"]
        self.screen.blit(self.fonts["normal"].render(status, True, color), (10, 35))

        views = ["Quaternions", "Raw IMU", "Fused", "Projections", "3D Orientation"]
        x = 100
        for i, view in enumerate(views):
            rect = pygame.Rect(x, 30, 150, 30)
            c = self.COLORS["accent"] if i == self.current_view else self.COLORS["panel"]
            pygame.draw.rect(self.screen, c, rect, border_radius=4)
            pygame.draw.rect(self.screen, self.COLORS["border"], rect, 2, border_radius=4)
            text = self.fonts["normal"].render(view, True, self.COLORS["text"])
            self.screen.blit(text, text.get_rect(center=rect.center))
            x += 160

        btn_x = self.width - 280
        pause_rect = pygame.Rect(btn_x, 5, 70, 25)
        c = self.COLORS["orange"] if self.paused else self.COLORS["panel"]
        pygame.draw.rect(self.screen, c, pause_rect, border_radius=4)
        self.screen.blit(self.fonts["normal"].render("PAUSE" if not self.paused else "RESUME", True, self.COLORS["text"]),
                        self.fonts["normal"].render("PAUSE" if not self.paused else "RESUME", True, self.COLORS["text"]).get_rect(center=pause_rect.center))

        calq_rect = pygame.Rect(btn_x + 80, 5, 70, 25)
        pygame.draw.rect(self.screen, self.COLORS["accent"], calq_rect, border_radius=4)
        self.screen.blit(self.fonts["normal"].render("CalQ", True, self.COLORS["text"]),
                        self.fonts["normal"].render("CalQ", True, self.COLORS["text"]).get_rect(center=calq_rect.center))

        calm_rect = pygame.Rect(btn_x + 160, 5, 80, 25)
        pygame.draw.rect(self.screen, self.COLORS["accent"], calm_rect, border_radius=4)
        self.screen.blit(self.fonts["normal"].render("CalM", True, self.COLORS["text"]),
                        self.fonts["normal"].render("CalM", True, self.COLORS["text"]).get_rect(center=calm_rect.center))

        self.button_rects = {
            "pause": pause_rect, "calq": calq_rect, "calm": calm_rect,
            "views": [pygame.Rect(100 + i * 160, 30, 150, 30) for i in range(len(views))],
        }

    # ============== QUATERNIONS VIEW ==============

    def _draw_quaternions_view(self, rect):
        """Left: graphs, Right: yaw compass, pitch vertical, roll circular gauge."""
        left_w = rect.width * 2 // 3
        graph_h = (rect.height - 10) // 2

        self._draw_graph(pygame.Rect(rect.x, rect.y, left_w, graph_h),
                        self.graph_views["quaternion_raw"], self.buffers["quaternions"])
        self._draw_graph(pygame.Rect(rect.x, rect.y + graph_h + 10, left_w, graph_h),
                        self.graph_views["quaternion_euler"], self.buffers["quaternions"])

        right_x = rect.x + left_w + 10
        right_w = rect.width - left_w - 10
        panel_h = rect.height // 3

        self._draw_compass(pygame.Rect(right_x, rect.y, right_w, panel_h - 5), self.latest["quaternions"])
        self._draw_vertical_bar(pygame.Rect(right_x, rect.y + panel_h, right_w, panel_h - 5), self.latest["quaternions"])
        self._draw_roll_gauge(pygame.Rect(right_x, rect.y + 2 * panel_h, right_w, panel_h - 5), self.latest["quaternions"])

    def _draw_compass(self, rect, packet: Optional[DataPacket]):
        """Circular compass with N/E/S/W labels and rotating needle."""
        pygame.draw.rect(self.screen, self.COLORS["panel"], rect)
        pygame.draw.rect(self.screen, self.COLORS["border"], rect, 2)
        self.screen.blit(self.fonts["title"].render("Yaw (Compass)", True, self.COLORS["accent"]), (rect.x + 10, rect.y + 5))

        cx = rect.x + rect.width // 2
        cy = rect.y + rect.height // 2 + 8
        r = min(rect.width, rect.height) * 0.35

        # Outer circle
        pygame.draw.circle(self.screen, (50, 50, 55), (cx, cy), int(r), 2)
        pygame.draw.circle(self.screen, (40, 40, 45), (cx, cy), int(r - 4), 1)

        # Cardinal labels
        for label, angle_deg in [("N", 0), ("E", 90), ("S", 180), ("W", 270)]:
            rad = math.radians(angle_deg - 90)
            lx = cx + int((r - 22) * math.cos(rad))
            ly = cy + int((r - 22) * math.sin(rad))
            text = self.fonts["normal"].render(label, True, self.COLORS["text_dim"])
            self.screen.blit(text, (lx - 5, ly - 6))

        # Degree ticks (every 30°)
        for deg in range(0, 360, 30):
            rad = math.radians(deg - 90)
            x1 = cx + int(r * math.cos(rad))
            y1 = cy + int(r * math.sin(rad))
            tick_len = 12 if deg % 90 == 0 else 6
            x2 = cx + int((r - tick_len) * math.cos(rad))
            y2 = cy + int((r - tick_len) * math.sin(rad))
            pygame.draw.line(self.screen, (100, 100, 110), (x1, y1), (x2, y2), 2)

        # Inner degree numbers (every 30°)
        for deg in range(0, 360, 30):
            if deg % 90 != 0:
                rad = math.radians(deg - 90)
                tx = cx + int((r - 28) * math.cos(rad))
                ty = cy + int((r - 28) * math.sin(rad))
                text = self.fonts["small"].render(str(deg), True, (80, 80, 90))
                self.screen.blit(text, (tx - 8, ty - 5))

        # Needle
        angle = 0.0
        if packet and packet.data and isinstance(packet.data, QuaternionData):
            angle = packet.data.yaw or 0.0

        rad = math.radians(-angle - 90)
        nx = cx + int((r - 18) * math.cos(rad))
        ny = cy + int((r - 18) * math.sin(rad))

        # Triangle needle
        tip = (nx, ny)
        left = (cx + int(10 * math.cos(rad + 2.4)), cy + int(10 * math.sin(rad + 2.4)))
        right = (cx + int(10 * math.cos(rad - 2.4)), cy + int(10 * math.sin(rad - 2.4)))
        pygame.draw.polygon(self.screen, self.COLORS["red"], [tip, left, right])
        pygame.draw.circle(self.screen, self.COLORS["white"], (cx, cy), 5)
        pygame.draw.circle(self.screen, self.COLORS["red"], (cx, cy), 5, 1)

        # Value
        val_text = self.fonts["big"].render(f"{angle:6.1f}°", True, self.COLORS["accent"])
        self.screen.blit(val_text, (rect.x + 10, rect.y + rect.height - 28))

    def _draw_vertical_bar(self, rect, packet: Optional[DataPacket]):
        """Vertical bar gauge for pitch (clinometer style)."""
        pygame.draw.rect(self.screen, self.COLORS["panel"], rect)
        pygame.draw.rect(self.screen, self.COLORS["border"], rect, 2)
        self.screen.blit(self.fonts["title"].render("Pitch", True, self.COLORS["accent"]), (rect.x + 10, rect.y + 5))

        cx = rect.x + rect.width // 2
        bar_top = rect.y + 30
        bar_bottom = rect.y + rect.height - 30
        bar_w = 24

        # Bar background
        pygame.draw.rect(self.screen, (40, 40, 45), (cx - bar_w//2, bar_top, bar_w, bar_bottom - bar_top))
        pygame.draw.rect(self.screen, self.COLORS["border"], (cx - bar_w//2, bar_top, bar_w, bar_bottom - bar_top), 2)

        # Center line (0°)
        center_y = (bar_top + bar_bottom) // 2
        pygame.draw.line(self.screen, self.COLORS["white"], (cx - 35, center_y), (cx + 35, center_y), 2)
        self.screen.blit(self.fonts["small"].render("0°", True, self.COLORS["text_dim"]), (cx + 40, center_y - 6))

        # Tick marks
        bar_h = bar_bottom - bar_top
        for deg in range(-90, 91, 15):
            tick_y = center_y - int((deg / 90) * (bar_h // 2 - 10))
            tick_w = 18 if deg % 30 == 0 else 10
            pygame.draw.line(self.screen, (100, 100, 110), (cx - tick_w, tick_y), (cx + tick_w, tick_y), 1)
            if deg % 30 == 0:
                self.screen.blit(self.fonts["small"].render(f"{deg}°", True, self.COLORS["text_dim"]),
                               (cx + 45, tick_y - 6))

        # Indicator
        angle = 0.0
        if packet and packet.data and isinstance(packet.data, QuaternionData):
            angle = packet.data.pitch or 0.0

        clamped = max(-90, min(90, angle))
        indicator_y = center_y - int((clamped / 90) * (bar_h // 2 - 10))

        # Triangle pointer
        pygame.draw.polygon(self.screen, self.COLORS["green"], [
            (cx - bar_w//2 - 8, indicator_y - 7),
            (cx - bar_w//2 - 8, indicator_y + 7),
            (cx - bar_w//2 + 2, indicator_y),
        ])
        pygame.draw.circle(self.screen, self.COLORS["green"], (cx, indicator_y), 6)
        pygame.draw.circle(self.screen, self.COLORS["white"], (cx, indicator_y), 6, 2)

        # Value
        val_text = self.fonts["big"].render(f"{angle:6.1f}°", True, self.COLORS["accent"])
        self.screen.blit(val_text, (rect.x + 10, rect.y + rect.height - 28))

    def _draw_roll_gauge(self, rect, packet: Optional[DataPacket]):
        """Circular roll gauge with tick bars at regular angles (like sport watch bezel)."""
        pygame.draw.rect(self.screen, self.COLORS["panel"], rect)
        pygame.draw.rect(self.screen, self.COLORS["border"], rect, 2)
        self.screen.blit(self.fonts["title"].render("Roll", True, self.COLORS["accent"]), (rect.x + 10, rect.y + 5))

        cx = rect.x + rect.width // 2
        cy = rect.y + rect.height // 2 + 8
        r = min(rect.width, rect.height) * 0.35

        # Outer ring with tick bars at regular angles
        for deg in range(0, 360, 10):
            rad = math.radians(deg - 90)  # -90 to start at top
            is_major = deg % 30 == 0
            is_cardinal = deg % 90 == 0

            # Tick bar length
            if is_cardinal:
                tick_len = 18
                tick_w = 3
            elif is_major:
                tick_len = 12
                tick_w = 2
            else:
                tick_len = 6
                tick_w = 1

            x1 = cx + int((r - tick_len) * math.cos(rad))
            y1 = cy + int((r - tick_len) * math.sin(rad))
            x2 = cx + int(r * math.cos(rad))
            y2 = cy + int(r * math.sin(rad))

            color = (180, 180, 190) if is_cardinal else (120, 120, 130) if is_major else (80, 80, 90)
            pygame.draw.line(self.screen, color, (x1, y1), (x2, y2), tick_w)

        # Cardinal labels
        for label, angle_deg in [("0°", 0), ("90°", 90), ("180°", 180), ("270°", 270)]:
            rad = math.radians(angle_deg - 90)
            lx = cx + int((r - 30) * math.cos(rad))
            ly = cy + int((r - 30) * math.sin(rad))
            text = self.fonts["small"].render(label, True, self.COLORS["text_dim"])
            self.screen.blit(text, (lx - 10, ly - 5))

        # Inner circle (bezel inner edge)
        pygame.draw.circle(self.screen, (60, 60, 70), (cx, cy), int(r - 22), 1)

        # Center dot
        pygame.draw.circle(self.screen, (80, 80, 90), (cx, cy), 4)

        # Roll indicator (aircraft symbol that rotates with roll)
        angle = 0.0
        if packet and packet.data and isinstance(packet.data, QuaternionData):
            angle = packet.data.roll or 0.0

        # Draw a small aircraft symbol that rotates
        rad = math.radians(angle)
        # Wings
        wing_len = int(r * 0.5)
        left_wing = (cx + int(wing_len * math.cos(rad + math.pi/2)), cy + int(wing_len * math.sin(rad + math.pi/2)))
        right_wing = (cx + int(wing_len * math.cos(rad - math.pi/2)), cy + int(wing_len * math.sin(rad - math.pi/2)))
        pygame.draw.line(self.screen, self.COLORS["yellow"], left_wing, right_wing, 3)

        # Nose
        nose = (cx + int(wing_len * 0.4 * math.cos(rad)), cy + int(wing_len * 0.4 * math.sin(rad)))
        pygame.draw.line(self.screen, self.COLORS["yellow"], (cx, cy), nose, 3)

        # Tail
        tail = (cx - int(wing_len * 0.3 * math.cos(rad)), cy - int(wing_len * 0.3 * math.sin(rad)))
        pygame.draw.line(self.screen, self.COLORS["yellow"], (cx, cy), tail, 2)

        # Bank angle arc indicator (small triangle at top pointing to current roll)
        bank_rad = math.radians(-angle - 90)
        bank_tip = (cx + int((r - 8) * math.cos(bank_rad)), cy + int((r - 8) * math.sin(bank_rad)))
        bank_left = (cx + int((r - 2) * math.cos(bank_rad + 0.15)), cy + int((r - 2) * math.sin(bank_rad + 0.15)))
        bank_right = (cx + int((r - 2) * math.cos(bank_rad - 0.15)), cy + int((r - 2) * math.sin(bank_rad - 0.15)))
        pygame.draw.polygon(self.screen, self.COLORS["red"], [bank_tip, bank_left, bank_right])

        # Value
        val_text = self.fonts["big"].render(f"{angle:6.1f}°", True, self.COLORS["accent"])
        self.screen.blit(val_text, (rect.x + 10, rect.y + rect.height - 28))

    # ============== RAW & FUSED VIEWS ==============

    def _draw_raw_view(self, rect):
        w = (rect.width - 20) // 2
        h = (rect.height - 20) // 2
        self._draw_graph(pygame.Rect(rect.x, rect.y, w, h), self.graph_views["raw_accel"], self.buffers["raw"])
        self._draw_graph(pygame.Rect(rect.x + w + 10, rect.y, w, h), self.graph_views["raw_gyro"], self.buffers["raw"])
        self._draw_graph(pygame.Rect(rect.x, rect.y + h + 10, w, h), self.graph_views["raw_mag"], self.buffers["raw"])
        self._draw_raw_info(pygame.Rect(rect.x + w + 10, rect.y + h + 10, w, h), self.latest["raw"])

    def _draw_fused_view(self, rect):
        w = (rect.width - 20) // 2
        h = (rect.height - 20) // 2
        self._draw_graph(pygame.Rect(rect.x, rect.y, w, h), self.graph_views["fused_accel"], self.buffers["fused"])
        self._draw_graph(pygame.Rect(rect.x + w + 10, rect.y, w, h), self.graph_views["fused_orientation"], self.buffers["fused"])
        self._draw_graph(pygame.Rect(rect.x, rect.y + h + 10, w, h), self.graph_views["fused_heading"], self.buffers["fused"])
        self._draw_fused_info(pygame.Rect(rect.x + w + 10, rect.y + h + 10, w, h), self.latest["fused"])

    # ============== PROJECTIONS VIEW (SIMPLIFIED) ==============

    def _draw_projections_view(self, rect):
        """Single projection panel with 2 dots: quaternion front, fused (pitch,compass) and (abs_pitch,compass)."""
        pygame.draw.rect(self.screen, self.COLORS["panel"], rect)
        pygame.draw.rect(self.screen, self.COLORS["border"], rect, 2)

        title = self.fonts["big"].render("Orientation Projection (Pitch vs Compass)", True, self.COLORS["accent"])
        self.screen.blit(title, (rect.x + 10, rect.y + 5))

        # Single large panel for the projection
        proj_rect = pygame.Rect(rect.x + 10, rect.y + 35, rect.width - 20, rect.height - 45)
        pygame.draw.rect(self.screen, (30, 30, 35), proj_rect)
        pygame.draw.rect(self.screen, (50, 50, 55), proj_rect, 1)

        cx = proj_rect.x + proj_rect.width // 2
        cy = proj_rect.y + proj_rect.height // 2
        scale = min(proj_rect.width, proj_rect.height) * 0.4

        # Grid lines
        for j in range(-4, 5):
            offset = j * scale / 4
            pygame.draw.line(self.screen, (50, 50, 55), (cx + int(offset), proj_rect.y + 10), (cx + int(offset), proj_rect.y + proj_rect.height - 10), 1)
            pygame.draw.line(self.screen, (50, 50, 55), (proj_rect.x + 10, cy + int(offset)), (proj_rect.x + proj_rect.width - 10, cy + int(offset)), 1)

        # Boundary circle
        pygame.draw.circle(self.screen, (70, 70, 75), (cx, cy), int(scale), 1)
        pygame.draw.circle(self.screen, (60, 60, 65), (cx, cy), 3)

        # Crosshair
        pygame.draw.line(self.screen, (100, 100, 105), (cx - 10, cy), (cx + 10, cy), 1)
        pygame.draw.line(self.screen, (100, 100, 105), (cx, cy - 10), (cx, cy + 10), 1)

        # Axis labels
        self.screen.blit(self.fonts["normal"].render("Compass →", True, self.COLORS["text_dim"]), (proj_rect.x + proj_rect.width - 80, cy + 5))
        self.screen.blit(self.fonts["normal"].render("↑ Pitch", True, self.COLORS["text_dim"]), (cx + 5, proj_rect.y + 5))

        # --- Quaternion dot (front projection: yaw on X, pitch on Y) ---
        qx, qy = cx, cy  # default center
        if self.latest["quaternions"] and self.latest["quaternions"].data:
            data = self.latest["quaternions"].data
            if isinstance(data, QuaternionData) and data.euler:
                yaw = ((data.yaw or 0) + 180) % 360 - 180  # normalize to [-180, 180]
                pitch = max(-90, min(90, data.pitch or 0))
                qx = cx + int(yaw / 180 * scale) *-2
                qy = cy - int(pitch / 90 * scale) *2

        # Draw quaternion dot with glow
        for r, alpha in [(20, 30), (14, 60), (8, 120)]:
            s = pygame.Surface((r*2, r*2), pygame.SRCALPHA)
            pygame.draw.circle(s, (*self.COLORS["quat_color"], alpha), (r, r), r)
            self.screen.blit(s, (qx - r, qy - r))
        pygame.draw.circle(self.screen, self.COLORS["quat_color"], (qx, qy), 6)
        pygame.draw.circle(self.screen, self.COLORS["white"], (qx, qy), 6, 2)

        # Label
        self.screen.blit(self.fonts["normal"].render("Quat", True, self.COLORS["quat_color"]), (qx + 12, qy - 8))

        # --- Fused dot 1: (pitch, compass) ---
        f1x, f1y = cx, cy
        if self.latest["fused"] and self.latest["fused"].data:
            data = self.latest["fused"].data
            if isinstance(data, FusedData) and data.valid:
                compass = ((data.compass or 0) / 10.0 + 180) % 360 - 180  # compass is -1800 to 1800, scale by 10
                pitch = (data.pitch or 0) / 10.0  # pitch is roughly -900 to 900, scale by 10
                pitch = max(-90, min(90, pitch))
                f1x = cx + int(compass / 180 * scale)
                f1y = cy - int(pitch / 90 * scale)

        for r, alpha in [(20, 30), (14, 60), (8, 120)]:
            s = pygame.Surface((r*2, r*2), pygame.SRCALPHA)
            pygame.draw.circle(s, (*self.COLORS["fused_color"], alpha), (r, r), r)
            self.screen.blit(s, (f1x - r, f1y - r))
        pygame.draw.circle(self.screen, self.COLORS["fused_color"], (f1x, f1y), 6)
        pygame.draw.circle(self.screen, self.COLORS["white"], (f1x, f1y), 6, 2)
        self.screen.blit(self.fonts["normal"].render("Fused(pitch)", True, self.COLORS["fused_color"]), (f1x + 12, f1y - 8))

        # --- Fused dot 2: (absolute_pitch, compass) ---
        f2x, f2y = cx, cy
        if self.latest["fused"] and self.latest["fused"].data:
            data = self.latest["fused"].data
            if isinstance(data, FusedData) and data.valid:
                compass = ((data.compass or 0) / 10.0 + 180) % 360 - 180
                abs_pitch = (data.absolute_pitch or 0) / 10.0
                abs_pitch = max(-90, min(90, abs_pitch))
                f2x = cx + int(compass / 180 * scale)
                f2y = cy - int(abs_pitch / 90 * scale)

        for r, alpha in [(20, 30), (14, 60), (8, 120)]:
            s = pygame.Surface((r*2, r*2), pygame.SRCALPHA)
            pygame.draw.circle(s, (*self.COLORS["fused_color2"], alpha), (r, r), r)
            self.screen.blit(s, (f2x - r, f2y - r))
        pygame.draw.circle(self.screen, self.COLORS["fused_color2"], (f2x, f2y), 6)
        pygame.draw.circle(self.screen, self.COLORS["white"], (f2x, f2y), 6, 2)
        self.screen.blit(self.fonts["normal"].render("Fused(abs_pitch)", True, self.COLORS["fused_color2"]), (f2x + 12, f2y - 8))

        # Legend at bottom
        legend_y = proj_rect.y + proj_rect.height - 20
        self.screen.blit(self.fonts["small"].render("● Quaternion (yaw,pitch)", True, self.COLORS["quat_color"]), (proj_rect.x + 10, legend_y))
        self.screen.blit(self.fonts["small"].render("● Fused (compass,pitch)", True, self.COLORS["fused_color"]), (proj_rect.x + 200, legend_y))
        self.screen.blit(self.fonts["small"].render("● Fused (compass,abs_pitch)", True, self.COLORS["fused_color2"]), (proj_rect.x + 400, legend_y))

    # ============== 3D ORIENTATION VIEW ==============

    def _draw_3d_orientation_view(self, rect):
        """Two 3D wand orientations side by side: quaternion and fused."""
        pygame.draw.rect(self.screen, self.COLORS["panel"], rect)
        pygame.draw.rect(self.screen, self.COLORS["border"], rect, 2)

        title = self.fonts["big"].render("3D Orientation", True, self.COLORS["accent"])
        self.screen.blit(title, (rect.x + 10, rect.y + 5))

        half_w = (rect.width - 20) // 2
        panel_h = rect.height - 40

        # Left: Quaternion 3D
        quat_rect = pygame.Rect(rect.x + 10, rect.y + 35, half_w, panel_h)
        self._draw_3d_wand(quat_rect, "Quaternion", self.COLORS["quat_color"], self.latest["quaternions"], is_quat=True)

        # Right: Fused 3D
        fused_rect = pygame.Rect(rect.x + 20 + half_w, rect.y + 35, half_w, panel_h)
        self._draw_3d_wand(fused_rect, "Fused", self.COLORS["fused_color"], self.latest["fused"], is_quat=False)

    def _draw_3d_wand(self, rect, label, color, packet, is_quat=True):
        """Draw a 3D wireframe wand with filled faces using orthographic projection."""
        pygame.draw.rect(self.screen, (30, 30, 35), rect)
        pygame.draw.rect(self.screen, (50, 50, 55), rect, 1)

        self.screen.blit(self.fonts["title"].render(f"{label} 3D", True, color), (rect.x + 10, rect.y + 5))

        cx = rect.x + rect.width // 2
        cy = rect.y + rect.height // 2 + 10
        scale = min(rect.width, rect.height) * 0.35

        # Get rotation matrix
        rot_matrix = None
        if packet and packet.data:
            if is_quat and isinstance(packet.data, QuaternionData):
                q = packet.data
                if q.raw:
                    rot_matrix = quat_to_rot_matrix(q.raw["q1"], q.raw["q2"], q.raw["q3"], q.raw["q4"])
            elif not is_quat and isinstance(packet.data, FusedData):
                d = packet.data
                if d.valid:
                    yaw = (d.compass or 0) / 10.0
                    pitch = (d.pitch or 0) / 10.0
                    roll = (d.roll or 0) / 10.0
                    rot_matrix = euler_to_rot_matrix(yaw, pitch, roll)

        if rot_matrix is None:
            self.screen.blit(self.fonts["normal"].render("No data", True, self.COLORS["text_dim"]), (cx - 30, cy))
            return

        # Rotate all vertices
        rotated = [rotate_point(v, rot_matrix) for v in self.wand_verts]

        # Project to 2D (orthographic for stability)
        projected = [project_point_ortho(p, cx, cy, scale) for p in rotated]

        # Calculate face depths for painter's algorithm (sort back to front)
        face_depths = []
        for i, face in enumerate(self.wand_faces):
            avg_z = sum(rotated[v][2] for v in face) / len(face)
            face_depths.append((avg_z, i))
        face_depths.sort(reverse=True)  # Draw furthest first

        # Draw faces (back to front)
        for _, face_idx in face_depths:
            face = self.wand_faces[face_idx]
            points = [projected[v] for v in face]
            face_color = self.wand_face_colors[face_idx]
            # Darken based on depth (simple lighting)
            depth_factor = 0.7 + 0.3 * (rotated[face[0]][2] + 2) / 4  # approximate
            lit_color = tuple(min(255, int(c * depth_factor)) for c in face_color)
            pygame.draw.polygon(self.screen, lit_color, points)
            pygame.draw.polygon(self.screen, color, points, 2)

        # Draw edges (wireframe overlay)
        for edge in self.wand_edges:
            pygame.draw.line(self.screen, color, projected[edge[0]], projected[edge[1]], 2)

        # Draw axes at origin (X=red, Y=green, Z=blue)
        axis_length = int(scale * 0.6)
        axis_colors = [(255, 80, 80), (80, 255, 80), (80, 80, 255)]
        axes = [[1,0,0], [0,1,0], [0,0,1]]
        for axis, acolor in zip(axes, axis_colors):
            rotated_axis = rotate_point(axis, rot_matrix)
            end = project_point_ortho(rotated_axis, cx, cy, axis_length)
            pygame.draw.line(self.screen, acolor, (cx, cy), end, 3)
            # Axis label
            label_pos = (end[0] + 5, end[1] - 5)
            label = "X" if axis == [1,0,0] else "Y" if axis == [0,1,0] else "Z"
            self.screen.blit(self.fonts["small"].render(label, True, acolor), label_pos)

        # Euler values
        if is_quat and isinstance(packet.data, QuaternionData) and packet.data.euler:
            e = packet.data.euler
            val_text = f"Y:{e.get('yaw',0):.0f}° P:{e.get('pitch',0):.0f}° R:{e.get('roll',0):.0f}°"
        elif not is_quat and isinstance(packet.data, FusedData):
            d = packet.data
            val_text = f"C:{d.compass or 0} P:{d.pitch or 0} R:{d.roll or 0}"
        else:
            val_text = ""

        if val_text:
            self.screen.blit(self.fonts["normal"].render(val_text, True, color), (rect.x + 10, rect.y + rect.height - 20))

    # ============== GRAPH DRAWING ==============

    def _draw_graph(self, rect, view: GraphView, buffer: DataBuffer):
        pygame.draw.rect(self.screen, self.COLORS["panel"], rect)
        pygame.draw.rect(self.screen, self.COLORS["border"], rect, 2)
        self.screen.blit(self.fonts["title"].render(view.title, True, self.COLORS["text"]), (rect.x + 10, rect.y + 5))

        packets = buffer.get_recent(3.0)
        if len(packets) < 2:
            self.screen.blit(self.fonts["normal"].render("No data...", True, self.COLORS["text_dim"]),
                           (rect.centerx - 40, rect.centery))
            return

        margin = 50
        graph_x = rect.x + margin
        graph_y = rect.y + 35
        graph_w = rect.width - margin * 2
        graph_h = rect.height - margin - 35

        for i in range(5):
            y = graph_y + (graph_h * i) / 4
            pygame.draw.line(self.screen, (50, 50, 55), (graph_x, y), (graph_x + graph_w, y), 1)

        t_min = packets[0].timestamp
        t_max = packets[-1].timestamp

        for series in view.series:
            points = []
            for pkt in packets:
                val = series.extractor(pkt.data)
                if val is None:
                    continue
                x = graph_x + ((pkt.timestamp - t_min) / (t_max - t_min)) * graph_w
                if view.y_range:
                    y_min, y_max = view.y_range
                else:
                    y_min, y_max = -32768, 32767
                y = graph_y + graph_h - ((val - y_min) / (y_max - y_min)) * graph_h
                points.append((int(x), int(y)))
            if len(points) > 1:
                pygame.draw.lines(self.screen, series.color, False, points, 2)

        pygame.draw.line(self.screen, self.COLORS["text"], (graph_x, graph_y), (graph_x, graph_y + graph_h), 2)
        pygame.draw.line(self.screen, self.COLORS["text"], (graph_x, graph_y + graph_h), (graph_x + graph_w, graph_y + graph_h), 2)

        legend_x = graph_x + graph_w - 80
        legend_y = graph_y + 5
        for series in view.series:
            pygame.draw.line(self.screen, series.color, (legend_x, legend_y + 5), (legend_x + 15, legend_y + 5), 3)
            self.screen.blit(self.fonts["small"].render(series.name, True, series.color), (legend_x + 20, legend_y))
            legend_y += 14

    # ============== INFO PANELS ==============

    def _draw_raw_info(self, rect, packet: Optional[DataPacket]):
        pygame.draw.rect(self.screen, self.COLORS["panel"], rect)
        pygame.draw.rect(self.screen, self.COLORS["border"], rect, 2)
        if not packet:
            self.screen.blit(self.fonts["normal"].render("No data", True, self.COLORS["text_dim"]), (rect.x + 10, rect.y + 10))
            return
        data: RawData = packet.data
        y = rect.y + 10
        self.screen.blit(self.fonts["title"].render("RawData", True, self.COLORS["accent"]), (rect.x + 10, y))
        y += 25
        if not data.valid:
            self.screen.blit(self.fonts["normal"].render(f"Error: {data.error}", True, self.COLORS["red"]), (rect.x + 10, y))
            return
        if data.accel:
            self.screen.blit(self.fonts["normal"].render(f"accel: {data.accel}", True, self.COLORS["text"]), (rect.x + 10, y))
            y += 16
        if data.gyro:
            self.screen.blit(self.fonts["normal"].render(f"gyro: {data.gyro}", True, self.COLORS["text"]), (rect.x + 10, y))
            y += 16
        if data.mag:
            self.screen.blit(self.fonts["normal"].render(f"mag: {data.mag}", True, self.COLORS["text"]), (rect.x + 10, y))
            y += 16
        self.screen.blit(self.fonts["small"].render(f"hex: {data.raw_hex}", True, self.COLORS["text_dim"]), (rect.x + 10, y))

    def _draw_fused_info(self, rect, packet: Optional[DataPacket]):
        pygame.draw.rect(self.screen, self.COLORS["panel"], rect)
        pygame.draw.rect(self.screen, self.COLORS["border"], rect, 2)
        if not packet:
            self.screen.blit(self.fonts["normal"].render("No data", True, self.COLORS["text_dim"]), (rect.x + 10, rect.y + 10))
            return
        data: FusedData = packet.data
        y = rect.y + 10
        self.screen.blit(self.fonts["title"].render("FusedData", True, self.COLORS["accent"]), (rect.x + 10, y))
        y += 25
        if not data.valid:
            self.screen.blit(self.fonts["normal"].render(f"Error: {data.error}", True, self.COLORS["red"]), (rect.x + 10, y))
            return
        if data.filtered_accel:
            self.screen.blit(self.fonts["normal"].render(f"filtered_accel: {data.filtered_accel}", True, self.COLORS["text"]), (rect.x + 10, y))
            y += 16
        fields = [("pitch", data.pitch), ("roll_sin", data.roll_sin), ("roll_cos", data.roll_cos),
                  ("compass", data.compass), ("absolute_pitch", data.absolute_pitch), ("roll", data.roll)]
        for name, val in fields:
            if val is not None:
                self.screen.blit(self.fonts["normal"].render(f"{name}: {val}", True, self.COLORS["text"]), (rect.x + 10, y))
                y += 16
        self.screen.blit(self.fonts["small"].render(f"hex: {data.raw_hex}", True, self.COLORS["text_dim"]), (rect.x + 10, y))

    def _draw_stats_bar(self):
        rect = pygame.Rect(10, self.height - 50, self.width - 20, 40)
        pygame.draw.rect(self.screen, self.COLORS["panel"], rect)
        pygame.draw.rect(self.screen, self.COLORS["border"], rect, 2)
        elapsed = time.time() - self.stats["start_time"]
        stats = [
            f"Q:{self.stats['quat_count']}({self.stats['quat_count']/max(elapsed,0.1):.0f}Hz)",
            f"R:{self.stats['raw_count']}({self.stats['raw_count']/max(elapsed,0.1):.0f}Hz)",
            f"F:{self.stats['fused_count']}({self.stats['fused_count']/max(elapsed,0.1):.0f}Hz)",
        ]
        x = rect.x + 15
        for text in stats:
            self.screen.blit(self.fonts["normal"].render(text, True, self.COLORS["text"]), (x, rect.y + 12))
            x += 180

    def _draw_instructions(self):
        self.screen.blit(self.fonts["small"].render("1/2/3/4/5: Views | P:Pause | C:Calibrate | ESC:Exit", True, self.COLORS["text_dim"]), (10, self.height - 20))

    # ============== INPUT ==============

    def handle_click(self, pos):
        for i, rect in enumerate(self.button_rects.get("views", [])):
            if rect.collidepoint(pos):
                self.current_view = i
                return
        if self.button_rects.get("pause") and self.button_rects["pause"].collidepoint(pos):
            self.toggle_pause()
        elif self.button_rects.get("calq") and self.button_rects["calq"].collidepoint(pos):
            asyncio.create_task(self.wand.reset_quaternions())
        elif self.button_rects.get("calm") and self.button_rects["calm"].collidepoint(pos):
            asyncio.create_task(self.wand.calibrate_imu())

    # ============== MAIN LOOP ==============

    async def run(self):
        if not await self.connect():
            print("Could not connect to a wand.")
            return
        self.init_pygame()
        try:
            while self.running:
                for event in pygame.event.get():
                    if event.type == QUIT:
                        self.running = False
                    elif event.type == KEYDOWN:
                        if event.key == K_ESCAPE:
                            self.running = False
                        elif event.key == K_p:
                            self.toggle_pause()
                        elif event.key == K_1:
                            self.current_view = 0
                        elif event.key == K_2:
                            self.current_view = 1
                        elif event.key == K_3:
                            self.current_view = 2
                        elif event.key == K_4:
                            self.current_view = 3
                        elif event.key == K_5:
                            self.current_view = 4
                        elif event.key == K_c and self.wand.is_connected:
                            asyncio.create_task(self.wand.reset_quaternions())
                            await asyncio.sleep(0.1)
                            asyncio.create_task(self.wand.calibrate_imu())
                    elif event.type == MOUSEBUTTONDOWN:
                        if event.button == 1:
                            self.handle_click(event.pos)
                self.draw()
                self.clock.tick(60)
                await asyncio.sleep(0.001)
        finally:
            self.running = False
            pygame.quit()
            if self.wand.is_connected:
                await self.disconnect()


if __name__ == "__main__":
    mac = sys.argv[1] if len(sys.argv) > 1 else None
    viz = WandVisualizer(mac_address=mac)
    asyncio.run(viz.run())
