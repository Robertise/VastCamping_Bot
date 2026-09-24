"""Gọi API tìm máy của Vast.ai và chuẩn hóa kết quả."""
import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

SEARCH_URL = "https://console.vast.ai/api/v0/bundles/"


class VastError(Exception):
    pass


def build_query(country: str, min_reliability: float, limit: int = 1000) -> dict:
    # Không lọc gpu_name phía server: số máy ở một nước ít, lấy hết rồi so khớp
    # phía client để khỏi phụ thuộc cách viết tên GPU của API.
    # Không đặt "type": mặc định của API là on-demand, dph_total là giá on-demand.
    return {
        "verified": {"eq": True},
        "rentable": {"eq": True},
        "rented": {"eq": False},
        "num_gpus": {"eq": 1},
        "reliability": {"gte": min_reliability},
        "geolocation": {"in": [country]},
        "order": [["dph_total", "asc"]],
        "limit": limit,
    }


class VastClient:
    def __init__(self, api_key: str, country: str = "VN", min_reliability: float = 0.9):
        self.country = country.upper()
        self.min_reliability = min_reliability
        self._client = httpx.AsyncClient(
            timeout=30,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        # Tài liệu gửi body trực tiếp; một số client cũ bọc trong {"q": ...}.
        # Thử kiểu trực tiếp trước, lỗi 400 thì thử kiểu bọc và nhớ kiểu chạy được.
        self._wrap_q = False

    async def close(self):
        await self._client.aclose()

    async def _post(self, body: dict) -> httpx.Response:
        delay = 5
        for _ in range(4):
            r = await self._client.post(SEARCH_URL, json=body)
            if r.status_code != 429:
                return r
            log.warning("Vast.ai trả 429, chờ %ss", delay)
            await asyncio.sleep(delay)
            delay *= 2
        return r

    async def search(self) -> list[dict]:
        query = build_query(self.country, self.min_reliability)
        try:
            r = await self._post({"q": query} if self._wrap_q else query)
            if r.status_code == 400 and not self._wrap_q:
                r2 = await self._post({"q": query})
                if r2.status_code == 200:
                    self._wrap_q = True
                    log.info("Chuyển sang kiểu body {'q': ...}")
                    r = r2
        except httpx.HTTPError as e:
            raise VastError(f"Lỗi mạng: {e}") from e
        if r.status_code == 429:
            raise VastError("Bị giới hạn tần suất (429)")
        if r.status_code != 200:
            raise VastError(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
        except ValueError as e:
            raise VastError("Response không phải JSON") from e
        offers = data.get("offers")
        if isinstance(offers, dict):  # phòng khi API trả 1 object
            offers = [offers]
        if not isinstance(offers, list):
            raise VastError(f"Response thiếu 'offers': {str(data)[:200]}")
        return filter_offers(offers, self.country, self.min_reliability)


def _reliability(o: dict) -> float:
    for k in ("reliability2", "reliability"):
        v = o.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def filter_offers(raw: list[dict], country: str, min_reliability: float) -> list[dict]:
    """Lọc lại phía client (phòng khi server bỏ qua filter), gộp theo machine_id."""
    best: dict = {}
    for o in raw:
        geo = str(o.get("geolocation") or "")
        if not geo.upper().endswith(country.upper()):
            continue
        if o.get("num_gpus") != 1:
            continue
        if o.get("verification") not in (None, "verified"):
            continue
        if o.get("rentable") is False or o.get("rented") is True:
            continue
        if _reliability(o) < min_reliability:
            continue
        mid = o.get("machine_id") or o.get("id")
        if mid is None or not o.get("gpu_name"):
            continue
        price = o.get("dph_total")
        if not isinstance(price, (int, float)):
            continue
        key = str(mid)
        if key not in best or price < best[key]["price"]:
            best[key] = {
                "key": key,
                "offer_id": o.get("id"),
                "machine_id": o.get("machine_id"),
                "gpu_name": o["gpu_name"],
                "price": float(price),
                "vram_gb": (o.get("gpu_ram") or 0) / 1024,
                "ram_gb": (o.get("cpu_ram") or 0) / 1024,
                "cpu_cores": o.get("cpu_cores_effective") or o.get("cpu_cores"),
                "disk_gb": o.get("disk_space"),
                "inet_down": o.get("inet_down"),
                "inet_up": o.get("inet_up"),
                "reliability": _reliability(o),
                "geolocation": geo,
                "cuda": o.get("cuda_max_good"),
            }
    return sorted(best.values(), key=lambda x: x["price"])
