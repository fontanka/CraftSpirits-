# CraftSpirits Simulator v2

Симулятор куба + колонны + узла отбора (LM, БКУ-сифон) + конденсатора + **детальной модели железа** (DS18B20 sentinels, SSR thermal, valve heat-soak, contactor chatter) для dry-run проверки логики stейт-машины и failure modes перед сборкой реального устройства.

## Архитектура

```
sim/
├─ physics.py       — Boiler + Column (multi-plate) + Condenser + Still
├─ controller.py    — state machine, SensorFilter, SuspectSensorAnalyzer
├─ hardware.py      — LevelSensor, Valve (15-var), DS18B20 (counterfeit), SSR, PZEM, Contactor
├─ tests.py         — 51 integration scenarios (headless)
├─ hardware_tests.py — 11 hardware unit tests (изолированные)
├─ sweep.py         — batch harness (N runs с randomized faults → KPI)
├─ server.py        — FastAPI + WebSocket (interactive UI backend)
├─ tui.py           — terminal client (stdlib curses, no deps)
└─ static/
   └─ index.html    — browser UI (Chart.js CDN)
```

## Что моделирует (v2)

### Физика
- **Boiler**: multi-component pseudo-VLE (этанол + congeners + H2O), стенки vs bulk T,
  dry-out, puking, foam-over, scale, CO2 outgassing подмоложенной браги
- **Column** (LM/REFLUX): multi-plate enrichment, flooding (захлёб), weeping,
  channeling, baroкорrekciya, mash-type-specific congener profile
- **Condenser**: тепловой баланс, water flow tracking, cutoff detection
- **БКУ сифон**: сифонный transfer головы→тело по объёму, mash-specific cup volume
- **Mash types**: sugar (clean MeOH), grain (baseline), fruit (pectin/MeOH-heavy), mixed

### Hardware (12.11.4/5/6)
- **DS18B20** с sentinel values (-127, 85.0000 exact), CRC fail clustering вокруг SSR
  switching, counterfeit families (A1/A2/B1/B2/C/D1) — 5-10× σ vs original,
  zero-crossing hang для некоторых, drift accelerated 1-2°C/year
- **Solenoid valve** — 15-var state (см. PLAN 12.11.6): heat-soak Cv drift ×1.5-1.9,
  stiction после long-off, dribble (seat debris), kickback без snubber,
  coil thermal aging (IEC 60085 class A/E/B/F/H), chatter at low V
- **SSR** — thermal model (Crydom genuine vs Fotek counterfeit thermal differential),
  fail_short при T_j > 100°C
- **PZEM-004T** — 1Hz Modbus update, CRC errors на длинном кабеле, stuck-value
  failure mode, ground-truth power для cross-check
- **Contactor** — chatter при V_coil < 0.85 × V_nom, drop-out при <65%, arc wear

### Failure modes (controller-side)
- **SuspectSensorAnalyzer** (12.11.4): если один DS18B20 has >5× больше CRC/sentinel
  fails чем median остальных — flag как «далёкий/дохлый»
- Median3+EMA filter (per-signal τ)
- Барокоррекция T_head через Antoine
- Re-fermentation guard, hot-start detection, stale-DONE auto-close

## Установка и запуск

```bash
cd sim/
pip install -r requirements.txt
```

### Headless тесты (без UI)
```bash
python tests.py             # 51 scenario integration tests
python hardware_tests.py    # 11 hardware unit tests
python sweep.py --n 100     # 100 randomized runs со статистикой
```

### Interactive UI (browser)
```bash
python server.py            # http://localhost:8000
```

Возможности:
- Realtime (1×) и fast-forward (10×, 100×, 1000×, **MAX** — без sleep)
- Live charts: T_kub/T_head/T_water_out, P_heater, V_body/V_heads, SSR T_j + valve Cv
- Setup: mash type, V_kub, ABV, viscosity, sugar, oborotniy co-charge
- Hardware toggle: enable realistic models с выбором counterfeit family per sensor,
  SSR genuine/Fotek, V_mains 170-260V, valve coil class
- Fault injection кнопками: cooling cutoff, hot water, E-stop, bimetal, Pi
  disconnect, pressure drift, **SSR пробой**, **valve leak (dribble)**, **valve T_coil
  hot**, **DS18B20 T_kub disconnect**
- Pause/resume, ACK emergency, reset

### Interactive UI (terminal)
```bash
# Запусти server.py в одном терминале, в другом:
python tui.py [--host localhost --port 8000]
```

Управление: `s`=start, `p`=pause, `1`–`5`=speed (1×/10×/100×/1000×/max),
`a`=ack emergency, `r`=reset, `f`=fault menu, `q`=quit.

## Тестовое покрытие

| Категория | Сценариев |
|---|---|
| Базовые REFLUX/POTSTILL | 5 |
| Safety/EMERGENCY | 8 |
| Mash type + run type | 4 |
| Re-fermentation / oborotniy | 2 |
| Algorithmic edge cases (hot-start, stale DONE) | 3 |
| Level sensor (БКУ) | 3 |
| Column dimensions, flooding ratio | 2 |
| Disturbances (noise, pressure, hot water, foam, viscosity) | 5 |
| Long-running, concurrent faults | 2 |
| **Hardware failure modes** (stage 6/7) | 12 |
| **Hardware integration** (Cv → flow, leak, suspect sensor) | 5 |
| **Итого** | **51 — все PASS детерминированно** |

Hardware unit tests: 11/11 PASS (изолированный test для каждого компонента).

## Sweep harness (batch KPI)

```bash
python sweep.py --n 500 --json results.json
```

Запускает N сессий с randomized:
- начальными условиями (V, ABV, mash_type, viscosity, sugar, oborotniy)
- fault probabilities (pressure drift 30%, hot water 20%, under-fermented 15%)
- hardware variants (counterfeit families per sensor 30% each, Fotek SSR 25%,
  V_mains 195-245V)

Агрегирует KPI: % completed normal, % EMERGENCY, average V_body, suspect sensor
detection rate, SSR peak T_j distribution.

## Известные упрощения

| Аспект | Что упрощено | Что не покрыто |
|---|---|---|
| VLE | Pseudo multi-component с этанолом + congeners + H2O | Полный UNIQUAC/NRTL refactor отложен (12.11.2) |
| Column hydrodynamics | Empirical flooding correlations | Точная Sherwood/Eckert модель |
| Тепловые потери | Линейные К к ambient 22 °C | Зависимость от изоляции, ветра |
| Wi-Fi/aioesphomeapi | Один процесс, pi_alive=False flag | Реальные сетевые retry, jitter |

## Реальный hardware (для калибровки)

### Колонна ХД/4-375 ККС-М (russsam.ru)
- 58 mm внутренний диаметр, 375 mm рабочая секция
- 5 колпачковых медно-стальных тарелок, 1" резьба
- Заявленный max ABV 91-92% при R>5
- Типовой ТЭН куба: 3 kW
- Преimport кнопкой «ХД-4 500 preset» в UI

### БКУ автоперевод (russsam.ru, артикул «устройство для автоперевода
отбора в другую ёмкость»)
- **Не сифон**, а **коромысло-балансир**. Стакан-сборник ловит головы;
  при наполнении до точки опрокидывания падает в сторону, сливая в
  отдельную бутылку; основное приёмное горлышко 40-50mm (для бутылей
  5/10/20 л в v2.0) принимает тело.

### Полный комплект сенсоров БКУ-07М (samogon-i-vodka.ru)

| Сенсор | Тип | Где | Назначение |
|---|---|---|---|
| Главный DS18B20 | 1-Wire цифровой | Термокарман узла отбора ХД/4 (60mm ID × 120mm) | Основной T для ШИМ регулировки отбора |
| Аварийный DS1821 (93°C) | 1-Wire, programmable trip | Атмосферная трубка дефлегматора | Hard-cutoff при прорыве пара/потере охлаждения |
| Уровень голов | Контактный щуп 70×10mm | Стакан-сборник коромысла БКУ | Сигнал «головы взяты» → переход на тело |
| Защита перелива | Контактный щуп | Главный приёмник тела | Стоп отбора при заполнении бутыли |
| Клапан отбора | Соленоид 220V | Узел отбора (через резьбовый штуцер) | ШИМ управление отбором |
| Клапан воды | Соленоид 220V с регулятором потока | На впуске воды охлаждения | Аварийная отсечка / завершение сессии |

БКУ-07М алгоритм: «плавная электронная регулировка отбора **через ШИМ
управление клапаном** с дискретностью 1%», диапазон 0% (total reflux) →
100% (full takeoff). Метод старт/стоп используется в конце для отсечки хвостов.

### Чего нет в стандартном БКУ-07М (и что недорого добавить)

- **Атм. давление**: BME280 (~$5, I2C) — даёт baro-correction T_head и
  температуру окружающей среды. Без него БКУ работает по фиксированным
  T-порогам (что и объясняет +1% разницы между сезонами с разной погодой)
- **Разлитие на полу**: 2 контакта (любые винтовые клеммы) + 10 kΩ
  pull-up на GPIO (~$1 материалов). Замыкание = вода под аппаратом
- **PZEM-004T**: $10 — sanity check на heater duty vs SSR commanded.
  Детектит пробой SSR и провалы напряжения

### Датчик наполнения (russsam.ru, артикул «датчик наполнения автомат
отбора 07», 1060 ₽)
- Контактный кондуктометрический щуп 70×10 mm, 2 m кабель
- Жидкость замыкает 2 электрода щупа (резистивный, не геркон)
- Подключение к ESP: digital input с pull-up, GND через жидкость
- «Механическое замыкание оператором» — для теста / force-advance
- **Чистка наждачкой перед каждой сессией** обязательна (упоминается
  на форумах БКУ-099)

## Газоанализатор для отсечки голов — стоит ли?

Краткий ответ: **дёшево не получится сделать осмысленно**.

| Уровень | Что есть | Цена | Толк |
|---|---|---|---|
| Hobby (MQ-3, TGS2620, BME680) | SnO2 arduino-классика | $5–30 | **Бесполезны** для head/body cut: реагируют на любой алкоголь одинаково. Этанол насыщает на 80%+ парах. По сути это T_head в другом виде. |
| Mid-tier DIY (Pd-SnO2 + Tenax pre-column) | ETH Pratsinis | $200–500 | Реально различает MeOH vs EtOH 1-1000 ppm. Имеет смысл для фруктовых браг и audit log. |
| PID (фотоионизация) | non-selective VOC | $1500–3500 | Total congener load, не MeOH специально. |
| Hand-held GC-FID | лабораторный | $5 000–15 000 | Спирт-завод уровень. |
| NDIR IR-спектрометр | MeOH-specific 3.4 μm | $1 000–5 000 | Реально работает для MeOH, требует калибровки. |

**Реалистичный вывод**: для grain/sugar mash твой текущий setup (T_head +
ручник или smooth feedback автомат) — оптимум по цена/качество.
Газоанализ имеет смысл только для **фруктовых браг серьёзно** (там MeOH
0.5-2% в головах vs 0.1-0.3% у grain) и/или audit log для производства.

Альтернатива по цене: за теже $200-500 эффективнее
- калиброванный спиртомер + ареометр для in-process замера ABV каждые
  N минут (ручник, но точно)
- коммерческий рефрактометр для liquor measurement
- второй DS18B20 в малый product condenser — детектит «жирность» пара
  по теплопередаче

## Stage history (sim v2 evolution)

| Stage | Что добавлено |
|---|---|
| 1-3 | Boiler/Column/Condenser refactor, Recipe, basic scenarios |
| 4 | Hot-start, stale-DONE, algorithmic edge cases |
| 5 | Mash types, run1/run2 distinction, oborotniy co-charge |
| 6 | Hardware module: Valve 15-var, DS18B20 + counterfeit, SSR, Contactor |
| 7 | Hardware integration в Still + 12 failure scenarios |
| 8 | Cv drift → product flow, valve leak, SuspectSensorAnalyzer + 5 scenarios |
| 9 | PZEM model, sweep harness, FastAPI server, browser UI, TUI |
| 10 | Bubble-cap column, 2× condenser series, configurable heater_kW |
| 11 | Smooth takeoff feedback (P-controller, эмулирует «руками по T_head») |
| 12 | ХД/4-375 docs: 58mm ID, 375mm, 5 plates; БКУ коромысло (не сифон); датчик контактный щуп |
| 13 | T_atm_tube observable + emergency 93°C cutoff (БКУ-07М alarm); полная sensor topology |
