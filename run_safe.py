"""安全购买版:扫描两屏、购买书签/奖牌,但不点击刷新按钮。
每轮结束后等待画面变化(用户手动刷新),共 ROUNDS 轮。"""
import os
import sys
import time
import cv2
import numpy as np

os.chdir(os.path.dirname(os.path.abspath(__file__)))
from auto_shop_gui import tap, swipe_up, screencap  # 复用底层函数(define 于 GUI 模块)

DEVICE = "127.0.0.1:5555"
THRESH = 0.85
BRIGHT_TH = 70
ROUNDS = 5

TARGETS = [
    ("誓约书签", "Feature_screenshot/bookmark.png", 184000),
    ("神秘奖牌", "Feature_screenshot/mystic.png", 280000),
]
BUY_BTN = "Feature_screenshot/buy_btn.png"
CONFIRM_BTN = "Feature_screenshot/confirm_btn.png"

def log(msg):
    print(msg, flush=True)

def match(img, tpath, roi=None):
    tpl = cv2.imread(tpath, 0)
    if tpl is None:
        return None, 0.0
    th, tw = tpl.shape[:2]
    search, off = img, (0, 0)
    if roi:
        x1, y1, x2, y2 = roi
        search = img[max(0,y1):y2, max(0,x1):x2]
        off = (x1, y1)
    if search.size == 0:
        return None, 0.0
    res = cv2.matchTemplate(search, tpl, cv2.TM_CCOEFF_NORMED)
    _, mv, _, ml = cv2.minMaxLoc(res)
    if mv >= THRESH:
        return (off[0] + ml[0] + tw/2, off[1] + ml[1] + th/2), mv
    return None, mv

def brightness(img, x, y, tw, th):
    x1, y1 = max(0, int(x-tw/2)), max(0, int(y-th/2))
    x2, y2 = min(img.shape[1], int(x+tw/2)), min(img.shape[0], int(y+th/2))
    if x2 <= x1 or y2 <= y1:
        return 0
    return float(np.mean(img[y1:y2, x1:x2]))

def steal_params():
    # 覆写 global stop_flag 防干扰;直接 import 函数即可
    return None

def scan_screen(tag, bought):
    p = screencap(DEVICE, f"run_{tag}.png")
    if not p:
        log(f"  [{tag}] 截屏失败"); return False
    img = cv2.imread(p, 0)
    if img is None:
        log(f"  [{tag}] 读图失败"); return False
    h, w = img.shape[:2]
    did = False
    for name, tpath, price in TARGETS:
        if name in bought:
            continue
        anchor, sc = match(img, tpath)
        if not anchor:
            log(f"  [{tag}] {name}: 未发现 (最高分 {sc:.3f})")
            continue
        tpl = cv2.imread(tpath, 0)
        th, tw = tpl.shape[:2]
        ax, ay = anchor
        b = brightness(img, ax, ay, tw, th)
        if b < BRIGHT_TH:
            log(f"  [{tag}] {name}: 带 0x 点亮端点亮度 {b:.0f} < 70 ~ 已购/变暗，跳过")
            continue
        log(f"  [{tag}] 发现 {name} 中心=({ax:.0f},{ay:.0f}) 置信度={sc:.3f} 亮度={b:.0f} → 购买")
        btn, _ = match(img, BUY_BTN, (int(ax+120), int(ay-50), int(ax+500), int(ay+50)))
        if btn:
            tap(DEVICE, btn[0], btn[1])
        else:
            tap(DEVICE, ax + 200, ay)
        time.sleep(1.2)
        pp = screencap(DEVICE, "popup.png")
        confirmed = False
        popup = cv2.imread(pp, 0) if pp else None
        if popup is not None:
            ph, pw = popup.shape[:2]
            conf, _ = match(popup, CONFIRM_BTN, (int(pw*0.40), int(ph*0.55), pw, int(ph*0.95)))
            if conf:
                tap(DEVICE, conf[0], conf[1])
                confirmed = True
        stats["gold"] += price
        stats[name] = stats.get(name, 0) + 1
        log(f"  [{tag}] ✅ 购买 {name}{'(确认弹窗)' if confirmed else '(兜底点击)'}，计花 {price:,} 金币 (累计 {stats['gold']:,})")
        bought.add(name)
        did = True
        time.sleep(1.0)
        p2 = screencap(DEVICE, f"run_{tag}.png")
        if p2:
            img = cv2.imread(p2, 0) or img
    return did

def wait_refresh(prev_gray, timeout=180):
    log("  → 请手动点击「立即更新」刷新商店，我等画面变化…")
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(3)
        p = screencap(DEVICE, "watch.png")
        img = cv2.imread(p, 0)
        if img is None:
            continue
        if img.shape != prev_gray.shape:
            log("  画面改变了 → 进入下一轮"); return img
        diff = cv2.absdiff(img, prev_gray)
        frac = float((diff > 40).mean())
        if frac > 0.05:
            log(f"  检测到画面变化({frac*100:.0f}%) → 进入下一轮")
            return img
    log("  ⏱ 等待超时，直接进入下一轮")
    return None

stats = {"gold": 0}
for r in range(1, ROUNDS + 1):
    log(f"\n========== 第 {r}/{ROUNDS} 轮 ==========")
    bought = set()
    scan_screen("第一屏", bought)
    p = screencap(DEVICE, f"scroll_{r}.png")
    if p:
        img = cv2.imread(p, 0)
        if img is not None:
            sh, sw = img.shape[:2]
            swipe_up(DEVICE, sw, sh)
            time.sleep(0.8)
    scan_screen("第二屏", bought)
    last = screencap(DEVICE, f"last_{r}.png")
    base = cv2.imread(last, 0) if last else None
    if r < ROUNDS and base is not None:
        wait_refresh(base)
    elif r < ROUNDS:
        log("  (无基准图，跳过等待)")
log(f"\n===== 完成 {ROUNDS} 轮：共购买 {sum(v for k,v in stats.items() if k!='gold')} 件，"
    f"花金币 {stats['gold']:,} =====")
