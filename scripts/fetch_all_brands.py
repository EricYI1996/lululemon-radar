#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美区上新风貌雷达 · 全品牌静态数据预生成脚本
================================================================
背景：
  终端用户位于中国大陆，*.workers.dev / allorigins.win 等代理通道被墙或极不稳定，
  而 github.io 静态文件可正常访问。因此把各品牌数据在 GitHub Actions（美区 runner，
  无 CORS / GFW 问题）服务端预抓取，写成静态 JSON 提交回仓库，经 GitHub Pages 分发，
  前端「静态优先 + 在线代理兜底」读取。

技术方案：
  与 scripts/fetch_lululemon.py 一致，使用 curl_cffi 复刻真实浏览器 TLS 指纹
  （impersonate），多指纹轮换 + 退避重试，绕过各站点的反爬/指纹识别。

抓取对象：
  1) Shopify 品牌（直接读取 /products.json?limit=50，原样保存 products 数组）：
       alo   -> https://www.aloyoga.com/products.json?limit=50
       adan  -> https://adanola.com/products.json?limit=50
       dfyne -> https://dfyne.com/products.json?limit=50
       tala  -> https://www.wearetala.com/products.json?limit=50
  2) Gymshark（gym）：抓取 new-releases 第 1~3 页，解析页面内嵌
       <script id="__NEXT_DATA__"> 的 JSON，读取
       props.pageProps.ssrQuery.hits（Algolia hit 数组），跨页按
       objectID/id/handle/title 去重，原样保存 hit 对象。

输出（仓库根目录，5 个文件）：
  alo.json / adan.json / dfyne.json / tala.json / gym.json
  统一形如：
    {
      "fetchedAt": "<iso8601 UTC>",
      "source": "<抓取地址>",
      "count": <数量>,
      "products": [ ...原样对象... ]   # gym 存的是 hit 对象，键名仍用 products
    }

健壮性：
  单个品牌失败不影响其它品牌。逐品牌收集结果并打印清晰日志。
  只要有 1 个品牌成功即退出码 0；全部失败才退出码 1。
================================================================
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from curl_cffi import requests as creq

# 与 fetch_lululemon.py 保持一致：实测可绕过指纹识别的浏览器指纹，按优先级轮换
IMPERSONATE_ORDER = ["chrome116", "chrome120", "chrome124", "chrome110", "chrome107"]

# Shopify 品牌：key -> 域名（用于拼接 products.json 地址）
SHOPIFY_BRANDS = {
    "alo": "www.aloyoga.com",
    "adan": "adanola.com",
    "dfyne": "dfyne.com",
    "tala": "www.wearetala.com",
}

# Gymshark new-releases 页面
GYMSHARK_BASE = "https://www.gymshark.com/collections/new-releases"
GYMSHARK_MAX_PAGES = int(os.environ.get("GYM_MAX_PAGES", "3"))

# 通用浏览器请求头
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
}

# products.json 接口偏向 API 调用，单独一套 Accept 头
JSON_HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_text(url, headers, attempts=12, expect_json=False, must_contain=None):
    last = ""
    last_status = None
    for i in range(attempts):
        imp = IMPERSONATE_ORDER[i % len(IMPERSONATE_ORDER)]
        try:
            r = creq.get(url, headers=headers, impersonate=imp, timeout=35)
        except Exception as e:
            print(f"      [{imp}] 请求异常: {type(e).__name__}: {e}", flush=True)
            time.sleep(1.3)
            continue
        text = r.text or ""
        last_status = r.status_code
        last = text
        ok = r.status_code == 200
        if ok and must_contain and must_contain not in text:
            ok = False
        if ok and expect_json:
            try:
                json.loads(text)
            except json.JSONDecodeError:
                ok = False
        if ok:
            print(f"      [{imp}] OK ({len(text)} bytes)", flush=True)
            return text
        print(f"      [{imp}] HTTP {r.status_code} size={len(text)} -> 重试", flush=True)
        time.sleep(1.3)
    print(f"      [fail] last_status={last_status}", flush=True)
    return last


def write_output(key, source, products):
    result = {
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "count": len(products),
        "products": products,
    }
    out_file = f"{key}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[done] {key}: wrote {len(products)} items -> {out_file}", flush=True)


def fetch_shopify(key, domain):
    url = f"https://{domain}/products.json?limit=50"
    print(f"[fetch] {key} (shopify): {url}", flush=True)
    text = fetch_text(url, JSON_HEADERS, expect_json=True)
    if not text:
        raise RuntimeError("空响应")
    data = json.loads(text)
    products = data.get("products") if isinstance(data, dict) else None
    if not isinstance(products, list) or not products:
        raise RuntimeError("products 字段缺失或为空")
    write_output(key, url, products)
    return len(products)


def parse_gymshark_hits(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</' + 'script>', html, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    try:
        hits = data["props"]["pageProps"]["ssrQuery"]["hits"]
    except (KeyError, TypeError):
        return []
    return hits if isinstance(hits, list) else []


def fetch_gymshark(key):
    seen = set()
    hits = []
    for pn in range(1, GYMSHARK_MAX_PAGES + 1):
        url = GYMSHARK_BASE if pn == 1 else f"{GYMSHARK_BASE}?page={pn}"
        print(f"[fetch] {key} (gymshark) page {pn}: {url}", flush=True)
        html = fetch_text(url, HEADERS, must_contain="__NEXT_DATA__")
        page_hits = parse_gymshark_hits(html) if html else []
        added = 0
        for h in page_hits:
            if not isinstance(h, dict):
                continue
            hid = h.get("objectID") or h.get("id") or h.get("handle") or h.get("title")
            if hid is not None and hid in seen:
                continue
            if hid is not None:
                seen.add(hid)
            hits.append(h)
            added += 1
        print(f"[fetch] {key} page {pn} -> +{added} (total {len(hits)})", flush=True)
        if pn < GYMSHARK_MAX_PAGES:
            time.sleep(1.5)
    if not hits:
        raise RuntimeError("未解析到任何 hits")
    write_output(key, GYMSHARK_BASE, hits)
    return len(hits)


def main():
    results = {}

    for key, domain in SHOPIFY_BRANDS.items():
        try:
            n = fetch_shopify(key, domain)
            results[key] = (True, n)
        except Exception as e:
            print(f"[error] {key} 抓取失败: {type(e).__name__}: {e}", flush=True)
            results[key] = (False, str(e))

    try:
        n = fetch_gymshark("gym")
        results["gym"] = (True, n)
    except Exception as e:
        print(f"[error] gym 抓取失败: {type(e).__name__}: {e}", flush=True)
        results["gym"] = (False, str(e))

    print("\n========== 汇总 ==========", flush=True)
    success = 0
    for key in list(SHOPIFY_BRANDS.keys()) + ["gym"]:
        ok, info = results.get(key, (False, "未执行"))
        if ok:
            success += 1
            print(f"  ✔ {key}: {info} items", flush=True)
        else:
            print(f"  ✘ {key}: 失败 ({info})", flush=True)
    print(f"成功 {success}/{len(results)} 个品牌", flush=True)

    if success == 0:
        print("[error] 所有品牌均失败，标记本次运行失败", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
