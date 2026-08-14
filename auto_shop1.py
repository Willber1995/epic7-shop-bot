import os
import time
import cv2
import numpy as np

# ========== 必须配置的参数 ==========
DEVICE_ID = "127.0.0.1:5557"

# 你准备好的两个锚点图片
ANCHOR_TEMPLATE_1 = "bookmark.png"   # 誓约书签（星星或文字）
ANCHOR_TEMPLATE_2 = "mystic.png"     # 神秘奖牌（刚才那张截图保存为这个）
BUY_BTN_TEMPLATE = "buy_btn.png"     # 商店列表里的绿色购买按钮
CONFIRM_BTN_TEMPLATE = "confirm_btn.png" # 弹窗里的纯绿色+购买文字的按钮
# ===================================

# ========== 可微调参数 ==========
MAX_REFRESH = 200
REFRESH_WAIT = 2.5
SWIPE_WAIT = 1.0        # 滑动后等待画面定格的常规时间
MATCH_THRESHOLD = 0.85

# ★★★ 亮度过滤参数 ★★★
BRIGHTNESS_THRESHOLD = 70  # 如果目标图标亮度低于 70，认为是"已购买变暗"，直接跳过

FALLBACK_OFFSET_X = 200  
POPUP_BASE_X_RATIO = 0.58
POPUP_BASE_Y_RATIO = 0.63
POPUP_X_OFFSET = 0
POPUP_Y_OFFSET = -10
# ===================================


def adb_cmd(cmd):
    os.system(f"adb -s {DEVICE_ID} {cmd}")

def tap(x, y):
    print(f"📌 点击坐标：({int(x)}, {int(y)})")
    adb_cmd(f"shell input tap {int(x)} {int(y)}")

def swipe_up(w, h):
    start = int(h * 0.75)
    end = int(h * 0.25)
    adb_cmd(f"shell input swipe {w//2} {start} {w//2} {end} 600")

def screencap(path="screen.png"):
    adb_cmd("shell screencap /sdcard/temp.png")
    os.system(f"adb -s {DEVICE_ID} pull /sdcard/temp.png {path} >nul 2>nul")
    return path

def get_region_brightness(gray_img, x, y, w, h):
    """计算目标区域的平均亮度，0-255"""
    x1 = max(0, int(x - w / 2))
    y1 = max(0, int(y - h / 2))
    x2 = min(gray_img.shape[1], int(x + w / 2))
    y2 = min(gray_img.shape[0], int(y + h / 2))
    if x2 <= x1 or y2 <= y1: return 0
    return float(np.mean(gray_img[y1:y2, x1:x2]))

def match_template(img_gray, tpl_path, thresh, search_roi=None):
    tpl = cv2.imread(tpl_path, 0)
    if tpl is None:
        print(f"❌ 找不到模板文件：{tpl_path}")
        return None
    th, tw = tpl.shape[:2]

    if search_roi:
        x1, y1, x2, y2 = search_roi
        y1, y2 = max(0, y1), min(img_gray.shape[0], y2)
        x1, x2 = max(0, x1), min(img_gray.shape[1], x2)
        if y2 <= y1 or x2 <= x1: return None

        roi = img_gray[y1:y2, x1:x2]
        res = cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val >= thresh:
            return (x1 + max_loc[0] + tw/2, y1 + max_loc[1] + th/2)
    else:
        res = cv2.matchTemplate(img_gray, tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val >= thresh:
            return (max_loc[0] + tw/2, max_loc[1] + th/2)
    return None


def try_buy_target(img_gray, w, h, name, tpl_path, bought_types):
    """尝试识别并购买特定目标，返回是否买到了"""
    # 如果在当前轮次已经买过这个类型，直接跳过
    if name in bought_types:
        return False

    anchor = match_template(img_gray, tpl_path, MATCH_THRESHOLD)
    if not anchor:
        return False

    ax, ay = anchor
    # 亮度过滤（检查是否已经变暗）
    tpl = cv2.imread(tpl_path, 0)
    th, tw = tpl.shape[:2]
    brightness = get_region_brightness(img_gray, ax, ay, tw, th)
    if brightness < BRIGHTNESS_THRESHOLD:
        print(f"⚠️ 发现已购买变暗的 {name}，跳过")
        return False

    # 找右侧的购买按钮
    roi_x1, roi_x2 = max(0, int(ax + 120)), min(w, int(ax + 500))
    roi_y1, roi_y2 = max(0, int(ay - 50)), min(h, int(ay + 50))
    
    btn = match_template(img_gray, BUY_BTN_TEMPLATE, MATCH_THRESHOLD, (roi_x1, roi_y1, roi_x2, roi_y2))
    if btn:
        print(f"✅ 找到 {name} 列表购买按钮")
        tap(btn[0], btn[1])
    else:
        fallback_x = ax + FALLBACK_OFFSET_X
        print(f"⚠️ {name} 列表按钮没识别，使用兜底点击 ({fallback_x}, {ay})")
        tap(fallback_x, ay)

    # 等待弹窗并购买
    time.sleep(1.8)
    screencap("popup.png")
    popup_gray = cv2.imread("popup.png", 0)
    if popup_gray is not None:
        pw, ph = popup_gray.shape[1], popup_gray.shape[0]
        search_area = (int(pw * 0.40), int(ph * 0.55), pw, int(ph * 0.95))
        confirm_btn = match_template(popup_gray, CONFIRM_BTN_TEMPLATE, MATCH_THRESHOLD, search_area)
        if confirm_btn:
            print(f"🎯 锁定 {name} 弹窗购买按钮！")
            tap(confirm_btn[0], confirm_btn[1])
            time.sleep(0.5)
            bought_types.add(name)
            return True

    # 兜底
    print("⚠️ 启用最终保底比例坐标...")
    backup_x = w * POPUP_BASE_X_RATIO + POPUP_X_OFFSET
    backup_y = h * POPUP_BASE_Y_RATIO + POPUP_Y_OFFSET
    tap(backup_x, backup_y)
    time.sleep(0.5)
    bought_types.add(name)
    return True


def scan_screen_and_buy_all(img_gray, w, h, targets, bought_types, screen_name):
    """扫描单屏，将可买的目标全部买完"""
    any_bought = False
    while True:
        # 轮流尝试购买所有目标
        bought_this_loop = False
        for name, tpl_path in targets:
            if try_buy_target(img_gray, w, h, name, tpl_path, bought_types):
                print(f"✅ {screen_name} 成功购买 {name}")
                bought_this_loop = True
                any_bought = True
                break  # 买了一个之后，立刻重新截屏，防止页面变化导致匹配出错
        
        # 如果这一轮什么都没买到，说明当前页彻底空了
        if not bought_this_loop:
            print(f"🔍 {screen_name} 未发现可购买目标")
            break
            
    return any_bought


def main():
    print("===== 第七史诗商店（双目标扫描版）启动 =====")
    
    # 配置目标列表
    targets = [
        ("誓约书签", ANCHOR_TEMPLATE_1),
        ("神秘奖牌", ANCHOR_TEMPLATE_2)
    ]
    
    refresh_count = 0

    while refresh_count < MAX_REFRESH:
        refresh_count += 1
        print(f"\n---------- 第 {refresh_count} 轮 ----------")
        bought_types = set()  # 记录本轮已买类型

        # 第一屏
        screencap("screen.png")
        img = cv2.imread("screen.png", 0)
        h, w = img.shape[:2]
        bought_1 = scan_screen_and_buy_all(img, w, h, targets, bought_types, "第一屏")

        if bought_1:
            print("⏳ 购买成功，强制等待 1.5 秒让界面恢复稳定...")
            time.sleep(1.5)

        print("⬇️ 正在下滑...")
        swipe_up(w, h)
        time.sleep(SWIPE_WAIT)

        # 第二屏
        screencap("screen.png")
        img = cv2.imread("screen.png", 0)
        h, w = img.shape[:2]
        bought_2 = scan_screen_and_buy_all(img, w, h, targets, bought_types, "第二屏")

        if bought_2:
            print("⏳ 购买成功，强制等待 1.5 秒让界面恢复稳定...")
            time.sleep(1.5)

        # 刷新
        print("🔄 两屏检查完毕，执行刷新")
        tap(w * 0.22, h * 0.92)
        time.sleep(1)
        tap(w * POPUP_BASE_X_RATIO + POPUP_X_OFFSET, h * POPUP_BASE_Y_RATIO + POPUP_Y_OFFSET)
        print("💎 刷新成功，等待加载...")
        time.sleep(REFRESH_WAIT)

    print("\n===== 所有轮次结束 =====")

if __name__ == "__main__":
    main()