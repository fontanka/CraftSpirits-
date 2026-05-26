"""
sim/hardware.py — модели «железа» с failure modes.

Сейчас (v2 stage 3) — только датчик уровня в приёмнике голов.
В следующих коммитах добавятся:
- Valve state vectors (15 переменных per клапан) + failure modes из 12.11.6
- DS18B20 sensor с sentinel values, EMI clustering, counterfeit families
- BME280 с internal self-heat
- SSR thermal model
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class LevelSensorState:
    """Контактный датчик уровня в приёмнике голов (поплавковый/герконовый)."""
    # Текущее состояние сигнала который читает GPIO
    raw_signal: bool = False  # True = contact closed = "level full"
    debounced_signal: bool = False  # после фильтрации
    last_change_t: float = 0.0  # время последней смены

    # Параметры износа/неисправности
    oxidation_level: float = 0.0  # 0..1, растёт со временем работы в парах
    mechanical_stuck_closed: bool = False  # поплавок застрял в верхнем положении
    mechanical_stuck_open: bool = False  # поплавок не всплывает совсем
    # Внутренний счётчик дребезга
    bounce_counter: int = 0


class LevelSensor:
    """Контактный датчик уровня с реалистичными failure modes:

    1. Окисление контактов (BKU-099 thread): растёт со временем экспозиции,
       проявляется как контактное сопротивление, при определённом пороге
       контакт может «не замыкаться» (false negative) или дребезжать.
    2. Mechanical sticking — поплавок зацепился, не двигается.
    3. Bouncing / chatter — нормальный дребезг + усиленный окислением.
    4. False positive — splash при сильном отборе может «дёрнуть» контакт.

    Debouncing — программный фильтр в контроллере. Здесь физика без фильтра.
    """

    # Порог окисления, после которого контакт начинает «не замыкать»
    OXIDATION_FAILURE_THRESHOLD = 0.8
    # Бэйз-вероятность дребезга при контакте (per second)
    BOUNCE_BASE_RATE = 0.01

    def __init__(self, debounce_window_ms: float = 500.0):
        self.s = LevelSensorState()
        self.debounce_window_ms = debounce_window_ms

    def step(self, dt: float, t_sim: float, physical_full: bool,
             vapor_exposure_factor: float = 1.0):
        """Один шаг модели датчика.

        physical_full: реально ли ёмкость полная (определяется physics layer)
        vapor_exposure_factor: 1.0 = нормально; >1.0 ускоряет окисление
            (например, при большем потоке через активную ёмкость)
        """
        s = self.s

        # Окисление растёт во время работы (накопительно)
        s.oxidation_level = min(1.0, s.oxidation_level + dt / 86400 * vapor_exposure_factor)

        # Определение raw_signal с учётом неисправностей
        if s.mechanical_stuck_open:
            new_raw = False  # никогда не сработает
        elif s.mechanical_stuck_closed:
            new_raw = True  # всегда «полно»
        elif s.oxidation_level > self.OXIDATION_FAILURE_THRESHOLD:
            # Сильное окисление: dropouts даже при physical_full=True
            # И редкие false-positive из-за wet contact bouncing
            if physical_full:
                new_raw = random.random() > (s.oxidation_level - 0.5)
            else:
                # Очень редко false-positive при высоком окислении
                new_raw = random.random() < 0.01 * s.oxidation_level
        else:
            # Нормальная работа — следуем физике, но с возможным дребезгом
            new_raw = physical_full
            if physical_full and random.random() < self.BOUNCE_BASE_RATE * dt:
                # bouncing на момент срабатывания
                new_raw = not new_raw  # короткий drop-out
                s.bounce_counter += 1

        # Debounce: новый стабильный сигнал требует window_ms подтверждения
        if new_raw != s.raw_signal:
            s.raw_signal = new_raw
            s.last_change_t = t_sim

        if t_sim - s.last_change_t >= self.debounce_window_ms / 1000.0:
            s.debounced_signal = s.raw_signal

    def reset(self):
        """Чистка наждачкой — оператор сбрасывает окисление перед сессией."""
        self.s.oxidation_level = 0.0
        self.s.bounce_counter = 0
