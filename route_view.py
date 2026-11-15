"""Offline map drawn from the bundled OpenStreetMap geometry."""

from math import cos, radians, sin
from collections import deque
import tkinter as tk

from campus import CampusTrack, Point, distance_m


INK = "#19382f"
GREEN = "#007957"
MAP_BG = "#edf1e7"
FONT = "Microsoft YaHei UI"


class RouteView(tk.Canvas):
    def __init__(self, parent, track: CampusTrack):
        super().__init__(parent, bg=MAP_BG, highlightthickness=0, width=320, height=240)
        self.clockwise = False
        self.scale = 1.0
        self.trail = deque(maxlen=220)
        self.set_track(track)
        self.bind("<Configure>", self.redraw)

    def set_track(self, track: CampusTrack) -> None:
        self.track = track
        self.current = track.points[0]
        self.heading = 0.0
        self.start = self.current
        self.trail.clear()
        xs, ys = zip(*track.points)
        self.lon = (min(xs) + max(xs)) / 2
        self.lat = (min(ys) + max(ys)) / 2
        self.x_m = 111_195 * cos(radians(self.lat))
        self.y_m = 111_195
        self.span_x = (max(xs)-min(xs))*self.x_m
        self.span_y = (max(ys)-min(ys))*self.y_m
        self.redraw()

    def project(self, point: Point) -> tuple[float, float]:
        return (self.winfo_width() / 2 + (point[0] - self.lon) * self.x_m * self.scale,
                self.winfo_height() / 2 - (point[1] - self.lat) * self.y_m * self.scale)

    def redraw(self, _event=None) -> None:
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        self.scale = min(width/(self.span_x*1.28+12), height/(self.span_y*1.28+12))
        palette = {
            "green": ("#d7e4cf", ""), "stadium": ("#e0e5d4", "#cfd9c7"),
            "football": ("#bfd4a9", "#f7faee"), "hall": ("#d4d9ca", "#c3cbbd"),
            "tennis": ("#cedeaa", "#eaf0de"), "building": ("#d7daca", "#c8cebe"),
            "water": ("#b0d6d4", "#91c5c7"),
        }
        order = ("green", "water", "building", "road", "path", "stadium", "hall", "tennis", "football", "route")
        for feature in sorted(self.track.features, key=lambda f: order.index(f["properties"]["kind"])):
            kind = feature["properties"]["kind"]
            geometry = feature["geometry"]
            line = geometry["coordinates"][0] if geometry["type"] == "Polygon" else geometry["coordinates"]
            points = [self.project(p) for p in line]
            flat = [value for p in points for value in p]
            if kind in palette:
                fill, outline = palette[kind]
                self.create_polygon(flat, fill=fill, outline=outline, width=2)
                if kind == "water" and feature["properties"].get("name"):
                    cx, cy = (sum(v)/len(v) for v in zip(*points[:-1]))
                    self.create_text(cx, cy, text=feature["properties"]["name"], fill="#39686a", font=(FONT, 9))
                if kind == "football":
                    left = tuple((a + b) / 2 for a, b in zip(points[0], points[1]))
                    right = tuple((a + b) / 2 for a, b in zip(points[2], points[3]))
                    cx, cy = ((a + b) / 2 for a, b in zip(left, right))
                    radius = 9.15 * self.scale
                    self.create_line(*left, *right, fill=outline, width=2)
                    self.create_oval(cx-radius, cy-radius, cx+radius, cy+radius, outline=outline, width=2)
            elif kind == "route":
                strokes = (("#fff9ed", 5), (GREEN, 2))
                if self.track.route_id == "track":
                    strokes = (("#c3cbb8", 13), ("#d9c7ae", 12), ("#eee5d5", 8), ("#d9c7ae", 7)) + strokes
                for color, meters in strokes:
                    self.create_line(flat, fill=color, width=max(2, meters*self.scale), joinstyle="round")
            elif kind == "road":
                self.create_line(flat, fill="#cdd5c5", width=max(3, 8*self.scale), joinstyle="round")
                self.create_line(flat, fill="#fbfcf5", width=max(2, 6*self.scale), joinstyle="round")
            else:
                self.create_line(flat, fill="#d0d9c9", width=max(1, 2*self.scale), joinstyle="round")

        self.create_text(width-22, 15, text="N", fill=INK, font=("Segoe UI", 9, "bold"))
        self.create_polygon(width-22, 25, width-27, 40, width-22, 36, width-17, 40, fill=INK)
        end = 14 + 25 * self.scale
        self.create_line(14, height-17, end, height-17, fill=INK, width=2)
        self.create_line(14, height-21, 14, height-16, fill=INK)
        self.create_line(end, height-21, end, height-16, fill=INK)
        self.create_text(14, height-27, text="25 m", anchor="w", fill=INK, font=("Segoe UI", 8))
        self.create_text(width-10, height-11, text="© OpenStreetMap contributors", anchor="e",
                         font=("Segoe UI", 8), fill="#4c6254")
        self.draw_position()

    def set_position(self, point: Point, clockwise: bool, *, reset=False, heading: float | None = None) -> None:
        self.current, self.clockwise = point, clockwise
        if heading is not None:
            self.heading = heading % 360
        if reset:
            self.trail.clear()
            self.start = point
        if not self.trail or distance_m(self.trail[-1], point) >= 3:
            self.trail.append(point)
        self.draw_position()

    def draw_position(self) -> None:
        self.delete("position")
        if len(self.trail) > 1:
            trace = [v for p in self.trail for v in self.project(p)]
            self.create_line(trace, fill="#247fbe", width=3, joinstyle="round", tags="position")
        start_x, start_y = self.project(self.start)
        self.create_oval(start_x-4, start_y-4, start_x+4, start_y+4, fill="white",
                         outline=GREEN, width=2, tags="position")
        self.create_text(start_x-13, start_y, anchor="e", text="起点", font=(FONT, 9, "bold"),
                         fill=INK, tags="position")
        x, y = self.project(self.current)
        angle = radians(self.heading)
        points = [(0, -12), (8, 9), (0, 5), (-8, 9)]
        arrow = [coordinate for dx, dy in points
                 for coordinate in (x+dx*cos(angle)-dy*sin(angle), y+dx*sin(angle)+dy*cos(angle))]
        self.create_polygon(arrow, fill=GREEN, outline="white", width=2, tags=("position", "heading"))
