#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SHEIN 品牌店铺上新抓取脚本（第三版：接口优先 + 浏览器兜底）
===============================================================
目标：
- 监控配置里的 SHEIN 品牌店铺页（store_code）
- 使用 Playwright 在 GitHub Actions 的浏览器环境中抓取商品卡片
- 输出仓库根目录 shein.json，供前端看板静态读取

注意：
- SHEIN 对服务端直连有明显风控，第一版以浏览器渲染 + DOM 提取为主
- 若命中 risk challenge / system updating，不抛致命错误，仍写出 shein.json
- 这样即使 SHEIN 暂时失败，也不会阻塞其它品牌 JSON 的生成与提交
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    from curl_cffi import requests as creq
except Exception:  # pragma: no cover
    creq = None

ROOT = Path(__file__).resolve().parents[1]
OUT_FILE = ROOT / "shein.json"
SOURCES_FILE = Path(__file__).resolve().with_name("shein_sources.json")

DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
    "Mobile/15E148 Safari/604.1"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(val: str) -> str:
    return re.sub(r"\s+", " ", (val or "")).strip()


def parse_price(text: str):
    if not text:
        return None
    m = re.search(r"\$\s*([0-9]+(?:\.[0-9]{1,2})?)", text)
    return float(m.group(1)) if m else None


def slug_from_url(url: str) -> str:
    path = urlparse(url).path or ""
    return path.rstrip("/").split("/")[-1]


def store_attempt_urls(store: dict) -> list[str]:
    store_code = store["storeCode"]
    urls = []
    for base in (store.get("url"), f"https://us.shein.com/store/home?store_code={store_code}&tab=home"):
        if base and base not in urls:
            urls.append(base)
    # 移动端页面有时比桌面端更少触发资源超时；作为第二入口。
    urls.append(f"https://m.shein.com/us/store/home?store_code={store_code}&tab=home")
    return urls


def add_stealth_init(context):
    context.add_init_script("""
      Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
      Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
      Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
      window.chrome = window.chrome || { runtime: {} };
    """)


def safe_goto(page, url: str, timeout: int = 45000) -> str:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        return "ok"
    except Exception as e:
        # 很多 SHEIN 页面会卡在 domcontentloaded，但部分 DOM 已经可读；不要直接放弃。
        return f"goto_warning:{type(e).__name__}: {str(e).splitlines()[0][:180]}"


def warmup(page):
    # 先访问首页建立基础 cookie / localStorage，降低直接打店铺页的挑战概率。
    for url in ("https://us.shein.com/", "https://us.shein.com/RecommendSelection/Sports-Outdoors-sc-017185553.html"):
        status = safe_goto(page, url, timeout=25000)
        page.wait_for_timeout(1200 + random.randint(0, 800))
        if not status.startswith("goto_warning"):
            break


def human_scroll(page, rounds: int = 7):
    for _ in range(rounds):
        page.mouse.wheel(0, random.randint(900, 2200))
        page.wait_for_timeout(random.randint(900, 1800))


def normalize_api_product(item: dict, brand_name: str, store_code: str) -> dict | None:
    if not isinstance(item, dict):
        return None
    goods_id = item.get("goods_id") or item.get("goodsId") or item.get("productRelationID") or item.get("product_id")
    title = clean_text(item.get("goods_name") or item.get("goodsName") or item.get("name") or item.get("title") or "")
    if not goods_id and not title:
        return None
    url = item.get("goods_url") or item.get("detail_url") or item.get("url") or ""
    if url and url.startswith("//"):
        url = "https:" + url
    elif url and url.startswith("/"):
        url = "https://us.shein.com" + url
    elif not url and goods_id and title:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-")
        url = f"https://us.shein.com/{slug}-p-{goods_id}.html"
    image = item.get("goods_img") or item.get("goodsImg") or item.get("image") or item.get("img") or ""
    if isinstance(image, dict):
        image = image.get("url") or image.get("origin_image") or image.get("thumbnail") or ""
    if image and image.startswith("//"):
        image = "https:" + image
    price_text = json.dumps(item.get("salePrice") or item.get("retailPrice") or item.get("price") or {}, ensure_ascii=False)
    price = parse_price(price_text) or parse_price(str(item.get("price") or item.get("salePrice") or ""))
    return {
        "brandName": brand_name,
        "storeCode": store_code,
        "title": title or str(goods_id),
        "url": url,
        "image": image,
        "price": price,
        "salePrice": price,
        "discount": clean_text(str(item.get("discount") or item.get("discountLabel") or "")),
        "soldText": clean_text(str(item.get("sales") or item.get("soldText") or item.get("comment_num") or "")),
        "tags": ["API"],
        "rawText": title,
        "slug": slug_from_url(url) if url else str(goods_id),
    }


def find_product_arrays(obj):
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            keys = set().union(*(x.keys() for x in obj[:5]))
            if {"goods_id", "goodsId", "goods_name", "goodsName", "productRelationID"} & keys:
                yield obj
        for x in obj:
            yield from find_product_arrays(x)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from find_product_arrays(v)


def fetch_store_via_api(store: dict):
    if creq is None:
        return {"ok": False, "error": "curl_cffi_unavailable"}, []
    brand = store["brand"]
    store_code = store["storeCode"]
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://us.shein.com",
        "Referer": store.get("url") or f"https://us.shein.com/store/home?store_code={store_code}&tab=home",
        "x-requested-with": "XMLHttpRequest",
    }
    params = {
        "store_code": store_code,
        "storeCode": store_code,
        "scene": "store",
        "page": "1",
        "limit": "120",
        "ignore_direct": "true",
        "page_name": "page_store",
        "isStore": "1",
        "tab": "home",
    }
    candidates = [
        ("GET", "https://us.shein.com/product/get_products_by_keywords", params, None),
        ("POST", "https://us.shein.com/product/get_products_by_keywords", params, {"queryObj": params}),
        ("GET", "https://us.shein.com/ccc/store/home_page", params, None),
        ("POST", "https://us.shein.com/ccc/store/home_page", params, {"queryObj": params}),
    ]
    errors = []
    for method, url, q, body in candidates:
        try:
            if method == "GET":
                resp = creq.get(url, params=q, headers=headers, impersonate="chrome120", timeout=30)
            else:
                resp = creq.post(url, params=q, json=body, headers=headers, impersonate="chrome120", timeout=30)
            text = resp.text or ""
            if resp.status_code != 200 or not text or text.strip() == "{}":
                errors.append(f"{method} {url} -> HTTP {resp.status_code} len={len(text)}")
                continue
            try:
                data = resp.json()
            except Exception:
                errors.append(f"{method} {url} -> non_json HTTP {resp.status_code}")
                continue
            products = []
            for arr in find_product_arrays(data):
                for item in arr:
                    product = normalize_api_product(item, brand, store_code)
                    if product:
                        products.append(product)
                if products:
                    break
            if products:
                dedup = {}
                for p in products:
                    dedup[p.get("url") or p.get("slug") or p["title"]] = p
                return {"ok": True, "count": len(dedup), "method": method, "endpoint": url}, list(dedup.values())
            errors.append(f"{method} {url} -> json_without_products")
        except Exception as e:
            errors.append(f"{method} {url} -> {type(e).__name__}: {str(e)[:120]}")
    return {"ok": False, "error": "api_failed", "apiErrors": errors[-6:]}, []


def extract_products(page, brand_name: str, store_code: str):
    js = r"""
    ({ brandName, storeCode }) => {
      const uniq = new Map();
      const anchors = Array.from(document.querySelectorAll('a[href*="-p-"]'));
      for (const a of anchors) {
        const href = a.getAttribute('href') || '';
        if (!href || href.includes('/store/')) continue;
        const abs = new URL(href, location.origin).href;
        if (uniq.has(abs)) continue;

        const container = a.closest('section, article, li, div');
        const text = (container?.innerText || a.innerText || '').replace(/\s+/g, ' ').trim();
        const title = (a.getAttribute('title') || a.textContent || text.split('$')[0] || '').replace(/\s+/g, ' ').trim();
        const imgNode = (container || a).querySelector('img');
        const img = imgNode?.getAttribute('src') || imgNode?.getAttribute('data-src') || imgNode?.getAttribute('data-lazy-src') || '';
        const soldText = /\b\d+[\d,.+]*\s*(sold|reviews?)\b/i.exec(text)?.[0] || '';
        const discount = /-\d+%/.exec(text)?.[0] || '';
        const prices = [...text.matchAll(/\$\s*([0-9]+(?:\.[0-9]{1,2})?)/g)].map(m => Number(m[1]));
        const tags = [];
        if (/\btrends?\b/i.test(text)) tags.push('Trends');
        if (/\blocal\b/i.test(text)) tags.push('Local');

        uniq.set(abs, {
          brandName,
          storeCode,
          title,
          url: abs,
          image: img,
          price: prices[0] ?? null,
          salePrice: prices[1] ?? null,
          discount,
          soldText,
          tags,
          rawText: text.slice(0, 600)
        });
      }
      return Array.from(uniq.values());
    }
    """
    return page.evaluate(js, {"brandName": brand_name, "storeCode": store_code})


def detect_block(page) -> str:
    try:
        url = page.url or ""
        body = clean_text(page.locator("body").inner_text(timeout=5000)[:2500])
    except Exception:
        url, body = "", ""
    if "/risk/challenge" in url:
        return "risk_challenge"
    if "System Updating" in body:
        return "system_updating"
    if "Access Denied" in body:
        return "access_denied"
    return ""


def scrape_store(playwright, store: dict):
    brand = store["brand"]
    store_code = store["storeCode"]
    result = {"brand": brand, "storeCode": store_code, "url": store.get("url"), "ok": False, "count": 0}

    api_summary, api_products = fetch_store_via_api(store)
    result["api"] = api_summary
    if api_products:
        result.update({"ok": True, "count": len(api_products), "strategy": "api"})
        print(f"[shein] API ok {brand} ({store_code}): {len(api_products)} items", flush=True)
        return result, api_products
    print(f"[shein] API failed {brand} ({store_code}): {api_summary.get('error')}", flush=True)

    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--lang=en-US,en",
        ],
    )

    last_error = ""
    try:
        for attempt_idx, target_url in enumerate(store_attempt_urls(store), start=1):
            is_mobile = "m.shein.com" in target_url
            context = browser.new_context(
                locale="en-US",
                timezone_id="America/Los_Angeles",
                viewport={"width": 390, "height": 1600} if is_mobile else {"width": 1440, "height": 1800},
                user_agent=MOBILE_UA if is_mobile else DESKTOP_UA,
                device_scale_factor=3 if is_mobile else 1,
                is_mobile=is_mobile,
                has_touch=is_mobile,
            )
            add_stealth_init(context)
            context.set_extra_http_headers({
                "Accept-Language": "en-US,en;q=0.9",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
            })
            page = context.new_page()

            try:
                print(f"[shein] open store {brand} ({store_code}) attempt={attempt_idx} mobile={is_mobile}", flush=True)
                warmup(page)
                status = safe_goto(page, target_url, timeout=45000)
                page.wait_for_timeout(2500 + random.randint(0, 1200))
                human_scroll(page, rounds=8)

                block = detect_block(page)
                products = [] if block else extract_products(page, brand, store_code)
                if products:
                    cleaned = []
                    seen = set()
                    for p in products:
                        p["title"] = clean_text(p.get("title") or "") or slug_from_url(p["url"]).replace("-", " ")
                        p["image"] = urljoin(page.url, p.get("image") or "") if p.get("image") else ""
                        p["slug"] = slug_from_url(p["url"])
                        key = p["url"]
                        if key in seen:
                            continue
                        seen.add(key)
                        cleaned.append(p)
                    result.update({"ok": True, "count": len(cleaned), "url": target_url, "attempt": attempt_idx, "navigation": status})
                    print(f"  -> ok: {len(cleaned)} items", flush=True)
                    return result, cleaned

                body_hint = ""
                try:
                    body_hint = clean_text(page.locator("body").inner_text(timeout=3000)[:300])
                except Exception:
                    pass
                last_error = block or (status if status != "ok" else "no_product_cards_found")
                print(f"  -> attempt failed: {last_error}; hint={body_hint[:120]}", flush=True)
            finally:
                context.close()

        result["error"] = last_error or "all_attempts_failed"
        print(f"  -> failed: {result['error']}", flush=True)
        return result, []
    finally:
        browser.close()


def main():
    if not SOURCES_FILE.exists():
        raise FileNotFoundError(f"missing config: {SOURCES_FILE}")
    conf = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    stores = conf.get("stores") or []
    if not stores:
        raise RuntimeError("shein_sources.json 没有 stores 配置")

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        payload = {
            "fetchedAt": now_iso(),
            "source": "SHEIN store pages",
            "count": 0,
            "products": [],
            "stores": stores,
            "errors": [{"scope": "bootstrap", "message": f"playwright unavailable: {type(e).__name__}: {e}"}],
        }
        OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[shein] playwright unavailable, wrote empty shein.json", flush=True)
        return 0

    all_products = []
    errors = []
    summaries = []

    with sync_playwright() as p:
        for store in stores:
            try:
                summary, products = scrape_store(p, store)
                summaries.append(summary)
                if summary.get("ok"):
                    all_products.extend(products)
                else:
                    errors.append(summary)
            except Exception as e:
                msg = {"brand": store.get("brand"), "storeCode": store.get("storeCode"), "error": f"{type(e).__name__}: {e}"}
                print(f"  -> exception: {msg['error']}", flush=True)
                summaries.append({**store, "ok": False, "count": 0, "error": msg["error"]})
                errors.append(msg)

    payload = {
        "fetchedAt": now_iso(),
        "source": "SHEIN store pages",
        "count": len(all_products),
        "products": all_products,
        "stores": summaries,
        "errors": errors,
        "note": "第三版为 SHEIN 接口优先 + Playwright DOM 兜底；若接口 403 或命中 risk challenge，将保留错误信息并返回空列表。",
    }
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[shein] wrote {len(all_products)} items -> {OUT_FILE}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
