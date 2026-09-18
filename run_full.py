"""全自动 5 轮:扫描两屏 → 购买书签/奖牌 → 脚本点「立即更新」→ 确认弹窗 → 进入下一轮。"""
import os
import time
import cv2
import numpy as np

os.chdir(os.path.dirname(os.path.abspath(__file__)))
from auto_shop_gui import tap, swipe_up, screencap

DEVICE = "127.0.0.1:5555"
THRESH = 0.85
BRIGHT_TH = 70
ROUNDS = 5

# 1280x720 实测比例
REFRESH_BTN = (0.176, 0.917)   # 立即更新按钮 (225,660)
CONFIRM_BTN_PT = (0.585, 0.640) # 消耗3钻弹窗的「确认」(749,461)

TARGETS = [
    ("誓约书签", "Feature_screenshot/bookmark.png", 184000),
    ("神秘奖牌", "Feature_screenshot/mystic.png", 280000),
]
BUY_TPL = "Feature_screenshot/buy_btn.png"

stats = {"gold": 0, "diamond": 0, "buy_counts": {}}

def log(m):
    print(m, flush=True)

def match(img, tpath, roi=None):
    tpl = cv2.imread(tpath, 0)
    if tpl is None:
        return None, 0.0
    th, tw = tpl.shape[:2]
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
    if mv >= THRESH:
        return (off[0] + ml[0] + tw/2, off[1] + ml[1] + th/2), mv
    return None, mv

def bright(img, x, y, tw, th):
    x1, y1 = max(0, int(x - tw/2)), max(0, int(y - th/2))
    x2, y2 = min(img.shape[1], int(x + tw/2)), min(img.shape[0], int(y + th/2))
    if x2 <= x1 or y2 <= y1:
        return 0.0
    return float(np.mean(img[y1:y2, x1:x2]))

def capture(name):
    p = screencap(DEVICE, name)
    if not p:
        return None
    return cv2.imread(p, 0)

def scan_screen(tag, bought):
    img = capture(f"full_{tag}.png")
    if img is None:
        log(f"  [{tag}] 截屏失败"); return
    h, w = img.shape[:2]
    for name, tpath, price in TARGETS:
        if name in bought:
            continue
        anchor, sc = match(img, tpath)
        if not anchor:
            log(f"  [{tag}] {name}: 未出现 (最高 {sc:.3f})"); continue
        tpl = cv2.imread(tpath, 0); th, tw = tpl.shape[:2]
        ax, ay = anchor
        b = bright(img, ax, ay, tw, th)
        if b < BRIGHT_TH:
            log(f"  [{tag}] {name}: 已购变暗(亮度{b:.0f}) 跳过"); continue
        log(f"  [{tag}] ⭐ 发现 {name} ({ax:.0f},{ay:.0f}) 分{sc:.3f} 亮度{b:.0f} → 购买")
        btn, _ = match(img, BUY_TPL, (int(ax+120), int(ay-50), int(ax+500), int(ay+50)))
        tap(DEVICE, btn[0], btn[1]) if btn else tap(DEVICE, ax+200, ay)
        time.sleep(1.3)
        popup = capture("buy_popup.png")
        confirmed = False
        dialog_open = False
        if popup is not None:
            # 弹窗会让画面明显变暗,变暗才算弹窗存在
            pre_c = img[int(h*0.4):int(h*0.9), int(w*0.3):w]
            pop_c = popup[int(h*0.4):int(h*0.9), int(w*0.3):w]
            dialog_open = float(pop_c.mean()) < float(pre_c.mean()) - 15
            ph, pw = popup.shape[:2]
            conf, _ = match(popup, "Feature_screenshot/confirm_btn.png",
                            (int(pw*0.40), int(ph*0.55), pw, int(ph*0.95)))
            if conf and dialog_open:
                tap(DEVICE, conf[0], conf[1]); confirmed = True
        if dialog_open and not confirmed:  # 兜底:仅弹窗存在时才点
            tap(DEVICE, w*0.93, h*0.83)
        stats["gold"] += price
        stats["buy_counts"][name] = stats["buy_counts"].get(name, 0) + 1
        log(f"  [{tag}] ✅ 买入 {name} ({price:,}金) 累计花费 {stats['gold']:,}")
        bought.add(name)
        time.sleep(1.1)
        img2 = capture(f"full_{tag}.png")
        if img2 is not None:
            img = img2

def do_refresh():
    img = capture("pre_refresh.png")
    if img is None:
        return
    h, w = img.shape[:2]
    log("  🔄 点击 立即更新 …")
    tap(DEVICE, w*REFRESH_BTN[0], h*REFRESH_BTN[1])
    time.sleep(1.3)
    dlg = capture("refresh_dialog.png")
    if dlg is not None:
        pre_c = img[int(h*0.3):int(h*0.75), int(w*0.25):int(w*0.75)]
        dlg_c = dlg[int(h*0.3):int(h*0.75), int(w*0.25):int(w*0.75)]
        if float(dlg_c.mean()) < float(pre_c.mean()) - 15:
            log("  🔄 弹窗已出现 → 点击 确认(消耗3钻)")
            tap(DEVICE, w*CONFIRM_BTN_PT[0], h*CONFIRM_BTN_PT[1])
            stats["diamond"] += 3
        else:
            log("  ⚠ 未检测到刷新弹窗,不盲点,跳过确认")
    else:
        log("  ⚠ 弹窗截屏失败,不盲点")
    # 等待列表刷新:第一屏商品区显著变化
    t0 = time.time()
    while time.time() - t0 < 15:
        time.sleep(2.5)
        now = capture("post_refresh.png")
        if now is None or now.shape != img.shape:
            continue
        # 只比对右侧商品列,排除左侧 NPC 动画
        a = img[80:650, 650:1250]
        b = now[80:650, 650:1250]
        frac = float((cv2.absdiff(a, b) > 40).mean())
        if frac > 0.12:
            log(f"  🔄 刷新完成 (商品区变化 {frac*100:.0f}%) 累计消耗钻石 {stats['diamond']}")
            return
    log(f"  ⚠ 未检测到明显变化,仍进入下一轮 (已计 {stats['diamond']} 钻)")

for r in range(1, ROUNDS + 1):
    log(f"\n========== 第 {r}/{ROUNDS} 轮 ==========")
    bought = set()
    scan_screen("第一屏", bought)
    img = capture(f"scroll_{r}.png")
    if img is not None:
        sh, sw = img.shape[:2]
        swipe_up(DEVICE, sw, sh)
        time.sleep(0.9)
    scan_screen("第二屏", bought)
    if not bought:
        log("  本轮无需购买")
    if r < ROUNDS:
        do_refresh()

log("\n===== 5 轮完成 =====")
log(f"购买明细: {stats['buy_counts']}")
log(f"共花金币 {stats['gold']:,} / 钻石 {stats['diamond']}")
