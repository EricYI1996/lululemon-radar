#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lululemon 美区上新爬取脚本
================================================================
背景：
  Lululemon 官网 (shop.lululemon.com) 由 Akamai 防护，直接 HTTP 请求会被
  403 拦截，必须用无头浏览器（Playwright + headless Chromium）渲染页面，
  再从页面内嵌的 `#__NEXT_DATA__` JSON 中提取上新商品。

输出：
  在脚本同级（默认仓库根目录）生成 `lululemon-new.json`，结构为：
    {
      "fetchedAt": "2026-06-12T02:00:00Z",
      "source": "https://shop.lululemon.com/c/whats-new/n1q0cf",
      "count": 748,
      "products": [
        {
          "productId": "prod...",
          "title": "Align High-Rise Pant 25\"",
          "url": "https://shop.lululemon.com/p/.../...",
          "price": 98.0,
          "colorsCount": 12,
          "image": "https://images.lululemon.com/....jpg",
          "gender": "women"
        },
        ...
      ]
    }

本脚本被 .github/workflows/fetch-lululemon.yml 定时调用。
================================================================
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

NEW_ARRIVALS_URL = "https://shop.lululemon.com/c/whats-new/n1q0cf"
OUTPUT_FILE = os.environ.get("LULU_OUTPUT", "lululemon-new.json")

# 翻页参数：lululemon 列表页支持 ?pn=N（page number）。设置一个上限避免无限翻。
MAX_PAGES = int(os.environ.get("LULU_MAX_PAGES", "12"))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def extract_next_data(html):
    """从页面 HTML 中取出 #__NEXT_DATA__ 的 JSON。"""
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def looks_like_product(node):
    """判断一个 dict 是否像 lululemon 的商品对象。"""
    if not isinstance(node, dict):
        return False
    has_id = any(k in node for k in ("productId", "productCode", "id"))
    has_name = any(
        k in node for k in ("displayName", "name", "title", "productName")
    )
    has_extra = any(
        k in node
        for k in (
            "colors",
            "colorCount",
            "swatches",
            "listPrice",
            "price",
            "pdpUrl",
            "images",
            "imageInfo",
        )
    )
    return has_id and has_name and has_extra


def _first(node, keys, default=None):
    for k in keys:
        if k in node and node[k] not in (None, "", []):
            return node[k]
    return default


def _to_price(val):
    """把各种价格写法转换为 float（取数字部分）。"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, dict):
        for k in ("price", "value", "amount", "min", "low", "current"):
            if k in val:
                return _to_price(val[k])
        return None
    if isinstance(val, list) and val:
        return _to_price(val[0])
    if isinstance(val, str):
        m = re.search(r"[\d]+(?:\.[\d]+)?", val.replace(",", ""))
        return float(m.group(0)) if m else None
    return None


def _count_colors(node):
    """统计商品颜色数量。"""
    for k in ("colorCount", "colorsCount"):
        v = node.get(k)
        if isinstance(v, int):
            return v
    for k in ("colors", "swatches", "colorList"):
        v = node.get(k)
        if isinstance(v, list):
            return len(v)
    return None


def _extract_image(node):
    """尽力取出一张商品图 URL。"""
    for k in ("primaryImage", "image", "imageUrl", "thumbnail"):
        v = node.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
        if isinstance(v, dict):
            u = _first(v, ("url", "src", "href"))
            if isinstance(u, str) and u.startswith("http"):
                return u
    for k in ("images", "imageInfo", "media"):
        v = node.get(k)
        if isinstance(v, list) and v:
            first = v[0]
            if isinstance(first, str) and first.startswith("http"):
                return first
            if isinstance(first, dict):
                u = _first(first, ("url", "src", "href", "imageUrl"))
                if isinstance(u, str) and u.startswith("http"):
                    return u
    return ""


def _build_url(node):
    """拼接完整商品详情页链接。"""
    url = _first(node, ("pdpUrl", "url", "productUrl", "href"))
    if isinstance(url, str) and url:
        if url.startswith("http"):
            return url
        return "https://shop.lululemon.com" + (
            url if url.startswith("/") else "/" + url
        )
    pid = _first(node, ("productId", "productCode", "id"))
    if pid:
        return "https://shop.lululemon.com/p/_/prod-" + str(pid)
    return NEW_ARRIVALS_URL


def normalize_product(node):
    pid = _first(node, ("productId", "productCode", "id"), "")
    title = _first(node, ("displayName", "name", "title", "productName"), "")
    if not title:
        return None
    return {
        "productId": str(pid),
        "title": str(title).strip(),
        "url": _build_url(node),
        "price": _to_price(_first(node, ("listPrice", "price", "priceRange"))),
        "colorsCount": _count_colors(node),
        "image": _extract_image(node),
        "gender": str(_first(node, ("gender", "department"), "")).lower(),
    }


def collect_products(obj, out):
    """递归遍历整棵 JSON 树，收集所有像商品的节点。"""
    if isinstance(obj, dict):
        if looks_like_product(obj):
            norm = normalize_product(obj)
            if norm:
                out.append(norm)
        for v in obj.values():
            collect_products(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_products(v, out)


def harvest_from_html(html, bucket, seen):
    """从单页 HTML 抽取商品并按 productId / title 去重。返回本页新增数量。"""
    data = extract_next_data(html)
    if not data:
        return 0
    found = []
    collect_products(data, found)
    added = 0
    for p in found:
        key = p["productId"] or p["title"]
        if key in seen:
            continue
        seen.add(key)
        bucket.append(p)
        added += 1
    return added


def main():
    products = []
    seen = set()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()

        for pn in range(1, MAX_PAGES + 1):
            url = NEW_ARRIVALS_URL if pn == 1 else f"{NEW_ARRIVALS_URL}?pn={pn}"
            print(f"[fetch] page {pn}: {url}", flush=True)
            try:
                page.goto(url, wait_until="networkidle", timeout=60000)
            except Exception as e:
                print(f"[warn] goto failed on page {pn}: {e}", flush=True)
                if pn == 1:
                    raise
                break

            page.wait_for_timeout(2500)
            html = page.content()
            added = harvest_from_html(html, products, seen)
            print(f"[fetch] page {pn} -> +{added} (total {len(products)})", flush=True)

            if added == 0 and pn > 1:
                break

        browser.close()

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
