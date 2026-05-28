# HANDOFF — продолжение проекта на твоём PC

Этот документ содержит всё что нужно чтобы другой Claude Code instance (или
ты сам) на PC мог подхватить работу с текущего состояния.

## TL;DR

```bash
# 1. Clone репо и checkout ветку
git clone <your-repo-url> CraftSpirits-
cd CraftSpirits-
git checkout claude/distillation-automation-rpi-N5tWd

# 2. Установить deps
cd sim
pip install -r requirements.txt   # fastapi + uvicorn + pydantic

# 3. Прогнать тесты
python tests.py            # должно: ALL 58 scenarios passed ✓
python hardware_tests.py   # должно: 11/11 hardware tests passed ✓

# 4. Запустить interactive UI
python server.py           # http://localhost:8000

# 5. Прочитать context
cat ../PLAN.md              # 1100+ строк spec — это «брифинг»
cat README.md               # архитектура sim
cat BOM.md                  # hardware bill of materials (Tier 1 MVP + Tier 2)
cat ECONOMICS.md            # анализ Israeli craft distillery
cat HANDOFF.md              # этот файл
```

## Контекст пользователя

- **Где живёт**: Israel (coastal, Tel Aviv area)
- **Оборудование** уже привезено:
  - Колонна ХД/4-375 ККС-М (58mm ID, 5 колпачковых медных тарелок, 375mm)
  - Дистиллятор ХД/4-2500ПК (дефлегматор + малый condenser встроенно)
  - Узел отбора ХД/4 (60mm × 120mm, термокарман для DS18B20)
  - Устройство автоперевода (гидрозатвор без подвижных частей — щуп определяет
    объём, см. подробности в physics.py docstrings и README.md)
  - ТЭН 3 kW
  - Соленоидный клапан спирта 220V (от БКУ)
  - Переходник-регулятор воды с электроклапаном 220V
- **Сенсоры**: DS18B20 (T_head) + DS1821 93°C аварийный + контактный щуп уровня
- **БКУ-07М сломан** — контроллер строим с нуля на ESP32 + Raspberry Pi
- **Ручник** даёт 93% ABV, автоматика БКУ-07М давала 92% (потолок колонны)
- **Climate Israel coastal**: RH 70%+, hard water ×3 scale, hot ambient 26-33°C

### Юридический контекст

Home distillation в Израиле **формально незаконна** (фруктовый бренди и спирт
одинаково regulated, нет exemption как в Германии/Италии). Penalty: до 2 лет
или ₪10 000 штраф. Enforcement для personal use minimal. **НЕ** обсуждать
продажу, рекламу или sharing продукта с не-друзьями. Bizniz variant
(commercial distillery): ₪200 000-400 000 startup, см. ECONOMICS.md.

## Структура проекта

```
CraftSpirits-/
├── PLAN.md                    1100+ строк spec проекта (изначальный брифинг)
├── sim/
│   ├── physics.py             880+ строк: Boiler/Column/Condenser/Still
│   ├── controller.py          900+ строк: state machine + filters + autotune
│   ├── hardware.py            900+ строк: DS18B20/Valve/SSR/BME680/Servo/PZEM
│   ├── tests.py               1300+ строк: 58 integration scenarios
│   ├── hardware_tests.py      210 строк: 11 hardware unit tests
│   ├── sweep.py               250 строк: batch random faults harness
│   ├── server.py              FastAPI + WebSocket для interactive UI
│   ├── tui.py                 stdlib curses TUI (альтернативный фронт)
│   ├── static/
│   │   └── index.html         Browser UI (Chart.js)
│   ├── demo_pwm_vs_smooth.py  Сравнение режимов отбора
│   ├── demo_three_modes.py    Manual vs Auto vs Hybrid
│   ├── README.md              Архитектура sim
│   ├── BOM.md                 Bill of materials Tier 1 MVP + Tier 2 expansion
│   ├── ECONOMICS.md           Israeli craft distillery analysis
│   └── requirements.txt       fastapi + uvicorn + pydantic
└── HANDOFF.md                 Этот файл
```

## История разработки (stages)

| Stage | Что сделано |
|---|---|
| 1-3 | Boiler/Column/Condenser refactor, Recipe, basic scenarios |
| 4 | Hot-start, stale-DONE, algorithmic edge cases |
| 5 | Mash types (sugar/grain/fruit), run1/run2 distinction, oborotniy co-charge |
| 6 | Hardware module: Valve 15-var state, DS18B20 + 6 counterfeit families, SSR thermal, Contactor |
| 7 | Hardware integration в Still + 12 failure scenarios |
| 8 | Cv drift → product flow, valve leak, SuspectSensorAnalyzer + 5 scenarios |
| 9 | PZEM model, sweep harness, FastAPI server, browser UI, TUI |
| 10 | Bubble-cap column, 2× condenser series, configurable heater_kW |
| 11 | Smooth takeoff feedback (P-controller, эмулирует «руками по T_head») |
| 12 | ХД/4-375 docs: 58mm ID, 375mm, 5 plates; БКУ коромысло (НЕ сифон); датчик щуп |
| 13 | T_atm_tube observable + emergency 93°C cutoff (БКУ-07М alarm) |
| 14 | Правильная модель автоперевода: гидрозатвор без подвижных частей |
| 15 | BME680 + помпа на атм. трубке + 2× condenser независимые circuits |
| 16 | Servo+needle valve, continuous takeoff mode, manual vs auto cooling topology |
| 17 | Column autocalibration step-test FOPDT identification + IMC tuning |
| 18 | Israeli climate profile + DS18B20 cable EMI + scale buildup |
| 19 | Bubble cap recalibration (v_flood=0.85, eff=0.9 optimum, +1 dephlegmator stage) |
| code-review | 10 verified bug fixes через /simplify --fix |

## Текущее состояние

**58/58 sim scenarios + 11/11 hardware tests PASS детерминированно** (seed=42).

### Что готово работать

- Sim физики (boiler + column bubble cap + condenser 2 contour + safety chain)
- Hardware модели с failure modes (counterfeit sensors, SSR thermal, valve heat-soak)
- Controller с state machine, smooth/PWM/continuous takeoff, water control modes
- WebUI с realtime + fast-forward (1× / 10× / 100× / 1000× / MAX), 4 charts
- TUI alternative (curses)
- Sweep harness для batch random fault analysis
- Column autocalibration через step-test
- Climate profiles (EU / Israel coastal / desert)

### Что отложено для будущей работы

- **Multi-component VLE с Wilson activity в Boiler.step** — функция доступна
  (`vapor_eq_mol_frac_nonideal()`), но не интегрирована в boiler step. Stage 20+.
- **Real hardware calibration**: sim даёт качественную картину, но не точные
  числа для ХД/4 + 3 kW. Нужны live-measured данные (T_head over time,
  V_body/h, real ABV через спиртомер каждые 30 мин) для tune bubble cap eff
  curve. Stage 21.
- **Multi-process sweep** (multiprocessing.Pool для ускорения 500 runs в 5×).
- **Telegram/MQTT integration** для notifications.
- **ESPHome firmware** на ESP32 (currently sim controller — Python class, нужен
  перенос логики в ESPHome YAML или C++ Arduino). Stage 22+.

### Active TODOs / questions

- [ ] Калибровать bubble cap eff и v_flood против реальных данных user'а (нужны замеры)
- [ ] WS snapshot должен включать scale_mm + heat_transfer_efficiency для UI
- [ ] StartReq.climate валидация через pydantic Literal вместо str
- [ ] Скрипт миграции sim controller → ESPHome YAML (или Arduino C++)
- [ ] Тест BME680 в реальном setup (5V supply, помпа, термозащита)

## Hardware у user (нужно купить для сборки)

См. **sim/BOM.md** для полного списка. Краткое:

**Tier 1 MVP** (~₪1 950 / ~$530):
- Raspberry Pi 4B + ESP32 DevKit
- DS18B20 (T_kub) + leak detector
- Crydom CWA2425 SSR + heat sink + контактор
- БП 5V 5A Mean Well RS-25-5
- RCD 30mA + автомат C16 + биметалл 110°C + E-stop NC
- Корпус IP54 300×200×120
- RC snubbers + conformal coating

**Tier 2 expansion** (~₪460-540 опц.):
- BME680 + помпа 5V (атм. давление + VOC)
- Servo SG90 + игольчатый клапан DN8 (smooth water)
- PZEM-004T (опц., cmd-vs-meas check)

GPIO pins зарезервированы в ESP32 firmware (см. BOM.md секцию «GPIO
reservation») — Tier 2 встанет без перепрошивки.

## Как продолжать работу на PC

### Стартовая команда для нового Claude Code на PC

Скажи Claude Code на PC буквально:

> Я хочу продолжить работу над distillation simulator + controller проектом.
> Прочитай PLAN.md (брифинг) и sim/HANDOFF.md (текущее состояние, контекст
> пользователя), потом запусти `python sim/tests.py` чтобы убедиться что 58/58
> scenarios проходят. Я в Израиле, у меня уже есть ХД/4-375 колонна + 3 kW
> ТЭН + сенсоры от сломанного БКУ-07М. Строим custom controller на ESP32 +
> Raspberry Pi. Сейчас sim работает, физика моделируется, hardware emulируется
> с failure modes — нужно двигаться к реальной сборке. Что хочешь делать дальше?

Дальше Claude Code может:
- Запустить tests чтобы убедиться baseline OK
- Прочитать любые из docs в `sim/`
- Открыть browser UI (`python server.py` → http://localhost:8000) чтобы
  визуально проверить
- Использовать /code-review для review текущего state
- Использовать /verify когда сделаешь физический setup

### Полезные команды на PC

```bash
# Тесты
cd sim && python tests.py
cd sim && python hardware_tests.py

# Demos
python sim/demo_pwm_vs_smooth.py
python sim/demo_three_modes.py

# Sweep (батч с random faults для статистики)
python sim/sweep.py --n 100 --max-time 21600

# Interactive UI (на лету параметры)
python sim/server.py
# → http://localhost:8000

# Terminal UI (alternative)
python sim/server.py &
python sim/tui.py
```

### Если хочешь продолжить sim work

Открытые направления (см. также «Active TODOs»):

1. **Real hardware calibration** (stage 21):
   - После первой реальной сессии замерь:
     - T_head over time (logged автоматически если используешь sim controller)
     - V_body / hour
     - ABV через спиртомер каждые 30 мин
   - Tune bubble cap eff curve в `physics.py:540-552` чтобы матчить
   - Re-run scenarios — должны давать realistic числа

2. **Multi-component VLE интеграция** (stage 20):
   - Функция `vapor_eq_mol_frac_nonideal()` уже в physics.py:135 (Wilson coef)
   - Подключить в Boiler.step (line 360 area) с флагом `use_wilson_vle=True`
   - Re-baseline существующие scenarios — могут shift на 0.5-1°C
   - Validate против Pelter/M&H public ABV data

3. **ESPHome firmware port**:
   - Controller state machine → ESPHome `lambda` blocks или native C++
   - DS18B20 через ESPHome dallas component (legacy, не 2024.6+ broken)
   - HTTP/MQTT integration с Pi server.py

### Если делаешь физическую сборку

Roadmap (см. BOM.md «Roadmap сборки»):

1. **Mech setup** (1 день): колонна/дистиллятор/узел отбора на каркас
2. **Plumbing** (1 день): tap → main + small condenser, drain. Заложить
   T-junction для Tier 2 needle valve
3. **Electrical** (2 дня): DIN-rail, RCD, автоматы, SSR, контактор в IP54
4. **Sensors** (1 день): DS18B20 в термокарманы, контактные щупы. **Не
   забыть I2C pull-ups 4.7 kΩ для Tier 2!**
5. **Firmware** (2 дня): ESP32 + ESPHome или micropython, Pi 4 с server.py
6. **Bench test** (1 день): без браги, холодная вода через ТЭН, проверка
   safety chain (RCD trip, E-stop, биметалл)
7. **First wet run** (1 день): 5 L умеренной браги, attended throughout
8. **Calibration run** (1 день): `POST /api/calibrate` для FOPDT identification
9. **Production sessions**: с full audit log

## Branch info

- **Repo**: `fontanka/CraftSpirits-`
- **Working branch**: `claude/distillation-automation-rpi-N5tWd`
- **Base**: фактически all dev на этой branch — нет main merge
- **Latest commit**: смотри `git log -1` (последний — BOM tier split)

## Связанный репозиторий

- `fontanka/localtuya` — ESPHome / device integration codebase (НЕ
  использовался в этой sim work, но related project в same org).

## Контакт «кто я был»

Это Claude Sonnet/Opus 4.7 на claude.ai/code remote execution environment.
Сессия эфемерная — после inactivity container удаляется. Все артефакты в
git, **ничего не теряется** при handoff. PC Claude Code instance может
буквально продолжить — sim самодостаточен.

Question если PC Claude Code что-то не понимает: всегда `cat PLAN.md` или
`cat sim/README.md` — там detailed context. Stage history в README показывает
эволюцию.

---

**Удачи с реальной сборкой**. Если получишь 93% auto через servo+needle —
это будет первая documented domestic automation reaching manual user's
93% ABV на 5-plate bubble cap.
