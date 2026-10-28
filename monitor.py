"""
丸久小山園 補貨監測 → LINE 推播
在 GitHub Actions 上每 10 分鐘跑一次。

需要的 GitHub Secrets：
  LINE_TOKEN    LINE Messaging API 的 Channel access token
  LINE_USER_ID  你的 LINE user ID（U 開頭）
"""
import gzip
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

PRODUCTS_FILE = "products.json"
STATE_FILE = "state.json"
BASE = "https://www.marukyu-koyamaen.co.jp/english/shop/products/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

OOS_WORDS = ("out-of-stock", "out of stock", "sold out", "sold-out", "unavailable")


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return r.status, body.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # 網路錯誤
        print(f"  fetch error: {e}")
        return 0, ""


def parse(page):
    """回傳 {"status": "ok"/"blocked", "in": [...], "out": [...]}"""
    low = page.lower()
    # 確認真的是商品頁，不是 Cloudflare 擋人頁面
    if "variations_form" not in low or "best before" not in low:
        return {"status": "blocked", "in": [], "out": []}

    start = low.index("variations_form")
    form = page[start:start + 30000]
    end = form.find("product-labelings")
    if end > 0:
        form = form[:end]

    whole_oos = re.search(r'class="[^"]*single-stock-status[^"]*out-of-stock', form) is not None

    # 每個規格一列：<div class="product-form-row ..."> ... <dd>40g can</dd>
    chunks = re.split(r'(?=<div class="product-form-row\b)', form)[1:]
    sizes_in, sizes_out = [], []
    for c in chunks:
        m = re.search(r'pa-size">\s*<dt>[^<]*</dt>\s*<dd>([^<]+)</dd>', c)
        size = htmllib.unescape(m.group(1).strip()) if m else "?"
        row_oos = any(w in c.lower() for w in OOS_WORDS)
        (sizes_out if (whole_oos or row_oos) else sizes_in).append(size)

    if not chunks:  # 單一規格商品
        (sizes_out if whole_oos else sizes_in).append("全部規格")

    return {"status": "ok", "in": sizes_in, "out": sizes_out}


def push_line(text):
    token, user = os.environ.get("LINE_TOKEN"), os.environ.get("LINE_USER_ID")
    if not token or not user:
        print("!! 沒設定 LINE_TOKEN / LINE_USER_ID，略過推播")
        print(text)
        return False
    data = json.dumps({"to": user, "messages": [{"type": "text", "text": text[:4900]}]}).encode()
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push", data=data, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"LINE: {r.status}")
            return True
    except urllib.error.HTTPError as e:
        print(f"LINE 失敗: {e.code} {e.read().decode(errors='replace')}")
        return False


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main():
    products = load(PRODUCTS_FILE, [])
    state = load(STATE_FILE, {})
    new_state = dict(state)
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

    if os.environ.get("TEST_LINE") == "1":
        ok = push_line("✅ 小山園補貨監測：LINE 推播測試成功")
        sys.exit(0 if ok else 1)

    blocks, has_new = [], False
    for i, p in enumerate(products):
        if i:
            time.sleep(3)  # 不要對網站太兇
        url = BASE + p["id"]
        code, page = fetch(url)
        r = parse(page) if code == 200 else {"status": "blocked", "in": [], "out": []}
        if r["status"] != "ok":
            print(f"{p['name']}: 被擋或讀取失敗 (HTTP {code})，沿用上次狀態")
            continue

        prev = state.get(p["id"], [])
        newly = [s for s in r["in"] if s not in prev]
        new_state[p["id"]] = r["in"]
        print(f"{p['name']}: 有貨{r['in']} 缺貨{r['out']}")

        if newly:
            has_new = True
        if r["in"]:
            lines = [f"🟢【{p['name']}】",
                     f"📦 有貨：{', '.join(r['in'])}" + (f"（🆕 {', '.join(newly)}）" if newly else "")]
            if r["out"]:
                lines.append(f"❌ 缺貨：{', '.join(r['out'])}")
            lines.append(f"👉 連結：{url}")
            blocks.append("\n".join(lines))

    if has_new:
        msg = "\n".join(["🎉【丸久小山園 補貨通知！】", f"時間：{now}",
                         "------------------------", "\n\n".join(blocks),
                         "", "（記得先登入會員才能下單）"])
        if not push_line(msg):
            # 推播失敗就不更新狀態，下次再通知
            new_state = state

    if new_state != state:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(new_state, f, ensure_ascii=False, indent=1)
        print("state.json 已更新")


if __name__ == "__main__":
    main()
