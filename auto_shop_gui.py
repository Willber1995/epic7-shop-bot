import os
import subprocess
import time
import json
import cv2
import numpy as np
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import queue

script_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(script_dir)

CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "device_id": "emulator-5556",
    "bookmark_template": "Feature_screenshot/bookmark.png",
    "mystic_template": "Feature_screenshot/mystic.png",
    "buy_btn_template": "Feature_screenshot/buy_btn.png",
    "confirm_btn_template": "Feature_screenshot/confirm_btn.png",
    "max_refresh": 333
}

log_queue = queue.Queue()
stop_flag = False

stats = {
    "gold": 0,
    "diamond": 0,
    "current_round": 0,
    "max_round": 333,
    "bookmark_count": 0,
    "mystic_count": 0
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v
            return cfg
        except:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except:
        pass

# ================== 底层自动化函数 ==================
def adb_cmd(device_id, cmd):
    full_cmd = f"adb -s {device_id} {cmd}"
    if os.name == 'nt':
        # 抓取报错信息，防止静默失败导致假运行
        result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode != 0:
            print(f"⚠️【ADB 报错】设备 {device_id} 执行失败！\n命令：{cmd}\n报错：{result.stderr.strip()}")
            return False
    else:
        os.system(full_cmd)
    return True

def tap(device_id, x, y):
    adb_cmd(device_id, f"shell input tap {int(x)} {int(y)}")

def swipe_up(device_id, w, h):
    start = int(h * 0.75)
    end = int(h * 0.25)
    adb_cmd(device_id, f"shell input swipe {w//2} {start} {w//2} {end} 600")

def screencap(device_id, path="screen.png"):
    # 先删旧图，防止读到残留文件
    if os.path.exists(path):
        try:
            os.remove(path)
        except:
            pass

    if not adb_cmd(device_id, "shell screencap /sdcard/temp.png"):
        return None

    full_cmd = f"adb -s {device_id} pull /sdcard/temp.png {path}"
    if os.name == 'nt':
        result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode != 0:
            print(f"⚠️【ADB 报错】拉取截图失败！\n报错：{result.stderr.strip()}")
            return None
    else:
        os.system(full_cmd)

    if os.path.exists(path):
        return path
    else:
        print("⚠️ 截图文件未生成！")
        return None

def get_region_brightness(gray_img, x, y, w, h):
    x1 = max(0, int(x - w/2)); y1 = max(0, int(y - h/2))
    x2 = min(gray_img.shape[1], int(x + w/2)); y2 = min(gray_img.shape[0], int(y + h/2))
    if x2 <= x1 or y2 <= y1: return 0
    return float(np.mean(gray_img[y1:y2, x1:x2]))

def match_template(img_gray, tpl_path, thresh, search_roi=None):
    tpl = cv2.imread(tpl_path, 0)
    if tpl is None: return None
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

# ================== 核心业务线程 ==================
def auto_shop_worker(params):
    global stop_flag, stats
    stop_flag = False
    stats["gold"] = 0
    stats["diamond"] = 0
    stats["bookmark_count"] = 0
    stats["mystic_count"] = 0
    stats["current_round"] = 0
    stats["max_round"] = params["max_refresh"]

    device_id = params["device_id"]
    threshold = 0.85
    brightness_thresh = 70

    targets = [
        ("誓约书签", params["bookmark_template"], 184000),
        ("神秘奖牌", params["mystic_template"], 280000)
    ]
    buy_btn_tpl = params["buy_btn_template"]
    confirm_btn_tpl = params["confirm_btn_template"]

    log_queue.put("===== 自动化商店采购启动 =====")

    # 启动前检测ADB连接状态，连不上直接退出，不跑假循环
    check = subprocess.run("adb devices", shell=True, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if device_id not in check.stdout:
        log_queue.put(f"❌ 未检测到设备 {device_id}！请确保模拟器已开启，ADB连接正常。")
        log_queue.put("===== 结束 =====")
        return

    while stats["current_round"] < stats["max_round"] and not stop_flag:
        stats["current_round"] += 1
        log_queue.put(f"\n---------- 第 {stats['current_round']} 轮 ----------")
        bought_types = set()

        def try_buy_target(img_gray, w, h, name, tpl_path, price):
            if name in bought_types: return False
            
            anchor = match_template(img_gray, tpl_path, threshold)
            if not anchor: return False
            
            ax, ay = anchor
            tpl = cv2.imread(tpl_path, 0)
            th, tw = tpl.shape[:2]
            brightness = get_region_brightness(img_gray, ax, ay, tw, th)
            if brightness < brightness_thresh:
                log_queue.put(f"⚠️ 发现已购买变暗的 {name}，跳过")
                return False

            roi_x1, roi_x2 = max(0, int(ax + 120)), min(w, int(ax + 500))
            roi_y1, roi_y2 = max(0, int(ay - 50)), min(h, int(ay + 50))
            
            btn = match_template(img_gray, buy_btn_tpl, threshold, (roi_x1, roi_y1, roi_x2, roi_y2))
            if btn: tap(device_id, btn[0], btn[1])
            else: tap(device_id, ax + 200, ay)

            time.sleep(1.2)
            screencap(device_id, "popup.png")
            popup = cv2.imread("popup.png", 0)
            if popup is not None:
                pw, ph = popup.shape[1], popup.shape[0]
                search_area = (int(pw * 0.40), int(ph * 0.55), pw, int(ph * 0.95))
                confirm = match_template(popup, confirm_btn_tpl, threshold, search_area)
                if confirm:
                    tap(device_id, confirm[0], confirm[1])
                    stats["gold"] += price
                    if name == "誓约书签": stats["bookmark_count"] += 1
                    elif name == "神秘奖牌": stats["mystic_count"] += 1
                    log_queue.put(f"💰 购买 {name}，消耗 {price:,} 金币")
                    bought_types.add(name)
                    return True
            backup_x = w * 0.58; backup_y = h * 0.63 - 10
            tap(device_id, backup_x, backup_y)
            stats["gold"] += price
            if name == "誓约书签": stats["bookmark_count"] += 1
            elif name == "神秘奖牌": stats["mystic_count"] += 1
            log_queue.put(f"💰 购买 {name}（兜底），消耗 {price:,} 金币")
            bought_types.add(name)
            return True

        def scan_screen(screen_name):
            path = screencap(device_id)
            if path is None: return False
            img = cv2.imread(path, 0)
            if img is None: return False
            h, w = img.shape[:2]
            
            any_bought = False
            while True:
                bought = False
                for name, tpl_path, price in targets:
                    if try_buy_target(img, w, h, name, tpl_path, price):
                        bought = True
                        any_bought = True
                        break
                if not bought:
                    log_queue.put(f"🔍 {screen_name} 无更多目标")
                    break
            return any_bought

        b1 = scan_screen("第一屏")
        if b1:
            time.sleep(1.0)
            log_queue.put("⏳ 等待界面恢复...")

        log_queue.put("⬇️ 下滑查看下方...")
        tmp_path = screencap(device_id)
        if tmp_path is not None:
            tmp_img = cv2.imread(tmp_path, 0)
            if tmp_img is not None:
                sh, sw = tmp_img.shape[:2]
                swipe_up(device_id, sw, sh)
                time.sleep(0.7)

        b2 = scan_screen("第二屏")
        if b2:
            time.sleep(1.0)
            log_queue.put("⏳ 等待界面恢复...")

        log_queue.put("🔄 两屏扫完，执行刷新")
        img_path = screencap(device_id)
        if img_path is not None:
            img_r = cv2.imread(img_path, 0)
            if img_r is not None:
                h_r, w_r = img_r.shape[:2]
                tap(device_id, w_r * 0.22, h_r * 0.92)
                time.sleep(1)
                tap(device_id, w_r * 0.58, h_r * 0.63 - 10)
                stats["diamond"] += 3
                log_queue.put(f"💎 刷新成功，已消耗 3 钻石")
            else:
                tap(device_id, 1080*0.22, 2400*0.92)
                time.sleep(1)
                tap(device_id, 1080*0.58, 2400*0.63 - 10)
                stats["diamond"] += 3

        time.sleep(2.0)
        log_queue.put(("STATS_UPDATE", stats["gold"], stats["diamond"], stats["current_round"], stats["bookmark_count"], stats["mystic_count"]))

    if stop_flag:
        log_queue.put("\n⚠️ 已手动停止脚本")
    else:
        log_queue.put(f"\n🏁 圆满完成 {stats['max_round']} 次刷新！")
    log_queue.put(("STATS_UPDATE", stats["gold"], stats["diamond"], stats["current_round"], stats["bookmark_count"], stats["mystic_count"]))
    log_queue.put("===== 结束 =====")


# ================== GUI 界面 ==================
def update_log_and_stats(text_widget, gold_label, dia_label, round_label, bookmark_label, mystic_label, bm_rate_label, my_rate_label):
    while not log_queue.empty():
        msg = log_queue.get()
        if isinstance(msg, tuple) and msg[0] == "STATS_UPDATE":
            gold_val, dia_val, cur_round, bm_count, my_count = msg[1], msg[2], msg[3], msg[4], msg[5]
            gold_label.config(text=f"{gold_val:,}")
            dia_label.config(text=f"{dia_val:,}")
            round_label.config(text=f"{cur_round} / {stats['max_round']}")
            bookmark_label.config(text=f"{bm_count}")
            mystic_label.config(text=f"{my_count}")
            bm_rate = (bm_count / max(1, cur_round)) * 100
            my_rate = (my_count / max(1, cur_round)) * 100
            bm_rate_label.config(text=f"{int(bm_rate)}%")
            my_rate_label.config(text=f"{int(my_rate)}%")
        else:
            text_widget.insert(tk.END, msg + "\n")
            text_widget.see(tk.END)
    text_widget.after(100, update_log_and_stats, text_widget, gold_label, dia_label, round_label, bookmark_label, mystic_label, bm_rate_label, my_rate_label)

def start_script(entries, log_text, gold_label, dia_label, round_label, bookmark_label, mystic_label, bm_rate_label, my_rate_label, btn_start, btn_stop):
    global stop_flag
    params = {}
    try:
        params["device_id"] = entries["device_id"].get().strip()
        params["max_refresh"] = int(entries["max_refresh"].get())
        params["bookmark_template"] = "Feature_screenshot/bookmark.png"
        params["mystic_template"] = "Feature_screenshot/mystic.png"
        params["buy_btn_template"] = "Feature_screenshot/buy_btn.png"
        params["confirm_btn_template"] = "Feature_screenshot/confirm_btn.png"
    except Exception as e:
        messagebox.showerror("错误", f"参数格式错误: {e}")
        return
    
    save_config(params)
    stop_flag = False
    btn_start.config(state=tk.DISABLED)
    btn_stop.config(state=tk.NORMAL)
    log_text.delete(1.0, tk.END)
    t = threading.Thread(target=auto_shop_worker, args=(params,), daemon=True)
    t.start()

def stop_script(btn_start, btn_stop):
    global stop_flag
    stop_flag = True
    btn_stop.config(state=tk.DISABLED)
    btn_start.config(state=tk.NORMAL)

def build_gui():
    root = tk.Tk()
    root.title("第七史诗 秘密商店自动助手")
    root.geometry("700x720")
    root.resizable(False, False)

    config = load_config()
    entries = {}

    frame_setting = ttk.LabelFrame(root, text="基础设置")
    frame_setting.pack(fill=tk.X, padx=10, pady=5)
    
    row1 = ttk.Frame(frame_setting)
    row1.pack(fill=tk.X, padx=5, pady=5)
    ttk.Label(row1, text="设备 ID:").pack(side=tk.LEFT)
    entries["device_id"] = ttk.Entry(row1, width=20)
    entries["device_id"].insert(0, config["device_id"])
    entries["device_id"].pack(side=tk.LEFT, padx=5)
    ttk.Label(row1, text="功能选择:").pack(side=tk.LEFT, padx=(20, 5))
    combo = ttk.Combobox(row1, values=["秘密商店自动购买"], state="readonly", width=20)
    combo.current(0)
    combo.pack(side=tk.LEFT)

    row2 = ttk.Frame(frame_setting)
    row2.pack(fill=tk.X, padx=5, pady=5)
    ttk.Label(row2, text="最大刷新次数:").pack(side=tk.LEFT)
    entries["max_refresh"] = ttk.Entry(row2, width=10)
    entries["max_refresh"].insert(0, str(config["max_refresh"]))
    entries["max_refresh"].pack(side=tk.LEFT, padx=5)
    ttk.Label(row2, text="(跑满自动停止)", foreground="#888").pack(side=tk.LEFT, padx=5)

    frame_stats = ttk.LabelFrame(root, text="实时统计")
    frame_stats.pack(fill=tk.X, padx=10, pady=5)

    stats_frame = ttk.Frame(frame_stats)
    stats_frame.pack(pady=8)
    
    row_stats1 = ttk.Frame(stats_frame)
    row_stats1.pack(fill=tk.X, pady=(0, 6))
    ttk.Label(row_stats1, text="消耗金币:").pack(side=tk.LEFT, padx=10)
    gold_label = ttk.Label(row_stats1, text="0", font=("Arial", 12, "bold"), foreground="#E6A817")
    gold_label.pack(side=tk.LEFT, padx=5)

    ttk.Label(row_stats1, text="消耗钻石:").pack(side=tk.LEFT, padx=30)
    dia_label = ttk.Label(row_stats1, text="0", font=("Arial", 12, "bold"), foreground="#4D94FF")
    dia_label.pack(side=tk.LEFT, padx=5)

    ttk.Label(row_stats1, text="当前刷新:").pack(side=tk.LEFT, padx=30)
    round_label = ttk.Label(row_stats1, text=f"0 / {config['max_refresh']}", font=("Arial", 12, "bold"))
    round_label.pack(side=tk.LEFT, padx=5)

    row_stats2 = ttk.Frame(stats_frame)
    row_stats2.pack(fill=tk.X)
    
    bm_frame = ttk.Frame(row_stats2)
    bm_frame.pack(side=tk.LEFT, padx=10)
    ttk.Label(bm_frame, text="📕 誓约书签:").pack(side=tk.LEFT)
    bookmark_label = ttk.Label(bm_frame, text="0", font=("Arial", 12, "bold"), foreground="#E6A817")
    bookmark_label.pack(side=tk.LEFT, padx=5)
    ttk.Label(bm_frame, text="(书签概率:").pack(side=tk.LEFT)
    bm_rate_label = ttk.Label(bm_frame, text="0%", font=("Arial", 12, "bold"), foreground="#000000")
    bm_rate_label.pack(side=tk.LEFT)
    ttk.Label(bm_frame, text=")").pack(side=tk.LEFT)

    my_frame = ttk.Frame(row_stats2)
    my_frame.pack(side=tk.LEFT, padx=30)
    ttk.Label(my_frame, text="📘 神秘奖牌:").pack(side=tk.LEFT)
    mystic_label = ttk.Label(my_frame, text="0", font=("Arial", 12, "bold"), foreground="#E6A817")
    mystic_label.pack(side=tk.LEFT, padx=5)
    ttk.Label(my_frame, text="(奖牌概率:").pack(side=tk.LEFT)
    my_rate_label = ttk.Label(my_frame, text="0%", font=("Arial", 12, "bold"), foreground="#000000")
    my_rate_label.pack(side=tk.LEFT)
    ttk.Label(my_frame, text=")").pack(side=tk.LEFT)

    frame_log = ttk.LabelFrame(root, text="运行日志")
    frame_log.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
    log_text = scrolledtext.ScrolledText(frame_log, wrap=tk.WORD, font=("Microsoft YaHei", 9))
    log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    frame_btn = ttk.Frame(root)
    frame_btn.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=10)

    btn_start = ttk.Button(frame_btn, text="▶ 启动脚本", width=15, command=lambda: start_script(
        entries, log_text, gold_label, dia_label, round_label, bookmark_label, mystic_label, bm_rate_label, my_rate_label, btn_start, btn_stop
    ))
    btn_start.pack(side=tk.LEFT, padx=20)

    btn_stop = ttk.Button(frame_btn, text="⏹ 停止脚本", width=15, state=tk.DISABLED, command=lambda: stop_script(btn_start, btn_stop))
    btn_stop.pack(side=tk.LEFT, padx=20)
    
    update_log_and_stats(log_text, gold_label, dia_label, round_label, bookmark_label, mystic_label, bm_rate_label, my_rate_label)
    root.mainloop()

if __name__ == "__main__":
    build_gui()