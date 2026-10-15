# -*- coding: utf-8 -*-
import os, re, csv, json
import requests

HERE = os.path.dirname(os.path.abspath(__file__))

# Winmart ghi đơn vị kiểu "0.5KG": True = đó là KHỐI LƯỢNG CỦA GÓI (giá cho 0.5kg),
# False = giá đó đã là giá theo kg.
WINMART_X_KG_IS_PACK = True

# Xóa các sản phẩm không quy đổi được (unit_std/price_std trống)
DELETE_UNRESOLVED = True
# An toàn: nếu định xóa quá tỉ lệ này trên tổng số sản phẩm thì dừng, không xóa
MAX_DELETE_RATIO = 0.4

NUM = r"(\d+(?:[.,]\d+)?)"
UNIT = r"(kilogam|kg|ký|kí|gram|gam|gr|g)(?![^\W\d_])"
W = re.compile(NUM + r"\s*" + UNIT, re.I)
MULT_PRE = re.compile(r"(?<!\d)(\d{1,2})\s*[x×]\s*" + NUM + r"\s*" + UNIT, re.I)    # 3 x 100g
MULT_POST = re.compile(NUM + r"\s*" + UNIT + r"\s*[x×]\s*(\d{1,2})(?!\d)", re.I)   # 100g x 3

NOT_EGG = re.compile(
    r"bánh|\bmì\b|sữa|bột|sốt|chả|xúc xích|nước|giò|ruốc|cuộn|chiên|kem|snack|đậu|lẩu|"
    r"bún|cháo|kẹo|socola|sô cô la|cá viên|nấu sẵn|ăn liền|tráng miệng|gia vị", re.I)

CONTAINER = re.compile(r"\b(vỉ|hộp|khay|túi|bịch|lốc)\b", re.I)
KEY_CONTAINER = re.compile(r"\b(vi|hop|khay|tui|bich|loc)\b")
CONTAINER_VN = {"vi": "vỉ", "hop": "hộp", "khay": "khay",
                "tui": "túi", "bich": "bịch", "loc": "lốc"}


def load_config():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    cfg = os.path.join(HERE, "bhx_config.json")
    if (not url or not key) and os.path.exists(cfg):
        d = json.load(open(cfg, encoding="utf-8-sig"))
        url = url or d.get("url", "")
        key = key or d.get("key", "")
    return url.rstrip("/"), key


def headers(key):
    h = {"apikey": key, "Content-Type": "application/json"}
    if not key.startswith("sb_"):
        h["Authorization"] = f"Bearer {key}"
    return h


def to_kg(n, u):
    return n if u.lower() in ("kg", "kilogam", "ký", "kí") else n / 1000


def key_text(key):
    """Biến product_key (dạng đường dẫn) thành chữ thường để đọc khối lượng."""
    k = (key or "").lower()
    k = re.sub(r"--[a-z]+\d+$", "", k)                                    # bỏ mã cuối
    k = re.sub(r"(?<!\d)(\d{1,2})-(\d)(kg)(?![a-z0-9])", r"\1.\2\3", k)  # 1-5kg -> 1.5kg
    return k.replace("-", " ").replace("_", " ")


def weight_kg(text):
    """Khối lượng (kg) ghi trong chuỗi, hoặc None."""
    if not text:
        return None
    m = MULT_PRE.search(text)
    if m:
        kg = int(m.group(1)) * to_kg(float(m.group(2).replace(",", ".")), m.group(3))
    else:
        m = MULT_POST.search(text)
        if m:
            kg = int(m.group(3)) * to_kg(float(m.group(1).replace(",", ".")), m.group(2))
        else:
            m = W.search(text)
            if not m:
                return None
            kg = to_kg(float(m.group(1).replace(",", ".")), m.group(2))
    return kg if 0.005 <= kg <= 30 else None


def is_egg(name):
    n = name.lower()
    return bool(re.search(r"\btrứng\b", n)) and not NOT_EGG.search(n)


def egg_count(text, key=""):
    t = (text or "").lower()
    for pat in (r"(\d+)\s*(?:quả|trái|trứng)", r"(?:vỉ|hộp|khay|túi|bịch|lốc)\s*(\d+)\b"):
        m = re.search(pat, t)
        if m and 1 <= int(m.group(1)) <= 100:
            return int(m.group(1))
    k = key_text(key)
    for pat in (r"(\d+)\s*(?:qua|trung)\b", r"(?:vi|hop|khay|tui|bich|loc)\s*(\d+)\b"):
        m = re.search(pat, k)
        if m and 1 <= int(m.group(1)) <= 100:
            return int(m.group(1))
    return None


def egg_container(name, unit, key):
    for text in (unit, name):
        m = CONTAINER.search(text or "")
        if m:
            return m.group(1).lower()
    m = KEY_CONTAINER.search(key_text(key))
    return CONTAINER_VN[m.group(1)] if m else None


def normalize(r):
    price = r.get("list_price")
    if not price:
        return None
    name = r.get("name") or ""
    unit = (r.get("unit") or "").strip()
    src = r.get("source") or ""
    pkey = r.get("product_key") or ""

    if is_egg(name):
        n = egg_count(f"{name} {unit}", pkey)
        if not n:
            return None
        box = egg_container(name, unit, pkey)
        label = f"{box} {n} quả" if box else f"{n} quả"
        return (label, round(price), f"giá nguyên {box or 'gói'}, {n} quả")

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

    kg = weight_kg(key_text(pkey))
    if kg:
        return ("kg", round(price / kg), "khối lượng ghi trong product_key")
    return None


def delete_keys(url, key, table, rows):
    """Xóa các dòng theo (source, product_key). Trả về số dòng đã xóa, hoặc None nếu lỗi."""
    by_src = {}
    for r in rows:
        by_src.setdefault(r["source"], []).append(r["product_key"])
    total = 0
    for src, keys in by_src.items():
        for i in range(0, len(keys), 40):
            chunk = keys[i:i + 40]
            in_list = "in.(" + ",".join('"' + k.replace('"', "") + '"' for k in chunk) + ")"
            resp = requests.delete(
                f"{url}/rest/v1/{table}",
                headers={**headers(key), "Prefer": "return=representation"},
                params={"source": f"eq.{src}", "product_key": in_list, "select": "product_key"},
                timeout=60)
            if resp.status_code >= 300:
                print(f"Lỗi xóa ở bảng {table}:", resp.status_code, resp.text[:300])
                return None
            try:
                total += len(resp.json())
            except Exception:
                pass
    return total


def main():
    url, key = load_config()
    if not url or not key:
        print("Chưa có cấu hình Supabase (bhx_config.json hoặc biến môi trường).")
        return
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

    payload, unresolved, basis = [], [], {}
    n_kg = n_egg = 0
    for r in rows:
        res = normalize(r)
        item = {"source": r["source"], "product_key": r["product_key"], "name": r["name"],
                "unit_std": None, "price_std": None, "std_basis": None}
        if res:
            item["unit_std"], item["price_std"], item["std_basis"] = res
            label = "trứng (giá nguyên vỉ/hộp)" if res[2].startswith("giá nguyên") else res[2]
            basis[label] = basis.get(label, 0) + 1
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

    print(f"Đã quy về kg: {n_kg} | trứng (giá cả vỉ/hộp): {n_egg}")
    for k, v in basis.items():
        print(f"   - {k}: {v}")
    print(f"Không quy đổi được: {len(unresolved)} sản phẩm "
          "(đơn vị, tên và product_key đều không ghi khối lượng/số quả)")

    if unresolved:
        with open("da_xoa_khong_quy_doi_duoc.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["source", "category", "name", "unit", "list_price", "product_key"],
                               extrasaction="ignore")
            w.writeheader()
            w.writerows(unresolved)
        for r in unresolved[:15]:
            print(f"  - [{r['source']}] {r['name']} ({r.get('unit') or '-'}) - {r['list_price']}")

        if ok and DELETE_UNRESOLVED:
            if len(unresolved) > MAX_DELETE_RATIO * len(rows):
                print(f"(!) Định xóa {len(unresolved)}/{len(rows)} sản phẩm, vượt mức an toàn "
                      f"{int(MAX_DELETE_RATIO * 100)}%. KHÔNG xóa gì. Hãy gửi danh sách mẫu ở trên cho mình xem.")
            else:
                d1 = delete_keys(url, key, "price_history", unresolved)
                d2 = delete_keys(url, key, "food_products", unresolved)
                if d1 is None or d2 is None:
                    print("Xóa có lỗi (thường do chưa cấp quyền xóa trên Supabase, xem lỗi ở trên).")
                elif d2 < len(unresolved):
                    print(f"(!) Chỉ xóa được {d2}/{len(unresolved)} sản phẩm: có thể thiếu "
                          "quyền xóa (policy) trên Supabase.")
                else:
                    print(f"Đã xóa {d2} sản phẩm không quy đổi được (cùng lịch sử giá của chúng).")
    print("Đã cập nhật Supabase." if ok else "Cập nhật Supabase có lỗi.")


if __name__ == "__main__":
    main()
