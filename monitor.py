import os
import requests
from datetime import datetime

def send_feishu(webhook: str, text: str):
    payload = {"msg_type": "text", "content": {"text": text}}
    r = requests.post(webhook, json=payload, timeout=15)
    r.raise_for_status()

def main():
    webhook = os.getenv("FEISHU_WEBHOOK")
    if not webhook:
        raise RuntimeError("Missing FEISHU_WEBHOOK secret")

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    msg = f"✅ GitHub Actions 测试成功\n时间: {now}\n后续将替换为机票降价提醒。"
    send_feishu(webhook, msg)
    print("done")

if __name__ == "__main__":
    main()
