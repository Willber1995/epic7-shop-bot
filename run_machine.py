"""状态机驱动的第七史诗秘密商店自动购买(控制版)。

状态流转:
IDLE → (start) → BOOT → GOLD_CHECK → SCAN_FIRST → [SCAN_MORE/SWIPE]*
  → [BUY_TAP→BUY_CONFIRM]* → REFRESH_OPEN → REFRESH_DIALOG
  → REFRESH_CONFIRM → REFRESH_WAIT → ROUND_DONE → … → DONE
暂停: 任意检查点 → PAUSED → (resume) 继续
金币 < 50w: GOLD_BLOCK 终止

控制: HTTP 服务 (默认 8787)
  GET /                 → monitor.html
  GET /state.js         → 状态快照
  GET /api/start?rounds=N
  GET /api/pause | /api/resume | /api/stop
前端 monitor.html 直连该服务。
"""
import json
import os
import re
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import cv2
import numpy as np
import pytesseract

os.chdir(os.path.dirname(os.path.abspath(__file__)))
from auto_shop_gui import tap, swipe_up, screencap

DEVICE = "127.0.0.1:5555"
THRESH = 0.85
BRIGHT_TH = 70
GOLD_MIN = 500_000
HTTP_PORT = 8787
STATE_FILE = "state.js"

REFRESH_BTN = (0.176, 0.917)
REFRESH_CONFIRM = (0.585, 0.640)

TPL = {
    "bookmark": "Feature_screenshot/bookmark.png",
    "mystic": "Feature_screenshot/mystic.png",
    "confirm": "Feature_screenshot/confirm_btn.png",
    "refresh_ok": "Feature_screenshot/confirm_refresh.png",
    "lobby_entry": "Feature_screenshot/lobby_shop_entry.png",
    "shop_btn": "Feature_screenshot/shop_entry_btn.png",
}
TARGETS = [
    ("誓约书签", TPL["bookmark"], 184000),
    ("神秘奖牌", TPL["mystic"], 280000),
]

_lock = threading.Lock()
state = {
    "state": "IDLE",
    "round": 0,
    "max_round": 5,
    "stats": {"gold": 0, "diamond": 0, "bought": {}, "gold_left": None},
    "log": [],
    "running": False,
    "paused": False,
    "updated": time.time(),
}
_cmd = {"start": threading.Event(), "pause": threading.Event(), "stop": threading.Event(),
        "rounds": 5, "shutdown": threading.Event()}


def _write_state():
    state["updated"] = time.time()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write("window.E7_STATE = " + json.dumps(state, ensure_ascii=False) + ";\n")


def checkpoint():
    """暂停/停止检查点: emit 与所有 wait() 都会经过。"""
    if _cmd["stop"].is_set() and state.get("running"):
        raise RuntimeError("用户停止")
    if _cmd["pause"].is_set() and state.get("running"):
        state["state"] = "PAUSED"
        state["paused"] = True
        _write_state()
        entry = {"t": time.strftime("%H:%M:%S"), "state": "PAUSED", "msg": "已暂停,等待继续…", "level": "warn"}
        state["log"].append(entry)
        state["log"] = state["log"][-300:]
        _write_state()
        print(f"[{entry['t']}] ⏸ [PAUSED] 已暂停", flush=True)
        while _cmd["pause"].is_set() and not _cmd["stop"].is_set() and state.get("running"):
            time.sleep(0.2)
        state["paused"] = False
        if _cmd["stop"].is_set():
            raise RuntimeError("用户停止")
        entry = {"t": time.strftime("%H:%M:%S"), "state": "PAUSED", "msg": "已继续", "level": "ok"}
        state["log"].append(entry)
        state["log"] = state["log"][-300:]
        _write_state()
        print(f"[{entry['t']}] ▶ [PAUSED] 已继续", flush=True)


def wait(sec):
    """可中断延时: 期间响应暂停/停止。"""
    deadline = time.time() + sec
    while True:
        checkpoint()
        left = deadline - time.time()
        if left <= 0:
            return
        time.sleep(min(0.15, left))


def emit(st, msg, level="info"):
    checkpoint()

    with _lock:
        state["state"] = st
        entry = {"t": time.strftime("%H:%M:%S"), "state": st, "msg": msg, "level": level}
        state["log"].append(entry)
        state["log"] = state["log"][-300:]
        _write_state()
    tag = {"info": "ℹ", "ok": "✓", "warn": "⚠", "err": "✗", "buy": "💰"}.get(level, "·")
    print(f"[{entry['t']}] {tag} [{st}] {msg}", flush=True)


def match(img, tpath, roi=None, th=None):
    tpl = cv2.imread(tpath, 0)
    if tpl is None:
        return None, 0.0
    th_, tw = tpl.shape[:2]
    search, off = img, (0, 0)
    if roi:
        x1, y1, x2, y2 = roi
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(img.shape[1], x2), min(img.shape[0], y2)
        if x2 <= x1 or y2 <= y1:
            return None, 0.0
        search, off = img[y1:y2, x1:x2], (x1, y1)
    res = cv2.matchTemplate(search, tpl, cv2.TM_CCOEFF_NORMED)
    _, mv, _, ml = cv2.minMaxLoc(res)
    t = th if th is not None else THRESH
    if mv >= t:
        return (off[0] + ml[0] + tw/2, off[1] + ml[1] + th_/2), mv
    return None, mv


def bright(img, x, y, tw, th):
    x1, y1 = max(0, int(x - tw/2)), max(0, int(y - th/2))
    x2, y2 = min(img.shape[1], int(x + tw/2)), min(img.shape[0], int(y + th/2))
    if x2 <= x1 or y2 <= y1:
        return 0.0
    return float(np.mean(img[y1:y2, x1:x2]))


def capture(name="cap.png"):
    p = screencap(DEVICE, name)
    if not p:
        return None
    return cv2.imread(p, 0)


def read_gold(img=None):
    """OCR 顶栏金币,返回 int 或 None。"""
    if img is None:
        img = capture("gold.png")
    if img is None:
        return None
    crop = img[8:48, 740:875]
    big = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY) if big.ndim == 3 else big
    txt = pytesseract.image_to_string(gray, config="--psm 8 -c tessedit_char_whitelist=0123456789,").strip()
    m = re.search(r"\d{1,3}(?:,\d{3})+", txt)
    if m:
        return int(m.group(0).replace(",", ""))
    m = re.match(r"\d{4,}", txt.replace(",", ""))
    if m:
        return int(m.group(0))
    return None


def gold_gate(reason):
    emit("GOLD_CHECK", f"{reason}: 读取金币…")
    g = read_gold()
    with _lock:
        state["stats"]["gold_left"] = g
    if g is None:
        emit("GOLD_CHECK", "金币 OCR 失败,放行(不阻断)", "warn")
        return True
    if g < GOLD_MIN:
        emit("GOLD_BLOCK", f"✗ 金币 {g:,} < {GOLD_MIN:,},金币不够,不能继续刷!", "err")
        return False
    emit("GOLD_CHECK", f"金币 {g:,} ≥ {GOLD_MIN:,} 通过", "ok")
    return True


def refresh_dialog(img):
    """只认蓝色「确认」键——刷新弹窗特有。
    不能用取消按钮判断:购买弹窗的取消是同一素材(实测0.999),会误杀购买。"""
    if img is None:
        return None
    pt, sc = match(img, TPL["refresh_ok"], th=0.97)
    if pt:
        return ("ok", pt, sc)
    return None


def clear_stuck_dialog(reason):
    img = capture("stuck.png")
    d = refresh_dialog(img)
    if d is None:
        return False
    _, pt, sc = d
    # 刷新弹窗: 蓝色确认在右,取消在其左侧等距处
    tap(DEVICE, pt[0] - 214, pt[1])
    emit("BOOT", f"{reason}: 粘滞刷新弹窗,点「取消」解除 (score={sc:.3f})", "warn")
    wait(1.0)
    return True


def ensure_clean(reason=""):
    for _ in range(3):
        img = capture("clean.png")
        if refresh_dialog(img) is None:
            return True
        clear_stuck_dialog(reason)
    return refresh_dialog(capture("clean.png")) is None


def scan_screen(st_name, tag, bought, failed=None):
    emit(st_name, f"{tag} 开始扫描")
    img = capture(f"full_{tag}.png")
    if img is None:
        emit(st_name, f"{tag} 截屏失败", "err")
        return
    if refresh_dialog(img):
        ensure_clean(st_name)
        img = capture(f"full_{tag}.png")
        if img is None or refresh_dialog(img):
            raise RuntimeError("扫描中弹窗无法清除")
    h, w = img.shape[:2]
    # 挂起的购买弹窗: 中带亮确认键 → 点确认把这单完成
    pdt, psc = purchase_dialog_ok(img)
    if pdt:
        emit(st_name, f"发现挂起的购买弹窗 (score={psc:.3f}) → 点确认完成", "warn")
        tap(DEVICE, pdt[0], pdt[1])
        wait(2.0)
        tap(DEVICE, w * 0.18, h * 0.45)
        wait(0.6)
        img2 = capture(f"full_{tag}.png")
        if img2 is not None:
            img = img2
    for name, tpath, price in TARGETS:
        if name in bought:
            continue
        anchor, sc = match(img, tpath)
        if not anchor:
            emit(st_name, f"{tag} {name}: 未出现 (最高 {sc:.3f})")
            continue
        tpl = cv2.imread(tpath, 0); th_, tw = tpl.shape[:2]
        ax, ay = anchor
        b = bright(img, ax, ay, tw, th_)
        if b < BRIGHT_TH:
            emit(st_name, f"{tag} {name}: 已购变暗(亮度{b:.0f}) 跳过", "warn")
            continue
        # 预算闸: 本地余额(启动OCR - 已购扣减)快跌破阈值才补一次OCR
        gl = state["stats"]["gold_left"]
        if gl is not None and gl - price < GOLD_MIN:
            emit("GOLD_CHECK", f"本地余额 {gl:,} 扣 {price:,} 将低于 {GOLD_MIN:,},OCR 复核", "warn")
            g = read_gold()
            if g is not None:
                state["stats"]["gold_left"] = g
                with _lock:
                    _write_state()
            gl = state["stats"]["gold_left"]
            if gl is not None and gl - price < GOLD_MIN:
                emit("GOLD_BLOCK", f"✗ 金币 {gl:,} 不够买 {name} ({price:,}),停止!", "err")
                raise PermissionError("金币不足")
        emit("BUY_TAP", f"{tag} ⭐ 发现 {name} ({ax:.0f},{ay:.0f}) 分{sc:.3f} → 点购买", "buy")
        # 行内绿色「购买」按钮: 用 confirm_btn 找(书签/神秘同一套按钮素材)
        btn, bsc = match(img, TPL["confirm"],
                         (int(ax + 100), max(0, int(ay - 60)), w, min(h, int(ay + 80))))
        if btn and bright(img, btn[0], btn[1], 81, 45) < BRIGHT_TH:
            emit("BUY_CONFIRM", f"行内匹配处偏暗(亮度{bright(img, btn[0], btn[1], 81, 45):.0f}),弃用", "warn")
            btn, bsc = None, 0.0
        if btn:
            buy_pt = btn
            emit("BUY_CONFIRM", f"行内购买按钮命中 (score={bsc:.3f}) → ({buy_pt[0]:.0f},{buy_pt[1]:.0f})")
        else:
            buy_pt = (ax + 454, ay + 21)  # 实测固定偏移: 锚点 → 右侧按钮中心
            emit("BUY_CONFIRM", f"行内按钮未匹配({bsc:.3f}) → 兜底固定偏移 ({buy_pt[0]:.0f},{buy_pt[1]:.0f})", "warn")

        def poll_buy_dialog(seconds):
            """轮询等待购买弹窗(中带亮确认键出现)。"""
            deadline = time.time() + seconds
            while time.time() < deadline:
                wait(0.35)
                cand = capture("buy_popup.png")
                pt, _ = purchase_dialog_ok(cand)
                if pt:
                    return cand
            return None

        confirmed = False
        for attempt in (1, 2):
            tap(DEVICE, buy_pt[0], buy_pt[1])
            emit("BUY_CONFIRM", f"点购买按钮 (第{attempt}次),轮询等待弹窗…")
            popup = poll_buy_dialog(3.0)
            if popup is None:
                emit("BUY_CONFIRM", f"第{attempt}次未见弹窗", "warn")
                continue
            conf, csc = purchase_dialog_ok(popup)
            if conf:
                emit("BUY_CONFIRM", f"确认购买弹窗 (score={csc:.3f}) → 点确认", "buy")
                tap(DEVICE, conf[0], conf[1])
                confirmed = True
            else:
                fx, fy = w * 0.585, h * 0.705
                fb = bright(popup, fx, fy, 81, 45)
                if fb >= BRIGHT_TH:
                    emit("BUY_CONFIRM", f"确认键未匹配 → 点弹窗中带亮键 (亮度{fb:.0f})", "warn")
                    tap(DEVICE, fx, fy)
                    confirmed = True
                else:
                    emit("BUY_CONFIRM", "弹窗中带不够亮,不盲点", "err")
            if confirmed:
                break

        # 购买成功动画 → 点任意位置关提示
        if confirmed:
            emit("BUY_CONFIRM", "等待购买成功动画 2s…")
            wait(2.0)
            tap(DEVICE, w * 0.18, h * 0.45)
            wait(0.6)
            state["stats"]["gold"] += price
            if state["stats"]["gold_left"] is not None:
                state["stats"]["gold_left"] -= price
                with _lock:
                    _write_state()
            state["stats"]["bought"][name] = state["stats"]["bought"].get(name, 0) + 1
            emit("BUY_CONFIRM", f"✅ 买入 {name} 花费 {price:,} 累计 {state['stats']['gold']:,}", "buy")
            bought.add(name)
        else:
            emit("BUY_CONFIRM", f"⚠ {name} 两次都没弹出购买弹窗,未计账", "err")
            if failed is not None:
                failed.add(name)

        nxt = capture(f"full_{tag}.png")
        if nxt is not None:
            img = nxt


def purchase_dialog_ok(img):
    """购买弹窗判定/定位: 中带存在够亮的绿色确认键 → (坐标, 分数),否则 (None, 0)。
    亮度过闸: 暗屏背后的列表按钮同样能被模板匹配,只有弹窗键是亮的。"""
    if img is None:
        return None, 0.0
    h, w = img.shape[:2]
    conf, sc = match(img, TPL["confirm"],
                     (int(w*0.45), int(h*0.60), int(w*0.78), int(h*0.82)))
    if conf and bright(img, conf[0], conf[1], 81, 45) >= BRIGHT_TH:
        return conf, sc
    return None, 0.0


def enter_shop():
    """大厅 → (必要时点一下唤出隐藏的UI) → 找左侧「秘密商店」入口 → 点进商店。
    已在商店则直接通过。"""
    emit("LOBBY", "检查当前位置 (大厅 / 秘密商店)…")
    for attempt in range(1, 9):
        checkpoint()
        img = capture("where.png")
        if img is None:
            wait(1.0)
            continue
        if refresh_dialog(img):
            clear_stuck_dialog("LOBBY")
            continue
        # 挂起的购买弹窗挡住进店 → 点确认完成
        pdt, psc = purchase_dialog_ok(img)
        if pdt:
            emit("LOBBY", f"挂起的购买弹窗 → 点确认完成 (score={psc:.3f})", "warn")
            tap(DEVICE, pdt[0], pdt[1])
            wait(2.0)
            tap(DEVICE, img.shape[1] * 0.18, img.shape[0] * 0.45)
            wait(0.6)
            continue
        # 已在商店: 「立即更新」按钮只在商店出现
        pt, sc = match(img, TPL["shop_btn"], th=0.90)
        if pt:
            emit("ENTER_SHOP", f"已在秘密商店 (按钮命中 {sc:.3f}),位于列表顶部,开始正式运作", "ok")
            return
        # 在大厅且入口可见: 点击进入(可点区在图标上,不在文字)
        pt, sc = match(img, TPL["lobby_entry"], th=0.85)
        if pt:
            tx, ty = pt[0] - 53, pt[1] - 10  # 模板中心 → 图标中心
            emit("LOBBY", f"大厅找到「秘密商店」入口 ({tx:.0f},{ty:.0f}) 分{sc:.3f} → 点击进入", "ok")
            tap(DEVICE, tx, ty)
            wait(2.0)
            continue
        # 大厅UI自动隐藏: 点一下屏幕中央唤出头像/功能入口,再重试
        h, w = img.shape[:2]
        emit("LOBBY", f"入口不可见 (第{attempt}次),点屏幕中央唤出UI…", "warn")
        tap(DEVICE, w * 0.5, h * 0.45)
        wait(1.0)
    raise RuntimeError("无法进入秘密商店,请手动点开大厅UI后再点开始")


def scroll_to_top():
    """从列表底部拉回顶部(重试扫描前用)。"""
    img = capture("top.png")
    if img is None:
        return
    h, w = img.shape[:2]
    for _ in range(3):
        os.system(f"adb -s {DEVICE} shell input swipe {w//2} {int(h*0.30)} {w//2} {int(h*0.75)} 350")
        wait(0.45)
    wait(0.5)


def scroll_scan_all(bought, failed=None):
    """一共 6 个项目两屏装得下:第1屏 → 一次大力下滑 → 第2屏,不再多滑一次探底。"""
    scan_screen("SCAN1", "第1屏", bought, failed)
    emit("SWIPE", "下滑查看第2屏")
    img = capture("sv_1.png")
    if img is not None:
        sh, sw = img.shape[:2]
        swipe_up(DEVICE, sw, sh)
        wait(0.7)
    scan_screen("SCAN1", "第2屏", bought, failed)
    emit("SCAN1", "两屏检查完毕 (共 6 项,一次滑到底)", "ok")
    return 2


def do_refresh():
    if not ensure_clean("REFRESH_OPEN"):
        raise RuntimeError("弹窗清不掉")
    img = capture("pre_refresh.png")
    h, w = img.shape[:2]
    emit("REFRESH_OPEN", "点击 立即更新")
    tap(DEVICE, w*REFRESH_BTN[0], h*REFRESH_BTN[1])
    wait(0.55)
    emit("REFRESH_DIALOG", "检测刷新确认弹窗")
    dlg = capture("refresh_dialog.png")
    d = refresh_dialog(dlg)
    if d is None or d[0] != "ok":
        emit("REFRESH_DIALOG", f"未检测到确认弹窗 (返回={d}),不盲点", "err")
        return False
    emit("REFRESH_CONFIRM", f"弹窗确认命中 (score={d[2]:.3f}) → 点确认,花 3 钻", "buy")
    tap(DEVICE, w*REFRESH_CONFIRM[0], h*REFRESH_CONFIRM[1])
    state["stats"]["diamond"] += 3
    wait(0.4)
    emit("REFRESH_WAIT", "等待商品列表刷新…")
    t0 = time.time()
    while time.time() - t0 < 12:
        wait(0.75)
        now = capture("post_refresh.png")
        if now is None or now.shape != img.shape:
            continue
        d2 = refresh_dialog(now)
        if d2 and d2[0] == "ok":
            emit("REFRESH_CONFIRM", "弹窗仍在,补点确认", "warn")
            tap(DEVICE, d2[1][0], d2[1][1])
            state["stats"]["diamond"] += 3
            wait(1.0)
            continue
        if d2 is None:
            a = img[80:650, 650:1250]
            b = now[80:650, 650:1250]
            frac = float((cv2.absdiff(a, b) > 40).mean())
            if frac > 0.12:
                emit("REFRESH_WAIT", f"刷新完成 (商品区变化 {frac*100:.0f}%) 累计钻石 {state['stats']['diamond']}", "ok")
                return True
    emit("REFRESH_WAIT", "等待超时,进入下一轮", "warn")
    return True


def run_rounds(max_round):
    state["max_round"] = max_round
    with _lock:
        state["stats"] = {"gold": 0, "diamond": 0, "bought": {}, "gold_left": None}
        state["round"] = 0
    emit("BOOT", "状态机启动,检查连接", "ok")
    chk = os.popen(f"adb -s {DEVICE} shell echo ok").read()
    if "ok" not in chk:
        emit("ERROR", f"设备 {DEVICE} 未连接", "err")
        return
    ensure_clean("BOOT")
    enter_shop()  # 必须先进店,顶栏才有金币可读
    if not gold_gate("进店后"):
        return

    for r in range(1, max_round + 1):
        state["round"] = r
        emit("ROUND_DONE", f"===== 第 {r}/{max_round} 轮开始 =====", "ok")
        enter_shop()  # 首轮从大厅进入;之后各轮仅校验仍在商店
        bought = set()
        failed = set()
        screens = scroll_scan_all(bought, failed)
        for retry in range(1, 3):
            if not failed:
                break
            emit("ROUND_DONE", f"购买失败 {sorted(failed)} → 本轮不刷新, 回顶重扫 (第{retry}/2次)", "warn")
            wait(2.0)
            scroll_to_top()
            failed.clear()
            screens = scroll_scan_all(bought, failed)
        if failed:
            emit("ERROR", f"重试后仍买不到 {sorted(failed)} → 停止(不刷新,保留商品)", "err")
            return
        if not bought:
            emit("ROUND_DONE", f"本轮 {screens} 屏检查完毕,无目标购买")
        if r < max_round:
            ok = do_refresh()
            emit("ROUND_DONE", f"第 {r} 轮刷新{'成功' if ok else '失败'}", "ok" if ok else "warn")
        else:
            emit("ROUND_DONE", f"最后一屏也已滑到底检查 (共 {screens} 屏),本轮结束", "ok")
    emit("DONE", f"全部完成: 购买 {state['stats']['bought']} 金币 {state['stats']['gold']:,} 钻石 {state['stats']['diamond']}", "ok")


def worker():
    while not _cmd["shutdown"].is_set():
        if _cmd["start"].wait(timeout=0.4):
            _cmd["start"].clear()
            _cmd["pause"].clear()
            _cmd["stop"].clear()
            with _lock:
                state["running"] = True
                state["paused"] = False
            try:
                run_rounds(int(_cmd["rounds"]))
            except PermissionError as e:
                emit("GOLD_BLOCK", f"终止: {e}", "err")
            except Exception as e:
                _cmd["stop"].clear()
                _cmd["pause"].clear()
                if "停止" in str(e):
                    emit("DONE", "已手动停止", "warn")
                else:
                    emit("ERROR", f"{e}", "err")
                    traceback.print_exc()
            finally:
                with _lock:
                    state["running"] = False
                    state["paused"] = False
                    _write_state()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _ok(self, body, ctype="application/json; charset=utf-8"):
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/monitor.html"):
            with open("monitor.html", "rb") as f:
                self._ok(f.read(), "text/html; charset=utf-8")
        elif u.path == "/state.js":
            with _lock:
                _write_state()
            with open(STATE_FILE, "rb") as f:
                self._ok(f.read(), "application/javascript; charset=utf-8")
        elif u.path == "/api/start":
            rounds = int(q.get("rounds", ["5"])[0])
            rounds = max(1, min(rounds, 333))
            _cmd["rounds"] = rounds
            _cmd["pause"].clear()
            _cmd["stop"].clear()
            _cmd["start"].set()
            self._ok(json.dumps({"ok": True, "rounds": rounds}).encode())
        elif u.path == "/api/pause":
            _cmd["pause"].set()
            self._ok(b'{"ok":true}')
        elif u.path == "/api/resume":
            _cmd["pause"].clear()
            self._ok(b'{"ok":true}')
        elif u.path == "/api/stop":
            _cmd["stop"].set()
            _cmd["pause"].clear()
            self._ok(b'{"ok":true}')
        elif u.path == "/api/ping":
            self._ok(b'{"ok":true}')
        else:
            self.send_response(404)
            self._cors()
            self.end_headers()


if __name__ == "__main__":
    emit("IDLE", "状态机就绪,等待前端「开始」", "ok")
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    srv = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler)
    print(f"控制服务: http://127.0.0.1:{HTTP_PORT}/  (monitor.html 同端口可直接打开)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        _cmd["shutdown"].set()
