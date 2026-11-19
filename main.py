#!/usr/bin/env python3
"""키보드 코스 측정기 - CMD 코스형 키보드 성능 점검 툴 (F ~ S 티어 판정)"""
import argparse
import json
import os
import re
import statistics
import sys
import threading
import time
from collections import deque
from pathlib import Path

try:
    import keyboard
except ImportError:
    sys.exit("keyboard 모듈이 없습니다:  pip install -r requirements.txt")
try:
    import hid
except ImportError:
    hid = None

ROOT = Path(__file__).resolve().parent


def col(n):
    return f"\x1b[38;5;{n}m"


R, CYAN, GREEN, RED, YEL, GRAY, WHITE = "\x1b[0m", col(51), col(46), col(196), col(220), col(244), col(255)


# ───────────────────────── 공통 유틸 ─────────────────────────
def clear():
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def flush_input():
    try:
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getch()
    except ImportError:
        try:
            import termios
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except Exception:
            pass


def wait_key(msg="키보드 키 아무거나 누르세요"):
    print(f"\n{YEL}▶ {msg}{R}", flush=True)
    time.sleep(0.2)
    flush_input()
    while True:
        e = keyboard.read_event()
        if e.event_type == "down":
            sc = e.scan_code
            break
    while True:  # 눌린 키가 떨어질 때까지 대기 (다음 단계 측정에 섞이지 않게)
        e = keyboard.read_event()
        if e.event_type == "up" and e.scan_code == sc:
            break
    time.sleep(0.15)
    flush_input()


def countdown():
    for n in (3, 2, 1):
        sys.stdout.write(f"\r{CYAN}  {n} ...{R}\x1b[K")
        sys.stdout.flush()
        time.sleep(0.7)
    sys.stdout.write(f"\r{GREEN}  GO!{R}\x1b[K\n")


def timed(secs, label, on_tick=None):
    end = time.perf_counter() + secs
    while (left := end - time.perf_counter()) > 0:
        sys.stdout.write(f"\r{CYAN}{label}  {left:4.1f}s{R}\x1b[K")
        sys.stdout.flush()
        if on_tick:
            on_tick()
        time.sleep(0.03)
    sys.stdout.write("\r\x1b[K")


def bar(score, width=20):
    n = int(round(score / 100 * width))
    c = GREEN if score >= 80 else YEL if score >= 50 else RED
    return f"{c}{'█' * n}{GRAY}{'░' * (width - n)}{R} {score:5.1f}"


def header(idx, title, lines):
    clear()
    print(f"{CYAN}━━━ STAGE {idx} · {title} ━━━{R}\n")
    for l in lines:
        print(f"  {l}")


def clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))



# ───────────────────────── 입력 기록기 ─────────────────────────
class Recorder:
    def __init__(self):
        self.events, self.down, self.orphan_up, self.active = [], set(), 0, False

    def cb(self, e):
        t = time.perf_counter_ns()
        if not self.active:
            return
        sc = e.scan_code
        if e.event_type == "down":
            if sc in self.down:  # 오토리핏 무시
                return
            self.down.add(sc)
        else:
            if sc not in self.down:
                self.orphan_up += 1
            self.down.discard(sc)
        self.events.append((t, sc, (e.name or str(sc)).lower(), e.event_type))

    def start(self):
        self.events, self.down, self.orphan_up, self.active = [], set(), 0, True

    def stop(self):
        self.active = False
        return list(self.events)


# ───────────────────────── 아날로그(HID) 소스 ─────────────────────────
class AnalogSource:
    """프로파일에 정의된 HID 리포트에서 키 깊이(mm)를 읽는다."""

    def __init__(self, p):
        self.p = p
        self.recent = deque(maxlen=40000)       # (t_ns, mm)
        self.packets = deque(maxlen=400000)     # (t_ns, bytes)
        self.dev = hid.device()
        cands = hid.enumerate(p["vendor_id"], p["product_id"])
        if p.get("interface_number") is not None:
            cands = [c for c in cands if c["interface_number"] == p["interface_number"]]
        if not cands:
            raise RuntimeError("프로파일의 VID/PID 기기를 찾지 못했습니다")
        self.info = cands[0]
        self.dev.open_path(self.info["path"])
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        p = self.p
        off, n = p["key_offset"], p.get("value_bytes", 2)
        while self.running:
            data = self.dev.read(64, 1)
            if not data:
                continue
            t = time.perf_counter_ns()
            self.packets.append((t, bytes(data)))
            raw = int.from_bytes(bytes(data[off:off + n]), p.get("endian", "little"))
            mm = min(raw / p["max_raw"], 1.0) * p.get("travel_mm", 4.0)
            self.recent.append((t, mm))

    def window(self, t0, t1):
        return [mm for t, mm in list(self.recent) if t0 <= t <= t1]

    def depth_at(self, t):
        last = None
        for ts, mm in list(self.recent):
            if ts > t:
                break
            last = mm
        return last


class Ctx:
    def __init__(self, rec, analog, profile):
        self.rec, self.analog, self.profile = rec, analog, profile or {}


# ───────────────────────── STAGE 1: Hall / Rapid Trigger ─────────────────────────
def render_graph(samples, marks, now, travel, win_ns=2_000_000_000, W=90, H=14):
    t0 = now - win_ns
    buckets = [None] * W
    for t, mm in samples:
        i = int((t - t0) / win_ns * W)
        if 0 <= i < W:
            buckets[i] = mm
    grid = [[" "] * W for _ in range(H)]
    row_of = lambda mm: H - 1 - int(clamp(mm / travel, 0, 1) * (H - 1))
    prev = None
    for x, mm in enumerate(buckets):
        if mm is None:
            continue
        r = row_of(mm)
        lo, hi = (r, r) if prev is None else (min(r, prev), max(r, prev))
        for y in range(lo, hi + 1):
            grid[y][x] = f"{CYAN}│{R}" if y != r else f"{CYAN}█{R}"
        prev = r
    for t, kind, mm in marks:
        i = int((t - t0) / win_ns * W)
        if 0 <= i < W:
            grid[row_of(mm)][i] = f"{GREEN if kind == 'down' else RED}●{R}"
    out = []
    for y in range(H):
        label = f"{travel * (H - 1 - y) / (H - 1):4.1f}mm"
        out.append(f"{GRAY}{label}{R}┤" + "".join(grid[y]))
    out.append(f"{GRAY}      └{'─' * W}{R}")
    out.append(f"  {GREEN}● KeyDown(Actuation){R}   {RED}● KeyUp(Release){R}")
    return out


def live_graph(ctx, seconds):
    a, rec = ctx.analog, ctx.rec
    travel = ctx.profile.get("travel_mm", 4.0)
    want = (ctx.profile.get("test_key") or "").lower()
    marks, seen, first = [], 0, True
    rec.start()
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        now = time.perf_counter_ns()
        evs = rec.events
        while seen < len(evs):
            t, _, name, typ = evs[seen]
            seen += 1
            if want and name != want:
                continue
            d = a.depth_at(t)
            if d is not None:
                marks.append((t, typ, d))
        lines = render_graph(list(a.recent), marks, now, travel)
        lines.append(f"  남은 시간 {end - time.perf_counter():4.1f}s")
        if not first:
            sys.stdout.write(f"\x1b[{len(lines)}A")
        first = False
        sys.stdout.write("\n".join(l + "\x1b[K" for l in lines) + "\n")
        sys.stdout.flush()
        time.sleep(1 / 60)
    rec.stop()
    return marks


def stage_hall(ctx):
    header(1, "자석축(Hall Effect) · 래피드 트리거", [
        "① 2초: 키에서 손을 떼고 가만히  ② 2초: 키를 끝까지 누른 채 유지",
        "③ 10초: 키를 반만 눌렀다 떼며 래피드 트리거를 마구 작동시키세요"])
    if not ctx.analog:
        print(f"\n{YEL}  아날로그 프로파일(profiles/*.json) 또는 hidapi 없음 → 이 단계는 건너뜁니다 (점수 제외){R}")
        return None, []
    wait_key()
    a, rec = ctx.analog, ctx.rec
    # ① 상단 노이즈
    countdown(); rec.start(); t0 = time.perf_counter_ns()
    timed(2, "손 떼고 대기")
    t1 = time.perf_counter_ns(); idle_ev = rec.stop()
    top = a.window(t0, t1)
    # ② 바닥 노이즈
    print(f"{YEL}  이제 키를 끝까지 누르고 유지하세요{R}")
    countdown(); rec.start(); t0 = time.perf_counter_ns()
    timed(2, "끝까지 누르는 중")
    t1 = time.perf_counter_ns(); hold_ev = rec.stop()
    bottom_all = a.window(t0, t1)
    bottom = [m for m in bottom_all if bottom_all and m >= 0.9 * max(bottom_all)]
    # ③ 래피드 트리거
    print(f"{YEL}  이제 반만 눌렀다 떼며 마구 움직이세요{R}\n")
    countdown()
    marks = live_graph(ctx, 10)

    if len(top) < 20 or len(bottom) < 20:
        return 0.0, [f"{RED}센서 샘플 부족 — 프로파일(key_offset, test_key)을 확인하세요{R}"]
    noise_top, noise_bot = statistics.pstdev(top), statistics.pstdev(bottom)
    noise_s = clamp(100 - max(noise_top, noise_bot) * 400)
    chat_cnt = len(idle_ev) + sum(1 for e in hold_ev if e[3] == "up")
    chat_s = clamp(100 - chat_cnt * 15)
    acts = [m for _, k, m in marks if k == "down"]
    rels = [m for _, k, m in marks if k == "up"]
    rows = [f"0.0mm 노이즈 σ : {noise_top:.3f} mm",
            f"4.0mm 노이즈 σ : {noise_bot:.3f} mm",
            f"데드존 채터링   : {chat_cnt}회 {'(이상 없음)' if chat_cnt == 0 else '⚠ 경고'}",
            f"액추에이션/릴리즈 : {len(acts)}회 / {len(rels)}회"]
    score = noise_s * 0.5 + chat_s * 0.3
    if len(acts) >= 5:
        sd = statistics.pstdev(acts)
        cons_s = clamp(100 - sd * 300)
        rows += [f"액추에이션 평균 {statistics.mean(acts):.2f}mm  (편차 σ {sd:.3f}mm)"]
        if rels:
            rows += [f"릴리즈 평균     {statistics.mean(rels):.2f}mm"]
        score += cons_s * 0.2
    else:
        rows.append(f"{YEL}래피드 트리거 샘플 부족(5회 미만) → 일관성 항목 제외{R}")
        score = score / 0.8
    return clamp(score), rows


# ───────────────────────── STAGE 2: 폴링레이트 ─────────────────────────
POLL_SCORE = {8000: 100, 4000: 90, 2000: 80, 1000: 65, 500: 40, 250: 20, 125: 10}


def stage_polling(ctx):
    src = "HID 패킷 도착 간격" if ctx.analog else "OS 키 이벤트 간격(근사치)"
    header(2, "폴링레이트 (8000Hz)", [
        "6초 동안 여러 키를 동시에 아무렇게나 최대한 빠르게 연타하세요",
        f"측정 기준: {src}"])
    wait_key()
    countdown()
    if ctx.analog:
        t0 = time.perf_counter_ns()
        timed(6, "측정 중")
        ts = [t for t, _ in list(ctx.analog.packets) if t >= t0]
    else:
        ctx.rec.start()
        timed(6, "측정 중")
        ts = [e[0] for e in ctx.rec.stop()]
    deltas = [(b - a) / 1e6 for a, b in zip(ts, ts[1:])]
    deltas = sorted(d for d in deltas if d >= 0.05)      # 같은 패킷 이벤트 제거
    if len(deltas) < 30:
        return 0.0, [f"{RED}샘플 부족({len(deltas)}개) — 더 많이 연타하세요{R}"]
    floor = deltas[int(len(deltas) * 0.05)]
    near = min(POLL_SCORE, key=lambda h: abs(h - 1000 / floor))
    ok = [d for d in deltas if d <= 5]
    rows = [f"추정 폴링레이트 : {1000 / floor:7.0f} Hz  (≈ {near}Hz 등급)",
            f"Δt 최소 / 평균 / 최대 : {min(ok):.3f} / {statistics.mean(ok):.3f} / {max(ok):.3f} ms",
            f"표본 수 : {len(deltas)}"]
    if not ctx.analog:
        rows.append(f"{YEL}※ OS 훅 기반이라 고주사율은 과소측정될 수 있음 (HID 프로파일 권장){R}")
    return float(POLL_SCORE[near]), rows


# ───────────────────────── STAGE 3: 초단타 / 채터링 ─────────────────────────
def stage_micro(ctx):
    header(3, "초단타(Micro-tap) 누락 감지", [
        "10초 동안 아무 키나 최대한 '톡톡' 짧게 치세요 (길게 누르지 말 것)"])
    wait_key()
    countdown()
    ctx.rec.start()
    timed(10, "측정 중")
    orphan = ctx.rec.orphan_up
    ev = ctx.rec.stop()
    last_dn, last_up, durs, chatter = {}, {}, [], 0
    for t, sc, _, typ in ev:
        if typ == "down":
            if sc in last_up and (t - last_up[sc]) / 1e6 < 5:
                chatter += 1
            last_dn[sc] = t
        else:
            if sc in last_dn:
                durs.append((t - last_dn.pop(sc)) / 1e6)
            last_up[sc] = t
    unmatched = len(last_dn)
    if len(durs) < 20:
        return 0.0, [f"{RED}샘플 부족({len(durs)}회) — 더 많이 치세요{R}"]
    micro = sum(1 for d in durs if d <= 3)
    rows = [f"총 입력(Down→Up 쌍) : {len(durs)}",
            f"1~3ms 초단타 : {micro}회  (최단 {min(durs):.2f}ms)",
            f"Down/Up 짝 누락 : {unmatched + orphan}회 {'(정상)' if unmatched + orphan == 0 else '⚠ 경고'}",
            f"채터링(5ms 내 재입력) : {chatter}회 {'(정상)' if chatter == 0 else '⚠ 경고'}"]
    return clamp(100 - (unmatched + orphan) * 25 - chatter * 10), rows


# ───────────────────────── STAGE 4: KPS / UR ─────────────────────────
def kps_run(ctx, label, hint):
    print(f"\n{WHITE}  [{label}] {hint}{R}")
    wait_key()
    countdown()
    ctx.rec.start()
    timed(5, f"{label} 측정 중")
    downs = [t for t, _, _, ty in ctx.rec.stop() if ty == "down"]
    if len(downs) < 20:
        return None
    peak, j = 0, 0
    for i, t in enumerate(downs):
        while t - downs[j] > 1_000_000_000:
            j += 1
        peak = max(peak, i - j + 1)
    iv = [(b - a) / 1e6 for a, b in zip(downs, downs[1:])]
    med = statistics.median(iv)
    iv = [x for x in iv if x <= med * 3]
    sd = statistics.pstdev(iv)
    return {"kps": peak, "avg": len(downs) / 5, "sd": sd, "ur": sd * 10}


def stage_kps(ctx):
    header(4, "KPS · 입력 안정성(UR)", ["연타(Jack) 5초 → 교대 타건(Stream) 5초"])
    scores, rows = [], []
    for label, hint in (("Jack", "키 하나를 최대한 빠르게 연타"), ("Stream", "두 키를 번갈아 최대한 빠르게")):
        r = kps_run(ctx, label, hint)
        if r is None:
            rows.append(f"{label}: {RED}샘플 부족{R}")
            scores.append(0)
            continue
        stab = clamp(100 - max(0, r["sd"] - 3) * 4)
        spd = clamp(r["kps"] / 14 * 100)
        scores.append(stab * 0.6 + spd * 0.4)
        rows.append(f"{label}: 최고 {r['kps']} KPS · 평균 {r['avg']:.1f} KPS · "
                    f"간격 편차 {r['sd']:.2f}ms · UR {r['ur']:.1f}")
    rows.append(f"{GRAY}※ 사람의 실력이 섞이는 항목입니다{R}")
    return statistics.mean(scores), rows


# ───────────────────────── STAGE 5: 로터리 엔코더 ─────────────────────────
def stage_knob(ctx):
    header(5, "노브(Rotary Encoder) 테스트", [
        "10초 동안 노브를 ← 좌로 5칸, → 우로 5칸 돌리고, 클릭을 1번 하세요"])
    knob = ctx.profile.get("knob")
    if not knob:
        print(f"\n{YEL}  프로파일에 knob 매핑 없음 → 건너뜁니다 (점수 제외){R}")
        return None, []
    wait_key()
    countdown()
    ctx.rec.start()
    cnt = {"left": 0, "right": 0, "click": 0}

    def tick():
        evs = [e for e in ctx.rec.events if e[3] == "down"]
        for k in cnt:
            cnt[k] = sum(1 for e in evs if e[2] == knob[k].lower())
        sys.stdout.write(f"\r  ◀ {cnt['left']:2d}   ▶ {cnt['right']:2d}   ● 클릭 {cnt['click']}   ")

    timed(10, "측정 중", tick)
    tick()
    ctx.rec.stop()
    err = abs(cnt["left"] - 5) + abs(cnt["right"] - 5) + abs(cnt["click"] - 1)
    rows = [f"좌회전 {cnt['left']}스텝 / 우회전 {cnt['right']}스텝 / 클릭 {cnt['click']}회",
            f"기대값(5/5/1) 오차 합계 : {err}"]
    return clamp(100 - err * 12), rows


# ───────────────────────── STAGE 6: HID Raw 디버거 ─────────────────────────
def stage_raw(ctx):
    header(6, "HID Raw 패킷 디버거", ["6초간 수신되는 원시 패킷(Hex)을 표시합니다. 키를 눌러보세요"])
    if not ctx.analog:
        print(f"\n{YEL}  HID 기기가 연결되지 않았습니다 → 건너뜁니다{R}")
        return None, []
    wait_key()
    a = ctx.analog
    i = a.info
    print(f"\n  기기 : {i.get('manufacturer_string')} {i.get('product_string')}")
    print(f"  VID : 0x{i['vendor_id']:04X}   PID : 0x{i['product_id']:04X}\n")
    start = len(a.packets)
    view = deque(maxlen=8)
    shown = 0

    def tick():
        nonlocal shown
        pk = list(a.packets)
        for _, d in pk[max(shown, start):]:
            view.append(d.hex(" "))
        shown = len(pk)
        sys.stdout.write("\x1b[8A" if getattr(tick, "drawn", False) else "")
        tick.drawn = True
        rows = list(view) + [""] * (8 - len(view))
        sys.stdout.write("\n".join(f"  {GRAY}{r[:110]}{R}\x1b[K" for r in rows) + "\n")

    timed(6, "수신 중", tick)
    pk = list(a.packets)[start:]
    ids = sorted({p[1][0] for p in pk}) if pk else []
    return None, [f"수신 패킷 {len(pk)}개 · 리포트 길이 {len(pk[0][1]) if pk else 0}B · "
                  f"첫 바이트 종류 {[hex(x) for x in ids]}"]


# ───────────────────────── 메인 ─────────────────────────
def show_title():
    clear()
    art = (ROOT / "title.txt").read_text(encoding="utf-8")
    print(f"{CYAN}{art}{R}")
    print(f"\n{GRAY}  Keyboard Performance Course · F ~ S Tier{R}")


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def ask_save_json(results):
    clear()
    print(f"{CYAN}━━━ 코스 종료 ━━━{R}\n")
    flush_input()
    ans = input("  테스트 기록을 JSON으로 출력(저장)할까요? (y/n) : ").strip().lower()
    if ans not in ("y", "yes", "ㅛ", "예", "네"):
        print(f"\n{GRAY}  저장하지 않고 종료합니다.{R}")
        return
    data = {
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stages": [{"stage": n, "score": None if sc is None else round(sc, 1),
                    "details": [ANSI_RE.sub("", r) for r in rows]} for n, sc, rows in results],
    }
    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    path = out / f"result_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{GREEN}  저장 완료 → {path}{R}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", help="아날로그/HID 프로파일 JSON 경로")
    ap.add_argument("--list-hid", action="store_true", help="연결된 HID 기기 목록 출력")
    args = ap.parse_args()
    os.system("")  # Windows ANSI 활성화
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.list_hid:
        if not hid:
            sys.exit("hidapi 미설치: pip install hidapi")
        for d in hid.enumerate():
            print(f"VID 0x{d['vendor_id']:04X}  PID 0x{d['product_id']:04X}  "
                  f"if={d['interface_number']}  {d['manufacturer_string']} {d['product_string']}")
        return

    pf = Path(args.profile) if args.profile else ROOT / "profiles" / "default.json"
    profile, analog = None, None
    if pf.exists():
        profile = json.loads(pf.read_text(encoding="utf-8"))
        if hid:
            try:
                analog = AnalogSource(profile)
            except Exception as e:
                print(f"{YEL}HID 연결 실패: {e}{R}")
                time.sleep(1.5)
    rec = Recorder()
    keyboard.hook(rec.cb)
    ctx = Ctx(rec, analog, profile)

    show_title()
    wait_key()
    stages = [("1 Hall / Rapid Trigger", stage_hall, 30), ("2 폴링레이트", stage_polling, 25),
              ("3 초단타 / 채터링", stage_micro, 15), ("4 KPS / UR", stage_kps, 15),
              ("5 노브 엔코더", stage_knob, 10), ("6 HID Raw 디버거", stage_raw, 0)]
    results = []
    for n, fn, w in stages:
        score, rows = fn(ctx)
        print()
        for r in rows:
            print(f"  {GREEN}▸{R} {r}")
        if score is not None:
            print(f"\n  단계 점수  {bar(score)}")
        results.append((n, score if w else None, rows))
        wait_key("다음 단계로 — 키보드 키 아무거나 누르세요")
    ask_save_json(results)
    input("\nEnter 를 누르면 종료합니다...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n중단됨")
