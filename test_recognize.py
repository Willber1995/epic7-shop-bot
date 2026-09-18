"""只识别、不点击的安全测试脚本。用法:
python test_recognize.py [次数] [间隔秒]
每次截屏后打印识别结果,绝不点击。"""
import os
import sys
import time
import cv2

os.chdir(os.path.dirname(os.path.abspath(__file__)))

TEMPLATES = [
    ("誓约书签", "Feature_screenshot/bookmark.png", 184000),
    ("神秘奖牌", "Feature_screenshot/mystic.png", 280000),
    ("购买按钮", "Feature_screenshot/buy_btn.png", 0),
    ("确认按钮", "Feature_screenshot/confirm_btn.png", 0),
]
DEVICE = "127.0.0.1:5555"
THRESH = 0.85

def snap(name):
    if os.path.exists(name):
        os.remove(name)
    os.system(f"adb -s {DEVICE} shell screencap /sdcard/t.png")
    os.system(f"adb -s {DEVICE} pull /sdcard/t.png {name} > /dev/null 2>&1")

def match(img, tpl_path):
    tpl = cv2.imread(tpl_path, 0)
    if tpl is None:
        return None, 0.0
    th, tw = tpl.shape[:2]
    res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
    _, mv, _, ml = cv2.minMaxLoc(res)
    if mv >= THRESH:
        return (ml[0] + tw // 2, ml[1] + th // 2), mv
    return None, mv

count = int(sys.argv[1]) if len(sys.argv) > 1 else 1
gap = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0

for i in range(1, count + 1):
    path = f"test_{i}.png"
    snap(path)
    img = cv2.imread(path, 0)
    if img is None:
        print(f"[{i}] 截屏失败")
        continue
    h, w = img.shape[:2]
    print(f"[{i}] 截图 {w}x{h}")
    found = False
    for name, tpath, _ in TEMPLATES:
        pt, score = match(img, tpath)
        if pt:
            print(f"    [发现 {name}] 中心 {pt}  置信度 {score:.3f}")
            cv2.rectangle(img, (pt[0]-70, pt[1]-35), (pt[0]+70, pt[1]+35), 255, 2)
            found = True
        else:
            print(f"    [未发现 {name}] 最高置信度 {score:.3f}")
    if found:
        out = path.replace(".png", "_marked.png")
        cv2.imwrite(out, img)
        print(f"    → 带框图已保存: {out}")
    if i < count:
        time.sleep(gap)
