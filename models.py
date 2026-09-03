"""
Data models for MCS Glove user app.
Finger-to-channel mapping, motor modes, scenario definitions.
"""
from dataclasses import dataclass, field
from enum import Enum, IntEnum


class Finger(IntEnum):
    THUMB  = 0
    INDEX  = 1
    MIDDLE = 2
    RING   = 3
    PINKY  = 4


class FingerState(Enum):
    IDLE     = "idle"
    SELECTED = "selected"
    ACTIVE   = "active"       # currently vibrating
    ERROR    = "error"        # diagnostics failed


class MotorMode(Enum):
    VIBRATION = "vibration"   # continuous RTP
    TICK      = "tick"        # single ROM library effect


# CH0→Thumb … CH4→Pinky.  Adjust here if the cable harness differs.
FINGER_CHANNEL: dict[Finger, int] = {
    Finger.THUMB:  0,
    Finger.INDEX:  1,
    Finger.MIDDLE: 2,
    Finger.RING:   3,
    Finger.PINKY:  4,
}

FINGER_LABEL: dict[Finger, str] = {
    Finger.THUMB:  "Thumb",
    Finger.INDEX:  "Index",
    Finger.MIDDLE: "Middle",
    Finger.RING:   "Ring",
    Finger.PINKY:  "Pinky",
}

ALL_FINGERS = list(Finger)


@dataclass
class ChannelInfo:
    finger: Finger
    channel: int
    state: FingerState = FingerState.IDLE
    diag_ok: bool | None = None        # None = not tested


# ── Scenarios ─────────────────────────────────────────────

@dataclass
class ScenarioStep:
    fingers: list[Finger]
    mode: MotorMode
    intensity: int = 100               # 0-100 %
    duration_ms: int = 500
    effect_id: int = 1                 # ROM effect for TICK mode


@dataclass
class Scenario:
    name: str
    description: str = ""
    steps: list[ScenarioStep] = field(default_factory=list)
    loop: bool = False


PRESETS: list[Scenario] = [
    Scenario(
        name="Sequential",
        description="Each finger one after another",
        steps=[
            ScenarioStep([f], MotorMode.VIBRATION, intensity=60, duration_ms=600)
            for f in ALL_FINGERS
        ],
    ),
    Scenario(
        name="Wave",
        description="Forward and back across all fingers",
        steps=[
            ScenarioStep([f], MotorMode.VIBRATION, intensity=50, duration_ms=400)
            for f in list(ALL_FINGERS) + list(reversed(ALL_FINGERS[1:-1]))
        ],
        loop=True,
    ),
    Scenario(
        name="All at once",
        description="All five motors simultaneously",
        steps=[
            ScenarioStep(ALL_FINGERS, MotorMode.VIBRATION, intensity=60, duration_ms=1000),
        ],
    ),
    Scenario(
        name="Alternating",
        description="Odd fingers, then even fingers",
        steps=[
            ScenarioStep(
                [Finger.THUMB, Finger.MIDDLE, Finger.PINKY],
                MotorMode.TICK, duration_ms=500, effect_id=1,
            ),
            ScenarioStep(
                [Finger.INDEX, Finger.RING],
                MotorMode.TICK, duration_ms=500, effect_id=1,
            ),
        ],
        loop=True,
    ),
    Scenario(
        name="Gentle pulse",
        description="Soft pulsing across all fingers",
        steps=[
            ScenarioStep(ALL_FINGERS, MotorMode.VIBRATION, intensity=30, duration_ms=400),
            ScenarioStep([], MotorMode.VIBRATION, intensity=0, duration_ms=300),
        ],
        loop=True,
    ),
]