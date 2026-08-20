import machine
import time
from machine import Pin, I2C, UART

# =========================================================================
# 1. 硬件 GPIO 引脚映射
# =========================================================================
PIN_POWER = 2   # 5305 PMOS 光耦开关控制
PIN_RADAR = 6   # LD012 雷达 OUT
PIN_P2 = 5      # LD012 雷达 P2 (远距离)

PIN_SDA = 12    # LIS2DH12 SDA
PIN_SCL = 11    # LIS2DH12 SCL
PIN_INT1 = 13   # LIS2DH12 INT1 震动中断

UART_TX = 18    # 对 LPA3588 RX
UART_RX = 17    # 对 LPA3588 TX

# =========================================================================
# 2. 参数配置
# =========================================================================
WINDOW_SIZE = 3.0   # 雷达滑动窗口 (秒)
ALERT_THRESHOLD = 15   # 雷达门槛触发次数

SLAVE_ADDR = 0x18   # LIS2DH12 I2C 地址

REG_WHO_AM_I = 0x0F
REG_CTRL_REG1 = 0x20
REG_CTRL_REG2 = 0x21
REG_CTRL_REG3 = 0x22
REG_CTRL_REG4 = 0x23
REG_CTRL_REG5 = 0x24
REG_INT1_CFG = 0x30
REG_INT1_SRC = 0x31
REG_INT1_THS = 0x32
REG_INT1_DURATION = 0x33

# =========================================================================
# 3. 硬件初始化
# =========================================================================
time.sleep(7)
lpa_power = Pin(PIN_POWER, Pin.OUT, value=0)
p2_ctrl = Pin(PIN_P2, Pin.OUT, value=1)
radar_pin = Pin(PIN_RADAR, Pin.IN, Pin.PULL_DOWN)

uart = UART(1, baudrate=9600, tx=Pin(UART_TX), rx=Pin(UART_RX))
i2c = I2C(0, scl=Pin(PIN_SCL), sda=Pin(PIN_SDA), freq=400000)


def write_reg(reg, val):
    i2c.writeto_mem(SLAVE_ADDR, reg, bytes([val]))


def read_reg(reg):
    return i2c.readfrom_mem(SLAVE_ADDR, reg, 1)[0]


def init_lis2dh12():
    try:
        if read_reg(REG_WHO_AM_I) != 0x33:
            print("❌ LIS2DH12 校验失败！")
            return False
    except Exception as e:
        print("❌ LIS2DH12 I2C 异常:", e)
        return False

    write_reg(REG_CTRL_REG1, 0x57)   # 100Hz, Normal, XYZ enable
    write_reg(REG_CTRL_REG2, 0x09)   # HPF for INT1
    write_reg(REG_CTRL_REG3, 0x40)   # IA1 on INT1 pin
    write_reg(REG_CTRL_REG4, 0x00)   # +/-2g
    write_reg(REG_CTRL_REG5, 0x08)   # Latch interrupt
    write_reg(REG_INT1_THS, 8)   # 灵敏度阈值 (128mg)
    write_reg(REG_INT1_DURATION, 0x00)
    write_reg(REG_INT1_CFG, 0x2A)   # Enable XYZ interrupts
    
    try:
        read_reg(REG_INT1_SRC)   # 清除残留
    except:
        pass

    print("✅ LIS2DH12TR 初始化成功。")
    return True


vibration_flag = False


def vibration_isr(pin):
    global vibration_flag
    vibration_flag = True


int1_pin = Pin(PIN_INT1, Pin.IN)
int1_pin.irq(trigger=Pin.IRQ_RISING, handler=vibration_isr)


def power_on():
    lpa_power.value(1)
    print("\n[Power] ⚡ PMOS 导通，12V 通电，唤醒 LPA3588...")


def power_off():
    lpa_power.value(0)
    print("[Power] 💤 PMOS 截止，12V 断电，进入低功耗待机状态\n")


# =========================================================================
# 4. 主逻辑
# =========================================================================
def main():
    global vibration_flag
    init_lis2dh12()

    system_state = "IDLE"
    trigger_history = []

    has_vibration = False
    quiet_start_time = None

    print("🚀 ESP32-S3 哨兵守护主程序已就绪，长期监听中...")

    while True:
        now = time.ticks_ms()

        # 清理超窗雷达数据
        trigger_history = [t for t in trigger_history if time.ticks_diff(now, t) <= WINDOW_SIZE * 1000]

        if radar_pin.value() == 1:
            trigger_history.append(now)

        radar_density = len(trigger_history)

        # ------------------- 待机监听 (IDLE) -------------------
        if system_state == "IDLE":
            if radar_density >= ALERT_THRESHOLD:
                print(f"\n🚨 雷达触发！扰动指数: {radar_density} >= {ALERT_THRESHOLD}")
                power_on()

                system_state = "RECORDING"
                has_vibration = False
                quiet_start_time = None
                trigger_history.clear()

        # ------------------- 主机运行 (RECORDING) -------------------
        elif system_state == "RECORDING":
            if vibration_flag:
                has_vibration = True
                try:
                    read_reg(REG_INT1_SRC)   # 清除锁存
                except Exception as e:
                    print(f"⚠️ 清除 LIS2DH12 锁存异常 (ENODEV等): {e}")
                    # 尝试重新初始化传感器，防止彻底失效
                    try:
                        init_lis2dh12()
                    except:
                        pass

                vibration_flag = False
                print("💥 抓取到车身突发震动！")

            is_radar_quiet = (radar_density == 0)
            is_vibration_quiet = (not vibration_flag)

            if is_radar_quiet and is_vibration_quiet:
                if quiet_start_time is None:
                    quiet_start_time = time.time()

                quiet_duration = time.time() - quiet_start_time
                required_quiet_delay = 60.0 if has_vibration else 45.0  #唤醒后静默时间

                if quiet_duration >= required_quiet_delay:
                    print(f"\n[System] 静默满 {quiet_duration:.1f}s，满足关机条件，进入关机握手状态...")

                    # ----------------- 脉冲式关机握手流程 -----------------
                    ack_received = False
                    wait_ack_start = time.time()
                    last_send_time = 0

                    # 最长等待 240 秒 (容纳 LPA3588 3 分钟应急录制 + 启动开机时间)
                    ACK_MAX_WAIT = 240

                    while time.time() - wait_ack_start < ACK_MAX_WAIT:
                        now_sec = time.time()

                        # 每隔 2 秒脉冲式重发一次关机指令（带状态）
                        if now_sec - last_send_time >= 2.0:
                            if has_vibration:
                                uart.write("CMD_SHUTDOWN_ALERT\n")  # 有震动时发这个
                            else:
                                uart.write("CMD_SHUTDOWN\n")        # 无震动时发这个
                            last_send_time = now_sec
                            print("[UART] -> 脉冲发送关机指令...")

                        # 监听 LPA3588 的 ACK 回传
                        if uart.any():
                            raw = uart.read()
                            if raw:
                                try:
                                    ack_buf = raw.decode('utf-8', 'ignore')
                                    if "ACK_SHUTDOWN_READY" in ack_buf:
                                        ack_received = True
                                        print("[UART] <- 收到 LPA3588 的 ACK 确认，允许切断电源！")
                                        break
                                except Exception:
                                    pass
                        time.sleep_ms(100)

                    if not ack_received:
                        print("⚠️ 240 秒等待 ACK 兜底超时 (LPA3588 可能已自行关机)，强制切断 PMOS 电源。")

                    power_off()
                    system_state = "IDLE"
                    quiet_start_time = None
            else:
                quiet_start_time = None

        time.sleep_ms(20)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n 程序被用户或 IDE 中断 (KeyboardInterrupt)，已安全退出。")
    except Exception as e:
        print(f"\n 程序发生异常崩溃: {e}")
        

