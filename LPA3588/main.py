import logging
import json
import os
import sys
import time
import threading
import socket
import serial
import subprocess  # 【新增】用于启动外部进程
from datetime import datetime, timedelta

# ==================== 1. 统一日志配置（必须放在最顶部！） ====================
LOG_FILE = "run.log"
STATE_FILE = "alert_state.json"  # 告警状态存储文件

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    encoding="utf-8"
)

from AHD import start_multi_ahd_recording
from robot import send_ding_alert

SERIAL_PORT = "/dev/ttysWK2"
BAUD_RATE = 9600


# ==================== 核心新增：外网连通性检测函数 ====================
def check_internet_reachability(host="223.5.5.5", port=53, timeout=2):
    """检测外网 TCP 连通性（默认使用阿里 DNS 223.5.5.5）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return True
    except Exception:
        return False


def ensure_network_or_wait(max_wait_sec=20):
    """
    发送消息前的网络保障函数：
    若当前网络不通，则循环等待，最多阻塞 max_wait_sec 秒。
    如果超时仍不通，则返回 False（供上层写入错误日志）。
    """
    if check_internet_reachability():
        return True
    
    print(f"[Network] 当前网络未就绪，开始等待网络连接 (最多 {max_wait_sec} 秒)...")
    start_time = time.time()
    while time.time() - start_time < max_wait_sec:
        time.sleep(2)
        if check_internet_reachability():
            print(f"[Network] 网络已成功连通！耗时 {int(time.time() - start_time)} 秒。")
            return True
            
    print(f"[Network] 等待 {max_wait_sec} 秒后网络依然不通。")
    return False


# ==================== 辅助函数：JSON 状态文件读写 ====================
def load_alert_state(state_file=STATE_FILE):
    """从 JSON 文件加载状态，文件不存在或损坏时返回默认数据结构"""
    default_state = {
        "last_com_sent_time": None,
        "last_cam_sent_time": None,
        "pending_alerts": []
    }
    if not os.path.exists(state_file):
        return default_state
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return default_state
            data.setdefault("last_com_sent_time", None)
            data.setdefault("last_cam_sent_time", None)
            data.setdefault("pending_alerts", [])
            return data
    except Exception as e:
        print(f"[State] 读取 JSON 状态文件失败: {e}")
        return default_state


def save_alert_state(state, state_file=STATE_FILE):
    """将状态字典写入 JSON 文件"""
    try:
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[State] 保存 JSON 状态文件失败: {e}")


# ==================== 功能一：日志与 JSON 大小超限智能裁剪 ====================
def trim_log_file(log_file=LOG_FILE, max_mb=500, target_mb=400):
    """当日志大于 max_mb 时，优先删除最早的正常日志；若仍超线再删除最早的报错日志"""
    if not os.path.exists(log_file):
        return

    current_bytes = os.path.getsize(log_file)
    max_bytes = max_mb * 1024 * 1024
    target_bytes = target_mb * 1024 * 1024

    if current_bytes <= max_bytes:
        return

    print(f"[Log] {log_file} 大小为 {current_bytes / (1024 * 1024):.2f}MB (超过 {max_mb}MB)，启动智能裁剪...")

    try:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        error_keywords = [
            "ERROR", "WARNING", "串口通信失败", "串口故障", "无法打开",
            "摄像头报错", "摄像头故障", "AHD录制失败", "网络错误", "未发送", "补发成功", "通知已发送"
        ]

        def is_error_line(line):
            upper = line.upper()
            return any(kw in upper or kw in line for kw in error_keywords)

        line_bytes = [len(line.encode("utf-8")) for line in lines]
        total_bytes = sum(line_bytes)
        keep_flags = [True] * len(lines)

        for i in range(len(lines)):
            if total_bytes <= target_bytes:
                break
            if not is_error_line(lines[i]):
                keep_flags[i] = False
                total_bytes -= line_bytes[i]

        if total_bytes > target_bytes:
            for i in range(len(lines)):
                if total_bytes <= target_bytes:
                    break
                if keep_flags[i]:
                    keep_flags[i] = False
                    total_bytes -= line_bytes[i]

        with open(log_file, "w", encoding="utf-8") as f:
            for i in range(len(lines)):
                if keep_flags[i]:
                    f.write(lines[i])

        print(f"[Log] {log_file} 裁剪完成，当前大小: {total_bytes / (1024 * 1024):.2f}MB")
    except Exception as e:
        print(f"[Log] 裁剪日志失败: {e}")


def trim_json_file(state_file=STATE_FILE, max_mb=500, target_mb=400):
    """当 JSON 状态文件超过 max_mb 时，自动清空 pending_alerts 中最早的内容"""
    if not os.path.exists(state_file):
        return

    current_bytes = os.path.getsize(state_file)
    max_bytes = max_mb * 1024 * 1024
    target_bytes = target_mb * 1024 * 1024

    if current_bytes <= max_bytes:
        return

    try:
        state = load_alert_state(state_file)
        pending = state.get("pending_alerts", [])

        while pending and current_bytes > target_bytes:
            pop_count = max(1, len(pending) // 10)
            pending = pending[pop_count:]
            state["pending_alerts"] = pending

            serialized = json.dumps(state, ensure_ascii=False, indent=2).encode('utf-8')
            current_bytes = len(serialized)

        save_alert_state(state, state_file)
    except Exception as e:
        print(f"[State] 清理 JSON 状态文件失败: {e}")


# ==================== 功能二：日志健康检测与滞留报警判定 ====================
def scan_and_check_health(log_file=LOG_FILE, state_file=STATE_FILE, days=5, threshold=5):
    state = load_alert_state(state_file)
    pending_alerts = state.get("pending_alerts", [])

    def parse_dt(ts_str):
        if ts_str:
            try:
                return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        return None

    last_com_sent_dt = parse_dt(state.get("last_com_sent_time"))
    last_cam_sent_dt = parse_dt(state.get("last_cam_sent_time"))

    now = datetime.now()
    cutoff_time = now - timedelta(days=days)

    com_error_count = 0
    cam_error_count = 0

    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line_dt = None
                    if line.startswith("[") and "]" in line:
                        ts_str = line[1:line.find("]")]
                        try:
                            line_dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            line_dt = None

                    if line_dt is not None and line_dt < cutoff_time:
                        continue

                    if "串口通信失败" in line or "串口故障" in line or "无法打开 /dev/ttyS1" in line:
                        com_error_count += 1
                    if "摄像头报错" in line or "摄像头故障" in line or "AHD录制失败" in line:
                        cam_error_count += 1
        except Exception as e:
            print(f"[Log] 扫描日志健康度失败: {e}")

    time_now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    state_changed = False

    # 1. 串口故障超标判断
    if com_error_count >= threshold:
        has_pending_com = any(item["type"] == "COM" for item in pending_alerts)
        recently_notified = (last_com_sent_dt is not None and last_com_sent_dt >= cutoff_time)

        if not has_pending_com and not recently_notified:
            if ensure_network_or_wait(20):
                com_title = "系统硬件预警：串口通信频繁失败"
                com_text = f"""### 系统错误警报：COM 串口通信异常\n**检测时间:** {time_now_str}"""
                if send_ding_alert(com_title, com_text, is_markdown=True):
                    logging.info(f"【com口错误通知已发送】时间: {time_now_str}")
                    state["last_com_sent_time"] = time_now_str
                    state_changed = True
                else:
                    logging.error("【网络错误】发送串口频繁故障通知失败。")
                    pending_alerts.append({"type": "COM", "time": time_now_str})
                    state_changed = True
            else:
                logging.error("【网络错误】发送串口频繁故障通知失败：网络不通（等待超时）。")
                pending_alerts.append({"type": "COM", "time": time_now_str})
                state_changed = True

    # 2. 摄像头故障超标判断
    if cam_error_count >= threshold:
        has_pending_cam = any(item["type"] == "CAM" for item in pending_alerts)
        recently_notified = (last_cam_sent_dt is not None and last_cam_sent_dt >= cutoff_time)

        if not has_pending_cam and not recently_notified:
            if ensure_network_or_wait(20):
                cam_title = "系统硬件预警：摄像头采集频繁报错"
                cam_text = f"""### 系统错误警报：AHD 摄像头异常\n**检测时间:** {time_now_str}"""
                if send_ding_alert(cam_title, cam_text, is_markdown=True):
                    logging.info(f"【摄像头报错通知已发送】时间: {time_now_str}")
                    state["last_cam_sent_time"] = time_now_str
                    state_changed = True
                else:
                    logging.error("【网络错误】发送摄像头频繁报错通知失败。")
                    pending_alerts.append({"type": "CAM", "time": time_now_str})
                    state_changed = True
            else:
                logging.error("【网络错误】发送摄像头频繁报错通知失败：网络不通（等待超时）。")
                pending_alerts.append({"type": "CAM", "time": time_now_str})
                state_changed = True

    if state_changed:
        state["pending_alerts"] = pending_alerts
        save_alert_state(state, state_file)


# ==================== 功能三：网络正常时补发滞留通知 ====================
def try_resend_pending_alerts(state_file=STATE_FILE):
    state = load_alert_state(state_file)
    pending_alerts = state.get("pending_alerts", [])

    if not pending_alerts:
        return

    # 尝试连网 20 秒
    if not ensure_network_or_wait(20):
        logging.error("【网络错误】尝试补发历史错误报告失败：网络未就绪。")
        print("[Log] 网络未就绪，跳过本次历史错误补发。")
        return

    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"[Log] 检查到有 {len(pending_alerts)} 条滞留的错误通知，正在补发...")

    state_changed = False
    for item in list(pending_alerts):
        err_type = item["type"]
        orig_time = item["time"]

        title = "【历史补发】系统硬件预警"
        text = f"### [历史补发] 原始时间: {orig_time} | 补发时间: {now_str}"

        if send_ding_alert(title, text, is_markdown=True):
            if err_type == "COM":
                state["last_com_sent_time"] = now_str
            elif err_type == "CAM":
                state["last_cam_sent_time"] = now_str
            logging.info(f"[{err_type}历史报错补发成功]原时间: {orig_time}")
            pending_alerts.remove(item)
            state_changed = True
        else:
            logging.error(f"[网络错误]补发 {err_type} 历史报错失败。")

    if state_changed:
        state["pending_alerts"] = pending_alerts
        save_alert_state(state, state_file)


def try_open_serial(retries=5):
    for _ in range(retries):
        try:
            ser = serial.Serial(
                port=SERIAL_PORT, baudrate=BAUD_RATE,
                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE, timeout=1
            )
            ser.reset_input_buffer()
            return ser
        except Exception:
            time.sleep(1)
    return None


def main():
    def delayed_dialing():
        try:
            time.sleep(10)  # 延迟 10 秒
            print("[System] 延迟 10 秒后，开始启动 quectel-CM 拨号进程...")
            subprocess.Popen("quectel-CM &", shell=True)
            logging.info("已在 10 秒延迟后触发后台执行命令: quectel-CM &")
        except Exception as e:
            print(f"[System] 启动 quectel-CM 进程失败: {e}")
            logging.error(f"启动 quectel-CM 进程异常: {e}")

    # 使用线程启动，不阻塞主流程
    threading.Thread(target=delayed_dialing, daemon=True).start()

    trim_log_file(LOG_FILE, max_mb=500, target_mb=400)
    trim_json_file(STATE_FILE, max_mb=500, target_mb=400)
    scan_and_check_health(LOG_FILE, STATE_FILE, days=5, threshold=5)

    rec_start_dt = time.localtime()
    rec_start_time_str = time.strftime("%Y-%m-%d %H:%M:%S", rec_start_dt)
    rec_start_timestamp = time.time()

    print(f"[System] 监控程序启动，时间: {rec_start_time_str}，检测串口 {SERIAL_PORT}...")

    ser = try_open_serial(retries=5)

    # 串口异常：应急保底录像
    if not ser:
        print(f"[System] 首次打开串口失败！启动 3 分钟应急保底录像模式...")
        logging.warning(f"首次尝试打开串口 {SERIAL_PORT} 失败，转入应急录制。")

        emg_stop_event = threading.Event()
        test_cam_mask = [1, 1, 0, 0, 0, 0, 0, 0]
        emg_camera_threads = start_multi_ahd_recording(test_cam_mask, emg_stop_event)

        time.sleep(180)
        emg_stop_event.set()
        for t in emg_camera_threads:
            t.join(timeout=8)

        ser = try_open_serial(retries=5)
        if not ser:
            logging.error(f"【串口故障事件】串口通信失败 (无法打开 {SERIAL_PORT})")
            try_resend_pending_alerts(STATE_FILE)
            print("[System] 串口二次检测仍然异常，执行系统关机...")
            time.sleep(0.5)
            os.system("sync")
            os.system("poweroff")
            sys.exit(1)

    # 正常监听循环
    stop_recording_event = threading.Event()
    test_cam_mask = [1, 1, 0, 0, 0, 0, 0, 0]
    camera_threads = start_multi_ahd_recording(test_cam_mask, stop_recording_event)

    notify_received = False

    try:
        while True:
            line = ser.readline()
            if line:
                recv_msg = line.decode('utf-8', errors='ignore').strip()

                if "CMD_NOTIFY" in recv_msg or "CMD_SHUTDOWN_ALERT" in recv_msg:
                    notify_received = True
                    print("\n[Signal] >>> 收到震动通知信号 <<<")

                if "CMD_SHUTDOWN" in recv_msg or "CMD_SHUTDOWN_ALERT" in recv_msg:
                    print("\n[Signal] >>> 收到关机指令！执行保存与关机流程 <<<")

                    rec_end_dt = time.localtime()
                    rec_end_time_str = time.strftime("%Y-%m-%d %H:%M:%S", rec_end_dt)
                    duration_sec = round(time.time() - rec_start_timestamp, 2)

                    # 步骤 1: 停止录像并安全写盘
                    stop_recording_event.set()
                    for t in camera_threads:
                        t.join(timeout=8)

                    rec_log_msg = f"【录制事件】开始时间: {rec_start_time_str} | 结束时间: {rec_end_time_str} | 录制时长: {duration_sec} 秒"

                    # 步骤 2: 发送钉钉告警（带 20 秒网络检测防卡死）
                    if notify_received:
                        print("[Action] 2. 检查网络并推送告警...")
                        if ensure_network_or_wait(20):
                            markdown_text = f"### 哨兵警报：有物体靠近且车身发生震动\n**时间:** {rec_end_time_str}"
                            if send_ding_alert("车身异常警报", markdown_text, is_markdown=True):
                                logging.info(rec_log_msg)
                                logging.info(f"【通知发送事件】成功: {rec_end_time_str}")
                            else:
                                logging.error("【网络错误】钉钉推送服务返回失败。")
                                # 写入滞留列表
                                state = load_alert_state(STATE_FILE)
                                state["pending_alerts"].append({"type": "ALARM", "time": rec_end_time_str})
                                save_alert_state(state, STATE_FILE)
                        else:
                            # 20秒网络不通，写日志并存入 pending
                            logging.error("【网络错误】发送车身异常警报失败：网络不可用（超时）。")
                            state = load_alert_state(STATE_FILE)
                            state["pending_alerts"].append({"type": "ALARM", "time": rec_end_time_str})
                            save_alert_state(state, STATE_FILE)
                            print("[Log] 网络不通，已存入 pending_alerts")
                    else:
                        logging.info(rec_log_msg)

                    # 步骤 3: 尝试补发历史滞留报错
                    try_resend_pending_alerts(STATE_FILE)

                    # 步骤 4: 回传 ACK 给 ESP32
                    for _ in range(3):
                        ser.write(b"ACK_SHUTDOWN_READY\n")
                        ser.flush()
                        time.sleep(0.05)

                    # 步骤 5: 关机
                    time.sleep(0.5)
                    os.system("sync")
                    os.system("poweroff")
                    break

    except KeyboardInterrupt:
        stop_recording_event.set()
        for t in camera_threads:
            t.join(timeout=3)


if __name__ == "__main__":
    main()
