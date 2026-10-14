# -*- coding: utf-8 -*-
import os, re, sys, csv, json
from datetime import datetime, timezone
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE = "https://www.bachhoaxanh.com"
SOURCE = "bachhoaxanh"

# Nấm / trái cây: nếu chưa có trong mục rau củ, bấm vào mục trên web,
# copy URL dán thêm vào đây theo cùng kiểu.
CATEGORIES = {
    "Thịt heo": BASE + "/thit-heo",
    "Thịt bò": BASE + "/thit-bo",
    "Thịt gà, vịt": BASE + "/thit-ga",
    "Cá, tôm, mực, hải sản": BASE + "/ca-tom-muc-ech",
    "Trứng": BASE + "/trung",
    "Rau củ, trái cây": BASE + "/rau-cu-trai-cay",
    "Rau lá": BASE + "/rau-sach-bach-hoa-xanh",
}

SETUP = "--setup" in sys.argv   # chọn khu vực (không bắt buộc)
SHOW = "--show" in sys.argv     # chạy có hiện trình duyệt
DEBUG = "--debug" in sys.argv   # lưu file html để kiểm tra
HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, "bhx_profile")

PRICE = re.compile(r"(\d{1,3}(?:\.\d{3})+)\s*[đ₫]")
PERKG = re.compile(r"\(\s*(\d{1,3}(?:\.\d{3})+)\s*[đ₫]\s*/[^)]*\)")
UNIT = re.compile(r"/\s*(\d+(?:[.,]\d+)?\s*(?:kg|g|ml|l)\b)", re.I)
DISC = re.compile(r"-\s*(\d{1,2})\s*%")


def money(x):
    return f"{x:,.0f}".replace(",", ".") + "đ" if x else "-"


def num(s):
    return float(s.replace(".", ""))


def slug(u):
    return u.rstrip("/").split("/")[-1]


def unit_kg(unit):
    m = re.match(r"^(\d+(?:[.,]\d+)?)(kg|g)$", unit or "", re.I)
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    return n if m.group(2).lower() == "kg" else n / 1000


def is_product(u):
    m = re.match(r"^https://www\.bachhoaxanh\.com/([^/?#]+)/([^/?#]+)$", u or "")
    return bool(m) and m.group(1) != "kinh-nghiem-hay" and not re.search(r"-ct\d+$", m.group(2))


def load_config():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    cfg = os.path.join(HERE, "bhx_config.json")
    if (not url or not key) and os.path.exists(cfg):
        d = json.load(open(cfg, encoding="utf-8-sig"))
        url = url or d.get("url", "")
        key = key or d.get("key", "")
    return url.rstrip("/"), key


def find_card(a):
    node = a
    for _ in range(7):
        node = node.parent
        if node is None:
            return None
        if PRICE.search(node.get_text(" ", strip=True)):
            links = {urljoin(BASE, x["href"]) for x in node.find_all("a", href=True)}
            links = {l for l in links if is_product(l)}
            return node if len(links) == 1 else None
    return None


def parse(html, category):
    """Trả về (sản phẩm đọc được giá, tập các thẻ sản phẩm có hình)."""
    soup = BeautifulSoup(html, "html.parser")
    rows, listing = {}, set()
    for a in soup.find_all("a", href=True):
        url = urljoin(BASE, a["href"])
        if not is_product(url):
            continue
        key = slug(url)
        if a.find("img") is not None:
            listing.add(key)
        card = find_card(a)
        if card is None or card.find("img") is None:  # bỏ chữ mô tả SEO
            continue
        text = card.get_text(" ", strip=True)
        pk = PERKG.search(text)
        site_per_kg = num(pk.group(1)) if pk else None
        clean = PERKG.sub(" ", text)
        prices = [num(x) for x in PRICE.findall(clean)]
        if not prices:
            continue
        dm = DISC.search(clean)
        if dm and len(prices) >= 2:
            sale, lst = min(prices), max(prices)
        else:
            sale = lst = prices[0]
        h = card.find(["h3", "h2"])
        name = h.get_text(strip=True) if h else (a.get("title") or a.get_text(strip=True))
        if not name:
            continue
        um = UNIT.search(clean)
        unit = um.group(1).replace(" ", "") if um else None

        # giá/kg tính theo GIÁ GỐC
        kg = unit_kg(unit)
        if kg:
            per_kg = round(lst / kg)
        elif lst > sale:
            per_kg = None          # không tính được theo giá gốc
        else:
            per_kg = site_per_kg   # không giảm giá: giá/kg của trang là đúng

        row = {
            "source": SOURCE, "product_key": key, "name": name,
            "category": category, "unit": unit,
            "list_price": lst, "sale_price": sale, "is_on_sale": lst > sale,
            "price_per_kg": per_kg,
            "discount_percent": float(dm.group(1)) if dm and lst > sale else None,
            "url": url,
        }
        if key not in rows or row["is_on_sale"]:
            rows[key] = row
    return rows, listing


def load_all(page, url, category):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    rows, listing = {}, set()

    def collect():
        html = page.content()
        r, l = parse(html, category)
        for k, v in r.items():
            if k not in rows or v["is_on_sale"]:
                rows[k] = v
        listing.update(l)
        return html

    # 1) cuộn xuống tận đáy cho tới khi trang không dài thêm
    last_h, same = 0, 0
    for _ in range(40):
        h = page.evaluate("document.documentElement.scrollHeight")
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        page.wait_for_timeout(1200)
        same = same + 1 if h == last_h else 0
        last_h = h
        if same >= 3:
            break

    # 2) cuộn ngược từ đáy lên đầu từng đoạn nhỏ, đọc ở mỗi đoạn
    html = ""
    y = page.evaluate("document.documentElement.scrollHeight")
    while y > -600:
        page.evaluate(f"window.scrollTo(0, {max(y, 0)})")
        page.wait_for_timeout(700)
        html = collect()
        y -= 500
    return rows, listing, html


def print_category(cat, rows, listing):
    print(f"\n=== {cat}: {len(rows)} sản phẩm ===")
    for i, r in enumerate(rows.values(), 1):
        unit = f" ({r['unit']})" if r["unit"] else ""
        print(f"{i:>3}. {r['name']}{unit} - {money(r['list_price'])}")
    missing = sorted(listing - set(rows))
    if missing:
        print(f"  (!) {len(missing)} sản phẩm thấy trên trang nhưng chưa đọc được giá: "
              + ", ".join(missing))


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
            input("Chọn khu vực/cửa hàng trên trình duyệt, xong bấm Enter ở đây...")
            ctx.close()
            return

        for n, (cat, url) in enumerate(CATEGORIES.items()):
            print(f"Đang lấy: {cat} ...")
            try:
                rows, listing, html = load_all(page, url, cat)
            except Exception as e:
                print("  Lỗi:", e)
                continue
            print_category(cat, rows, listing)
            if DEBUG or not rows:
                open(f"debug_{n}.html", "w", encoding="utf-8").write(html)
            for k, v in rows.items():
                if k not in all_rows:
                    all_rows[k] = v
        ctx.close()

    rows = list(all_rows.values())
    if not rows:
        print("\nKhông lấy được sản phẩm nào.")
        return

    fn = f"bhx_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
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