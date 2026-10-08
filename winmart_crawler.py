# -*- coding: utf-8 -*-
import os, re, sys, csv, json
from datetime import datetime, timezone
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE = "https://winmart.vn"
SOURCE = "winmart"

# Vị trí giả lập để Winmart tự chọn cửa hàng gần đó (mặc định: trung tâm TP.HCM).
# Bạn ở nơi khác thì đổi 2 số này (vĩ độ, kinh độ).
GEO = (10.7769, 106.7009)

# Muốn tự chọn mục thì điền vào đây (để trống = tự dò từ menu của web)
CATEGORIES_OVERRIDE = {
    # "Thịt - Hải Sản Tươi": BASE + "/thit-hai-san-tuoi--c03",
}
FALLBACK = {"Thịt - Hải Sản Tươi": BASE + "/thit-hai-san-tuoi--c03"}

KEEP = re.compile(r"thịt|hải sản|\bcá\b|trứng|\brau\b|\bcủ\b|\bquả\b|nấm|trái cây|hoa quả|\btôm\b|\bmực\b", re.I)
SKIP = re.compile(r"cá nhân|chế biến|đông lạnh|\bkhô\b|sấy|đóng hộp|\bhộp\b|gia vị|nước|sữa|bánh|kẹo|\bmì\b|chăm sóc|đồ uống|\bdầu\b|mắm|snack|hóa phẩm|thú cưng|nêm", re.I)
CODE = re.compile(r"--(c[0-9a-z]+)$", re.I)

SETUP = "--setup" in sys.argv   # (dự phòng) tự chọn cửa hàng bằng tay
SHOW = "--show" in sys.argv     # chạy có hiện trình duyệt
LIST = "--list" in sys.argv     # chỉ in các mục sẽ crawl rồi thoát
DEBUG = "--debug" in sys.argv   # lưu html + log API để kiểm tra
HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, "winmart_profile")

LEAF = re.compile(r"^\s*\d{1,3}(?:\.\d{3})+\s*(?:₫|đ)?\s*$")
DISC = re.compile(r"-\s*(\d{1,2})\s*%")
UNIT_ONLY = re.compile(
    r"^(\d+(?:[.,]\d+)?\s*(?:kg|g|gr|l|ml)|gói|khay|hộp|túi|chai|lon|cái|quả|bó|con|kg|vỉ|miếng|củ|mớ|chục)\.?$",
    re.I)
WEIGHT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|gr|g)\b", re.I)
BAD_LINES = {"mua", "thêm", "thêm vào giỏ", "thêm vào giỏ hàng", "giỏ hàng", "chọn mua"}
POPUP_BTN = re.compile(r"^(Đồng ý|Cho phép|Đã hiểu|Đóng|Bỏ qua|Để sau|Tiếp tục)$", re.I)
NO_STORE = "chưa được phục vụ"


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


def dismiss_popups(page):
    for _ in range(3):
        hit = False
        try:
            for b in page.get_by_role("button", name=POPUP_BTN).all():
                try:
                    if b.is_visible():
                        b.click(timeout=2000)
                        hit = True
                        page.wait_for_timeout(800)
                except Exception:
                    pass
        except Exception:
            pass
        if not hit:
            break


def open_page(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    dismiss_popups(page)
    if NO_STORE in page.content():  # chưa có cửa hàng -> thử tải lại 1 lần
        page.wait_for_timeout(3000)
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        dismiss_popups(page)


# ---------- dò mục từ menu ----------
def discover(page):
    open_page(page, BASE)
    soup = BeautifulSoup(page.content(), "html.parser")
    found = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(BASE, a["href"]).split("?")[0].split("#")[0]
        text = a.get_text(" ", strip=True)
        if CODE.search(url) and text and len(text) <= 40 and a.find("img") is None:
            found.setdefault(url, text)
    return found


def pick(found):
    chosen, codes = {}, []
    for url, text in sorted(found.items(), key=lambda kv: len(CODE.search(kv[0]).group(1))):
        code = CODE.search(url).group(1).lower()
        if not KEEP.search(text) or SKIP.search(text):
            continue
        if any(code.startswith(c) for c in codes):  # mục con đã nằm trong mục cha
            continue
        codes.append(code)
        chosen[text] = url
    return chosen


# ---------- đọc sản phẩm ----------
def card_of(leaf):
    node = leaf.parent
    for _ in range(10):
        if node is None:
            return None
        if node.find("img") is not None:
            return node
        node = node.parent
    return None


def parse(html, category):
    soup = BeautifulSoup(html, "html.parser")
    rows, done = {}, set()
    for leaf in soup.find_all(string=LEAF):
        card = card_of(leaf)
        if card is None or id(card) in done:
            continue
        done.add(id(card))
        leaves = card.find_all(string=LEAF)
        if len(leaves) > 3:  # khung chứa nhiều sản phẩm, bỏ qua
            continue
        prices = [num(s) for s in leaves][:2]
        text = card.get_text(" ", strip=True)
        dm = DISC.search(text)
        if len(prices) >= 2:
            sale, lst = min(prices), max(prices)
        else:
            sale = lst = prices[0]

        lines = []
        for t in card.get_text("\n", strip=True).split("\n"):
            t = t.strip()
            if t and t not in lines:
                lines.append(t)
        name = next((t for t in lines
                     if len(t) >= 4 and not LEAF.match(t) and not DISC.fullmatch(t)
                     and not UNIT_ONLY.match(t) and t.lower() not in BAD_LINES), None)
        if not name:
            continue
        unit = " ".join(t for t in lines if UNIT_ONLY.match(t)) or None

        a = card.find("a", href=True) or card.find_parent("a", href=True)
        url = urljoin(BASE, a["href"]) if a else None
        if url and url.rstrip("/") != BASE:
            key = url.rstrip("/").split("/")[-1]
        else:
            key = re.sub(r"\W+", "-", name.lower()).strip("-")

        # giá/kg chỉ tính khi tên ghi rõ khối lượng (vd "khay 500g")
        w = WEIGHT.search(name)
        kg = None
        if w:
            n = float(w.group(1).replace(",", "."))
            kg = n if w.group(2).lower() == "kg" else n / 1000
        per_kg = round(lst / kg) if kg else None

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
    return rows


MORE = re.compile(r"^\s*(Xem thêm|Hiển thị thêm|Tải thêm)(\s+\d+)?(\s+sản phẩm)?\s*$", re.I)


def click_more(page):
    loc = page.get_by_text(MORE)
    for i in range(loc.count()):
        try:
            el = loc.nth(i)
            if not el.is_visible():
                continue
            before = page.url
            el.scroll_into_view_if_needed()
            el.click(timeout=3000)
            page.wait_for_timeout(1500)
            if page.url != before:
                page.go_back()
                page.wait_for_timeout(1500)
        except Exception:
            pass


def load_all(page, url, category):
    open_page(page, url)
    rows = {}

    def collect():
        html = page.content()
        for k, v in parse(html, category).items():
            if k not in rows or v["is_on_sale"]:
                rows[k] = v
        return html

    last_h, same = 0, 0
    for _ in range(40):
        h = page.evaluate("document.documentElement.scrollHeight")
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        page.wait_for_timeout(1200)
        click_more(page)
        same = same + 1 if h == last_h else 0
        last_h = h
        if same >= 3:
            break

    html = ""
    y = page.evaluate("document.documentElement.scrollHeight")
    while y > -600:
        page.evaluate(f"window.scrollTo(0, {max(y, 0)})")
        page.wait_for_timeout(700)
        html = collect()
        y -= 500
    return rows, html


def print_category(cat, rows):
    print(f"\n=== {cat}: {len(rows)} sản phẩm ===")
    for i, r in enumerate(rows.values(), 1):
        unit = f" ({r['unit']})" if r["unit"] else ""
        print(f"{i:>3}. {r['name']}{unit} - {money(r['list_price'])}")


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
    api_log, all_rows = [], {}
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, headless=not (SETUP or SHOW), locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            geolocation={"latitude": GEO[0], "longitude": GEO[1]},
            permissions=["geolocation"],
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1366, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if SETUP:
            page.goto(BASE)
            input("Chọn cửa hàng WinMart trên trình duyệt (nếu cần), xong bấm Enter ở đây...")
            ctx.close()
            return

        if DEBUG:
            def on_response(resp):
                try:
                    if resp.request.resource_type in ("xhr", "fetch") \
                            and "json" in resp.headers.get("content-type", ""):
                        body = resp.text()
                        if "rice" in body:
                            api_log.append(f"{resp.status} {resp.url}\n{body[:300]}\n")
                except Exception:
                    pass
            page.on("response", on_response)

        cats = dict(CATEGORIES_OVERRIDE)
        if not cats:
            found = discover(page)
            with open("winmart_categories.txt", "w", encoding="utf-8") as f:
                for u, t in found.items():
                    f.write(f"{t}\t{u}\n")
            cats = pick(found) or FALLBACK
        print("Các mục sẽ lấy:")
        for t, u in cats.items():
            print(f"  - {t}: {u}")
        if LIST:
            ctx.close()
            return

        for n, (cat, url) in enumerate(cats.items()):
            print(f"\nĐang lấy: {cat} ...")
            try:
                rows, html = load_all(page, url, cat)
            except Exception as e:
                print("  Lỗi:", e)
                continue
            if NO_STORE in html and not rows:
                print("  (!) Winmart vẫn chưa tự chọn được cửa hàng. Thử: "
                      "python winmart_crawler.py --show  (xem trình duyệt có hỏi gì không).")
            print_category(cat, rows)
            if DEBUG or not rows:
                open(f"winmart_debug_{n}.html", "w", encoding="utf-8").write(html)
            for k, v in rows.items():
                if k not in all_rows:
                    all_rows[k] = v
        ctx.close()

    if DEBUG:
        open("winmart_api.txt", "w", encoding="utf-8").write("\n".join(api_log))
        print(f"\nĐã ghi {len(api_log)} request API vào winmart_api.txt")

    rows = list(all_rows.values())
    if not rows:
        print("\nKhông lấy được sản phẩm nào.")
        return

    fn = f"winmart_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
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