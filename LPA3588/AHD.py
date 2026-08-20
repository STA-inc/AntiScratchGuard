import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
import sys
import os
import time
import threading
import logging
import shutil
from datetime import datetime

# 初始化 GStreamer
Gst.init(None)

# 设置时区
os.environ['TZ'] = 'Asia/Shanghai'
try:
    time.tzset()
except AttributeError:
    pass

disk_clean_lock = threading.Lock()

def check_and_clean_sdcard(mount_point="/mnt/sdcard", min_free_gb=1.5):
    """检查磁盘空间，防止空间不足（保护 120s 内新建的活跃文件）"""
    if not disk_clean_lock.acquire(blocking=False):
        return

    try:
        if not os.path.exists(mount_point):
            logging.error(f"存储路径 {mount_point} 不存在！")
            return

        if not os.path.ismount(mount_point):
            print(f"[Disk 警告] {mount_point} 未挂载外部存储设备，数据将写入主板闪存！")

        total, used, free = shutil.disk_usage(mount_point)
        free_gb = free / (1024 ** 3)

        if free_gb < min_free_gb:
            print(f"[Disk] 磁盘剩余空间 ({free_gb:.2f} GB) 低于安全阈值 ({min_free_gb} GB)，清理旧文件...")

            mp4_files = []
            now = time.time()
            for root, dirs, files in os.walk(mount_point):
                for file in files:
                    if file.endswith(".mp4"):
                        full_path = os.path.join(root, file)
                        try:
                            mtime = os.path.getmtime(full_path)
                            if (now - mtime) > 120:
                                mp4_files.append((full_path, mtime))
                        except Exception:
                            pass

            mp4_files.sort(key=lambda x: x[1])

            for file_path, _ in mp4_files:
                try:
                    os.remove(file_path)
                    print(f"[Disk] 已清理旧视频分段: {file_path}")
                except Exception as e:
                    logging.error(f"删除旧文件 {file_path} 失败: {e}")

                _, _, current_free = shutil.disk_usage(mount_point)
                if (current_free / (1024 ** 3)) >= min_free_gb:
                    print(f"[Disk] 清理完成，可用空间: {current_free / (1024 ** 3):.2f} GB")
                    break
    except Exception as e:
        logging.error(f"检查 SD 卡空间发生异常: {e}")
    finally:
        disk_clean_lock.release()


def _record_camera_worker(cam_id, segment_duration, stop_event):
    """
    单个摄像头录制的工作线程：通过循环分段录制替代 splitmuxsink
    :param cam_id: 摄像头 ID (1-8)
    :param segment_duration: 每个分段视频的时长（秒），例如 60 秒
    :param stop_event: 全局停止事件
    """
    out_dir = f"/mnt/sdcard/{cam_id}"
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as e:
        logging.error(f"摄像头 {cam_id}: 创建目录 {out_dir} 失败: {e}")
        return

    # 匹配硬件设备节点
    if 1 <= cam_id <= 4:
        device_node = f"/dev/video{10 + cam_id}"
    elif 5 <= cam_id <= 8:
        device_node = f"/dev/video{8 - cam_id}"
    else:
        return

    print(f"[AHD] 启动摄像头通道 {cam_id} ({device_node})...")
    last_disk_check_time = 0

    # 循环切片录制，直到收到全局停止信号
    while not stop_event.is_set():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = f"{out_dir}/rec_{cam_id}_{timestamp}.mp4"

        pipeline_str = (
            f'v4l2src device={device_node} io-mode=dmabuf ! '
            'video/x-raw,format=NV12,width=1920,height=1080,framerate=30/1 ! '
            'videoconvert ! '
            'clockoverlay time-format="%Y-%m-%d  %H:%M:%S" font-desc="Sans 20" '
            'halignment=center valignment=bottom shaded-background=false ! '
            'videoconvert ! '
            'video/x-raw,format=NV12 ! '
            'queue max-size-buffers=30 max-size-bytes=0 max-size-time=0 ! '
            'mpph264enc rc-mode=vbr bps=4000000 gop=60 ! '
            'h264parse ! '
            'mp4mux fragment-duration=2000 ! '
            f'filesink location={output_file}'
        )

        try:
            pipeline = Gst.parse_launch(pipeline_str)
        except Exception as e:
            logging.error(f"摄像头 {cam_id} 管道解析错误: {e}")
            time.sleep(2)
            continue

        bus = pipeline.get_bus()

        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            logging.error(f"摄像头 {cam_id} 无法启动，硬件可能未连接或节点被占用。")
            time.sleep(3)
            continue

        segment_start_time = time.time()
        eos_sent = False
        pipeline_failed = False

        # 当前分段的生命周期循环
        while not stop_event.is_set():
            msg = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)

            if msg:
                if msg.type == Gst.MessageType.EOS:
                    print(f"[AHD] 摄像头 {cam_id}: 当前分段已安全保存 -> {output_file}")
                    break
                elif msg.type == Gst.MessageType.ERROR:
                    err, debug = msg.parse_error()
                    logging.error(f"摄像头 {cam_id} 发生错误: {err.message}")
                    pipeline_failed = True
                    break

            # 周期性检查磁盘空间（每 10 秒）
            now = time.time()
            if now - last_disk_check_time >= 10:
                last_disk_check_time = now
                check_and_clean_sdcard(mount_point="/mnt/sdcard", min_free_gb=1.5)

            # 达到单段时长限制，主动注入 EOS 准备切分下一个文件
            if not eos_sent and (time.time() - segment_start_time) >= segment_duration:
                pipeline.send_event(Gst.Event.new_eos())
                eos_sent = True

        # 如果收到全局退出信号但还没发过 EOS，主动注入 EOS 结束当前片段
        if stop_event.is_set() and not eos_sent:
            pipeline.send_event(Gst.Event.new_eos())
            # 等待最后的 EOS 消息落地
            while True:
                msg = bus.timed_pop_filtered(200 * Gst.MSECOND, Gst.MessageType.EOS)
                if msg or pipeline_failed:
                    break

        # 释放当前管道资源
        pipeline.set_state(Gst.State.NULL)

        if pipeline_failed:
            time.sleep(2)  # 出错后稍作等待再重试

        # 如果全局要求停止，则退出大循环
        if stop_event.is_set():
            break

    print(f"[AHD] 摄像头通道 {cam_id} 录制线程已安全退出。")


def start_multi_ahd_recording(cam_array, stop_event, segment_duration=60, min_free_gb=1.5):
    """
    启动多路 AHD 录制
    :param cam_array: 长度为 8 的列表，如 [1, 1, 0, ...]
    :param stop_event: 控制全体线程退出的 threading.Event()
    :param segment_duration: 每个 MP4 切片文件的时长（秒），默认 60 秒
    :param min_free_gb: 磁盘空间最低保留阈值
    """
    if len(cam_array) != 8:
        return []

    # 录像前预检查 SD 卡
    check_and_clean_sdcard(mount_point="/mnt/sdcard", min_free_gb=min_free_gb)

    threads = []
    for idx, is_enabled in enumerate(cam_array):
        if is_enabled == 1:
            cam_id = idx + 1
            t = threading.Thread(
                target=_record_camera_worker,
                args=(cam_id, segment_duration, stop_event),
                name=f"AHD_Cam_{cam_id}"
            )
            threads.append(t)
            t.start()

    return threads