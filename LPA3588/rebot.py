import base64
import hashlib
import hmac
import json
import logging
import os
import socket
import subprocess
import time
import urllib.parse
import requests


# ==================== 钉钉机器人配置 ====================
WEBHOOK_URL = "https://oapi.dingtalk.com/robot/send?access_token=i-will-not-tell-you"
SECRET = "SECi-will-not-tell-you-too"

def check_network(host="oapi.dingtalk.com", port=443, retries=5, delay=2):
    """网络连通性检测（带退避重试机制）"""
    for attempt in range(1, retries + 1):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect((host, port))
            s.close()
            return True
        except Exception as e:
            if attempt < retries:
                time.sleep(delay)
            else:
                logging.error(f"网络连接失败 (重试{retries}次) | 目标: {host}:{port} | 异常: {e}")
                return False

def get_signed_url(webhook, secret):
    """钉钉 HMAC-SHA256 加签签名"""
    timestamp = str(round(time.time() * 1000))
    secret_enc = secret.encode("utf-8")
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    hmac_code = hmac.new(secret_enc, string_to_sign, digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"{webhook}&timestamp={timestamp}&sign={sign}"

def send_ding_alert(title, text_content, is_markdown=True):
    """向钉钉发送警报消息（带网络重试）"""
    print("[Robot] 正在准备发送钉钉告警消息...")
    if not check_network(retries=5, delay=2):
        print("[Robot] 错误：网络无法连接到钉钉服务器，取消推送！")
        return False

    url = get_signed_url(WEBHOOK_URL, SECRET)
    headers = {"Content-Type": "application/json; charset=utf-8"}

    if is_markdown:
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": text_content},
            "at": {"isAtAll": True},
        }
    else:
        payload = {
            "msgtype": "text",
            "text": {"content": f"【{title}】\n{text_content}"},
            "at": {"isAtAll": True},
        }

    try:
        response = requests.post(url, data=json.dumps(payload), headers=headers, timeout=8)
        result = response.json()
        if result.get("errcode") == 0:
            print("[Robot] 钉钉警报推送成功！")
            return True
        else:
            logging.error(f"钉钉 API 报错: {result.get('errmsg')}")
            return False
    except Exception as e:
        logging.error(f"钉钉网络请求异常: {e}")
        return False


# current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
# markdown_text = f"""### 哨兵警报：有物体靠近且车辆发生震动 ###
#                     触发时间:{current_time}
#                     当前状态: 视频主机已唤醒并开启紧急录像。
#                     > 请尽快检查车辆周边环境或查看实时监控！
#                     """
# try:
#     send_ding_alert("车身异常警报", markdown_text, is_markdown=True)
# except Exception as e:
#     err_msg = f"触发告警推送异常: {e}"
#     print(f"[Warning] {err_msg}")
#     logging.error(err_msg)
