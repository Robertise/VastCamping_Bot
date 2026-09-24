"""Kiểm tra API Vast.ai trước khi chạy bot. Không cần Telegram.

Cách dùng:
    python check_api.py          # dùng COUNTRY trong .env (mặc định VN)
    python check_api.py US       # thử với US để chắc chắn có kết quả
"""
import asyncio
import os
import sys
from collections import Counter

from dotenv import load_dotenv

import gpu
from vast import SEARCH_URL, VastClient, VastError, build_query

load_dotenv()


async def main():
    country = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("COUNTRY", "VN")).upper()
    rel = float(os.getenv("MIN_RELIABILITY", "0.9"))
    c = VastClient(os.environ["VAST_API_KEY"], country, rel)
    print(f"Gọi {SEARCH_URL} với country={country}, reliability>={rel}")

    # 1. Xem response thô để biết server có áp dụng filter không.
    r = await c._post(build_query(country, rel))
    print(f"HTTP {r.status_code} (body trực tiếp)")
    if r.status_code != 200:
        print(r.text[:500])
    else:
        raw = r.json().get("offers", [])
        geos = Counter(str(o.get("geolocation", "")).split(",")[-1].strip() for o in raw)
        print(f"Server trả {len(raw)} offer thô. Quốc gia: {dict(geos.most_common(5))}")
        print(f"num_gpus: {dict(Counter(o.get('num_gpus') for o in raw))}")
        if raw:
            o = raw[0]
            print("Các field mẫu:", {k: o.get(k) for k in (
                "id", "machine_id", "gpu_name", "dph_total", "gpu_ram", "cpu_ram",
                "inet_down", "inet_up", "reliability", "reliability2",
                "verification", "geolocation", "num_gpus")})
        if geos and set(geos) != {country}:
            print("⚠️ Server KHÔNG lọc theo quốc gia; bot vẫn lọc lại phía client, "
                  "nhưng có thể bị cắt bởi limit.")

    # 2. Chạy đúng đường mà bot dùng.
    try:
        offers = await c.search()
    except VastError as e:
        print("❌ search() lỗi:", e)
        await c.close()
        return
    print(f"\n✅ Sau khi lọc: {len(offers)} máy (kiểu body {'q' if c._wrap_q else 'trực tiếp'})")
    names = Counter(o["gpu_name"] for o in offers)
    for name, n in names.most_common():
        print(f"  {name!r:28} x{n}  -> /gpu {gpu.normalize(name)}")
    for o in offers[:3]:
        print(f"  mẫu: {o['gpu_name']} ${o['price']:.3f}/h VRAM {o['vram_gb']:.0f}GB "
              f"rel {o['reliability']:.3f} {o['geolocation']}")
    await c.close()


asyncio.run(main())
