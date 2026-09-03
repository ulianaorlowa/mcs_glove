"""
MCS Glove — end-user (patient-facing) application.

UI: hand silhouette with a per-finger power slider, a global power slider,
built-in and user-defined scenarios, session timer, motor diagnostics.
All visible text is Russian by design; the code and comments are English.

Architecture
------------
userapp.py       — UI and session logic only (this file)
glove_client.py  — BLE transport (bleak): connect, write characteristics,
                   read battery (MAX17055), per-channel diagnostics
models.py        — shared types: Finger, MotorMode, FingerState, ChannelInfo,
                   FINGER_CHANNEL (finger -> DRV2605L channel), PRESETS

Threading
---------
Qt widgets live in the main thread. Everything that touches BLE (connect,
scenario playback, battery polling, diagnostics) runs in a daemon thread and
returns to the UI ONLY through Bridge signals. Calling widget methods
directly from a worker thread is not supported by Qt and will eventually
crash the app.

Demo mode
---------
self._demo_mode is True while the glove is not connected. The whole UI stays
functional but no BLE command is sent — useful for working on the interface
without hardware.

External files (looked up next to this script)
----------------------------------------------
hand.png            — hand image
user_scenarios.json — user scenarios, created on first "Сохранить текущий"

File map (sections appear in this order)
----------------------------------------
LOCALIZATION · STYLE · BATTERY · BRIDGE · CONNECTION PANEL · HAND WIDGET ·
DIAGNOSTICS DIALOG · CHANNEL SETTINGS DIALOG · SCENARIO PANEL ·
MAIN WINDOW · ENTRY POINT

Requires: pip install PySide6 bleak
Run:      python userapp.py
"""

import sys
import json
import time
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QObject, Signal, QTimer, QRectF, QPointF
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QSpinBox, QComboBox, QSlider,
    QGroupBox, QCheckBox, QRadioButton, QButtonGroup,
    QDialog, QSizePolicy, QInputDialog,
)
from PySide6.QtGui import (
    QFont, QPainter, QColor, QPen, QBrush, QPixmap,
    QPainterPath, QPainterPathStroker,
)

from models import (
    Finger, FingerState, MotorMode, ChannelInfo,
    FINGER_CHANNEL, FINGER_LABEL, ALL_FINGERS,
    PRESETS, Scenario,
)
from glove_client import GloveClient


# ══════════════════════════════════════════════════════════
#  LOCALIZATION
# ══════════════════════════════════════════════════════════

FINGER_LABEL_RU: dict[Finger, str] = {
    Finger.THUMB:  "Большой",
    Finger.INDEX:  "Указательный",
    Finger.MIDDLE: "Средний",
    Finger.RING:   "Безымянный",
    Finger.PINKY:  "Мизинец",
}

# When a scenario is added to models.PRESETS, add its name to SCENARIO_NAMES_RU
#  as well — otherwise it is displayed in English, exactly as defined.
SCENARIO_NAMES_RU: dict[str, str] = {
    "Sequential":   "Каскад",
    "Wave":         "Волна",
    "All at once":  "Пульс",
    "Alternating":  "Маятник",
    "Gentle pulse": "Дыхание",
    "Music":        "Музыка",
}


MANUAL_MODE_NAME = "Свои настройки"

USER_SCENARIOS_FILE = Path(__file__).with_name("user_scenarios.json")


def finger_ru(f: Finger) -> str:
    return FINGER_LABEL_RU.get(f, FINGER_LABEL[f])


# ══════════════════════════════════════════════════════════
#  STYLE
# ══════════════════════════════════════════════════════════

STYLE = """
QWidget {
    background: #FAFAFB; color: #1C1C1F;
    font-family: 'Segoe UI', Arial, sans-serif; font-size: 13px;
}
QLabel { background: transparent; color: #1C1C1F; }
QCheckBox { background: transparent; color: #1C1C1F; }
QRadioButton { background: transparent; color: #1C1C1F; padding: 3px 0; }
QRadioButton::indicator {
    width: 14px; height: 14px;
    border: 2px solid #BFBFC6; border-radius: 9px; background: #FFFFFF;
}
QRadioButton::indicator:hover { border-color: #9A84CE; }
QRadioButton::indicator:checked {
    border: 5px solid #9A84CE; border-radius: 12px; background: #FFFFFF;
}
QWidget#fingerSlider { background: transparent; }
QCheckBox::indicator {
    width: 15px; height: 15px;
    border: 2px solid #BFBFC6; border-radius: 5px; background: #FFFFFF;
}
QCheckBox::indicator:hover { border-color: #9A84CE; }
QCheckBox::indicator:checked {
    border: 2px solid #9A84CE; background: #9A84CE;
}
QGroupBox {
    background: #FFFFFF; border: 1px solid #E6E6EA; border-radius: 10px;
    margin-top: 9px; padding: 11px 10px 7px 10px;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 12px; padding: 0 4px;
    color: #2E2E33; font-weight: 600;
}
QPushButton {
    background: #FFFFFF; border: 1px solid #D6D6DC; border-radius: 8px;
    padding: 7px 14px; color: #1C1C1F;
}
QPushButton:hover { background: #F2F2F5; border-color: #C2C2CA; }
QPushButton:disabled { color: #ADADB5; border-color: #E6E6EA; }

QPushButton#run {
    background: #6FCFAC; color: #0E4A35; border: none; font-weight: 600;
}
QPushButton#run:hover { background: #5FC5A0; }
QPushButton#run:disabled { background: #D3EDE2; color: #7FA795; }

QPushButton#stopAll {
    background: #EFA5A5; color: #6B2020; border: none; font-weight: 600;
}
QPushButton#stopAll:hover { background: #E79191; }

QComboBox {
    background: #FFFFFF; border: 1px solid #D6D6DC;
    border-radius: 6px; padding: 3px 6px; color: #1C1C1F;
}
QSpinBox {
    background: #FFFFFF; border: 1px solid #D6D6DC; border-radius: 6px;
    padding: 3px 22px 3px 8px; color: #1C1C1F;
}
QSpinBox::up-button, QSpinBox::down-button {
    subcontrol-origin: border; width: 18px;
    background: transparent; border-left: 1px solid #EDEDF1;
}
QSpinBox::up-button   { subcontrol-position: top right; }
QSpinBox::down-button { subcontrol-position: bottom right; }
QSpinBox:disabled, QComboBox:disabled { color: #ADADB5; background: #F7F7F9; }

QSlider::groove:horizontal {
    height: 4px; background: #E2E2E7; border-radius: 2px;
}
QSlider { background: transparent; }
QSlider::sub-page:horizontal {
    background: #9A84CE; border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 14px; height: 14px; margin: -5px 0;
    border-radius: 7px; background: #9A84CE;
}
QSlider::handle:horizontal:hover { background: #8069BB; }
QSlider::handle:horizontal:disabled { background: #C6C6CD; }
QSlider::sub-page:horizontal:disabled { background: #C6C6CD; }
"""

GREEN, RED, GREY = "#2AA67B", "#A32D2D", "#B4B2A9"
GREEN_TEXT, RED_TEXT = "#1B7A59", "#8E2626"   # надписи на белом фоне
VIOLET, VIOLET_DARK = "#9A84CE", "#8069BB"   # регуляторы, переключатели
HAND_INK = "#57575E"                         # мягкий контур руки вместо чёрного

# Мягкие кнопки: (заливка, текст, заливка при наведении)
BTN_GREEN = ("#6FCFAC", "#0E4A35", "#5FC5A0")
BTN_RED   = ("#EFA5A5", "#6B2020", "#E79191")

# Пастельные кружки на пальцах: (заливка, обводка, цвет цифры)
PASTEL_VIOLET = ("#C9BCEB", "#9F8AD2", "#3B2C6B")   # палец выбран
PASTEL_GREEN  = ("#9EDCC1", "#5FBE97", "#12523B")   # вибрирует сейчас
PASTEL_RED    = ("#F0AEAE", "#D28282", "#6E2222")   # неисправность


# ══════════════════════════════════════════════════════════
#  BATTERY
# ══════════════════════════════════════════════════════════
# Charge indicator. Values come from glove_client.battery_info() (MAX17055 fuel
# gauge) through Bridge.battery — see MotorControlApp._poll_battery.

class HorizontalBatteryWidget(QWidget):
    """Компактный горизонтальный аккумулятор с процентами справа."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._level = 5
        self._percent = 100
        self.setFixedSize(120, 32)

    def setChargePercent(self, percent: int):
        self._percent = max(0, min(100, percent))
        level = max(0, min(5, int(percent / 20)))
        if level != self._level:
            self._level = level
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()

        colors = {
            0: QColor("#707070"),
            1: QColor("#E00000"),
            2: QColor("#FFB000"),
            3: QColor("#F0E000"),
            4: QColor("#90D000"),
            5: QColor("#00B000"),
        }
        color = colors[self._level]

        text_zone_w = 40
        batt_w_total = w - text_zone_w - 8

        body_w = batt_w_total - 6
        body_h = h * 0.65
        body_x = 2
        body_y = (h - body_h) / 2

        p.setPen(QPen(color, 2.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(body_x, body_y, body_w, body_h), 4, 4)

        tip_w = 4
        tip_h = body_h * 0.4
        p.setBrush(QBrush(color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(
            QRectF(body_x + body_w, body_y + (body_h - tip_h) / 2, tip_w, tip_h), 1, 1)

        if self._level > 0:
            padding = 3
            available_w = body_w - padding * 2
            seg_w = (available_w - 4 * 2) / 5
            seg_h = body_h - padding * 2
            gap = 2
            for i in range(self._level):
                x = body_x + padding + i * (seg_w + gap)
                p.drawRoundedRect(QRectF(x, body_y + padding, seg_w, seg_h), 1.5, 1.5)

        p.setPen(QPen(QColor("#1A1915")))
        p.setFont(QFont("Segoe UI", 9, QFont.Weight.Medium))
        p.drawText(QRectF(body_x + body_w + 6, 0, text_zone_w, h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{self._percent}%")
        p.end()


# ══════════════════════════════════════════════════════════
#  BRIDGE
# ══════════════════════════════════════════════════════════
# The only sanctioned way to move data from a worker thread into the UI.
# A worker calls bridge.<signal>.emit(...); Qt delivers the call on the main
# thread. All signal-to-handler wiring is in MotorControlApp.__init__.

class Bridge(QObject):
    status       = Signal(str)
    connected    = Signal(bool)
    devinfo      = Signal(object)
    battery      = Signal(object)
    health       = Signal(int, str, str)        
    diag_result  = Signal(object, object)       
    motor_update = Signal()
    run_end      = Signal()


# ══════════════════════════════════════════════════════════
#  CONNECTION PANEL
# ══════════════════════════════════════════════════════════
# Top-left panel: connect button, textual status, model and firmware revisions
# read from the Device Information Service.
# The connect call itself runs in a worker thread — BLE scanning takes seconds
# and would freeze the UI.

class ConnectionPanel(QGroupBox):
    def __init__(self, bridge: Bridge, glove: GloveClient, parent=None):
        super().__init__("Подключение", parent)
        self._bridge = bridge
        self._glove = glove

        lay = QHBoxLayout(self)
        lay.setSpacing(8)

        self.btn_conn = QPushButton("Подключить")
        self.btn_conn.setFixedWidth(180)
        self.btn_conn.setFixedHeight(52)
        self.btn_conn.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        self.btn_conn.clicked.connect(self._on_connect_click)
        lay.addWidget(self.btn_conn)

        self.status_label = QLabel("Не подключено")
        self.status_label.setStyleSheet("color: #45454C; font-size: 13px; font-weight: 600;")
        lay.addWidget(self.status_label, 1)

        self.dev_label = QLabel("")
        self.dev_label.setStyleSheet("color: #6A6A72; font-size: 11px;")
        lay.addWidget(self.dev_label)

        self._bridge.connected.connect(self._on_connected_state)
        self._bridge.devinfo.connect(self._show_devinfo)
        self._update_button_style(False)

    def _on_connect_click(self):
        if self._glove.connected:
            self._on_disconnect()
        else:
            self._on_connect()

    def _update_button_style(self, connected: bool):
        g_fill, g_txt, g_hover = BTN_GREEN
        r_fill, r_txt, _ = BTN_RED
        if connected:
            self.btn_conn.setText("Отключить")
            self.btn_conn.setStyleSheet(f"""
                QPushButton {{
                    background: white; color: #1C1C1F;
                    border: 2px solid {g_fill}; border-radius: 10px; font-weight: bold;
                }}
                QPushButton:hover {{
                    background: {r_fill}; color: {r_txt}; border: none;
                }}
            """)
        else:
            self.btn_conn.setText("Подключить")
            self.btn_conn.setStyleSheet(f"""
                QPushButton {{
                    background: {g_fill}; color: {g_txt};
                    border: none; border-radius: 10px; font-weight: bold;
                }}
                QPushButton:hover {{ background: {g_hover}; }}
            """)

    def _on_connect(self):
        self.btn_conn.setEnabled(False)
        self.status_label.setText("Сканирование")

        def worker():
            try:
                self._glove.connect()
                self._bridge.connected.emit(True)
                if hasattr(self._glove, 'device_info'):
                    self._bridge.devinfo.emit(self._glove.device_info)
            except Exception as e:
                self._bridge.status.emit(f"Ошибка подключения: {e}")
                self._bridge.connected.emit(False)
            finally:
                self.btn_conn.setEnabled(True)

        threading.Thread(target=worker, daemon=True).start()

    def _on_disconnect(self):
        try:
            self._glove.disconnect()
        except Exception as e:
            self._bridge.status.emit(f"Ошибка отключения: {e}")
        self._bridge.connected.emit(False)

    def _on_connected_state(self, ok: bool):
        self._update_button_style(ok)
        self.status_label.setText("Подключено" if ok else "Не подключено")
        self.status_label.setStyleSheet(
            f"color: {GREEN_TEXT}; font-weight: 600; font-size: 13px;" if ok
            else "color: #45454C; font-size: 13px; font-weight: 600;"
        )
        if not ok:
            self.dev_label.setText("")

    def _show_devinfo(self, info):
        parts = []
        if info.get("model"):   parts.append(info["model"])
        if info.get("fw_rev"):  parts.append(f"FW {info['fw_rev']}")
        if info.get("hw_rev"):  parts.append(f"HW {info['hw_rev']}")
        self.dev_label.setText("  ·  ".join(parts))


# ══════════════════════════════════════════════════════════
#  HAND WIDGET
# ══════════════════════════════════════════════════════════
# The central UI element.
# 1) draw the hand (hand.png if present, otherwise a code-drawn silhouette),
# 2) show per-finger state as a colored dot,
# 3) position a power mini-slider next to each fingertip.
#
# IMPORTANT: every coordinate below is a FRACTION of the hand rectangle (0..1),
# never a pixel value — that is what keeps the layout correct as the window is
# resized. If hand.png is replaced, these fractions must be re-measured.


# Fingertip pad centers as fractions of the hand rectangle (0..1 per axis).
# These drive both the indicator dots AND click hit-testing.
# Measured by hand against hand.png — re-measure if the image is replaced.
FINGER_POINTS: dict[Finger, QPointF] = {
    Finger.THUMB:  QPointF(0.186, 0.556),
    Finger.INDEX:  QPointF(0.364, 0.227),
    Finger.MIDDLE: QPointF(0.555, 0.141),
    Finger.RING:   QPointF(0.703, 0.237),
    Finger.PINKY:  QPointF(0.846, 0.435),
}

SLIDER_ANCHORS: dict[Finger, QPointF] = {
    Finger.THUMB:  QPointF( 0.186, 0.425),
    Finger.INDEX:  QPointF( 0.365, 0.060),
    Finger.MIDDLE: QPointF( 0.557, -0.054),
    Finger.RING:   QPointF( 0.742, 0.072),
    Finger.PINKY:  QPointF( 0.898, 0.275),
}

HAND_MARGINS = (0.12, 0.13, 0.12, 0.02)

CLICK_RADIUS = 0.07   # click hit radius, fraction of hand width. Noticeably
                      # larger than the drawn dot so it is easy to hit by mouse

FINGER_BASES: dict[Finger, QPointF] = {
    Finger.THUMB:  QPointF(0.38, 0.72),
    Finger.INDEX:  QPointF(0.45, 0.50),
    Finger.MIDDLE: QPointF(0.56, 0.48),
    Finger.RING:   QPointF(0.67, 0.50),
    Finger.PINKY:  QPointF(0.77, 0.58),
}

FINGER_WIDTHS: dict[Finger, float] = {
    Finger.THUMB:  0.105,
    Finger.INDEX:  0.085,
    Finger.MIDDLE: 0.088,
    Finger.RING:   0.083,
    Finger.PINKY:  0.072,
}

HAND_ASPECT = 0.95   

class FingerSlider(QWidget):

    def __init__(self, finger: Finger, parent=None):
        super().__init__(parent)
        self.finger = finger
        self.setObjectName("fingerSlider")    
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(60)
        self.slider.setFixedSize(88, 22)  # 22 px is the minimum: any shorter and the
                                          # round handle is clipped top and bottom

        self.value_lbl = QLabel("60 %")
        self.value_lbl.setStyleSheet("font-size: 10px; color: #45454C; font-weight: 600;")
        self.value_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lay.addWidget(self.slider)
        lay.addWidget(self.value_lbl)

        self.slider.valueChanged.connect(
            lambda v: self.value_lbl.setText(f"{v} %"))

    def value(self) -> int:
        return self.slider.value()

    def setValue(self, v: int):
        self.slider.setValue(v)


class HandWidget(QGroupBox):
    """Hand. Choosing a finger by clicking on it. Sliding bar next to it"""

    selection_changed = Signal()

    def __init__(self, channels: dict[Finger, ChannelInfo], parent=None):
        super().__init__("", parent)
        self._channels = channels
        self.setMinimumSize(430, 380)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._hand_pixmap = self._load_hand()

        self._selected: dict[Finger, bool] = {f: False for f in ALL_FINGERS}
        self.sliders: dict[Finger, FingerSlider] = {}
        for f in ALL_FINGERS:
            fs = FingerSlider(f, self)
            self.sliders[f] = fs

        self._update_slider_enabled()


    @staticmethod
    def _load_hand() -> QPixmap:
        """hand.png loading"""
        pm = QPixmap(str(Path(__file__).with_name("hand.png")))
        if pm.isNull():
            return pm
        tinted = QPixmap(pm.size())
        tinted.fill(Qt.GlobalColor.transparent)
        p = QPainter(tinted)
        p.drawPixmap(0, 0, pm)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        p.fillRect(tinted.rect(), QColor(HAND_INK))
        p.end()
        return tinted

    # finger selection
    def is_selected(self, f: Finger) -> bool:
        return self._selected[f]

    def selected_fingers(self) -> list[Finger]:
        return [f for f in ALL_FINGERS if self._selected[f]]

    def set_selected(self, f: Finger, sel: bool):
        self._selected[f] = sel
        self._update_slider_enabled()
        self.update()
        self.selection_changed.emit()

    def set_all_selected(self, sel: bool):
        for f in ALL_FINGERS:
            self._selected[f] = sel
        self._update_slider_enabled()
        self.update()
        self.selection_changed.emit()

    def power(self, f: Finger) -> int:
        return self.sliders[f].value()

    def set_all_power(self, v: int):
        for fs in self.sliders.values():
            fs.setValue(v)

    def set_power(self, f: Finger, v: int):
        self.sliders[f].setValue(v)

    def _update_slider_enabled(self):
        for f, fs in self.sliders.items():
            fs.slider.setEnabled(self._selected[f])

    # geometry
    def _hand_rect(self) -> QRectF:
        ml, mt, mr, mb = HAND_MARGINS
        box = QRectF(self.width() * ml,
                     self.height() * mt,
                     self.width() * (1 - ml - mr),
                     self.height() * (1 - mt - mb))
        if box.width() <= 0 or box.height() <= 0:
            return QRectF(self.rect())

        if self._hand_pixmap.isNull():
            aspect = HAND_ASPECT
        else:
            aspect = self._hand_pixmap.width() / self._hand_pixmap.height()

        w = min(box.width(), box.height() * aspect)
        h = w / aspect
        return QRectF(box.x() + (box.width() - w) / 2,
                      box.y() + (box.height() - h) / 2, w, h)

    def _finger_px(self, f: Finger, rect: QRectF) -> tuple[float, float]:
        p = FINGER_POINTS[f]
        return (rect.x() + p.x() * rect.width(),
                rect.y() + p.y() * rect.height())

    def _place_sliders(self):
        """The mini sliders are plain child widgets, not layout items: their
        position is recomputed by hand on every resize/show because it is
        anchored to the drawn hand rather than to a grid."""

        rect = self._hand_rect()
        for f, fs in self.sliders.items():
            a = SLIDER_ANCHORS[f]
            cx = rect.x() + a.x() * rect.width()
            cy = rect.y() + a.y() * rect.height()
            fs.adjustSize()
            x = int(cx - fs.width() / 2)
            y = int(cy - fs.height() / 2)
            # не даём слайдеру уехать за пределы виджета
            x = max(2, min(x, self.width() - fs.width() - 2))
            y = max(2, min(y, self.height() - fs.height() - 2))
            fs.move(x, y)
            fs.show()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_sliders()

    def showEvent(self, event):
        super().showEvent(event)
        self._place_sliders()

    # clicks
    def mousePressEvent(self, event):
        rect = self._hand_rect()
        pos = event.position()
        rel_x = (pos.x() - rect.x()) / rect.width()
        rel_y = (pos.y() - rect.y()) / rect.height()

        for f, point in FINGER_POINTS.items():
            dist = ((rel_x - point.x()) ** 2 + (rel_y - point.y()) ** 2) ** 0.5
            if dist < CLICK_RADIUS:
                self.set_selected(f, not self._selected[f])
                return

    def update_display(self):
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self._hand_rect()

        # Dot color priority (order matters):
            # vibrating now > faulty > selected > idle.
            # An active motor stays green even on a faulty channel, so it is
            # visible that the command actually went out over BLE.

        if not self._hand_pixmap.isNull():
            p.drawPixmap(rect.toRect(), self._hand_pixmap)
        else:
            self._draw_hand_silhouette(p, rect)

        for f in ALL_FINGERS:
            ci = self._channels[f]
            cx, cy = self._finger_px(f, rect)

            if ci.state == FingerState.ACTIVE:
                f_, b_, t_ = PASTEL_GREEN
                fill, border, txt = QColor(f_), QColor(b_), QColor(t_)
            elif ci.state == FingerState.ERROR or ci.diag_ok is False:
                f_, b_, t_ = PASTEL_RED
                fill, border, txt = QColor(f_), QColor(b_), QColor(t_)
            elif self._selected[f]:
                f_, b_, t_ = PASTEL_VIOLET
                fill, border, txt = QColor(f_), QColor(b_), QColor(t_)
            else:
                fill, border, txt = QColor("#FFFFFF"), QColor("#C9C9D1"), QColor("#45454C")

            radius = max(10.0, min(18.0, rect.width() * 0.037))
            p.setPen(QPen(border, 2))
            p.setBrush(QBrush(fill))
            p.drawEllipse(QPointF(cx, cy), radius, radius)

            p.setPen(QPen(txt))
            p.setFont(QFont("Segoe UI", max(8, int(radius * 0.72)), QFont.Weight.Bold))
            p.drawText(QRectF(cx - radius, cy - radius, radius * 2, radius * 2),
                       Qt.AlignmentFlag.AlignCenter, str(f.value + 1))
            # The digit is the FINGER number shown to the patient (1..5), not
            # the DRV2605L channel. Finger-to-channel mapping is FINGER_CHANNEL.

        p.end()


# ══════════════════════════════════════════════════════════
#  DIAGNOSTICS DIALOG
# ══════════════════════════════════════════════════════════

class DiagnosticsDialog(QDialog):
    # Tests motors one at a time. Two independent checks per channel:
#   1) hardware — glove_client.diagnose_finger() reads the DRV2605L STATUS
#      register (open circuit, over-temperature, over-current);
#   2) subjective — the patient confirms whether vibration was actually felt.
# A motor counts as healthy only if both pass. The verdict is stored in
# ChannelInfo.diag_ok and becomes visible on the hand as a red dot.

    def __init__(self, parent, bridge: Bridge, glove: GloveClient,
                 channels: dict[Finger, ChannelInfo], demo: bool):
        super().__init__(parent)
        self.setWindowTitle("Диагностика моторов")
        self.setMinimumWidth(420)
        self.setModal(True)
        self._bridge = bridge
        self._glove = glove
        self._channels = channels
        self._demo = demo
        self._queue: list[Finger] = []
        self._current: Finger | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        info = QLabel(
            "Каждый мотор кратко завибрирует.\n"
            "Подтвердите, ощущается ли вибрация на каждом пальце."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #45454C;")
        layout.addWidget(info)

        self._result_grid = QGridLayout()
        self._result_labels: dict[Finger, QLabel] = {}
        for row, f in enumerate(ALL_FINGERS):
            name = QLabel(finger_ru(f))
            name.setStyleSheet("font-weight: 500;")
            self._result_grid.addWidget(name, row, 0)
            st = QLabel("—")
            st.setStyleSheet(f"color: {GREY};")
            st.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._result_grid.addWidget(st, row, 1)
            self._result_labels[f] = st
        layout.addLayout(self._result_grid)

        self._info_label = QLabel("")
        self._info_label.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        self._info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._info_label)

        self._confirm_row = QHBoxLayout()
        self._btn_yes = QPushButton("Да, чувствую")
        self._btn_yes.setObjectName("run")
        self._btn_yes.clicked.connect(lambda: self._user_answer(True))
        self._btn_yes.setVisible(False)
        self._confirm_row.addWidget(self._btn_yes)

        self._btn_no = QPushButton("Вибрации нет")
        self._btn_no.setObjectName("stopAll")
        self._btn_no.clicked.connect(lambda: self._user_answer(False))
        self._btn_no.setVisible(False)
        self._confirm_row.addWidget(self._btn_no)
        layout.addLayout(self._confirm_row)

        self._queue = list(ALL_FINGERS)
        QTimer.singleShot(300, self._test_next)

    def _test_next(self):
        if not self._queue:
            self._done()
            return

        self._current = self._queue.pop(0)
        self._info_label.setText(f"Проверка: {finger_ru(self._current)}")
        self._result_labels[self._current].setText("проверка")
        self._result_labels[self._current].setStyleSheet("color: #45454C;")
        self._btn_yes.setVisible(False)
        self._btn_no.setVisible(False)

        if self._demo:
            self._demo_test(self._current)
        else:
            threading.Thread(
                target=self._hw_test, args=(self._current,), daemon=True
            ).start()

    def _hw_test(self, finger: Finger):
        try:
            result = self._glove.diagnose_finger(finger)
        except Exception as e:
            result = {"ok": False, "detail": str(e), "status_raw": -1}
        self._bridge.diag_result.emit(finger, result)

    def _demo_test(self, finger: Finger):
        def worker():
            time.sleep(0.8)
            self._bridge.diag_result.emit(finger, {
                "ok": True, "detail": "OK", "status_raw": 0xE0,
            })
        threading.Thread(target=worker, daemon=True).start()

    def handle_diag_result(self, finger, result):
        if finger != self._current:
            return
        if not result["ok"]:
            self._result_labels[finger].setText(f"СБОЙ: {result['detail']}")
            self._result_labels[finger].setStyleSheet(f"color: {RED_TEXT}; font-weight: 600;")
            self._channels[finger].diag_ok = False
            self._channels[finger].state = FingerState.ERROR
            self._test_next()
        else:
            self._info_label.setText(
                f"Ощущалась ли вибрация ({finger_ru(finger)})?")
            self._btn_yes.setVisible(True)
            self._btn_no.setVisible(True)

    def _user_answer(self, felt: bool):
        f = self._current
        self._channels[f].diag_ok = felt
        self._channels[f].state = FingerState.IDLE if felt else FingerState.ERROR

        lbl = self._result_labels[f]
        if felt:
            lbl.setText("ОК")
            lbl.setStyleSheet(f"color: {GREEN_TEXT}; font-weight: 600;")
        else:
            lbl.setText("НЕ ОЩУЩАЕТСЯ")
            lbl.setStyleSheet(f"color: {RED_TEXT}; font-weight: 600;")

        self._btn_yes.setVisible(False)
        self._btn_no.setVisible(False)
        self._test_next()

    def _done(self):
        failures = [finger_ru(f) for f, ci in self._channels.items()
                    if ci.diag_ok is False]
        if failures:
            self._info_label.setText(f"Неисправности: {', '.join(failures)}")
            self._info_label.setStyleSheet(f"color: {RED_TEXT};")
        else:
            self._info_label.setText("Все моторы в порядке ✓")
            self._info_label.setStyleSheet(f"color: {GREEN_TEXT};")

        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.accept)
        self.layout().addWidget(close_btn)


# ══════════════════════════════════════════════════════════
#  SETTINGS DIALOG
# ══════════════════════════════════════════════════════════
# Per-channel advanced parameters.
#   M_RTP — continuous vibration; amplitude is written to the DRV2605L RTP
#           register and is driven by the power slider;
#   M_LIB — single click, effect no. 1..123 from the DRV2605L built-in library
#           (amplitude is fixed by the effect itself, the slider does nothing).
# The dialog only collects values; MotorControlApp._adv is what applies them.

M_RTP, M_LIB = "Вибрация (RTP)", "Щелчок (библиотека)"

class ChannelSettingsDialog(QDialog):
    """Extra parameters for the channel."""

    def __init__(self, parent, channels: dict[Finger, ChannelInfo]):
        super().__init__(parent)
        self.setWindowTitle("Настройки каналов")
        self.setMinimumWidth(620)
        self._channels = channels
        self._widgets: dict[Finger, dict] = {}

        layout = QVBoxLayout(self)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        headers = ["Канал", "Режим", "Нарастание (мс)", "Спад (мс)", "Эффект №"]
        for col, h in enumerate(headers):
            lbl = QLabel(h)
            lbl.setStyleSheet("font-weight: 600; color: #2E2E33;")
            grid.addWidget(lbl, 0, col)

        for r, finger in enumerate(ALL_FINGERS, start=1):
            ch = FINGER_CHANNEL[finger]
            ch_label = QLabel(f"CH{ch} · {finger_ru(finger)}")

            mode = QComboBox()
            mode.addItems([M_RTP, M_LIB])
            mode.setFixedWidth(170)

            up = QSpinBox();   up.setRange(0, 5000);  up.setSingleStep(10)
            up.setValue(150);  up.setFixedWidth(90)
            down = QSpinBox(); down.setRange(0, 5000); down.setSingleStep(10)
            down.setValue(200); down.setFixedWidth(90)
            eff = QSpinBox();  eff.setRange(1, 123);  eff.setValue(1)
            eff.setFixedWidth(80)

            def _mode_changed(m, _up=up, _down=down, _eff=eff):
                is_rtp = (m == M_RTP)
                _up.setEnabled(is_rtp)
                _down.setEnabled(is_rtp)
                _eff.setEnabled(not is_rtp)

            mode.currentTextChanged.connect(_mode_changed)
            _mode_changed(mode.currentText())

            grid.addWidget(ch_label, r, 0)
            grid.addWidget(mode, r, 1)
            grid.addWidget(up, r, 2)
            grid.addWidget(down, r, 3)
            grid.addWidget(eff, r, 4)

            self._widgets[finger] = {"mode": mode, "up": up, "down": down, "eff": eff}

        layout.addLayout(grid)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("ОК")
        ok_btn.setObjectName("run")
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

    def get_settings(self, finger: Finger) -> dict:
        w = self._widgets[finger]
        return {
            "mode": MotorMode.VIBRATION if w["mode"].currentText() == M_RTP else MotorMode.TICK,
            "ramp_up_ms": w["up"].value(),
            "ramp_down_ms": w["down"].value(),
            "effect_id": w["eff"].value(),
        }


# ══════════════════════════════════════════════════════════
#  SCENARIO PANEL
#  
# ══════════════════════════════════════════════════════════
# List of operating modes, exactly one active (QButtonGroup, exclusive).
# Three kinds of entries:
#   "Свои настройки" — a HIDDEN radio button (rb_manual). It is deliberately
#       not shown in the list: it represents the absence of a scenario. It is
#       selected programmatically whenever the user touches fingers or sliders.
#   built-in         — from models.PRESETS; list order equals the preset index;
#   user-defined     — loaded from user_scenarios.json.

class ScenarioPanel(QGroupBox):

    save_requested = Signal()
    user_scenario_selected = Signal(dict)
    mode_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__("Встроенные сценарии", parent)
        self._lay = QVBoxLayout(self)
        self._lay.setSpacing(2)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        self.rb_manual = QRadioButton(MANUAL_MODE_NAME, self)
        self.rb_manual.setVisible(False)       # не пункт списка, а его отсутствие
        self.rb_manual.setChecked(True)
        self._group.addButton(self.rb_manual)

        self._preset_radios: list[QRadioButton] = []
        for s in PRESETS:
            rb = QRadioButton(SCENARIO_NAMES_RU.get(s.name, s.name))
            self._group.addButton(rb)
            self._lay.addWidget(rb)
            self._preset_radios.append(rb)

        self._user_radios: list[tuple[QRadioButton, dict]] = []
        self._user_lay = QVBoxLayout()
        self._user_lay.setSpacing(2)
        self._lay.addLayout(self._user_lay)

        btn_save = QPushButton("Сохранить текущий")
        btn_save.clicked.connect(self.save_requested.emit)
        self._lay.addWidget(btn_save)

        self._group.buttonToggled.connect(
            lambda btn, on: on and self.mode_changed.emit(btn.text()))

    def add_user_scenario(self, cfg: dict, select: bool = False):
        rb = QRadioButton(cfg.get("name", "Без имени"))
        self._group.addButton(rb)
        self._user_lay.addWidget(rb)
        self._user_radios.append((rb, cfg))
        rb.toggled.connect(
            lambda on, c=cfg: on and self.user_scenario_selected.emit(c))
        if select:
            rb.setChecked(True)

    def clear_scenario(self):
        """Снять выбор со всех сценариев (вернуться к своим настройкам)."""
        if not self.rb_manual.isChecked():
            self.rb_manual.setChecked(True)

    def selection(self):
        """→ ('manual', None) | ('preset', index) | ('user', cfg)"""
        if self.rb_manual.isChecked():
            return ("manual", None)
        for i, rb in enumerate(self._preset_radios):
            if rb.isChecked():
                return ("preset", i)
        for rb, cfg in self._user_radios:
            if rb.isChecked():
                return ("user", cfg)
        return ("manual", None)


# ══════════════════════════════════════════════════════════
#  MAIN WINDOW
# ══════════════════════════════════════════════════════════
# Assembles everything and owns the session state.
# Key attributes:
#   _glove      — BLE client, shared by every panel and dialog
#   _demo_mode  — True while disconnected: no BLE command leaves the app
#   _channels   — ChannelInfo per finger: state (IDLE/ACTIVE/ERROR) and the
#                 diagnostics verdict; this is what the hand rendering reads
#   _adv        — advanced per-channel settings from ChannelSettingsDialog
#   _running    — "session in progress"; the worker thread polls this flag and
#                 stops itself (cooperative stop, no thread is ever killed)
#   _deadline   — wall-clock time at which the session must end

class MotorControlApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MCS Glove — Вибротактильная терапия")
        self.setMinimumSize(940, 600)

        self._glove = GloveClient()
        self._demo_mode = True
        self._running = False
        self._deadline = 0.0
        self._applying_cfg = False     # True while a scenario is being loaded.
                                       # Without it, set_selected() inside
                                       # _apply_config would fire
                                       # _on_hand_selection, which would
                                       # immediately reset the selection back
                                       # to "Свои настройки".

        self._channels: dict[Finger, ChannelInfo] = {
            f: ChannelInfo(finger=f, channel=FINGER_CHANNEL[f])
            for f in ALL_FINGERS
        }

        self._adv: dict[Finger, dict] = {
            f: {"mode": MotorMode.VIBRATION, "ramp_up_ms": 150,
                "ramp_down_ms": 200, "effect_id": 1}
            for f in ALL_FINGERS
        }

        self._bridge = Bridge()
        self._glove.dbg.on_status = lambda msg: self._bridge.status.emit(msg)
        self._bridge.connected.connect(self._on_connected)
        self._bridge.status.connect(self._on_status)
        self._bridge.battery.connect(self._on_battery)
        self._bridge.health.connect(self._on_health)
        self._bridge.diag_result.connect(self._on_diag_result)
        self._bridge.motor_update.connect(self._refresh_hand)
        self._bridge.run_end.connect(self._run_ended)

        self._build_ui()
        self._load_user_scenarios()

        self._batt_timer = QTimer(self)
        self._batt_timer.setInterval(5000)
        self._batt_timer.timeout.connect(self._poll_battery)

        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(200)
        self._countdown_timer.timeout.connect(self._update_countdown)

        self._on_connected(False)
        self._on_battery({"percent": 0})

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        main_layout = QVBoxLayout(root)
        main_layout.setSpacing(8)

        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(4, 4, 12, 4)
        top_bar.setSpacing(10)

        self._conn_panel = ConnectionPanel(self._bridge, self._glove)
        top_bar.addWidget(self._conn_panel, stretch=2)

        power_box = QGroupBox("Сила вибрации")
        power_lay = QVBoxLayout(power_box)
        power_lay.setSpacing(2)

        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("0 %"))
        self._global_slider = QSlider(Qt.Orientation.Horizontal)
        self._global_slider.setRange(0, 100)
        self._global_slider.setValue(60)
        self._global_slider.valueChanged.connect(self._on_global_power)
        slider_row.addWidget(self._global_slider, 1)
        slider_row.addWidget(QLabel("100 %"))
        power_lay.addLayout(slider_row)

        self._cb_all = QCheckBox("Выбрать все")
        self._cb_all.setChecked(False)
        self._cb_all.toggled.connect(self._on_select_all)
        power_lay.addWidget(self._cb_all)
        top_bar.addWidget(power_box, stretch=2)

        self._batt_widget = HorizontalBatteryWidget()
        top_bar.addWidget(self._batt_widget,
                          alignment=Qt.AlignmentFlag.AlignVCenter)

        top_widget = QWidget()
        top_widget.setLayout(top_bar)
        main_layout.addWidget(top_widget)

        body = QHBoxLayout()
        body.setSpacing(12)

        left = QVBoxLayout()
        left.setSpacing(8)

        self._scen_panel = ScenarioPanel()
        self._scen_panel.save_requested.connect(self._save_current_scenario)
        self._scen_panel.user_scenario_selected.connect(self._apply_config)
        left.addWidget(self._scen_panel)

        adv_box = QGroupBox("Доп. настройки")
        adv_grid = QGridLayout(adv_box)
        adv_grid.setHorizontalSpacing(8)
        adv_grid.setVerticalSpacing(6)

        adv_grid.addWidget(QLabel("Длительность сеанса, с"), 0, 0)
        self._spin_session = QSpinBox()
        self._spin_session.setRange(5, 3600)
        self._spin_session.setValue(60)
        adv_grid.addWidget(self._spin_session, 0, 1)

        adv_grid.addWidget(QLabel("Импульс, мс"), 1, 0)
        self._spin_pulse = QSpinBox()
        self._spin_pulse.setRange(100, 10000)
        self._spin_pulse.setSingleStep(50)
        self._spin_pulse.setValue(500)
        adv_grid.addWidget(self._spin_pulse, 1, 1)

        adv_grid.addWidget(QLabel("Пауза, мс"), 2, 0)
        self._spin_pause = QSpinBox()
        self._spin_pause.setRange(0, 5000)
        self._spin_pause.setSingleStep(50)
        self._spin_pause.setValue(200)
        adv_grid.addWidget(self._spin_pause, 2, 1)

        left.addWidget(adv_box)

        btn_diag = QPushButton("Диагностика")
        btn_diag.clicked.connect(self._open_diagnostics)
        left.addWidget(btn_diag)

        btn_ch = QPushButton("Настройки каналов")
        btn_ch.clicked.connect(self._open_channel_settings)
        left.addWidget(btn_ch)

        left.addStretch()
        body.addLayout(left, stretch=2)

        right = QVBoxLayout()
        right.setSpacing(6)

        self._hand = HandWidget(self._channels)
        self._hand.selection_changed.connect(self._on_hand_selection)
        right.addWidget(self._hand, stretch=1)

        right.addSpacing(14)          

        run_row = QHBoxLayout()
        run_row.setContentsMargins(0, 0, 0, 6)

        run_row.addStretch()

        self._timer_lbl = QLabel("")
        self._timer_lbl.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        self._timer_lbl.setStyleSheet("color: #2E2E33;")
        run_row.addWidget(self._timer_lbl)
        run_row.addSpacing(10)

        self._btn_run = QPushButton("Запуск")
        self._btn_run.setObjectName("run")
        self._btn_run.setFixedSize(160, 48)
        self._btn_run.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        self._btn_run.clicked.connect(self._on_run_click)
        run_row.addWidget(self._btn_run)

        right.addLayout(run_row)
        body.addLayout(right, stretch=3)

        main_layout.addLayout(body, stretch=1)

    def _on_global_power(self, v: int):
        self._hand.set_all_power(v)

    def _on_select_all(self, checked: bool):
        self._hand.set_all_selected(checked)

    def _on_hand_selection(self):
        all_sel = len(self._hand.selected_fingers()) == len(ALL_FINGERS)
        # Update the "select all" checkbox without emitting: otherwise it calls
        # set_all_selected(), which re-enters this handler — infinite recursion.
        self._cb_all.blockSignals(True)
        self._cb_all.setChecked(all_sel)
        self._cb_all.blockSignals(False)
      
        if not self._applying_cfg:
            self._scen_panel.clear_scenario()

    def _on_connected(self, ok: bool):
        self._demo_mode = not ok
        if ok:
            self._batt_timer.start()
            self._poll_battery()
        else:
            self._batt_timer.stop()
            self._on_battery({"percent": 0})

    def _on_status(self, msg: str):
        self._conn_panel.status_label.setText(msg)

    def _open_diagnostics(self):
        self._diag_dialog = DiagnosticsDialog(
            self, self._bridge, self._glove, self._channels, self._demo_mode,
        )
        self._diag_dialog.finished.connect(self._refresh_hand)
        self._diag_dialog.exec()
        self._refresh_hand()

    def _on_diag_result(self, finger, result):
        if hasattr(self, '_diag_dialog') and self._diag_dialog.isVisible():
            self._diag_dialog.handle_diag_result(finger, result)
        self._refresh_hand()

    def _open_channel_settings(self):
        dlg = ChannelSettingsDialog(self, self._channels)
        for f, w in dlg._widgets.items():
            s = self._adv[f]
            w["mode"].setCurrentText(
                M_RTP if s["mode"] == MotorMode.VIBRATION else M_LIB)
            w["up"].setValue(s["ramp_up_ms"])
            w["down"].setValue(s["ramp_down_ms"])
            w["eff"].setValue(s["effect_id"])

        if dlg.exec() == QDialog.DialogCode.Accepted:
            for f in ALL_FINGERS:
                self._adv[f] = dlg.get_settings(f)

    def _current_config(self) -> dict:
        return {
            "fingers": {
                str(int(f)): {
                    "selected": self._hand.is_selected(f),
                    "power": self._hand.power(f),
                } for f in ALL_FINGERS
            },
            "session_s": self._spin_session.value(),
            "pulse_ms": self._spin_pulse.value(),
            "pause_ms": self._spin_pause.value(),
        }

    def _apply_config(self, cfg: dict):
        self._applying_cfg = True
        try:
            for f in ALL_FINGERS:
                fc = cfg.get("fingers", {}).get(str(int(f)))
                if fc is None:
                    continue
                self._hand.set_selected(f, bool(fc.get("selected", True)))
                self._hand.set_power(f, int(fc.get("power", 60)))
            self._spin_session.setValue(int(cfg.get("session_s", 60)))
            self._spin_pulse.setValue(int(cfg.get("pulse_ms", 500)))
            self._spin_pause.setValue(int(cfg.get("pause_ms", 200)))
        finally:
            self._applying_cfg = False

    def _save_current_scenario(self):
        name, ok = QInputDialog.getText(
            self, "Сохранить сценарий", "Название сценария:")
        if not ok or not name.strip():
            return
        cfg = self._current_config()
        cfg["name"] = name.strip()

        scenarios = self._read_user_scenarios()
        scenarios.append(cfg)
        try:
            USER_SCENARIOS_FILE.write_text(
                json.dumps(scenarios, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as e:
            self._bridge.status.emit(f"Ошибка сохранения сценария: {e}")
            return
        self._scen_panel.add_user_scenario(cfg, select=True)

    def _read_user_scenarios(self) -> list[dict]:
        try:
            return json.loads(USER_SCENARIOS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _load_user_scenarios(self):
        for cfg in self._read_user_scenarios():
            self._scen_panel.add_user_scenario(cfg)

    def _on_run_click(self):
        if self._running:
            self._stop_run()
        else:
            self._start_run()

    def _start_run(self):
        kind, payload = self._scen_panel.selection()

        if kind == "user":
            self._apply_config(payload)     

        if kind == "manual":
            fingers = self._hand.selected_fingers()
            if not fingers:
                self._bridge.status.emit("Не выбран ни один палец")
                return

        self._running = True
        self._deadline = time.time() + self._spin_session.value()
        self._btn_run.setText("Стоп")
        self._btn_run.setObjectName("stopAll")
        self._btn_run.setStyleSheet("")       # Qt caches the resolved style, so
        self._btn_run.style().unpolish(self._btn_run)   # after changing
        self._btn_run.style().polish(self._btn_run)     # objectName it must be
                                                        # unpolished/repolished
                                                        # or the button stays green
        self._countdown_timer.start()
        self._update_countdown()

        if kind == "preset":
            scenario = PRESETS[payload]
            worker = lambda: self._play_preset(scenario)
        else:
            fingers = self._hand.selected_fingers()
            powers = {f: self._hand.power(f) for f in fingers}
            pulse_s = self._spin_pulse.value() / 1000.0
            pause_s = self._spin_pause.value() / 1000.0
            worker = lambda: self._play_manual(fingers, powers, pulse_s, pause_s)

        def run():
            try:
                worker()
            except Exception as e:
                self._bridge.status.emit(f"Ошибка сценария: {e}")
            finally:
                self._bridge.run_end.emit()

        threading.Thread(target=run, daemon=True).start()

    def _stop_run(self):
        """Cooperative stop: only clear the flag. The scenario thread notices it
        within 100 ms, finishes, and emits run_end itself. all_off() is sent in
        parallel so the motors go quiet immediately rather than at the next
        loop iteration."""

        self._running = False
        if not self._demo_mode:
            threading.Thread(target=self._glove.all_off, daemon=True).start()

    def _run_ended(self):
        self._running = False
        self._countdown_timer.stop()
        self._timer_lbl.setText("")
        self._btn_run.setText("Запуск")
        self._btn_run.setObjectName("run")
        self._btn_run.setStyleSheet("")
        self._btn_run.style().unpolish(self._btn_run)
        self._btn_run.style().polish(self._btn_run)
        for f in ALL_FINGERS:
            if self._channels[f].state == FingerState.ACTIVE:
                self._channels[f].state = FingerState.IDLE
        self._refresh_hand()

    def _update_countdown(self):
        remaining = max(0, int(self._deadline - time.time() + 0.5))
        self._timer_lbl.setText(f"{remaining // 60}:{remaining % 60:02d}")

    def _sleep_run(self, seconds: float) -> bool:
        """Sleep in 100 ms slices, checking the stop flag and the deadline.
        A single time.sleep() for the full step would make the Stop button
        unresponsive — the session would keep running until the step ended.
        -> True if the session may continue."""
        end = time.time() + seconds
        while time.time() < end:
            if not self._running or time.time() >= self._deadline:
                return False
            time.sleep(min(0.1, end - time.time()))
        return self._running and time.time() < self._deadline

    def _play_manual(self, fingers, powers, pulse_s, pause_s):
        vib = [f for f in fingers if self._adv[f]["mode"] == MotorMode.VIBRATION]
        tick = [f for f in fingers if self._adv[f]["mode"] == MotorMode.TICK]

        for f in fingers:
            self._channels[f].state = FingerState.ACTIVE
        if not self._demo_mode:
            for f in vib:
                self._glove.vibration_on(f, powers[f])
            for f in tick:
                self._glove.tick(f, self._adv[f]["effect_id"])
        self._bridge.motor_update.emit()

        # DRV2605L library effects are one-shot: the driver plays the click and
        # stops. To keep clicks going for the whole session they have to be
        # re-triggered manually every (pulse + pause) ms.
        tick_interval = pulse_s + pause_s
        tick_interval = pulse_s + pause_s
        last_tick = time.time()
        while self._running and time.time() < self._deadline:
            time.sleep(0.1)
            if tick and not self._demo_mode and \
                    time.time() - last_tick >= tick_interval:
                for f in tick:
                    self._glove.tick(f, self._adv[f]["effect_id"])
                last_tick = time.time()

        if not self._demo_mode:
            self._glove.all_off()

    def _play_preset(self, scenario: Scenario):
        while self._running and time.time() < self._deadline:
            for step in scenario.steps:
                if not self._running or time.time() >= self._deadline:
                    break

                for f in step.fingers:
                    self._channels[f].state = FingerState.ACTIVE
                    if not self._demo_mode:
                        if step.mode == MotorMode.VIBRATION:
                            self._glove.vibration_on(f, step.intensity)
                        else:
                            self._glove.tick(f, step.effect_id)
                self._bridge.motor_update.emit()

                self._sleep_run(step.duration_ms / 1000.0)

                for f in step.fingers:
                    if not self._demo_mode:
                        self._glove.vibration_off(f)
                    if self._channels[f].state == FingerState.ACTIVE:
                        self._channels[f].state = FingerState.IDLE
                self._bridge.motor_update.emit()
                time.sleep(0.05)

            if not scenario.loop:
                break

        if not self._demo_mode:
            self._glove.all_off()

    def _on_health(self, finger_int: int, text: str, color: str):
        finger = Finger(finger_int)
        if color == RED:
            self._channels[finger].state = FingerState.ERROR
        self._bridge.status.emit(f"{finger_ru(finger)}: {text}")
        self._refresh_hand()

    def _poll_battery(self):
        if self._demo_mode or not self._glove.connected:
            return

        def worker():
            try:
                self._bridge.battery.emit(self._glove.battery_info())
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _on_battery(self, info: dict):
        self._batt_widget.setChargePercent(info.get("percent", 0))

    def _refresh_hand(self):
        self._hand.update_display()

    def closeEvent(self, event):
        """Order matters: silence the motors first, drop the link second.
        Otherwise the glove keeps vibrating until the battery runs flat."""
        
        self._batt_timer.stop()
        self._running = False
        try:
            if not self._demo_mode:
                self._glove.all_off()
        except Exception:
            pass
        self._glove.close()
        super().closeEvent(event)


# ══════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════
# The stylesheet is applied before the window is constructed; otherwise some
# widgets keep the native platform look.

def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    window = MotorControlApp()
    window.resize(1000, 640)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
