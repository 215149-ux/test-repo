# Reed-Switch Matrix Wiring — Raspberry Pi 3 Model B

## المخطط العام

```
        COL0   COL1   COL2   COL3   COL4   COL5   COL6   COL7
        (a)    (b)    (c)    (d)    (e)    (f)    (g)    (h)
        GPIO26 GPIO21 GPIO20 GPIO16 GPIO12 GPIO25 GPIO24 GPIO23
         │      │      │      │      │      │      │      │
ROW0 ────┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡── GPIO4  (rank 8)
ROW1 ────┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡── GPIO17 (rank 7)
ROW2 ────┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡── GPIO27 (rank 6)
ROW3 ────┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡── GPIO22 (rank 5)
ROW4 ────┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡── GPIO5  (rank 4)
ROW5 ────┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡── GPIO6  (rank 3)
ROW6 ────┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡── GPIO13 (rank 2)
ROW7 ────┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡──┼──⚡── GPIO19 (rank 1)
         │      │      │      │      │      │      │      │
         ⚡ = Reed Switch (مغلق عندما يوجد مغناطيس/قطعة فوقه)
```

## جدول التوصيلات

### الصفوف (ROWS) — Input مع Pull-up داخلي

| الصف | Rank | GPIO Pin | Physical Pin |
|------|------|----------|--------------|
| ROW0 | 8 (أسود) | GPIO 4 | Pin 7 |
| ROW1 | 7 | GPIO 17 | Pin 11 |
| ROW2 | 6 | GPIO 27 | Pin 13 |
| ROW3 | 5 | GPIO 22 | Pin 15 |
| ROW4 | 4 | GPIO 5 | Pin 29 |
| ROW5 | 3 | GPIO 6 | Pin 31 |
| ROW6 | 2 (أبيض) | GPIO 13 | Pin 33 |
| ROW7 | 1 (أبيض) | GPIO 19 | Pin 35 |

### الأعمدة (COLS) — Output

| العمود | File | GPIO Pin | Physical Pin |
|--------|------|----------|--------------|
| COL0 | a | GPIO 26 | Pin 37 |
| COL1 | b | GPIO 21 | Pin 40 |
| COL2 | c | GPIO 20 | Pin 38 |
| COL3 | d | GPIO 16 | Pin 36 |
| COL4 | e | GPIO 12 | Pin 32 |
| COL5 | f | GPIO 25 | Pin 22 |
| COL6 | g | GPIO 24 | Pin 18 |
| COL7 | h | GPIO 23 | Pin 16 |

## كيف يعمل المسح (Scanning)

1. كل الأعمدة تبدأ HIGH (معطّلة).
2. نفعّل عمود واحد بوضعه LOW.
3. نقرأ كل الصفوف:
   - إذا الصف = LOW → السويتش مغلق → قطعة موجودة.
   - إذا الصف = HIGH → السويتش مفتوح → خانة فاضية.
4. نطفئ العمود (نرجّعه HIGH).
5. ننتقل للعمود التالي.
6. دورة المسح الكاملة (8 أعمدة) ← ~2ms.

## المغناطيسات

- ضع مغناطيس **نيوديميوم N35** (قطر 6-8mm, سمك 2-3mm) في قاعدة كل قطعة.
- تأكد أن المغناطيس قريب بما يكفي من الريد سويتش (≤5mm).
- جرّب بـ `python3 gpio_sensor_board.py` قبل ما تشغّل النظام الكامل.

## حماية إضافية (اختياري)

لو عندك مشاكل تداخل (ghosting) على مصفوفات كبيرة:
- أضف **ديود (1N4148)** بالتسلسل مع كل ريد سويتش.
  هذا يمنع تيار العودة عبر سويتشات أخرى.

```
COL_pin ──|>|── [Reed Switch] ── ROW_pin
          diode (cathode toward ROW)
```

## التشغيل

### اختبار مستقل (بدون ROS):
```bash
cd ~/catkin_ws/src/chess_robot/scripts/rasperry
python3 gpio_sensor_board.py
```
يعرض اللوحة كل 0.5 ثانية. تأكد أن كل الخانات تعمل.

### تشغيل كامل (مع ROS):
```bash
rosrun chess_robot board_tracker_rpi.py
```

### تشغيل الاختبار (بدون ROS، بدون GPIO — على PC):
```bash
cd ~/catkin_ws/src/chess_robot/scripts
python3 board_tracker_node.py
# ← يستخدم SimulatedSensorBoard + واجهة terminal
```
