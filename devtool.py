"""
devtool.py — MCS Glove developer / diagnostic tool (Russian UI).
  * 8-channel motor grid, per-channel mode: RTP / ROM effect library /
    autocalibration / diagnostics. Honest health after each run.
  * Battery panel (MAX17055) as a narrow auto-refreshing column on the right.
Requires devclient.py and: pip install PySide6 bleak.  No safety cap.
"""

import sys
import threading
import time
import json
import os
import asyncio
from pathlib import Path

from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QSpinBox, QTextEdit, QGroupBox, QCheckBox, QComboBox,
    QDialog, QTableWidget, QTableWidgetItem, QHeaderView, QScrollArea,
    QProgressBar, QFileDialog
)
from PySide6.QtGui import QColor

from bleak import BleakClient, BleakScanner

from devclient import DebugClient

DEVICE_NAME  = "MCS Glove"
NUM_CHANNELS = 8
RSENSE_MOHM  = 10.0

SETTINGS_FILE = "mcs_glove_last_settings.json"

# OTA 
OTA_SERVICE_UUID = "1d14d6ee-fd63-4fa1-bfa4-8f47b42119f0"
OTA_CONTROL_UUID = "f7bf3564-fb6d-4e53-88a4-5e37e0326063"
OTA_DATA_UUID    = "984227f3-34fc-4045-a5d0-2c581f81a153"

OTA_CMD_START = 0x00      
OTA_CMD_END   = 0x03      

DFU_NAMES = (DEVICE_NAME, "OTA", "Apploader", "MCS Glove OTA")

GBL_MAGIC = bytes((0xEB, 0x17, 0xA6, 0x03))   

# DRV registers / modes
REG_MODE, REG_RTP, REG_STATUS = 0x01, 0x02, 0x00
REG_LIBRARY, REG_WAVE1, REG_WAVE2, REG_GO = 0x03, 0x04, 0x05, 0x0C
MODE_INTERNAL, MODE_RTP, MODE_DIAG, MODE_AUTOCAL, MODE_STANDBY = 0x00, 0x05, 0x06, 0x07, 0x40

# mode dropdown labels
M_RTP, M_LIB, M_CAL, M_DIAG = "RTP", "Библиотека", "Автокалибр.", "Диагностика"

# Fixed Library B
DEFAULT_LIBRARY_NAME = "B · 3 В  быстр."
DEFAULT_LIBRARY_VAL = 2

# ==================== STYLE ====================
STYLE = """
QWidget { background:#FAF9F5; color:#1A1915;
          font-family:'Segoe UI',Arial,sans-serif; font-size:13px; }
QLabel { background:transparent; }
QCheckBox { background:transparent; }
QGroupBox { background:#FFFFFF; border:1px solid #E4E2DA; border-radius:10px;
            margin-top:6px; padding:6px; font-weight:500; }
QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 4px; color:#6B6A63; }
QPushButton { background:#FFFFFF; border:1px solid #D8D6CC; border-radius:8px; padding:5px 10px; }
QPushButton:hover { background:#F1EFE8; }
QPushButton:disabled { color:#B4B2A9; border-color:#E4E2DA; }
QPushButton#run { background:#2AA67B; color:#FFFFFF; border:none; }
QPushButton#run:hover { background:#24906A; }
QPushButton#stopAll { background:#A32D2D; color:#FFFFFF; border:none; font-weight:500; }
QPushButton#stopAll:hover { background:#922828; }
QComboBox { background:#FFFFFF; border:1px solid #D8D6CC; border-radius:6px; padding:3px 6px; }
QSpinBox {
    background:#FFFFFF; border:1px solid #D8D6CC; border-radius:6px;
    padding:3px 6px;
}
QSpinBox::up-button, QSpinBox::down-button {
    subcontrol-origin: border; width:18px;
    background:transparent; border-left:1px solid #ECEAE2;
}
QSpinBox::up-button   { subcontrol-position: top right; }
QSpinBox::down-button { subcontrol-position: bottom right; }
QSpinBox:disabled, QComboBox:disabled { color:#C4C2B8; background:#F7F6F1; }
QTextEdit { background:#FFFFFF; border:1px solid #E4E2DA; border-radius:8px;
            font-family:'Consolas',monospace; font-size:12px; color:#6B6A63; }
"""

GREEN, RED, GREY = "#2AA67B", "#A32D2D", "#B4B2A9"

def designator(ch):
    return f"U{22 + ch}"

# ==================== MAX17055 METRICS ====================
def s16(raw):
    return raw - 65536 if raw >= 32768 else raw

def dec_voltage(raw): return f"{raw * 5 / 64:.0f}"
def dec_current(raw): return f"{s16(raw) * 1.5625 / RSENSE_MOHM:.1f}"
def dec_temp(raw):    return f"{s16(raw) / 256:.1f}"
def dec_soc(raw):     return f"{raw / 256:.1f}"
def dec_cap(raw):     return f"{raw * 5.0 / RSENSE_MOHM:.1f}"
def dec_time(raw):    return f"{raw * 5.625 / 60:.0f}"
def dec_pct(raw):     return f"{raw / 256:.2f}"
def dec_cycles(raw):  return f"{raw / 100:.1f}"

METRICS = [
    ("Напряжение", 0x09, dec_voltage, "мВ"),
    ("Ток",        0x0A, dec_current, "мА"),
    ("Заряд",      0x06, dec_soc,     "%"),
    ("Ёмкость",    0x05, dec_cap,     "мА·ч"),
    ("Темп.",      0x08, dec_temp,    "°C"),
    ("До разряда", 0x11, dec_time,    "мин"),
    ("До заряда",  0x20, dec_time,    "мин"),
    ("Циклы",      0x17, dec_cycles,  ""),
    ("Износ",      0x07, dec_pct,     "%"),
]

# ==================== FULL 123 EFFECTS (English, clean) ====================
EFFECTS_LIST = [
    (1, "Strong Click - 100%"),
    (2, "Strong Click - 60%"),
    (3, "Strong Click - 30%"),
    (4, "Sharp Click - 100%"),
    (5, "Sharp Click - 60%"),
    (6, "Sharp Click - 30%"),
    (7, "Soft Bump - 100%"),
    (8, "Soft Bump - 60%"),
    (9, "Soft Bump - 30%"),
    (10, "Double Click - 100%"),
    (11, "Double Click - 60%"),
    (12, "Triple Click - 100%"),
    (13, "Soft Fuzz - 60%"),
    (14, "Strong Buzz - 100%"),
    (15, "750 ms Alert 100%"),
    (16, "1000 ms Alert 100%"),
    (17, "Strong Click 1 - 100%"),
    (18, "Strong Click 2 - 80%"),
    (19, "Strong Click 3 - 60%"),
    (20, "Strong Click 4 - 30%"),
    (21, "Medium Click 1 - 100%"),
    (22, "Medium Click 2 - 80%"),
    (23, "Medium Click 3 - 60%"),
    (24, "Sharp Tick 1 - 100%"),
    (25, "Sharp Tick 2 - 80%"),
    (26, "Sharp Tick 3 - 60%"),
    (27, "Short Double Click Strong 1 - 100%"),
    (28, "Short Double Click Strong 2 - 80%"),
    (29, "Short Double Click Strong 3 - 60%"),
    (30, "Short Double Click Strong 4 - 30%"),
    (31, "Short Double Click Medium 1 - 100%"),
    (32, "Short Double Click Medium 2 - 80%"),
    (33, "Short Double Click Medium 3 - 60%"),
    (34, "Short Double Sharp Tick 1 - 100%"),
    (35, "Short Double Sharp Tick 2 - 80%"),
    (36, "Short Double Sharp Tick 3 - 60%"),
    (37, "Long Double Sharp Click Strong 1 - 100%"),
    (38, "Long Double Sharp Click Strong 2 - 80%"),
    (39, "Long Double Sharp Click Strong 3 - 60%"),
    (40, "Long Double Sharp Click Strong 4 - 30%"),
    (41, "Long Double Sharp Click Medium 1 - 100%"),
    (42, "Long Double Sharp Click Medium 2 - 80%"),
    (43, "Long Double Sharp Click Medium 3 - 60%"),
    (44, "Long Double Sharp Tick 1 - 100%"),
    (45, "Long Double Sharp Tick 2 - 80%"),
    (46, "Long Double Sharp Tick 3 - 60%"),
    (47, "Buzz 1 - 100%"),
    (48, "Buzz 2 - 80%"),
    (49, "Buzz 3 - 60%"),
    (50, "Buzz 4 - 40%"),
    (51, "Buzz 5 - 20%"),
    (52, "Pulsing Strong 1 - 100%"),
    (53, "Pulsing Strong 2 - 60%"),
    (54, "Pulsing Medium 1 - 100%"),
    (55, "Pulsing Medium 2 - 60%"),
    (56, "Pulsing Sharp 1 - 100%"),
    (57, "Pulsing Sharp 2 - 60%"),
    (58, "Transition Click 1 - 100%"),
    (59, "Transition Click 2 - 80%"),
    (60, "Transition Click 3 - 60%"),
    (61, "Transition Click 4 - 40%"),
    (62, "Transition Click 5 - 20%"),
    (63, "Transition Click 6 - 10%"),
    (64, "Transition Hum 1 - 100%"),
    (65, "Transition Hum 2 - 80%"),
    (66, "Transition Hum 3 - 60%"),
    (67, "Transition Hum 4 - 40%"),
    (68, "Transition Hum 5 - 20%"),
    (69, "Transition Hum 6 - 10%"),
    (70, "Transition Ramp Down Long Smooth 1 - 100 to 0%"),
    (71, "Transition Ramp Down Long Smooth 2 - 100 to 0%"),
    (72, "Transition Ramp Down Medium Smooth 1 - 100 to 0%"),
    (73, "Transition Ramp Down Medium Smooth 2 - 100 to 0%"),
    (74, "Transition Ramp Down Short Smooth 1 - 100 to 0%"),
    (75, "Transition Ramp Down Short Smooth 2 - 100 to 0%"),
    (76, "Transition Ramp Down Long Sharp 1 - 100 to 0%"),
    (77, "Transition Ramp Down Long Sharp 2 - 100 to 0%"),
    (78, "Transition Ramp Down Medium Sharp 1 - 100 to 0%"),
    (79, "Transition Ramp Down Medium Sharp 2 - 100 to 0%"),
    (80, "Transition Ramp Down Short Sharp 1 - 100 to 0%"),
    (81, "Transition Ramp Down Short Sharp 2 - 100 to 0%"),
    (82, "Transition Ramp Up Long Smooth 1 - 0 to 100%"),
    (83, "Transition Ramp Up Long Smooth 2 - 0 to 100%"),
    (84, "Transition Ramp Up Medium Smooth 1 - 0 to 100%"),
    (85, "Transition Ramp Up Medium Smooth 2 - 0 to 100%"),
    (86, "Transition Ramp Up Short Smooth 1 - 0 to 100%"),
    (87, "Transition Ramp Up Short Smooth 2 - 0 to 100%"),
    (88, "Transition Ramp Up Long Sharp 1 - 0 to 100%"),
    (89, "Transition Ramp Up Long Sharp 2 - 0 to 100%"),
    (90, "Transition Ramp Up Medium Sharp 1 - 0 to 100%"),
    (91, "Transition Ramp Up Medium Sharp 2 - 0 to 100%"),
    (92, "Transition Ramp Up Short Sharp 1 - 0 to 100%"),
    (93, "Transition Ramp Up Short Sharp 2 - 0 to 100%"),
    (94, "Transition Ramp Down Long Smooth 1 - 50 to 0%"),
    (95, "Transition Ramp Down Long Smooth 2 - 50 to 0%"),
    (96, "Transition Ramp Down Medium Smooth 1 - 50 to 0%"),
    (97, "Transition Ramp Down Medium Smooth 2 - 50 to 0%"),
    (98, "Transition Ramp Down Short Smooth 1 - 50 to 0%"),
    (99, "Transition Ramp Down Short Smooth 2 - 50 to 0%"),
    (100, "Transition Ramp Down Long Sharp 1 - 50 to 0%"),
    (101, "Transition Ramp Down Long Sharp 2 - 50 to 0%"),
    (102, "Transition Ramp Down Medium Sharp 1 - 50 to 0%"),
    (103, "Transition Ramp Down Medium Sharp 2 - 50 to 0%"),
    (104, "Transition Ramp Down Short Sharp 1 - 50 to 0%"),
    (105, "Transition Ramp Down Short Sharp 2 - 50 to 0%"),
    (106, "Transition Ramp Up Long Smooth 1 - 0 to 50%"),
    (107, "Transition Ramp Up Long Smooth 2 - 0 to 50%"),
    (108, "Transition Ramp Up Medium Smooth 1 - 0 to 50%"),
    (109, "Transition Ramp Up Medium Smooth 2 - 0 to 50%"),
    (110, "Transition Ramp Up Short Smooth 1 - 0 to 50%"),
    (111, "Transition Ramp Up Short Smooth 2 - 0 to 50%"),
    (112, "Transition Ramp Up Long Sharp 1 - 0 to 50%"),
    (113, "Transition Ramp Up Long Sharp 2 - 0 to 50%"),
    (114, "Transition Ramp Up Medium Sharp 1 - 0 to 50%"),
    (115, "Transition Ramp Up Medium Sharp 2 - 0 to 50%"),
    (116, "Transition Ramp Up Short Sharp 1 - 0 to 50%"),
    (117, "Transition Ramp Up Short Sharp 2 - 0 to 50%"),
    (118, "Long buzz for programmatic stopping - 100%"),
    (119, "Smooth Hum 1 (No kick or brake pulse) - 50%"),
    (120, "Smooth Hum 2 (No kick or brake pulse) - 40%"),
    (121, "Smooth Hum 3 (No kick or brake pulse) - 30%"),
    (122, "Smooth Hum 4 (No kick or brake pulse) - 20%"),
    (123, "Smooth Hum 5 (No kick or brake pulse) - 10%"),
]

class EffectListDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Список эффектов DRV2605L (123 эффекта)")
        self.resize(1050, 750)
        self.setModal(True)

        layout = QVBoxLayout(self)

        table = QTableWidget(len(EFFECTS_LIST), 2, self)
        table.setHorizontalHeaderLabels(["№", "Название эффекта"])
        
        # Better column sizing
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.NoEditTriggers)      # read-only
        table.setSelectionMode(QTableWidget.NoSelection)        # disable selection completely
        table.verticalHeader().setVisible(False)                # hide default row numbers

        # Fill table
        for row, (num, name) in enumerate(EFFECTS_LIST):
            table.setItem(row, 0, QTableWidgetItem(str(num)))
            table.setItem(row, 1, QTableWidgetItem(name))

        # Nice clean styling
        table.setStyleSheet("""
            QTableWidget {
                background-color: #FAF9F5;
                color: #1A1915;
                gridline-color: #E4E2DA;
                font-size: 13px;
            }
            QTableWidget::item {
                padding: 4px;
            }
            QTableWidget::item:alternate {
                background-color: #F1EFE8;
            }
            QHeaderView::section {
                background-color: #E4E2DA;
                color: #1A1915;
                font-weight: 500;
                padding: 6px;
                border: none;
            }
        """)

        layout.addWidget(table)
        self.setFocus()   # allow closing with Esc

class DiagnosticsDialog(QDialog):
    def __init__(self, parent, dbg):
        super().__init__(parent)
        self.dbg = dbg
        self.setWindowTitle("Диагностика DRV2605L — Все 8 каналов")
        self.resize(1150, 680)

        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        refresh_btn = QPushButton("Обновить все")
        refresh_btn.clicked.connect(self._refresh_all)
        
        dump_btn = QPushButton("Полный дамп выбранного канала")
        dump_btn.clicked.connect(self._show_full_dump)
        
        toolbar.addWidget(refresh_btn)
        toolbar.addWidget(dump_btn)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self.table = QTableWidget(8, 9, self)
        headers = ["Канал", "DEVICE_ID", "STATUS", "MODE", "LIBRARY", "RTP", "Диагностика", "Проблемы", "Статус"]
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)

        self._refresh_all()

    def _read_reg(self, ch, reg):
        try:
            r = self.dbg.read_reg(ch, reg)
            return r["value"] if r["ok"] else None
        except:
            return None

    def _refresh_all(self):
        for ch in range(8):
            status = self._read_reg(ch, 0x00)
            mode   = self._read_reg(ch, 0x01)
            lib    = self._read_reg(ch, 0x03)
            rtp    = self._read_reg(ch, 0x02)

            devid = "??"
            if status is not None:
                devid_val = (status >> 5) & 0x07
                names = {3:"DRV2605", 7:"DRV2605L", 4:"DRV2604", 6:"DRV2604L"}
                devid = names.get(devid_val, f"0x{devid_val:02X}")

            problems = []
            if status is not None:
                if status & 0x01: problems.append("OC/Нет мотора")
                if (status >> 1) & 0x01: problems.append("Перегрев")
                if (status >> 3) & 0x01: problems.append("Ошибка калибровки")

            status_str = f"0x{status:02X}" if status is not None else "—"

            self.table.setItem(ch, 0, QTableWidgetItem(f"CH{ch}"))
            self.table.setItem(ch, 1, QTableWidgetItem(devid))
            self.table.setItem(ch, 2, QTableWidgetItem(status_str))
            self.table.setItem(ch, 3, QTableWidgetItem(f"0x{mode:02X}" if mode is not None else "—"))
            self.table.setItem(ch, 4, QTableWidgetItem(f"0x{lib:02X}" if lib is not None else "—"))
            self.table.setItem(ch, 5, QTableWidgetItem(f"0x{rtp:02X}" if rtp is not None else "—"))
            self.table.setItem(ch, 6, QTableWidgetItem("Проблема" if problems else "OK"))
            self.table.setItem(ch, 7, QTableWidgetItem(", ".join(problems) if problems else "—"))
            self.table.setItem(ch, 8, QTableWidgetItem("ПРОБЛЕМА" if problems else "OK"))

    def _show_full_dump(self):
        row = self.table.currentRow()
        if row < 0:
            return
        dialog = FullRegisterDumpDialog(self, self.dbg, row)
        dialog.exec()


class FullRegisterDumpDialog(QDialog):
    def __init__(self, parent, dbg, ch):
        super().__init__(parent)
        self.dbg = dbg
        self.ch = ch
        self.setWindowTitle(f"Полный дамп регистров DRV2605L — CH{ch}")
        self.resize(800, 600)

        layout = QVBoxLayout(self)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setFontFamily("Consolas")
        layout.addWidget(self.text)

        btn = QPushButton("Обновить дамп")
        btn.clicked.connect(self._dump_registers)
        layout.addWidget(btn)

        self._dump_registers()

    def _dump_registers(self):
        text = f"=== Регистры DRV2605L CH{self.ch} ===\n\n"
        for reg in range(0x00, 0x20):   # основные регистры
            val = self._read_reg(self.ch, reg)
            text += f"0x{reg:02X} = 0x{val:02X}\n"
        self.text.setText(text)

    def _read_reg(self, ch, reg):
        try:
            r = self.dbg.read_reg(ch, reg)
            return r["value"] if r["ok"] else 0xFF
        except:
            return 0xFF
        
class CycleSettingsDialog(QDialog):
    """Editor for cycling parameters. Writes back into the params dict."""
    def __init__(self, params, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки цикла")
        self.setMinimumWidth(400)
        self.params = params

        v = QVBoxLayout(self)
        grid = QGridLayout()
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(0, 1)

        self.on    = TimeEdit(params["on"],    max_ms=3600000)
        self.pause = TimeEdit(params["pause"], max_ms=3600000)

        self.cycles = QSpinBox(); self.cycles.setRange(1, 100000)
        self.limit  = QSpinBox(); self.limit.setRange(1, 240)
        self.cool   = QSpinBox(); self.cool.setRange(1, 240)
        for sb in (self.cycles, self.limit, self.cool):
            sb.setMinimumWidth(110)
        self.cycles.setValue(params["cycles"])
        self.limit.setValue(params["limit"])
        self.cool.setValue(params["cool"])

        self.infinite = QCheckBox("Бесконечно (до нажатия «Стоп цикла»)")
        self.infinite.setChecked(params["infinite"])

        rows = [("Работа,", self.on),
                ("Пауза, ", self.pause),
                ("Число циклов", self.cycles)]
        for r, (text, wdg) in enumerate(rows):
            grid.addWidget(QLabel(text), r, 0)
            grid.addWidget(wdg, r, 1)
        v.addLayout(grid)
        v.addWidget(self.infinite)

        prot = QGroupBox("Защита моторов от перегрева")
        pg = QGridLayout(prot)
        pg.setColumnStretch(0, 1)
        pg.addWidget(QLabel("Лимит работы, мин"), 0, 0); pg.addWidget(self.limit, 0, 1)
        pg.addWidget(QLabel("Охлаждение, мин"),   1, 0); pg.addWidget(self.cool, 1, 1)
        v.addWidget(prot)

        hint = QLabel("После суммарной работы, равной лимиту, цикл делает паузу "
                      "на время охлаждения и продолжает. Дополнительно цикл "
                      "останавливается автоматически при перегреве драйвера.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#6B6A63;")
        v.addWidget(hint)

        btns = QHBoxLayout()
        ok = QPushButton("Применить"); ok.setObjectName("run")
        cancel = QPushButton("Отмена")
        ok.clicked.connect(self._apply)
        cancel.clicked.connect(self.reject)
        btns.addStretch(); btns.addWidget(cancel); btns.addWidget(ok)
        v.addLayout(btns)

        self.infinite.toggled.connect(lambda on: self.cycles.setEnabled(not on))
        self.cycles.setEnabled(not self.infinite.isChecked())

    def _apply(self):
        self.params["on"] = self.on.value_ms()
        self.params["pause"] = self.pause.value_ms()
        self.params["cycles"] = self.cycles.value()
        self.params["infinite"] = self.infinite.isChecked()
        self.params["limit"] = self.limit.value()
        self.params["cool"] = self.cool.value()
        self.accept()

class TimeEdit(QWidget):
    """Spinbox + unit selector (мс / с / мин). Value is always handled in ms."""
    UNITS = [("мс", 1), ("с", 1000), ("мин", 60000)]

    def __init__(self, ms=500, max_ms=3600000, default_unit=None, parent=None):
        super().__init__(parent)
        self._max_ms = max_ms
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self.spin = QSpinBox()
        self.spin.setRange(0, 999999)
        self.spin.setFixedWidth(72)
        self.unit = QComboBox()
        for name, _f in self.UNITS:
            self.unit.addItem(name)
        self.unit.setFixedWidth(56)

        lay.addWidget(self.spin, 1)
        lay.addWidget(self.unit, 0)

        self._factor = 1
        self.unit.currentIndexChanged.connect(self._unit_changed)
        self.set_ms(ms)
        if default_unit is not None:
            self.unit.setCurrentIndex(default_unit)

    def value_ms(self):
        return min(self.spin.value() * self._factor, self._max_ms)

    def set_ms(self, ms, unit_idx=None):
        """Set the value. unit_idx pins the unit; otherwise keep the current one."""
        ms = max(0, min(int(ms), self._max_ms))
        if unit_idx is not None:
            self.unit.blockSignals(True)
            self.unit.setCurrentIndex(unit_idx)
            self.unit.blockSignals(False)
            self._factor = self.UNITS[unit_idx][1]
        self.spin.setValue(round(ms / self._factor))

    def setEnabled(self, on):
        super().setEnabled(on)
        self.spin.setEnabled(on)
        self.unit.setEnabled(on)

    def _unit_changed(self, idx):
        """Unit is the user's choice — the number stays as typed."""
        self._factor = self.UNITS[idx][1]

class Bridge(QObject):
    status = Signal(str)
    connected = Signal(bool)
    health = Signal(int, str, str, int)
    battery = Signal(object)
    devinfo = Signal(object)
    cycle_done = Signal()

class OtaBridge(QObject):
    log      = Signal(str)
    progress = Signal(int, int)        # sent, total
    finished = Signal(bool, str)       # ok, message


class OtaUploader:
    """Silicon Labs OTA DFU over BLE via AppLoader (In-Place OTA DFU).

    Phase 1 — if the device is running the application, write 0x00 to
              ota_control; the stack reboots into AppLoader.
    Phase 2 — reconnect, write 0x00 to ota_control, stream the .gbl to
              ota_data, write 0x03 to finish. The device verifies the image,
              boots the new application and drops the link.

    DFU mode is detected by the presence of ota_data: the application exposes
    only ota_control, AppLoader exposes both.
    """

    def __init__(self, bridge, address=None):
        self.bridge = bridge
        self.address = address
        self._cancel = threading.Event()

    # ---- public ----
    def start(self, gbl_path, reliable=False):
        self._cancel.clear()
        threading.Thread(target=self._main, args=(gbl_path, reliable),
                         daemon=True).start()

    def cancel(self):
        self._cancel.set()

    # ---- thread entry ----
    def _main(self, gbl_path, reliable):
        try:
            asyncio.run(self._run(gbl_path, reliable))
        except Exception as e:
            self.bridge.finished.emit(False, str(e))

    def _say(self, msg):
        self.bridge.log.emit(msg)

    def _check_cancel(self):
        if self._cancel.is_set():
            raise RuntimeError("отменено пользователем")

    # ---- main sequence ----
    async def _run(self, gbl_path, reliable):
        data = Path(gbl_path).read_bytes()
        if data[:4] != GBL_MAGIC:
            raise RuntimeError("файл не похож на GBL (неверная сигнатура)")
        self._say(f"Файл: {Path(gbl_path).name} — {len(data)} байт")

        client = await self._open()
        try:
            if not self._has(client, OTA_DATA_UUID):
                self._say("Устройство в рабочем режиме — перевод в DFU...")
                await self._write_ctrl(client, OTA_CMD_START, tolerate_drop=True)
                await self._close(client)
                client = None
                await asyncio.sleep(2.0)
                client = await self._open()
                if not self._has(client, OTA_DATA_UUID):
                    raise RuntimeError(
                        "AppLoader не найден: характеристика ota_data отсутствует")
            self._say("Режим DFU подтверждён")
            await self._upload(client, data, reliable)
        finally:
            await self._close(client)

    # ---- scan / connect ----
    async def _find(self, timeout=6.0):
        devs = await BleakScanner.discover(timeout=timeout)
        if self.address:
            for d in devs:
                if d.address.upper() == self.address.upper():
                    return d
        for d in devs:
            if d.name and d.name.strip() in DFU_NAMES:
                return d
        return None

    async def _connect(self, dev, timeout=20.0):
        # use_cached_services=False — Windows caches the GATT database and would
        # otherwise still show the application's table after the AppLoader boots
        try:
            client = BleakClient(dev, timeout=timeout,
                                 winrt=dict(use_cached_services=False))
        except TypeError:
            client = BleakClient(dev, timeout=timeout)
        await client.connect()
        return client

    async def _open(self, attempts=5):
        last = "—"
        for i in range(1, attempts + 1):
            self._check_cancel()
            dev = await self._find()
            if dev is None:
                last = "устройство не найдено при сканировании"
            else:
                try:
                    client = await self._connect(dev)
                    self.address = dev.address
                    self._say(f"Подключено: {dev.address} ({dev.name or '—'})")
                    return client
                except Exception as e:
                    last = str(e)
            self._say(f"Попытка {i}/{attempts}: {last}")
            await asyncio.sleep(1.5)
        raise RuntimeError(f"не удалось подключиться: {last}")

    @staticmethod
    def _has(client, uuid):
        try:
            return client.services.get_characteristic(uuid) is not None
        except Exception:
            return False

    @staticmethod
    async def _close(client):
        try:
            if client is not None and client.is_connected:
                await client.disconnect()
        except Exception:
            pass

    async def _write_ctrl(self, client, value, tolerate_drop=False):
        try:
            await client.write_gatt_char(OTA_CONTROL_UUID, bytes([value]),
                                         response=True)
        except Exception:
            if not tolerate_drop:
                raise
            self._say(f"(разрыв связи при записи ota_control={value} — это норма)")

    # ---- transfer ----
    async def _upload(self, client, data, reliable):
        mtu = getattr(client, "mtu_size", 0) or 23
        chunk = max(20, min(244, mtu - 3))
        chunk -= chunk % 4                      # 4-байтовое выравнивание
        self._say(f"MTU={mtu}, блок={chunk} Б, запись "
                  f"{'с подтверждением' if reliable else 'без подтверждения'}")

        await self._write_ctrl(client, OTA_CMD_START)
        self._say("DFU начат (ota_control = 0)")

        total, sent, n = len(data), 0, 0
        t0 = time.time()
        while sent < total:
            self._check_cancel()
            piece = data[sent:sent + chunk]
            await client.write_gatt_char(OTA_DATA_UUID, piece, response=reliable)
            sent += len(piece)
            n += 1
            self.bridge.progress.emit(sent, total)
            if not reliable and n % 16 == 0:
                await asyncio.sleep(0.01)       # даём очереди стека разгрузиться

        dt = max(time.time() - t0, 0.001)
        self._say(f"Передано {total} Б за {dt:.1f} с ({total / dt / 1024:.1f} КБ/с)")

        await self._write_ctrl(client, OTA_CMD_END, tolerate_drop=True)
        self._say("DFU завершён (ota_control = 3) — устройство перезагружается")
        self.bridge.finished.emit(True, "Прошивка загружена успешно")

class OtaDialog(QDialog):
    def __init__(self, parent, address=None):
        super().__init__(parent)
        self.setWindowTitle("Обновление ПО по BLE (OTA DFU)")
        self.setMinimumWidth(600)
        self.path = None
        self.running = False

        self.bridge = OtaBridge()
        self.up = OtaUploader(self.bridge, address=address)
        self.bridge.log.connect(self._log)
        self.bridge.progress.connect(self._progress)
        self.bridge.finished.connect(self._finished)

        v = QVBoxLayout(self)

        pick_row = QHBoxLayout()
        self.pick_btn = QPushButton("Выбрать .gbl")
        self.pick_btn.clicked.connect(self._pick)
        self.path_lab = QLabel("Файл не выбран")
        self.path_lab.setStyleSheet("color:#6B6A63;")
        pick_row.addWidget(self.pick_btn)
        pick_row.addWidget(self.path_lab, 1)
        v.addLayout(pick_row)

        self.reliable = QCheckBox("Надёжная запись (с подтверждением, медленнее)")
        v.addWidget(self.reliable)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        v.addWidget(self.bar)

        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setMinimumHeight(220)
        v.addWidget(self.text)

        hint = QLabel("Не выключайте устройство и не закрывайте окно до конца "
                      "прошивки. Адрес BLE в режиме AppLoader не меняется.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#6B6A63;")
        v.addWidget(hint)

        btns = QHBoxLayout()
        self.start_btn = QPushButton("Прошить")
        self.start_btn.setObjectName("run")
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._start)
        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.clicked.connect(self._cancel)
        btns.addStretch()
        btns.addWidget(self.cancel_btn)
        btns.addWidget(self.start_btn)
        v.addLayout(btns)

        if address:
            self._log(f"Целевой адрес: {address}")

    def _log(self, msg):
        self.text.append(msg)

    def _pick(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите файл прошивки", "", "GBL (*.gbl);;Все файлы (*)")
        if not path:
            return
        self.path = path
        self.path_lab.setText(Path(path).name)
        self.start_btn.setEnabled(True)

    def _start(self):
        if not self.path:
            return
        self.running = True
        self.start_btn.setEnabled(False)
        self.pick_btn.setEnabled(False)
        self.reliable.setEnabled(False)
        self.bar.setValue(0)
        self._log("Старт обновления...")
        self.up.start(self.path, reliable=self.reliable.isChecked())

    def _cancel(self):
        if self.running:
            self.up.cancel()
            self._log("Запрошена отмена...")
        else:
            self.reject()

    def _progress(self, sent, total):
        self.bar.setValue(int(sent * 100 / total))
        self.bar.setFormat(f"{sent // 1024} / {total // 1024} КБ  (%p%)")

    def _finished(self, ok, msg):
        self.running = False
        self._log(("ГОТОВО: " if ok else "ОШИБКА: ") + msg)
        self.pick_btn.setEnabled(True)
        self.reliable.setEnabled(True)
        self.start_btn.setEnabled(True)
        self.cancel_btn.setText("Закрыть")

    def closeEvent(self, e):
        if self.running:
            self.up.cancel()
        super().closeEvent(e)

class DevTool(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MCS Glove — Утилита для тестирования")
        self.dbg = DebugClient()

        self.bridge = Bridge()
        self.dbg.on_status = self.bridge.status.emit
        self.bridge.status.connect(self._log)
        self.bridge.connected.connect(self._on_connected)
        self.bridge.health.connect(self._set_health)
        self.bridge.battery.connect(self._update_battery)
        self.bridge.devinfo.connect(self._show_devinfo)
        self.bridge.cycle_done.connect(self._cycle_finished)
        self.cyc_params = {"on": 1000, "pause": 1000,
                           "cycles": 20, "infinite": False,
                           "limit": 10, "cool": 5}
        
        self.rows = {}
        self._dev_address = None
        self.batt_labels = {}
        self._cycle_stop = threading.Event()
        self._cycle_thread = None
        self._motor_busy = 0

        self._build_ui()
        self._load_settings()
        self._on_connected(False)

        self._batt_timer = QTimer(self)
        self._batt_timer.setInterval(5000)
        self._batt_timer.timeout.connect(self._poll_battery)
        self._batt_timer.start()

    def _build_ui(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setSpacing(6)

        # Top bar
        top = QHBoxLayout()
        self.connect_btn = QPushButton("Подключить")
        self.disconnect_btn = QPushButton("Отключить")
        self.stop_all_btn = QPushButton("СТОП")
        self.stop_all_btn.setObjectName("stopAll")
        self.ota_btn = QPushButton("Обновить ПО (OTA)")
        self.conn_label = QLabel("Не подключено")
        self.dev_label = QLabel("")
        self.dev_label.setStyleSheet("color:#6B6A63;")
        self.connect_btn.clicked.connect(self._do_connect)
        self.disconnect_btn.clicked.connect(self._do_disconnect)
        self.stop_all_btn.clicked.connect(self._stop_all)
        self.ota_btn.clicked.connect(self._open_ota)
        top.addWidget(self.connect_btn)
        top.addWidget(self.disconnect_btn)
        top.addWidget(self.conn_label)
        top.addWidget(self.dev_label, 1)
        top.addWidget(self.ota_btn)
        top.addWidget(self.stop_all_btn)
        outer.addLayout(top)

        # Main area
        split = QHBoxLayout()
        split.setSpacing(12)

        left = QVBoxLayout()

        # === Новая панель над таблицей моторв ===
        # Row 1 — general controls
        controls_row = QHBoxLayout()
        self.run_sel_btn = QPushButton("Запустить выбранные")
        self.run_sel_btn.setObjectName("run")
        self.run_sel_btn.clicked.connect(self._run_selected)

        load_btn = QPushButton("Загрузить последние")
        load_btn.clicked.connect(self._load_settings)
        reset_btn = QPushButton("Сбросить к базовым")
        reset_btn.clicked.connect(self._reset_to_defaults)
        diag_btn = QPushButton("Диагностика драйверов")
        diag_btn.clicked.connect(self._open_diagnostics)
        effects_btn = QPushButton("Список эффектов")
        effects_btn.clicked.connect(self._show_effects)

        for b in (self.run_sel_btn, load_btn, reset_btn, diag_btn):
            controls_row.addWidget(b)
        controls_row.addStretch()
        controls_row.addWidget(effects_btn)
        left.addLayout(controls_row)

        # Row 2 — cycling
        cycle_row = QHBoxLayout()
        self.cycle_start_btn = QPushButton("Старт цикла")
        self.cycle_start_btn.setObjectName("run")
        self.cycle_stop_btn  = QPushButton("Стоп цикла")
        cyc_set_btn = QPushButton("Настройки цикла")
        self.cycle_start_btn.clicked.connect(self._cycle_start)
        self.cycle_stop_btn.clicked.connect(self._cycle_stop_req)
        cyc_set_btn.clicked.connect(self._edit_cycle_settings)

        cyc_lab = QLabel("Цикл:")
        cyc_lab.setStyleSheet("color:#6B6A63;")
        cycle_row.addWidget(cyc_lab)
        for b in (self.cycle_start_btn, self.cycle_stop_btn, cyc_set_btn):
            cycle_row.addWidget(b)
        cycle_row.addStretch()
        left.addLayout(cycle_row)

        box = QGroupBox("Моторы")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)

        headers = ["", "Канал", "Режим", "Мощн.%", "Длит.", "Нарастание", "Убывание", "Эффект №", "Запуск", "Состояние"]
        for col, h in enumerate(headers):
            lab = QLabel(h)
            lab.setStyleSheet("font-weight:500; color:#6B6A63;")
            grid.addWidget(lab, 0, col)

        for r, ch in enumerate(range(NUM_CHANNELS), start=1):
            sel = QCheckBox()
            ch_lab = QLabel(f"CH{ch} · {designator(ch)}")
            ch_lab.setMinimumWidth(96)

            mode = QComboBox()
            mode.addItems([M_RTP, M_LIB, M_CAL, M_DIAG])
            mode.setMinimumWidth(90)
            mode.currentTextChanged.connect(lambda _t, c=ch: self._mode_changed(c))

            power = QSpinBox(); power.setRange(0, 100); power.setValue(60); power.setMinimumWidth(56)
            dur   = TimeEdit(500, max_ms=3600000)
            up    = TimeEdit(150, max_ms=60000)
            down  = TimeEdit(200, max_ms=60000)
            eff   = QSpinBox(); eff.setRange(1, 123); eff.setValue(1); eff.setMinimumWidth(56)
            run_btn = QPushButton("Запуск")
            run_btn.setObjectName("run")
            run_btn.clicked.connect(lambda _, c=ch: self._run(c))

            health = QLabel("—")
            health.setStyleSheet(f"color:{GREY};")

            widgets = [sel, ch_lab, mode, power, dur, up, down, eff, run_btn, health]
           
            for col, wdg in enumerate(widgets):
                grid.addWidget(wdg, r, col)

            self.rows[ch] = {
                "sel": sel, "mode": mode, "power": power, "dur": dur,
                "up": up, "down": down, "eff": eff, "run": run_btn, "health": health
            }

            self._mode_changed(ch)

        for col, s in enumerate([0, 2, 2, 1, 3, 3, 3, 1, 0, 2]):
            grid.setColumnStretch(col, s)
        left.addWidget(box)

        # === Общие параметры для всех каналов ===
        all_box = QGroupBox("Общие параметры")
        ag = QGridLayout(all_box)
        ag.setHorizontalSpacing(10)
        ag.setVerticalSpacing(8)

        self.all_mode  = QComboBox(); self.all_mode.addItems([M_RTP, M_LIB, M_CAL, M_DIAG])
        self.all_mode.setMinimumWidth(96)
        self.all_power = QSpinBox(); self.all_power.setRange(0, 100); self.all_power.setValue(60); self.all_power.setMinimumWidth(56)
        self.all_dur   = TimeEdit(500, max_ms=3600000)
        self.all_up    = TimeEdit(150, max_ms=60000)
        self.all_down  = TimeEdit(200, max_ms=60000)
        self.all_eff   = QSpinBox(); self.all_eff.setRange(1, 123); self.all_eff.setValue(1); self.all_eff.setMinimumWidth(56)

        # row 0: labels + fields
        fields = [("Режим", self.all_mode),
                  ("Мощн.%", self.all_power),
                  ("Длит.", self.all_dur),
                  ("Нарастание", self.all_up),
                  ("Убывание", self.all_down),
                  ("Эффект №", self.all_eff)]
        for col, (text, wdg) in enumerate(fields):
            lab = QLabel(text)
            lab.setStyleSheet("color:#6B6A63;")
            ag.addWidget(lab, 0, col)
            ag.addWidget(wdg, 1, col)

        # row 2: buttons
        apply_all_btn = QPushButton("Применить ко всем")
        apply_all_btn.setObjectName("run")
        apply_sel_btn = QPushButton("Применить к выбранным")
        apply_all_btn.clicked.connect(lambda: self._apply_common(False))
        apply_sel_btn.clicked.connect(lambda: self._apply_common(True))

        btn_row = QHBoxLayout()
        btn_row.addWidget(apply_all_btn)
        btn_row.addWidget(apply_sel_btn)
        btn_row.addStretch()
        ag.addLayout(btn_row, 2, 0, 1, len(fields))

        ag.setColumnStretch(len(fields), 1)
        left.addWidget(all_box)
        left.addStretch()
        split.addLayout(left, 1)

        # Battery panel 
        # Battery panel
        bbox = QGroupBox("Батарея (MAX17055)")
        bbox.setMaximumWidth(240)
        bgrid = QGridLayout(bbox)
        bgrid.setVerticalSpacing(8)
        for r, (label, reg, _dec, unit) in enumerate(METRICS):
            bgrid.addWidget(QLabel(label), r, 0)
            val = QLabel("—"); val.setStyleSheet("font-weight:500;")
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            bgrid.addWidget(val, r, 1)
            u = QLabel(unit); u.setStyleSheet("color:#8A8880;")
            bgrid.addWidget(u, r, 2)
            self.batt_labels[reg] = val

        bwrap = QVBoxLayout()
        bwrap.addWidget(bbox)
        bwrap.addStretch()
        split.addLayout(bwrap, 0)
        split.addStretch(1)

        outer.addLayout(split)

        self.log = QTextEdit(); 
        self.log.setReadOnly(True); 
        self.log.setMinimumHeight(80)
        outer.addWidget(self.log)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(root)
        self.setCentralWidget(scroll)

    def _mode_changed(self, ch):
        w = self.rows[ch]
        m = w["mode"].currentText()
        is_rtp = (m == M_RTP)
        is_lib = (m == M_LIB)

        for k in ("power", "dur", "up", "down"):
            w[k].setEnabled(is_rtp)
        w["eff"].setEnabled(is_lib)

    # ---- connection ----
    def _do_connect(self):
        self.connect_btn.setEnabled(False)
        self._log("Поиск устройства...")

        def worker():
            try:
                self.dbg.connect(name=DEVICE_NAME)
                self.bridge.connected.emit(True)
                self.bridge.devinfo.emit(self.dbg.read_device_info())  
            except Exception as e:
                self.bridge.status.emit(f"Ошибка подключения: {e}")
                self.bridge.connected.emit(False)

        threading.Thread(target=worker, daemon=True).start()

    def _do_disconnect(self):
        self.dbg.disconnect()
        self.bridge.connected.emit(False)

    def _on_connected(self, ok):
        self.connect_btn.setEnabled(not ok)
        self.disconnect_btn.setEnabled(ok)
        self.stop_all_btn.setEnabled(ok)
        self.run_sel_btn.setEnabled(ok)
        self.conn_label.setText("Подключено" if ok else "Не подключено")
        self.cycle_start_btn.setEnabled(ok)
        self.cycle_stop_btn.setEnabled(False)

        for w in self.rows.values():
            w["run"].setEnabled(ok)
        if not ok:
            for lab in self.batt_labels.values():
                lab.setText("—")

    def _show_devinfo(self, info):
        if not info:
            self.dev_label.setText("")
            return
        self._dev_address = info.get("address") or self._dev_address
        parts = []
        if info.get("serial"):  parts.append(f"с/н {info['serial']}")
        if info.get("model"):   parts.append(f"модель {info['model']}")
        if info.get("fw_rev"):  parts.append(f"ПО {info['fw_rev']}")
        if info.get("address"): parts.append(info["address"])
        self.dev_label.setText("   ·   ".join(parts))
        self._log("Устройство: " + " | ".join(f"{k}={v}" for k, v in info.items() if v))

    # ---- battery ----
    def _poll_battery(self):
        if not self.dbg.connected:
            return

        def worker():
            results = {}
            for _l, reg, _d, _u in METRICS:
                try:
                    r = self.dbg.read_gauge(reg)
                    results[reg] = (r["word"], r["ok"])
                except Exception:
                    results[reg] = (0, False)
            self.bridge.battery.emit(results)

        threading.Thread(target=worker, daemon=True).start()

    def _update_battery(self, results):
        for _l, reg, dec, _u in METRICS:
            word, ok = results.get(reg, (0, False))
            self.batt_labels[reg].setText(dec(word) if ok else "—")

    # ---- run (branches by mode) ----
    def _run(self, ch):
        w = self.rows[ch]
        m = w["mode"].currentText()
        w["run"].setEnabled(False)
        self._motor_busy += 1

        def worker():
            try:
                if m == M_RTP:
                    self._do_rtp(ch)
                elif m == M_LIB:
                    self._do_library(ch)
                elif m == M_CAL:
                    self._do_mode(ch, MODE_AUTOCAL, "калибровка")
                elif m == M_DIAG:
                    self._do_mode(ch, MODE_DIAG, "диагностика")
            except Exception as e:
                self.bridge.status.emit(f"CH{ch}: ошибка: {e}")
                try:
                    self.dbg.write_reg(ch, REG_RTP, 0)
                    self.dbg.write_reg(ch, REG_MODE, MODE_STANDBY)
                except Exception:
                    pass
            finally:
                self._motor_busy -= 1
                self.rows[ch]["run"].setEnabled(True)

        threading.Thread(target=worker, daemon=True).start()

    def _do_rtp(self, ch):
        w = self.rows[ch]
        power, dur, up, down = (w["power"].value(), w["dur"].value_ms(),
                                w["up"].value_ms(), w["down"].value_ms())
        self._log(f"CH{ch} ({designator(ch)}): RTP {power}% {dur} мс")
        target = power * 255 // 100
        self.dbg.write_reg(ch, REG_MODE, MODE_RTP)
        self.dbg.read_reg(ch, REG_STATUS)              # clear stale latch
        self._ramp(ch, 0, target, up)
        if dur > 0:
            self.dbg.write_reg(ch, REG_RTP, target)
            time.sleep(dur / 1000.0)
        self._ramp(ch, target, 0, down)
        self.dbg.write_reg(ch, REG_RTP, 0)
        self.dbg.write_reg(ch, REG_MODE, MODE_STANDBY)
        self._emit_run_health(ch, self.dbg.read_reg(ch, REG_STATUS))

    def _do_library(self, ch):
        w = self.rows[ch]
        effect = w["eff"].value()
        self._log(f"CH{ch} ({designator(ch)}): эффект №{effect} ({DEFAULT_LIBRARY_NAME})")

        self.dbg.write_reg(ch, REG_MODE, MODE_INTERNAL)
        self.dbg.write_reg(ch, REG_LIBRARY, DEFAULT_LIBRARY_VAL)
        self.dbg.read_reg(ch, REG_STATUS)
        self.dbg.write_reg(ch, REG_WAVE1, effect)
        self.dbg.write_reg(ch, REG_WAVE2, 0x00)
        self.dbg.write_reg(ch, REG_GO, 0x01)
        time.sleep(0.5)
        self.dbg.write_reg(ch, REG_MODE, MODE_STANDBY)
        self._emit_run_health(ch, self.dbg.read_reg(ch, REG_STATUS))

    def _show_effects(self):
        dialog = EffectListDialog(self)
        dialog.exec()

    def _open_diagnostics(self):
        if not self.dbg.connected:
            self._log("Сначала подключитесь к устройству")
            return
        dialog = DiagnosticsDialog(self, self.dbg)
        dialog.exec()

    def _open_ota(self):
        """Firmware update over BLE. The debug link must be released first —
        the device accepts one connection at a time."""
        if self._cycle_thread and self._cycle_thread.is_alive():
            self._cycle_stop.set()
            self._log("Цикл остановлен перед прошивкой")
        if self.dbg.connected:
            self._log("Отключение перед прошивкой...")
            try:
                self.dbg.disconnect()
            except Exception:
                pass
            self.bridge.connected.emit(False)
            time.sleep(1.0)

        dlg = OtaDialog(self, address=self._dev_address)
        dlg.exec()
        self._log("Окно OTA закрыто. Подключитесь заново, чтобы проверить "
                  "версию ПО в строке устройства.")

    def _do_mode(self, ch, mode, name):
        self._log(f"CH{ch} ({designator(ch)}): {name}...")
        r = self.dbg.run_mode(ch, mode, wait=2.0)
        if not r["ok"]:
            self.bridge.health.emit(ch, "нет связи с драйвером", RED, -1)
        elif ((r["status"] >> 3) & 1) == 0:
            self.bridge.health.emit(ch, f"{name} OK", GREEN, r["status"])
        else:
            self.bridge.health.emit(ch, f"{name}: ошибка", RED, r["status"])
        self.dbg.write_reg(ch, REG_MODE, MODE_STANDBY)

    def _ramp(self, ch, start, end, ms):
        if ms <= 0 or start == end:
            self.dbg.write_reg(ch, REG_RTP, end)
            return
        steps = max(1, ms // 20)
        for i in range(steps + 1):
            self.dbg.write_reg(ch, REG_RTP, start + (end - start) * i // steps)
            time.sleep(ms / 1000.0 / steps)

    def _run_selected(self):
        chans = [ch for ch, w in self.rows.items() if w["sel"].isChecked()]
        if not chans:
            self._log("Не выбрано ни одного мотора")
            return
        self._log("Запуск выбранных: " + ", ".join(f"CH{c}" for c in chans))
        for ch in chans:
            self._run(ch)

    # ---- cycling ----
    def _cycle_start(self):
        chans = [ch for ch, w in self.rows.items() if w["sel"].isChecked()]
        if not chans:
            self._log("Не выбрано ни одного мотора")
            return
        if self._cycle_thread and self._cycle_thread.is_alive():
            self._log("Цикл уже запущен")
            return

        p = self.cyc_params
        on_ms    = p["on"]
        pause_ms = p["pause"]
        limit_s  = p["limit"] * 60
        cool_s   = p["cool"] * 60
        max_cyc  = 0 if p["infinite"] else p["cycles"]

        self._cycle_stop.clear()
        self.cycle_start_btn.setEnabled(False)
        self.cycle_stop_btn.setEnabled(True)
        self._log(f"Цикл: {', '.join('CH'+str(c) for c in chans)} — "
                  + self._cycle_desc())

        self._cycle_thread = threading.Thread(
            target=self._cycle_worker,
            args=(chans, on_ms, pause_ms, limit_s, cool_s, max_cyc),
            daemon=True)
        self._cycle_thread.start()

    def _cycle_stop_req(self):
        self._cycle_stop.set()
        self._log("Цикл: остановка...")

    def _cycle_finished(self):
        self.cycle_start_btn.setEnabled(self.dbg.connected)
        self.cycle_stop_btn.setEnabled(False)

    def _cycle_worker(self, chans, on_ms, pause_ms, limit_s, cool_s, max_cyc):
        run_accum = 0.0
        n = 0
        self._motor_busy += 1
        try:
            while not self._cycle_stop.is_set():
                n += 1
                for ch in chans:
                    if self._cycle_stop.is_set():
                        break
                    t0 = time.time()
                    fault = self._cycle_pulse(ch, on_ms)
                    run_accum += time.time() - t0
                    if fault:
                        self.bridge.status.emit(
                            f"Цикл остановлен: CH{ch} ({designator(ch)}) — {fault}")
                        return

                if self._cycle_stop.is_set():
                    break

                if max_cyc and n >= max_cyc:
                    self.bridge.status.emit(f"Цикл: выполнено {n} из {max_cyc}")
                    break

                if run_accum >= limit_s:
                    self.bridge.status.emit(
                        f"Цикл {n}: лимит работы ({run_accum/60:.1f} мин) → "
                        f"охлаждение {cool_s//60} мин")
                    if self._cycle_stop.wait(cool_s):
                        break
                    run_accum = 0.0
                else:
                    if self._cycle_stop.wait(pause_ms / 1000.0):
                        break
        finally:
            self._motor_busy -= 1
            for ch in chans:
                try:
                    self.dbg.write_reg(ch, REG_RTP, 0)
                    self.dbg.write_reg(ch, REG_MODE, MODE_STANDBY)
                except Exception:
                    pass
            self.bridge.cycle_done.emit()
            self.bridge.status.emit("Цикл завершён")

    def _cycle_pulse(self, ch, on_ms):
        """One burst on one channel. Returns None if fine, else a fault string."""
        w = self.rows[ch]
        power = w["power"].value()
        up, down = w["up"].value_ms(), w["down"].value_ms()
        target = power * 255 // 100
        try:
            self.dbg.write_reg(ch, REG_MODE, MODE_RTP)
            self.dbg.read_reg(ch, REG_STATUS)          # clear stale latch
            self._ramp(ch, 0, target, up)
            if on_ms > 0:
                self.dbg.write_reg(ch, REG_RTP, target)
                self._cycle_stop.wait(on_ms / 1000.0)  # interruptible hold
            self._ramp(ch, target, 0, down)
            self.dbg.write_reg(ch, REG_RTP, 0)
            self.dbg.write_reg(ch, REG_MODE, MODE_STANDBY)

            st = self.dbg.read_reg(ch, REG_STATUS)
            self._emit_run_health(ch, st)
            if st["ok"] and ((st["value"] >> 1) & 1):
                return "перегрев драйвера"
        except Exception as e:
            return f"ошибка связи: {e}"
        return None
    
    def _edit_cycle_settings(self):
        dlg = CycleSettingsDialog(self.cyc_params, self)
        if dlg.exec():
            self._log("Параметры цикла: " + self._cycle_desc())

    def _cycle_desc(self):
        p = self.cyc_params
        n = "бесконечно" if p["infinite"] else f"{p['cycles']} цикл(ов)"
        return (f"{n} · работа {p['on']} мс · пауза {p['pause']} мс · "
                f"лимит {p['limit']} мин · охлаждение {p['cool']} мин")

    # ---- health ----
    def _emit_run_health(self, ch, st):
        if not st["ok"]:
            self.bridge.health.emit(ch, "нет связи с драйвером", RED, -1)
            return
        v = st["value"]
        if (v >> 1) & 1:
            self.bridge.health.emit(ch, "перегрев драйвера", RED, v)
        elif v & 1:
            self.bridge.health.emit(ch, "не подключено или неисправен", RED, v)
        else:
            self.bridge.health.emit(ch, "OK", GREEN, v)

    def _status_detail(self, value):
        devid = (value >> 5) & 0x07
        diag, otemp, oc = (value >> 3) & 1, (value >> 1) & 1, value & 1
        names = {3: "DRV2605", 4: "DRV2604", 6: "DRV2604L", 7: "DRV2605L"}
        return "\n".join([
            f"STATUS = 0x{value:02X}",
            f"  DEVICE_ID [7:5] = {devid} ({names.get(devid, '?')})",
            f"  DIAG_RESULT [3] = {diag} ({'пройдена' if diag == 0 else 'ошибка'})",
            f"  OVER_TEMP [1]   = {otemp} ({'норма' if otemp == 0 else 'перегрев'})",
            f"  OC_DETECT [0]   = {oc} ({'норма' if oc == 0 else 'не подключено или неисправен'})",
        ])

    def _set_health(self, ch, text, color, raw):
        lab = self.rows[ch]["health"]
        if raw < 0:
            lab.setText(text); lab.setToolTip("Драйвер не ответил по I2C")
        else:
            lab.setText(f"{text}  ·  0x{raw:02X}")
            lab.setToolTip(self._status_detail(raw))
        lab.setStyleSheet(f"color:{color}; font-weight:500;")

    def _stop_all(self):
        self._cycle_stop.set()
        def worker():
            for ch in range(NUM_CHANNELS):
                self.dbg.stop_priority(ch)
            self.bridge.status.emit("Все моторы остановлены")
        threading.Thread(target=worker, daemon=True).start()

    def _log(self, msg):
        self.log.append(msg)

    def closeEvent(self, e):
        self._save_settings()
        try:
            self._stop_all()
        except Exception:
            pass
        self.dbg.close()
        super().closeEvent(e)


    def _apply_common(self, selected_only):
        chans = [ch for ch, w in self.rows.items()
                 if (not selected_only) or w["sel"].isChecked()]
        if not chans:
            self._log("Не выбрано ни одного мотора")
            return

        mode = self.all_mode.currentText()
        for ch in chans:
            w = self.rows[ch]
            w["mode"].setCurrentText(mode)
            w["power"].setValue(self.all_power.value())
            w["dur"].set_ms(self.all_dur.value_ms())
            w["up"].set_ms(self.all_up.value_ms())
            w["down"].set_ms(self.all_down.value_ms())
            w["eff"].setValue(self.all_eff.value())
            self._mode_changed(ch)

        target = "выбранным" if selected_only else "всем"
        self._log(f"Параметры применены ко {target} каналам "
                  f"({len(chans)}): {mode}, {self.all_power.value()}%, "
                  f"{self.all_dur.value_ms()} мс")

    def _save_settings(self):
        """Save current UI parameters to file"""
        try:
            settings = {}
            for ch in range(NUM_CHANNELS):
                w = self.rows[ch]
                settings[ch] = {
                    "mode": w["mode"].currentText(),
                    "power": w["power"].value(),
                    "dur": w["dur"].value_ms(),
                    "up": w["up"].value_ms(),
                    "down": w["down"].value_ms(),
                    "eff": w["eff"].value(),
                }
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
        except Exception as e:
            self._log(f"Не удалось сохранить настройки: {e}")

    def _load_settings(self):
        """Load last used parameters"""
        if not os.path.exists(SETTINGS_FILE):
            self._log("Файл настроек не найден — используются значения по умолчанию")
            return

        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                settings = json.load(f)

            for ch_str, data in settings.items():
                ch = int(ch_str)
                if ch not in self.rows:
                    continue
                w = self.rows[ch]
                try:
                    idx = w["mode"].findText(data["mode"])
                    if idx >= 0:
                        w["mode"].setCurrentIndex(idx)
                    w["power"].setValue(data.get("power", 60))
                    w["dur"].set_ms(data.get("dur", 500))
                    w["up"].set_ms(data.get("up", 150))
                    w["down"].set_ms(data.get("down", 200))
                    w["eff"].setValue(data.get("eff", 1))
                    self._mode_changed(ch)
                except Exception:
                    pass
            self._log("Загружены последние параметры")
        except Exception as e:
            self._log(f"Ошибка загрузки настроек: {e}")

    def _reset_to_defaults(self):
        """Reset all channels to basic parameters"""
        for ch in range(NUM_CHANNELS):
            w = self.rows[ch]
            w["mode"].setCurrentText(M_RTP)
            w["power"].setValue(60)
            w["dur"].set_ms(500)
            w["up"].set_ms(150)
            w["down"].set_ms(200)
            w["eff"].setValue(1)
            self._mode_changed(ch)
        self._log("Сброшено к параметрам по умолчанию")

    


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    w = DevTool()
    w.adjustSize()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()