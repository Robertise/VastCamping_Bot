"""Chuẩn hóa tên GPU để so khớp lệnh người dùng với tên GPU trong API Vast.ai.

Ví dụ: "RTX 5070 Ti", "rtx5070ti", "5070ti", "GeForce RTX 5070 Ti" -> "5070ti".
So khớp là bằng nhau tuyệt đối, nên "3090" không khớp "3090ti".
Thêm dấu * ở cuối để khớp theo tiền tố: "h100*" khớp "H100 SXM", "H100 PCIE".
"""
import re

_BRAND = ("nvidia", "geforce")
_FAMILY = ("rtx", "gtx", "tesla", "quadro")

# Dùng khi người dùng gõ tên có dấu cách, ví dụ "/gpu RTX 5070 Ti 3090".
_PREFIX_WORDS = {"nvidia", "geforce", "rtx", "gtx", "tesla", "quadro", "pro"}
_SUFFIX_WORDS = {"ti", "super", "sxm", "sxm4", "sxm5", "pcie", "nvl", "ada", "d"}


def normalize(name: str) -> str:
    s = re.sub(r"[^a-z0-9*]", "", name.lower())
    # Vast.ai viết Super là "S" và Ti Super là "S Ti" ("RTX 4070S Ti", "RTX 4080S").
    s = s.replace("tisuper", "sti").replace("super", "s")
    changed = True
    while changed:
        changed = False
        for p in _BRAND:
            if s.startswith(p):
                s = s[len(p):]
                changed = True
    for p in _FAMILY:
        if s.startswith(p) and len(s) > len(p):
            s = s[len(p):]
            break
    return s


def matches(pattern: str, gpu_name: str) -> bool:
    n = normalize(gpu_name)
    if pattern.endswith("*"):
        return n.startswith(pattern[:-1])
    return n == pattern


def parse_args(args: list[str]) -> list[str]:
    """Tách danh sách GPU từ tham số lệnh thành các mẫu đã chuẩn hóa.

    Hỗ trợ dấu phẩy ("rtx 5070 ti, 3090") và gộp từ rời ("RTX 5070 Ti 3090").
    """
    text = " ".join(args).strip()
    if not text:
        return []
    if "," in text:
        chunks = [c for c in (x.strip() for x in text.split(",")) if c]
    else:
        chunks = []
        pending_prefix = ""
        for tok in text.split():
            low = tok.lower()
            if low in _PREFIX_WORDS:
                pending_prefix += tok
                continue
            if low in _SUFFIX_WORDS and chunks and not pending_prefix:
                chunks[-1] += tok
                continue
            chunks.append(pending_prefix + tok)
            pending_prefix = ""
    out = []
    for c in chunks:
        n = normalize(c)
        if n and n != "*" and n not in out:
            out.append(n)
    return out
