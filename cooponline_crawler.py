# -*- coding: utf-8 -*-
import os, re, sys, csv, json
from datetime import datetime, timezone
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE = "https://cooponline.vn"
SOURCE = "cooponline"
INCLUDE_OOS = False  # True = lấy cả sản phẩm đang "hết hàng"

# Hai mục đầu là trang danh sách đã kiểm tra được. Hai mục sau là mục cha,
# bao gồm cả trứng, hải sản, trái cây. Sản phẩm trùng sẽ tự được gộp lại.
CATEGORIES = {
    "Thịt": BASE + "/c/thit",
    "Rau củ": BASE + "/c/rau-cu",
    "Thịt, trứng, hải sản": BASE + "/c/thit-trung-hai-san",
    "Rau củ, trái cây": BASE + "/c/rau-cu-trai-cay",
}

SETUP = "--setup" in sys.argv   # (tùy chọn) chọn siêu thị giao hàng
SHOW = "--show" in sys.argv     # chạy có hiện trình duyệt
DEBUG = "--debug" in sys.argv   # lưu file html để kiểm tra
HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, "coop_profile")

PROD = re.compile(r"^https://cooponline\.vn/[^/?#]+--s\d+$")
PRICE = re.compile(r"(\d{1,3}(?:\.\d{3})+)\s*₫")
DISC = re.compile(r"-\s*(\d{1,2})\s*%")
UNIT = re.compile(r"₫\s*/\s*(\S+)")
WEIGHT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|gr|g)\b", re.I)
OOS = re.compile(r"^(Tạm hết hàng|Hết hàng)\s*", re.I)
TOTAL = re.compile(r"\(\s*(\d[\d.,]*)\s*sản phẩm\s*\)")
MORE = re.compile(r"^\s*Xem thêm sản phẩm\s*$", re.I)


def money(x):
    return f"{x:,.0f}".replace(",", ".") + "đ" if x else "-"


def num(s):
    return float(re.sub(r"[^\d]", "", s))


def load_config():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    cfg = os.path.join(HERE, "bhx_config.json")  # dùng chung file với BHX
    if (not url or not key) and os.path.exists(cfg):
        d = json.load(open(cfg, encoding="utf-8-sig"))
        url = url or d.get("url", "")
        key = key or d.get("key", "")
    return url.rstrip("/"), key


def expected_total(html):
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    m = TOTAL.search(text)
    return int(re.sub(r"\D", "", m.group(1))) if m else None


def parse(html, category):
    """Trả về (các sản phẩm đọc được, số sản phẩm hết hàng bị bỏ, tổng thẻ có giá)."""
    soup = BeautifulSoup(html, "html.parser")
    info = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(BASE, a["href"]).split("?")[0].split("#")[0]
        if not PROD.match(url):
            continue
        t = a.get_text(" ", strip=True)
        d = info.setdefault(url, {"name": None, "price": None, "oos": False})
        if OOS.match(t):
            d["oos"] = True
        if PRICE.search(t):
            if d["price"] is None:
                d["price"] = t
        elif t and d["name"] is None:
            d["name"] = OOS.sub("", t).strip()

    rows, oos_count, seen = {}, 0, 0
    for url, d in info.items():
        if not d["price"]:
            continue
        seen += 1
        if d["oos"] and not INCLUDE_OOS:
            oos_count += 1
            continue
        t = d["price"]
        prices = [num(x) for x in PRICE.findall(t)][:2]
        if len(prices) >= 2:
            sale, lst = min(prices), max(prices)
        else:
            sale = lst = prices[0]
        name = d["name"] or OOS.sub("", t[:PRICE.search(t).start()]).strip()
        if not name:
            continue
        um = UNIT.search(t)
        unit = um.group(1) if um else None
        dm = DISC.search(t)

        # giá/kg theo GIÁ GỐC
        per_kg = None
        if unit and unit.lower() == "kg":
            per_kg = lst
        else:
            w = WEIGHT.search(name)
            if w:
                n = float(w.group(1).replace(",", "."))
                kg = n if w.group(2).lower() == "kg" else n / 1000
                if kg > 0:
                    per_kg = round(lst / kg)

        key = url.rstrip("/").split("/")[-1]
        rows[key] = {
            "source": SOURCE, "product_key": key, "name": name,
            "category": category, "unit": unit,
            "list_price": lst, "sale_price": sale, "is_on_sale": lst > sale,
            "price_per_kg": per_kg,
            "discount_percent": float(dm.group(1)) if dm and lst > sale else None,
            "url": url,
        }
    return rows, oos_count, seen


def count_products(page):
    return page.evaluate(r"""() => new Set([...document.querySelectorAll('a[href]')]
        .map(a => a.href.split('?')[0])
        .filter(h => /^https:\/\/cooponline\.vn\/[^\/?#]+--s\d+$/.test(h))).size""")


def click_more(page):
    try:
        btn = page.get_by_text(MORE).first
        if btn.count() == 0 or not btn.is_visible():
            return False
        btn.scroll_into_view_if_needed()
        btn.click(timeout=5000)
        return True
    except Exception:
        return False


def load_all(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(5000)
    expected = expected_total(page.content())
    prev, stale = count_products(page), 0
    for _ in range(300):
        if expected and prev >= expected:
            break
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(500)
        clicked = click_more(page)
        page.wait_for_timeout(1800 if clicked else 1000)
        cur = count_products(page)
        stale = stale + 1 if cur <= prev else 0
        prev = cur
        if stale >= 5:
            break
    return page.content(), expected


def upsert(base_url, key, table, rows, conflict=None):
    url = f"{base_url}/rest/v1/{table}"
    if conflict:
        url += f"?on_conflict={conflict}"
    headers = {
        "apikey": key, **({} if key.startswith("sb_") else {"Authorization": f"Bearer {key}"}),
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal" if conflict else "return=minimal",
    }
    for i in range(0, len(rows), 100):
        r = requests.post(url, headers=headers,
                          data=json.dumps(rows[i:i + 100]), timeout=60)
        if r.status_code >= 300:
            print("  Lỗi Supabase:", r.status_code, r.text[:400])
            return False
    return True


def main():
    all_rows = {}
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, headless=not (SETUP or SHOW), locale="vi-VN",
            viewport={"width": 1366, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if SETUP:
            page.goto(BASE)
            input("Chọn siêu thị giao hàng trên trình duyệt, xong bấm Enter ở đây...")
            ctx.close()
            return

        for n, (cat, url) in enumerate(CATEGORIES.items()):
            print(f"\nĐang lấy: {cat} ... (có thể mất vài phút)")
            try:
                html, expected = load_all(page, url)
                rows, oos, seen = parse(html, cat)
            except Exception as e:
                print("  Lỗi:", e)
                continue
            new = {k: v for k, v in rows.items() if k not in all_rows}
            head = f"=== {cat}: {len(new)} sản phẩm mới"
            head += f" (trang ghi {expected} sản phẩm" if expected else " ("
            head += f", đọc được {seen}, bỏ {oos} hết hàng) ==="
            print(head)
            if expected and seen < expected:
                print(f"  (!) Mới đọc được {seen}/{expected} sản phẩm của trang.")
            for i, r in enumerate(new.values(), 1):
                unit = f" (/{r['unit']})" if r["unit"] else ""
                print(f"{i:>4}. {r['name']}{unit} - {money(r['list_price'])}")
            if DEBUG or not rows:
                open(f"coop_debug_{n}.html", "w", encoding="utf-8").write(html)
            all_rows.update(new)
        ctx.close()

    rows = list(all_rows.values())
    if not rows:
        print("\nKhông lấy được sản phẩm nào.")
        return

    fn = f"cooponline_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    cols = ["category", "name", "unit", "list_price", "price_per_kg",
            "sale_price", "discount_percent", "url"]
    with open(fn, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"\nTổng {len(rows)} sản phẩm (không trùng). Đã lưu: {fn}")

    sb_url, sb_key = load_config()
    if sb_url and sb_key:
        stamp = datetime.now(timezone.utc).isoformat()
        for r in rows:
            r["updated_at"] = stamp
        ok = upsert(sb_url, sb_key, "food_products", rows, conflict="source,product_key")
        hist = [{"source": r["source"], "product_key": r["product_key"],
                 "list_price": r["list_price"], "sale_price": r["sale_price"]}
                for r in rows]
        ok = upsert(sb_url, sb_key, "price_history", hist) and ok
        print("Đã cập nhật tên và giá lên Supabase." if ok
              else "Đẩy Supabase có lỗi (xem dòng lỗi ở trên).")
    else:
        print("Chưa có bhx_config.json nên chỉ lưu CSV, chưa đẩy Supabase.")


if __name__ == "__main__":
    main()