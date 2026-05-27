# Bill of Materials + Wiring — ХД/4-375 ректификатор с custom ESP32 контроллером

Состояние user'а:
- Колонна ХД/4-375 ККС-М (58 mm ID, 5 колпачковых тарелок, 375 mm) — ✓ есть
- Дистиллятор ХД/4-2500ПК (дефлегматор + малый condenser) — ✓ есть
- Узел отбора ХД/4 с термокарманом — ✓ есть
- Устройство автоперевода (гидрозатвор) — ✓ есть
- Соленоидный клапан спирта 220V (БКУ) — ✓ есть
- Переходник-регулятор воды с электроклапаном 220V — ✓ есть
- ТЭН 3 kW — ✓ есть
- Датчики DS18B20 + DS1821 93°C + контактный щуп уровня — ✓ есть
- **БКУ-07М контроллер — сломан**, строим с нуля

Целевой climate: Israeli coastal (Tel Aviv / Haifa / Netanya). Учитывает жёсткую воду, влажность 70%+, ambient 26-33°C.

---

## Часть 1 — Brain (вычислительная)

| Компонент | Спецификация | Источник | Цена |
|---|---|---|---|
| **Raspberry Pi 4B / 5 (4-8 GB)** | Высокоуровневая логика, WebUI, recipes, MQTT, audit log | Mouser IL / Avnet IL | ₪400-700 |
| Карта microSD 64 GB A2 SanDisk Extreme | Для Pi OS + logs (НЕ дешёвые: коррапт за месяцы) | Local computer store | ₪80 |
| Корпус Pi + heatsink + fan | Active cooling обязательно для непрерывной работы | AliExpress / Adafruit | ₪80 |
| **ESP32 DevKit V4** (или WROOM-32) | Real-time control: GPIO, ADC, PWM, 1-Wire bus, I2C | AliExpress (10-14 days) | ₪40 |
| USB-C cable Type B (Pi → ESP) | Связь HUB↔real-time. Длина ≤1m, ferrite bead | Any | ₪15 |
| Wi-Fi router (existing) | Ezel'naya integration с HA / Telegram | Existing | — |

**Итого brain**: ~₪700-900

---

## Часть 2 — Sensors

| Компонент | Назначение | Источник | Цена |
|---|---|---|---|
| **DS18B20 водонепроницаемый в гильзе 1 m** | T_kub (в браге) | AliExpress | ₪25 |
| **DS18B20 в термокармане** (твой, в узле отбора) | T_head | Имеющийся | — |
| **DS1821 в гильзе** (твой, на атм. трубке) | Аварийный 93°C | Имеющийся | — |
| **BME680** + помпа 5V 75 mL/min + 3D-печатный корпус | VOC на атм. трубке + atm pressure + ambient T/RH | Mouser IL | ₪220 |
| Контактный щуп (твой, в БКУ автопереводе) | Триггер тела | Имеющийся | — |
| **Контактный щуп для overflow protection** | Защита от перелива тела (запасной) | Local hardware store + 2× M3 винтовые клеммы | ₪10 |
| **Leak detector** (2 контакта на полу + 10 kΩ pull-up) | $1 материалов | Любые провода + клеммы | ₪10 |
| **PZEM-004T v3** (опционально, для cmd-vs-meas) | Cross-check heater duty | AliExpress | ₪80 |

**Кабели для сенсоров**:
| Кабель | Размер | Цена |
|---|---|---|
| Shielded 4-core 0.5 mm² (для DS18B20 1-Wire bus) | 5 m | ₪60 |
| 2-core flex для контактных щупов | 5 m | ₪20 |
| 4-core для BME680 I2C + power | 2 m | ₪25 |

**Итого sensors**: ~₪450

---

## Часть 3 — Actuators

| Компонент | Назначение | Источник | Цена |
|---|---|---|---|
| **Crydom CWA2425** (25A AC) SSR | Heater 3 kW (13 A) | Mouser IL / Digikey IL | ₪180 |
| **Heat sink** 100×80×40 мм + thermal paste | Для SSR (без него прожарится за час) | AliExpress | ₪40 |
| Контактор LS MC-9b (или Schneider LC1D09) | Между SSR и ТЭНом (физическая защита) | Local hardware store | ₪70 |
| **SG90 servo + MG996R** (запасной) | Привод на игольчатый клапан воды | AliExpress | ₪40 |
| **Игольчатый клапан DN8 BSP** (с резьбой 1/2") | Регулировка воды в дефлегматор | Local plumbing OR Российский Самодел shipping | ₪150 |
| Bracket 3D-печать (servo + needle) | Linkage | DIY | ₪10 |
| Соленоидный клапан спирта (твой) | — | Имеющийся | — |
| Соленоидный клапан воды (твой) | — | Имеющийся | — |
| **RC snubber** для каждого AC соленоида (0.1 µF X2 + 100 Ω/2W) | Suppression kickback при отключении | RS Components IL | ₪25 × 3 = ₪75 |
| **Varistor 275 V** на mains input | Surge protection | Mouser IL | ₪10 |

**Итого actuators**: ~₪575

---

## Часть 4 — Power + Safety

| Компонент | Назначение | Цена |
|---|---|---|
| **Двухполюсный RCD 30 mA** | Защита по утечке — **ОБЯЗАТЕЛЬНО для воды + 3 kW** | ₪150 |
| **Автомат C16 двухполюсный** | Защита по току (13 A continuous = OK) | ₪50 |
| **Контактор главный** (для killer-switch от ESP) | Аппаратный E-stop | ₪80 |
| **Кнопка E-stop NC NO** (грибовидная) | Operator emergency | ₪80 |
| Корпус IP54 пластиковый DIN-rail (300×200×120) | Для всей электроники, защита от влажности | ₪120 |
| Кабельный ввод PG-13.5 ×4 | Для проводки внутрь корпуса | ₪40 |
| **БП 5V 5A (Mean Well RS-25-5)** | Для Pi + ESP + sensors + servo | ₪80 |
| Клеммники DIN-rail (10 шт по 4 mm²) | Распайка mains | ₪60 |
| Земляная шина | Grounding всех metal parts | ₪40 |
| **Conformal coating** Plasti Dip или MG422B | PCB защита от Israeli humidity 70%+ | ₪80 |

**Итого power+safety**: ~₪780

---

## Часть 5 — Connectivity и интерфейс

| Компонент | Назначение | Цена |
|---|---|---|
| 7" TFT touch display (для Pi) | Локальный UI (опционально) | ₪250 |
| HDMI cable короткий | Display | ₪30 |
| Buzzer пьезо | Аудио alert | ₪10 |
| LED индикаторы (3шт 5mm) | Power / Running / Alert | ₪10 |
| Кнопки тактильные 2 шт | Reset / ACK | ₪10 |

**Итого UI**: ~₪310 (или ₪40 если без display, только WebUI с телефона)

---

## ИТОГО BOM

| Категория | Минимум | Полный setup |
|---|---|---|
| Brain | ₪700 | ₪900 |
| Sensors | ₪350 | ₪450 |
| Actuators | ₪450 | ₪575 |
| Power+Safety | ₪780 | ₪780 |
| Connectivity | ₪40 | ₪310 |
| **TOTAL** | **₪2 320** | **₪3 015** |

В долларах ~$650-850. По сравнению с покупкой готовой БКУ-097 ($400) + сенсоры — близко по цене, но **существенно больше функциональности** (web UI, audit log, calibration, integration с Home Assistant, custom safety logic).

---

## Электрическая схема (текстом)

```
MAINS 230V (Israeli L1+N+PE, через SI 32)
   │
   ├──── RCD 30 mA ──┐
   │                  │
   │                  ├── Автомат C16 (heater branch)
   │                  │       │
   │                  │       └─→ Контактор (KM1, ESP-controlled)
   │                  │              │
   │                  │              └─→ Crydom SSR + Heat sink (на DIN rail)
   │                  │                      │
   │                  │                      └─→ ТЭН 3 kW (в кубе, через гермоввод)
   │                  │
   │                  ├── Автомат C2 (control branch)
   │                  │       │
   │                  │       ├─→ Соленоидный клапан спирта (220V)
   │                  │       │       └─ RC snubber 0.1µF + 100Ω через клапан
   │                  │       │
   │                  │       └─→ Соленоидный клапан воды (220V)
   │                  │               └─ RC snubber 0.1µF + 100Ω через клапан
   │                  │
   │                  └── БП 5V 5A (Mean Well RS-25-5)
   │                          │
   │                          ├─→ Raspberry Pi 4 (USB-C)
   │                          ├─→ ESP32 DevKit (через 5V)
   │                          ├─→ BME680 + air pump
   │                          ├─→ SG90 servo
   │                          └─→ Buzzer + LEDs
   │
   └── PE земля → корпус IP54 + все metal parts (column, куб, дефлегматор) ОБЯЗАТЕЛЬНО
                                                  ↑↑↑ критично с водой + 3 kW

ESP32 GPIO mapping (предложение):
  GPIO 4   ← 1-Wire bus (DS18B20 + DS1821, общая шина)
  GPIO 5   → SSR контактор enable
  GPIO 17  → SSR спирт клапан
  GPIO 18  → SSR вода клапан
  GPIO 19  → Servo PWM (50 Hz, 1-2 ms)
  GPIO 21,22 ← I2C SDA/SCL (BME680 + PZEM-004T)
  GPIO 25  ← Контактный щуп (уровень голов автопереводе)
  GPIO 26  ← Контактный щуп (overflow protection)
  GPIO 27  ← Leak detector на полу
  GPIO 34  ← Кнопка ACK
  GPIO 35  ← Кнопка E-stop (interrupt, NC normally closed!)
  GPIO 32  → Buzzer
  GPIO 33  → LED Running
  GPIO 23  → LED Alert

Pull-up резисторы:
  DS18B20 1-Wire: **4.7 kΩ** между data и 3.3V (НЕ 10 кΩ — на 5m не работает)
  Контактные щупы: **10 kΩ** внутренний ESP32 или внешний на 3.3V
  I2C SDA/SCL: внешние 4.7 kΩ на 3.3V

Shielding:
  - DS18B20 кабель — экранированный, экран на ground ТОЛЬКО ESP-side
  - Mains к SSR — НЕ параллельно signal cables (расстояние ≥30 см)
  - PE земля корпуса соединена с PE mains, НЕ с signal ground
```

---

## Чек-лист safety перед первым включением

1. ☐ RCD 30 mA проверен (test кнопка нажимается = срабатывает)
2. ☐ Заземление куба, колонны, дефлегматора — мультиметром < 1 Ω до PE
3. ☐ SSR на heat sink с термопастой
4. ☐ ТЭН не висит в воздухе — погружён в брагу с минимальным уровнем 50mm над верхом TENа
5. ☐ E-stop кнопка отключает контактор физически (не через ESP)
6. ☐ Биметаллический термостат 110°C на стенке куба → последовательно с контактором (hard safety)
7. ☐ RC snubber'ы поставлены на все соленоиды
8. ☐ Кабели DS18B20 проложены ВДАЛИ от mains лининий
9. ☐ Air pump для BME680 запитан **после** DS1821 (thermal protection chain)
10. ☐ Conformal coating нанесён на все PCB после тестирования
11. ☐ Аварийный bypass воды (open valve) на случай servo failure
12. ☐ Огнетушитель класса B (для алкогольных пожаров) — рядом

---

## Известные подводные камни (из форумов)

1. **DS18B20 long cable** — pull-up 4.7 kΩ (НЕ 10 kΩ), 3-wire (НЕ parasitic), shielded cable, отдельная 1-Wire bus при > 3 sensor
2. **ESPHome 2024.6+ bug** с Dallas 1-Wire — использовать legacy `dallas:` component или Arduino IDE
3. **Counterfeit Fotek SSR** — только Crydom через Mouser/Digikey, проверять holographic stickers
4. **Servo dead zone** на низких углах — калибровать flow curve индивидуально
5. **SI 32 plug compatibility** — все Israeli, не используй RU Schuko напрямую
6. **Israeli summer 35°C ambient** — корпус с пассивной вентиляцией или 5V fan inside
7. **Hard water scale** — CIP каждые 10 сессий, иначе heat transfer падает на 30%/год

---

## Roadmap сборки (порядок)

1. **Mech setup**: установить колонну/дистиллятор/узел отбора на каркас (1 день)
2. **Plumbing**: вода tap → tee → main condenser + small condenser, drainage (1 день)
3. **Electrical**: смонтировать DIN-rail, RCD, автоматы, SSR, контактор в корпусе IP54 (2 дня)
4. **Sensors**: установить DS18B20 в термокарманы, BME680 на атм. трубке, контактные щупы (1 день)
5. **Firmware**: ESP32 + ESPHome или micropython, Pi 4 с server.py из sim/ (2 дня)
6. **Bench test**: имитация без браги — холодная вода через ТЭН, проверка safety (1 день)
7. **First wet run**: 5 L умеренной браги, attended throughout, мерять всё (1 день)
8. **Calibration run**: использовать column_autocalibration → сохранить K, τ, θ (1 день)
9. **Production sessions**: с full audit log (continuous)

**Итого**: ~10 дней работы + ₪2500-3000 железа.
