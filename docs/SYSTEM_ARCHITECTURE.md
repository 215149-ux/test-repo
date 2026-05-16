# Chess Robot — System Architecture & Message Flow

## Overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              ROS NODES                                    │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌─────────────┐    ┌──────────────────┐    ┌───────────────────┐       │
│  │  disp3.2.py │    │ board_tracker_   │    │   stokfish.py     │       │
│  │  (Display)  │    │ node.py          │    │   (Engine Node)   │       │
│  │             │    │ (Board Tracker)  │    │                   │       │
│  └──────┬──────┘    └────────┬─────────┘    └─────────┬─────────┘       │
│         │                    │                        │                  │
│         │     ┌──────────────┼────────────────────────┘                  │
│         │     │              │                                           │
│         ▼     ▼              ▼                                           │
│  ┌─────────────────────────────────────────────┐                        │
│  │             ROS Topic Bus                    │                        │
│  └─────────────────────────────────────────────┘                        │
│         │     │              │              │                            │
│         ▼     ▼              ▼              ▼                            │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  ┌──────────────┐           │
│  │ Panda 1  │  │ Panda 2  │  │ Raspberry │  │ Reed-Switch  │           │
│  │ (Arm)    │  │ (Arm)    │  │ Pi GPIO   │  │ Matrix 8x8   │           │
│  └──────────┘  └──────────┘  └───────────┘  └──────────────┘           │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Nodes (العقد)

| Node | الملف | المهمة |
|------|-------|--------|
| **Display** | `disp3.2.py` | واجهة المستخدم (PyQt5): اختيار الوضع/اللون/المستوى، عرض اللوحة، ساعة، dialog ترقية |
| **Board Tracker** | `board_tracker_node.py` | مراقبة الريد سويتشات، كشف حركة اللاعب البشري، تحويلها لـ UCI |
| **Engine (Stockfish)** | `stokfish.py` | محرك الشطرنج: يستقبل حركات اللاعب، يحسب رد الروبوت، يرسل أوامر للذراع |
| **Panda 1** | `playroutine_P1.py` | تحريك الذراع الأولى |
| **Panda 2** | `playroutine_P2.py` | تحريك الذراع الثانية (Robot vs Robot فقط) |

---

## Topics (القنوات) — من ينشر ومن يستمع

| Topic | Type | الناشر | المستمع | المحتوى |
|-------|------|--------|---------|---------|
| `/chess/game_start` | String (JSON) | Display | Tracker, Engine | `{"mode":"Human vs Robot", "color":"White", "difficulty":"Medium"}` |
| `/chess/game_stop` | String | Display | Engine | `"stop"` |
| `/chess/pause` | String | Display | Engine | `"pause"` / `"resume"` |
| `/chess/move` | String (UCI) | **Tracker** أو **Display** | **Engine** | `"e2e4"` أو `"e7e8q"` (مع حرف الترقية) |
| `/chess/promotion_request` | String (JSON) | Tracker | Display | `{"move":"e7e8", "color":"white"}` |
| `/chess/board_state` | String (JSON) | Engine | Tracker, Display | `{"board":[[...]], "turn":"black", ...}` |
| `/chess/status` | String | Engine, Tracker | Display | `"White's turn"`, `"ANOMALY:..."`, `"__GAME_OVER__"` |
| `robot_move_cmd` | String | Engine | Panda 1 | UCI أو JSON (castling) |
| `robot2_move_cmd` | String | Engine | Panda 2 | UCI أو JSON (castling) |
| `robot_status` | String | Panda 1/2 | Engine | `"panda1_ready"` / `"panda2_ready"` |
| `/panda1/color` | String (latch) | Engine | Panda 1 | `"white"` / `"black"` |
| `/panda2/color` | String (latch) | Engine | Panda 2 | `"white"` / `"black"` |
| `/chess/board_occupancy` | String (JSON, latch) | Tracker | (debug) | `{"occupancy":[[0,1,...]], "fen":"...", "mode":"ACTIVE", ...}` |

---

## الخوارزمية التفصيلية — Human vs Robot (HvR)

### Phase 0: الإعداد

```
المستخدم يضغط START على الشاشة
    │
    ▼
Display ينشر:
    /chess/game_start → {"mode":"Human vs Robot", "color":"White", "difficulty":"Medium"}
    │
    ├──→ Engine يستلم: يبدأ stockfish، يعيّن المستوى، ينشر:
    │        /chess/board_state → {board: initial, turn: "white"}
    │        /panda1/color → "black" (عكس لون اللاعب)
    │
    └──→ Tracker يستلم: يضبط game_mode=HvR، human_color=WHITE
             يدخل وضع ACTIVE (لأن الأبيض يبدأ وهو اللاعب)
```

### Phase 1: دور اللاعب البشري (ACTIVE)

```
اللاعب يحرّك قطعة فعلياً على اللوحة (مثلاً e2→e4)
    │
    ▼
الحساسات (reed switches) تعكس التغيير:
    خانة e2 → 0 (فرغت)
    خانة e4 → 1 (امتلأت) — أو تبقى 1 لو أكل
    │
    ▼
scan_loop (20 Hz) يقرأ المصفوفة كل 50ms
    │
    ▼
هل اللوحة استقرت لـ 3 دورات متتالية (150ms)؟
    │
    ├── لا → ننتظر (ما اكتملت الحركة بعد)
    │
    └── نعم → ندخل _active_classify
              │
              ▼
         MoveDetector.classify(chess_board, current_occ, anchor_occ)
              │
              ├── kind='clean' → لا تغيير (مثلاً اللاعب رفع ورجّع)
              │
              ├── kind='complete' → حركة واحدة قانونية تطابق!
              │       │
              │       ▼
              │   _publish_move("e2e4")
              │       │
              │       ├── ينشر /chess/move → "e2e4"
              │       ├── يطبّق محلياً: chess_board.push(e2e4)
              │       ├── يدخل LOCKED
              │       └── ينتظر /chess/board_state
              │
              ├── kind='promotion' → 4 حركات بنفس from/to (e7e8q/r/b/n)
              │       │
              │       ▼
              │   _publish_promotion_request({"move":"e7e8","color":"white"})
              │       │
              │       ├── ينشر /chess/promotion_request
              │       ├── يدخل LOCKED
              │       └── ينتظر Display يرسل e7e8q على /chess/move
              │
              ├── kind='pending' → حالة وسطية صالحة
              │       │
              │       ▼
              │   اللاعب لسا ما خلّص (رافع قطعة بس ما حطها)
              │   ننتظر بصمت. بعد 30 ثانية: تحذير.
              │
              └── kind='anomaly' → التغيير لا يطابق أي حركة قانونية
                      │
                      ▼
                  ANOMALY warning على /chess/status
                  (اللاعب حط القطعة بمكان خاطئ)
```

### Phase 2: Engine يعالج حركة اللاعب

```
Engine يستلم /chess/move → "e2e4"
    │  (من move_queue — أو من stdin للاختبار اليدوي)
    │
    ▼
يتحقق من القانونية (chess.Move.from_uci + legal_moves)
    │
    ├── غير قانونية → ينشر /chess/status "Illegal move: ..."
    │                  لا يتقدّم (يبقى ينتظر حركة صالحة)
    │
    └── قانونية → يطبّق على stockfish:
                   position startpos moves e2e4
                   │
                   ▼
              يحسب رد الروبوت:
                   go depth 10 → bestmove d7d5
                   │
                   ▼
              يرسل أمر الذراع:
                   robot_move_cmd → "d7d5" (أو JSON للكاستلنج)
                   │
                   ▼
              ينتظر:
                   robot_status → "panda1_ready"
                   │
                   ▼
              ينشر الحالة الجديدة:
                   /chess/board_state → {board:[[...]], turn:"white", ...}
                   /chess/status → "White's turn"
```

### Phase 3: Tracker يستلم board_state

```
Tracker يستلم /chess/board_state (بعد رد الروبوت)
    │
    ▼
يزامن chess_board من FEN الجديدة
يزامن الحساس (في المحاكاة فقط)
    │
    ▼
يخرج من LOCKED → يقرأ turn:
    │
    ├── turn == human_color → ACTIVE (دور اللاعب من جديد)
    │       → يبدأ يراقب الحساسات من جديد
    │
    └── turn != human_color → MONITOR
            → يراقب بصمت (لو الذراع خبصت ← BOARD MISMATCH)
```

### Phase 4: وضع MONITOR (دور الروبوت)

```
scan_loop يراقب الحساسات
    │
    ├── الحساسات = المتوقع → OK، لا شيء
    │
    └── الحساسات ≠ المتوقع (بعد 3s grace period):
            → BOARD MISMATCH warning
            (يعني الذراع ما وصّلت القطعة لمكانها، أو قطعة وقعت)
```

---

## الخوارزمية التفصيلية — Robot vs Robot (RvR)

```
Display ينشر:
    /chess/game_start → {"mode":"Robot vs Robot", "robot1":{...}, "robot2":{...}}
    │
    ├──→ Engine: يعيّن مستويين، ينشر ألوان الروبوتين
    │
    └──→ Tracker: يضبط game_mode=RvR
             يدخل WAITING (inert كلياً — لا يراقب ولا يصنّف ولا ينشر)

Engine يلعب لوحده:
    while not game_over:
        ├── stockfish يحسب حركة Robot 1
        ├── ينشر robot_move_cmd → Panda 1
        ├── ينتظر panda1_ready
        ├── ينشر /chess/board_state
        ├── stockfish يحسب حركة Robot 2
        ├── ينشر robot2_move_cmd → Panda 2
        ├── ينتظر panda2_ready
        └── ينشر /chess/board_state

Tracker: لا يتدخل أبداً. WAITING طوال اللعبة.
```

---

## الترقية (Promotion) — التفصيل الكامل

```
Step 1: اللاعب يحرّك بيدق إلى الصف الأخير (e7→e8)
         الحساسات: e7=0, e8=1 (أو يبقى 1 لو أكل)

Step 2: Tracker يصنّف:
         يجد 4 حركات قانونية بنفس from/to:
           e7e8q, e7e8r, e7e8b, e7e8n
         (كلها بنفس occupancy نهائي)
         → kind='promotion'

Step 3: Tracker ينشر:
         /chess/promotion_request → {"move":"e7e8", "color":"white"}
         يدخل LOCKED

Step 4: Display يستلم /chess/promotion_request
         يفتح PromotionDialog
         اللاعب يختار Queen

Step 5: Display ينشر:
         /chess/move → "e7e8q"

Step 6: Engine يستلم "e7e8q" من move_queue
         يطبّقها على stockfish
         يحسب رد الروبوت
         ينشر /chess/board_state

Step 7: Tracker يستلم /chess/board_state
         يخرج من LOCKED
         يزامن chess_board
         يدخل ACTIVE أو MONITOR حسب الدور
```

---

## معالجة "الحركات الغبية" — كل السيناريوهات

| # | سلوك اللاعب | ما يراه الـ Tracker | الاستجابة |
|---|---|---|---|
| 1 | رفع e2 ثم رجّعها | `PENDING[e2]` → `clean` | لا شيء — إلغاء طبيعي |
| 2 | رفع e2 ثم وضع e4 | `PENDING[e2]` → `complete(e2e4)` | نشر `/chess/move` |
| 3 | رفع e2 ثم وضع e5 (illegal) | `PENDING[e2]` → `ANOMALY` | warning على `/chess/status` |
| 4 | أكل: رفع a4 أولاً ثم b5 ثم وضع b5 | `PENDING[a4]` → `PENDING[a4,b5]` → `complete(a4b5)` | نشر |
| 5 | أكل: رفع b5 (المأكولة) أولاً ثم a4 ثم وضع b5 | `PENDING[b5]` → `PENDING[b5,a4]` → `complete(a4b5)` | نشر |
| 6 | رفع e2 ثم ترك 30 ثانية | `PENDING[e2]` لـ 30s | warning "pieces still off-board" |
| 7 | رفع d7 (قطعة الخصم) في دور الأبيض | `ANOMALY` | warning — لا حركة قانونية للأبيض من d7 |
| 8 | كاستلنج: رفع e1 ثم h1 ثم وضع g1 ثم f1 | `PENDING` عبر مراحل → `complete(e1g1)` | نشر |
| 9 | كاستلنج: رفع e1 ووضع g1 فقط (نسي الرخ) | `PENDING[e1]` → `ANOMALY` (g1 وحدها لا تطابق) | warning |
| 10 | الذراع ما وصّلت القطعة (MONITOR) | `BOARD MISMATCH` بعد 3s | warning |

---

## حالات الـ Mode — دورة الحياة

```
                    ┌─────────────────────────────────┐
                    │                                 │
    /game_start     ▼     /board_state               │
   ──────────→ [ACTIVE] ─────────────────→ [MONITOR] ─┘
                  │  ▲                        │
         move     │  │ /board_state          │ (الذراع تحرّك)
      detected    │  │ (turn=human)          │
                  ▼  │                        │
              [LOCKED] ◄──────────────────────┘
                  │         timeout 30s
                  │         (fallback to
                  └────→ [ACTIVE/MONITOR] ← local push)


    /game_start (RvR)
   ──────────────────→ [WAITING] (inert forever until game ends)
```

---

## ملخص: مَن يبعث مَن يستقبل

```
┌──────────────────────────────────────────────────────────────────┐
│                       MESSAGE FLOW DIAGRAM                        │
│                                                                  │
│   Display ────/game_start────→ Engine                            │
│      │                            │                              │
│      │                            ├───/board_state──→ Tracker    │
│      │                            │                     │        │
│      │                            │                     │        │
│      │    ┌───/chess/move─────────┤◄────────────────────┘        │
│      │    │   (from tracker      │   (detected move              │
│      │    │    OR from display   │    from physical board)       │
│      │    │    after promo)      │                               │
│      │    │                      │                               │
│      │    └──────────────────────┤                               │
│      │                           │                               │
│      │◄──/promotion_request──────┤ (Tracker)                     │
│      │                           │                               │
│      │───/chess/move─────────────┤ (Display replies with         │
│      │   (e7e8q after dialog)    │  promotion piece)             │
│      │                           │                               │
│      │◄──/chess/status───────────┤ (Engine + Tracker)            │
│      │◄──/board_state────────────┤ (Engine)                      │
│      │                           │                               │
│      │                           ├───robot_move_cmd──→ Panda 1   │
│      │                           ├───robot2_move_cmd─→ Panda 2   │
│      │                           │                               │
│      │                           │◄──robot_status─── Panda 1/2   │
└──────────────────────────────────────────────────────────────────┘
```

---

## Dependencies (المكتبات المطلوبة)

| Node | Python packages |
|------|----------------|
| board_tracker_node.py | `python-chess`, `rospy`, `std_msgs` |
| stokfish.py | `python-chess`, `rospy`, `std_msgs`, `stockfish` (binary) |
| disp3.2.py | `PyQt5`, `rospy`, `std_msgs` |

---

## كيف تشغّل النظام الكامل (HvR)

```bash
# Terminal 1
roscore

# Terminal 2 — المحرك (لازم يكون stockfish binary مثبّت)
rosrun chess_robot stokfish.py

# Terminal 3 — الشاشة
rosrun chess_robot disp3.2.py

# Terminal 4 — تعقّب اللوحة (على الراسبيري أو المحاكاة)
rosrun chess_robot board_tracker_node.py
```

1. اللاعب يضغط **START** على الشاشة.
2. يختار **Human vs Robot** + **White** + **Medium**.
3. `/chess/game_start` ينطلق → كل النودات تجهّز.
4. اللاعب يحرّك فيزيائياً → tracker يكشف → `/chess/move` → stockfish يرد.
5. الذراع تتحرك → `board_state` → tracker يدخل ACTIVE مجدداً.
6. التكرار حتى checkmate.

---

## كيف تشغّل النظام الكامل (RvR)

```bash
# نفس الترتيب، لكن:
# - board_tracker_node.py اختياري (سيبقى WAITING ولن يتدخل)
# - الشاشة تختار Robot vs Robot
```

1. `/chess/game_start` مع mode="Robot vs Robot".
2. Engine يلعب لوحده: Robot 1 → Robot 2 → Robot 1 → ...
3. tracker inert (WAITING) — **لا يراقب ولا ينشر**.
4. Display يعرض اللوحة من `/chess/board_state`.
