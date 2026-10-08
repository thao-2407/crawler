# -*- coding: utf-8 -*-
import os, re, csv, json
import requests

HERE = os.path.dirname(os.path.abspath(__file__))

# Winmart ghi đơn vị kiểu "0.5KG": True = đó là KHỐI LƯỢNG CỦA GÓI (giá cho 0.5kg),
# False = giá đó đã là giá theo kg. Hãy đối chiếu 1 sản phẩm trên web rồi chỉnh.
WINMART_X_KG_IS_PACK = True

W = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|gr|g)\b", re.I)
MULT = re.compile(r"(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(kg|gr|g)\b", re.I)

NOT_EGG = re.compile(
    r"bánh|\bmì\b|sữa|bột|sốt|chả|xúc xích|nước|giò|ruốc|cuộn|chiên|kem|snack|đậu|lẩu|"
    r"bún|cháo|kẹo|socola|sô cô la|cá viên|nấu sẵn|ăn liền|tráng miệng|gia vị", re.I)


def load_config():
    cfg = os.path.join(HERE, "bhx_config.json")
    d = json.load(open(cfg, encoding="utf-8-sig"))
    return d["url"].rstrip("/"), d["key"]


def headers(key):
    h = {"apikey": key, "Content-Type": "application/json"}
    if not key.startswith("sb_"):
        h["Authorization"] = f"Bearer {key}"
    return h


def weight_kg(text):
    """Khối lượng (kg) ghi trong chuỗi, hoặc None."""
    if not text:
        return None
    m = MULT.search(text)
    if m:
        n = float(m.group(2).replace(",", ".")) * int(m.group(1))
        u = m.group(3).lower()
    else:
        m = W.search(text)
        if not m:
            return None
        n = float(m.group(1).replace(",", "."))
        u = m.group(2).lower()
    kg = n if u == "kg" else n / 1000
    return kg if kg > 0 else None


def is_egg(name):
    n = name.lower()
    return bool(re.search(r"\btrứng\b", n)) and not NOT_EGG.search(n)


def egg_count(name):
    n = name.lower()
    for pat in (r"(\d+)\s*(?:quả|trái|trứng)", r"(?:vỉ|hộp|khay|túi|bịch|lốc)\s*(\d+)\b"):
        m = re.search(pat, n)
        if m and 1 <= int(m.group(1)) <= 100:
            return int(m.group(1))
    return None


def normalize(r):
    price = r.get("list_price")
    if not price:
        return None
    name = r.get("name") or ""
    unit = (r.get("unit") or "").strip()
    src = r.get("source") or ""

    if is_egg(name):
        n = egg_count(name)
        return ("quả", round(price / n, 1), f"tên ghi {n} quả") if n else None

    if re.fullmatch(r"kg\.?", unit, re.I):
        return ("kg", round(price), "web ghi giá theo kg")

    kg = weight_kg(unit)
    if kg:
        if src == "winmart" and not WINMART_X_KG_IS_PACK:
            return ("kg", round(price), "winmart: giá theo kg")
        return ("kg", round(price / kg), "khối lượng ghi ở đơn vị")

    kg = weight_kg(name)
    if kg:
        return ("kg", round(price / kg), "khối lượng ghi trong tên")
    return None


def main():
    url, key = load_config()
    rows, offset = [], 0
    while True:
        r = requests.get(
            f"{url}/rest/v1/food_products", headers=headers(key), timeout=60,
            params={"select": "source,product_key,name,category,unit,list_price",
                    "order": "source,product_key", "limit": 1000, "offset": offset})
        if r.status_code >= 300:
            print("Lỗi đọc Supabase:", r.status_code, r.text[:300])
            return
        chunk = r.json()
        rows += chunk
        if len(chunk) < 1000:
            break
        offset += 1000
    print(f"Đọc được {len(rows)} sản phẩm.")

    payload, unresolved, n_kg, n_egg = [], [], 0, 0
    for r in rows:
        res = normalize(r)
        item = {"source": r["source"], "product_key": r["product_key"], "name": r["name"],
                "unit_std": None, "price_std": None, "std_basis": None}
        if res:
            item["unit_std"], item["price_std"], item["std_basis"] = res
            if res[0] == "kg":
                n_kg += 1
            else:
                n_egg += 1
        else:
            unresolved.append(r)
        payload.append(item)

    ok = True
    for i in range(0, len(payload), 100):
        resp = requests.post(
            f"{url}/rest/v1/food_products?on_conflict=source,product_key",
            headers={**headers(key), "Prefer": "resolution=merge-duplicates,return=minimal"},
            data=json.dumps(payload[i:i + 100]), timeout=60)
        if resp.status_code >= 300:
            print("Lỗi Supabase:", resp.status_code, resp.text[:400])
            ok = False
            break

    print(f"Đã quy về kg: {n_kg} sản phẩm | quy về quả (trứng): {n_egg} sản phẩm")
    print(f"Chưa quy đổi được: {len(unresolved)} sản phẩm (không ghi khối lượng/số quả)")
    if unresolved:
        with open("chua_chuan_hoa.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["source", "category", "name", "unit", "list_price"],
                               extrasaction="ignore")
            w.writeheader()
            w.writerows(unresolved)
        print("Danh sách đã lưu vào chua_chuan_hoa.csv. Một vài mẫu:")
        for r in unresolved[:15]:
            print(f"  - [{r['source']}] {r['name']} ({r.get('unit') or '-'}) - {r['list_price']}")
    print("Đã cập nhật Supabase." if ok else "Cập nhật Supabase có lỗi.")


if __name__ == "__main__":
    main()