# AntiScratchGuard
基于毫米波雷达与震动传感器的防范停车剐蹭逃逸的车载哨兵系统/In-Vehicle Sentry System for Preventing Hit-and-Run Scrapes While Parked, Based on Millimeter-Wave Radar and Vibration Sensors

系统由LD012毫米波雷达*1、光耦开关*1、ESP32S3*1、LPA3588主机*1、LIS2DH12震动传感器*1、com口转端子模块*1组成
配套代码对应的引脚如下：

加速度计：
NT1（绿线）接GPIO13
CS（绿白线）接3v3
SDO / SAC（橙线）接GND
SDA / SD1（蓝白线）接GPIO12
SCL / SPC（蓝线）接GPIO11
VCC（棕线）接3v3
GND（黑白线）接GND

com口通信芯片：
RX接GPIO17
TX接GPIO18
GND接GND
VCC接3v3
波特率需要设置为9600

LD012雷达：
VIN接ESP32的3v3
GND接ESP32的GND
OUT接ESP32的GPIO6
CK\P2接ESP32的GPIO5
DA\P3不接线（悬空）

光耦开关：
四接线柱接线：
G接电源零线
V接电源火线
RG接LPA3588电源负极
RV接LPA3588电源正极
二接线柱接线：
IN接ESP32的GPIO2
G（靠近红色接线帽的那端是G）接ESP32的GND












