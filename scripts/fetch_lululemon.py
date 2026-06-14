#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lululemon 美区上新爬取脚本（curl_cffi · TLS 指纹伪装 + __NEXT_DATA__ 解析）
================================================================
背景：
  Lululemon 官网 (shop.lululemon.com) 由 Akamai 防护。普通 requests / headless
  Chromium 都会被拦截（403 Access Denied / net::ERR_HTTP2_PROTOCOL_ERROR），
  因为 Akamai 会对 TLS/JA3 指纹做识别。

方案：
  使用 `curl_cffi`，它能复刻真实浏览器的 TLS 指纹（impersonate）。实测多指纹
  轮换（chrome116 优先，遇 400 自动换下一指纹）可稳定拿到 HTTP 200。

数据位置（关键）：
  商品列表并非通过独立的客户端 API 加载，而是被服务端直接渲染进页面内嵌的
  `<script id="__NEXT_DATA__">` JSON 中，路径为：
    props.pageProps.dehydratedState.queries[?].state.data.pages[0].products
  其中 queries[?] 为 queryKey[0] == "CategoryPageDataQuery" 的那一项（索引不固定，
  需遍历查找，切勿写死）。同级 pages[0] 含分页元信息：results / totalProductPages。

翻页：
  ?page=N（1-based）。第 1 页无需参数，后续 ?page=2 / ?page=3 ...

输出（保持不变）：
  lululemon-new.json:
    {
      "fetchedAt": "...Z",
      "source": "https://shop.lululemon.com/c/whats-new/n1q0cf",
      "count": N,
      "products": [
        {"productId","title","url","price","colorsCount","image","gender"}, ...
      ]
    }
================================================================
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from curl_cffi import requests as creq

NEW_ARRIVALS_URL = "https://shop.lululemon.com/c/whats-new/n1q0cf"
BASE = "https://shop.lululemon.com"
OUTPUT_FILE = os.environ.get("LULU_OUTPUT", "lululemon-new.json")
MAX_PAGES = int(os.environ.get("LULU_MAX_PAGES", "12"))

# 实测可绕过 Akamai 的指纹，按优先级轮换（chrome116 最稳，遇 400 换下一项）
IMPERSONATE_ORDER = ["chrome116", "chrome120", "chrome124", "chrome110", "chrome107"]

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
}


def fetch_html(url, attempts=12):
    """多 TLS 指纹轮换重试。Akamai 会间歇性返回 400，遇到即换指纹+退避重试。"""
    last = ""
    last_status = None
    for i in range(attempts):
        imp = IMPERSONATE_ORDER[i % len(IMPERSONATE_ORDER)]
        try:
            r = creq.get(url, headers=HEADERS, impersonate=imp, timeout=35)
        except Exception as e:
            print(f"      [{imp}] 请求异常: {type(e).__name__}", flush=True)
            time.sleep(1.3)
            continue
        html = r.text or ""
        last_status = r.status_code
        if r.status_code == 200 and "Access Denied" not in html and "__NEXT_DATA__" in html:
            print(f"      [{imp}] OK ({len(html)} bytes)", flush=True)
            return html
        print(f"      [{imp}] HTTP {r.status_code} size={len(html)} -> 重试", flush=True)
        last = html
        time.sleep(1.3)
    print(f"      [fail] last_status={last_status}", flush=True)
    return last


def extract_next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def get_plp(data):
    """定位 CategoryPageDataQuery 的 pages[0]（含 products 与分页元信息）。"""
    try:
        queries = data["props"]["pageProps"]["dehydratedState"]["queries"]
    except (KeyError, TypeError):
        return None
    for q in queries:
        if (q.get("queryKey") or [None])[0] == "CategoryPageDataQuery":
            try:
                return q["state"]["data"]["pages"][0]
            except (KeyError, TypeError, IndexError):
                return None
    return None


def _to_price(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, list) and val:
        return _to_price(val[0])
    if isinstance(val, str):
        m = re.search(r"[\d]+(?:\.[\d]+)?", val.replace(",", ""))
        return float(m.group(0)) if m else None
    return None


def _gender_of(p):
    s = ((p.get("parentCategoryUnifiedId") or "") + " " + (p.get("pdpUrl") or "")).lower()
    if "women" in s:
        return "women"
    if "men" in s:
        return "men"
    return ""


def _image_of(p):
    sw = p.get("swatches") or []
    if sw and isinstance(sw[0], dict):
        img = sw[0].get("primaryImage")
        if isinstance(img, str) and img.startswith("http"):
            return img + ("?wid=600" if "?" not in img else "")
    sso = p.get("skuStyleOrder") or []
    if sso and isinstance(sso[0], dict):
        imgs = sso[0].get("images") or []
        if imgs and isinstance(imgs[0], str) and imgs[0].startswith("http"):
            return imgs[0]
    return ""


def _colors_count(p):
    sw = p.get("swatches")
    if isinstance(sw, list) and sw:
        return len(sw)
    sso = p.get("skuStyleOrder")
    if isinstance(sso, list) and sso:
        return len(sso)
    return None


def _build_url(p):
    pdp = p.get("pdpUrl")
    if isinstance(pdp, str) and pdp:
        return pdp if pdp.startswith("http") else BASE + (pdp if pdp.startswith("/") else "/" + pdp)
    pid = p.get("productId")
    return f"{BASE}/p/_/prod-{pid}" if pid else NEW_ARRIVALS_URL


def normalize_product(p):
    title = p.get("displayName") or p.get("name")
    if not title:
        return None
    return {
        "productId": str(p.get("productId") or p.get("repositoryId") or ""),
        "title": str(title).strip(),
        "url": _build_url(p),
        "price": _to_price(p.get("listPrice") or p.get("price")),
        "colorsCount": _colors_count(p),
        "image": _image_of(p),
        "gender": _gender_of(p),
    }


def main():
    products = []
    seen = set()
    total_expected = None
    total_pages = None

    for pn in range(1, MAX_PAGES + 1):
        url = NEW_ARRIVALS_URL if pn == 1 else f"{NEW_ARRIVALS_URL}?page={pn}"
        print(f"[fetch] page {pn}: {url}", flush=True)
        html = fetch_html(url)
        data = extract_next_data(html) if html else None
        if not data:
            if pn == 1:
                print("[error] 首页抓取失败（Akamai 拦截或结构变化）", flush=True)
                break
            print(f"[fetch] page {pn} 无有效内容，停止翻页", flush=True)
            break

        plp = get_plp(data)
        if not plp:
            if pn == 1:
                print("[error] 未找到 CategoryPageDataQuery 商品数据（结构可能变化）", flush=True)
                break
            print(f"[fetch] page {pn} 无商品数据，停止翻页", flush=True)
            break

        if total_expected is None:
            total_expected = plp.get("results")
            total_pages = plp.get("totalProductPages")
            print(f"[meta] 总商品数={total_expected} 总页数={total_pages}", flush=True)

        added = 0
        for raw in (plp.get("products") or []):
            if not isinstance(raw, dict):
                continue
            norm = normalize_product(raw)
            if not norm:
                continue
            key = norm["productId"] or norm["title"]
            if key in seen:
                continue
            seen.add(key)
            products.append(norm)
            added += 1
        print(f"[fetch] page {pn} -> +{added} (total {len(products)})", flush=True)

        if added == 0 and pn > 1:
            break
        if total_pages and pn >= total_pages:
            break
        time.sleep(1.5)

    result = {
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "source": NEW_ARRIVALS_URL,
        "count": len(products),
        "products": products,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"[done] wrote {len(products)} products -> {OUTPUT_FILE}", flush=True)

    if len(products) == 0:
        print("[error] 0 products extracted, marking run as failed", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
