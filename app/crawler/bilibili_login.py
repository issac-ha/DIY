"""
B站扫码登录：生成二维码 → 手机扫码确认 → 自动把新 Cookie 写回 .env

SESSDATA 的有效期由 B站服务端决定，没有可调用的刷新接口，过期只能重新登录。
这个脚本把"重新登录"压缩成扫一次码，省掉开 F12 翻请求头复制 Cookie 的步骤。

用法：python -m app.crawler.bilibili_login
"""
import base64
import io
import time
import webbrowser
from pathlib import Path

import qrcode
import requests

from app.core.logger import logger

GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# 项目根目录（本文件在 app/crawler/ 下，往上三层）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
QR_FILE = PROJECT_ROOT / "qrcode_login.png"
QR_HTML = PROJECT_ROOT / "qrcode_login.html"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://passport.bilibili.com/login",
}

# 轮询间隔与总等待时间（秒）
POLL_INTERVAL = 2
POLL_TIMEOUT = 180

# 扫码接口的返回码
CODE_SUCCESS = 0
CODE_WAITING_SCAN = 86101
CODE_WAITING_CONFIRM = 86090
CODE_EXPIRED = 86038


def _update_env(cookie: str):
    """把新 Cookie 写回 .env，保留其他配置行"""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("BILIBILI_COOKIE="):
            lines[i] = f"BILIBILI_COOKIE={cookie}"
            break
    else:
        lines.append(f"BILIBILI_COOKIE={cookie}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_qr_page(qr_url: str) -> bool:
    """
    生成二维码图片和展示页，并尝试自动打开浏览器

    只存 PNG 需要用户自己去找文件，部分看图工具缩放后也扫不出来。
    内嵌进本地 HTML 居中显示，打开即可扫。

    :return: 浏览器是否成功唤起
    """
    buffer = io.BytesIO()
    qrcode.make(qr_url).save(buffer)
    image = buffer.getvalue()
    QR_FILE.write_bytes(image)

    data_uri = base64.b64encode(image).decode("ascii")
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>B站扫码登录</title>
<style>
  body {{
    margin: 0; min-height: 100vh; display: flex;
    align-items: center; justify-content: center;
    background: #f5f6f7; font-family: "Microsoft YaHei", sans-serif;
  }}
  .box {{
    background: #fff; padding: 40px 48px; border-radius: 12px;
    box-shadow: 0 4px 24px rgba(0, 0, 0, .08); text-align: center;
  }}
  h1 {{ font-size: 20px; margin: 0 0 8px; color: #18191c; }}
  p {{ font-size: 14px; color: #61666d; margin: 0 0 24px; }}
  img {{ width: 260px; height: 260px; display: block; }}
</style>
</head>
<body>
  <div class="box">
    <h1>B站扫码登录</h1>
    <p>请用 B站 App 扫描下方二维码，{POLL_TIMEOUT} 秒内有效</p>
    <img src="data:image/png;base64,{data_uri}" alt="登录二维码">
  </div>
</body>
</html>
"""
    QR_HTML.write_text(html, encoding="utf-8")
    return webbrowser.open(QR_HTML.as_uri())


def login() -> bool:
    """
    走一遍扫码登录流程

    :return: 是否登录成功并写回 .env
    """
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    resp = session.get(GENERATE_URL, timeout=15)
    if resp.status_code != 200 or "json" not in resp.headers.get("Content-Type", ""):
        logger.error(
            f"[login] 生成二维码失败: HTTP {resp.status_code}, "
            f"Content-Type={resp.headers.get('Content-Type')}"
        )
        return False
    data = resp.json()
    if data.get("code") != 0:
        logger.error(f"[login] 生成二维码失败: {data.get('message')}")
        return False

    qr_url = data["data"]["url"]
    qrcode_key = data["data"]["qrcode_key"]
    opened = _render_qr_page(qr_url)
    logger.info(f"[login] 二维码页面: {QR_HTML}")
    if opened:
        logger.info(f"[login] 已自动打开浏览器，请用 B站 App 扫码，{POLL_TIMEOUT} 秒内有效")
    else:
        logger.warning(f"[login] 浏览器未自动打开，请手动双击 {QR_HTML}，{POLL_TIMEOUT} 秒内有效")

    deadline = time.time() + POLL_TIMEOUT
    last_status = None
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            poll = session.get(
                POLL_URL, params={"qrcode_key": qrcode_key}, timeout=15
            ).json()
        except Exception as e:
            logger.warning(f"[login] 轮询失败，稍后重试: {e}")
            continue

        status = (poll.get("data") or {}).get("code")
        if status == CODE_SUCCESS:
            break
        if status == CODE_EXPIRED:
            logger.error("[login] 二维码已过期，请重新运行脚本")
            return False
        # 未扫码/未确认只提示一次，避免刷屏
        if status != last_status:
            if status == CODE_WAITING_SCAN:
                logger.info("[login] 等待扫码")
            elif status == CODE_WAITING_CONFIRM:
                logger.info("[login] 已扫码，请在手机上确认登录")
            last_status = status
    else:
        logger.error("[login] 等待超时，未完成扫码")
        return False

    cookie = "; ".join(f"{c.name}={c.value}" for c in session.cookies)
    if "SESSDATA" not in cookie:
        logger.error("[login] 登录成功但未取到 SESSDATA，放弃写入")
        return False

    # 用新 Cookie 自检一次，避免把无效串写进 .env
    check = session.get(
        "https://api.bilibili.com/x/web-interface/nav", timeout=10
    ).json()
    if not (check.get("code") == 0 and (check.get("data") or {}).get("isLogin")):
        logger.error("[login] 登录态校验失败，未写入 .env")
        return False

    _update_env(cookie)
    logger.info(f"[login] 登录成功，Cookie 已写入 {ENV_FILE}")
    logger.info(f"[login] 当前账号: {(check.get('data') or {}).get('uname')}")
    return True


if __name__ == "__main__":
    raise SystemExit(0 if login() else 1)
