"""零依赖 QR 码编码器（仅供 ClawBot 扫码登录使用）。

设计约束：
  - 只用 Python 标准库；PNG 输出用 Pillow（已随运行环境提供），若缺失则降级为 ASCII。
  - 只做 byte 模式（登录二维码内容是 URL），覆盖版本 1..40、L/M/Q/H 四级纠错。
  - 不做结构化追加、不做 Kanji 模式 —— 登录二维码用不到。

分块表与对齐图形位置表取自 ISO/IEC 18004 标准参数（参数按 ISO/IEC 18004 标准（与公开实现一致））。
"""

from __future__ import annotations

from typing import List, Sequence

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

MODE_8BIT_BYTE = 4

ERROR_CORRECT_L = 1
ERROR_CORRECT_M = 0
ERROR_CORRECT_Q = 3
ERROR_CORRECT_H = 2

# 纠错等级 -> RS_BLOCK_TABLE 的列偏移
_RS_BLOCK_OFFSET = {
    ERROR_CORRECT_L: 0,
    ERROR_CORRECT_M: 1,
    ERROR_CORRECT_Q: 2,
    ERROR_CORRECT_H: 3,
}

# 每 4 行一个版本，顺序为 L / M / Q / H。
# 每个三元组 = (块数, 块总码字数, 块数据码字数)
_RS_BLOCK_TABLE = (
    # 1
    (1, 26, 19), (1, 26, 16), (1, 26, 13), (1, 26, 9),
    # 2
    (1, 44, 34), (1, 44, 28), (1, 44, 22), (1, 44, 16),
    # 3
    (1, 70, 55), (1, 70, 44), (2, 35, 17), (2, 35, 13),
    # 4
    (1, 100, 80), (2, 50, 32), (2, 50, 24), (4, 25, 9),
    # 5
    (1, 134, 108), (2, 67, 43), (2, 33, 15, 2, 34, 16), (2, 33, 11, 2, 34, 12),
    # 6
    (2, 86, 68), (4, 43, 27), (4, 43, 19), (4, 43, 15),
    # 7
    (2, 98, 78), (4, 49, 31), (2, 32, 14, 4, 33, 15), (4, 39, 13, 1, 40, 14),
    # 8
    (2, 121, 97), (2, 60, 38, 2, 61, 39), (4, 40, 18, 2, 41, 19), (4, 40, 14, 2, 41, 15),
    # 9
    (2, 146, 116), (3, 58, 36, 2, 59, 37), (4, 36, 16, 4, 37, 17), (4, 36, 12, 4, 37, 13),
    # 10
    (2, 86, 68, 2, 87, 69), (4, 69, 43, 1, 70, 44), (6, 43, 19, 2, 44, 20), (6, 43, 15, 2, 44, 16),
    # 11
    (4, 101, 81), (1, 80, 50, 4, 81, 51), (4, 50, 22, 4, 51, 23), (3, 36, 12, 8, 37, 13),
    # 12
    (2, 116, 92, 2, 117, 93), (6, 58, 36, 2, 59, 37), (4, 46, 20, 6, 47, 21), (7, 42, 14, 4, 43, 15),
    # 13
    (4, 133, 107), (8, 59, 37, 1, 60, 38), (8, 44, 20, 4, 45, 21), (12, 33, 11, 4, 34, 12),
    # 14
    (3, 145, 115, 1, 146, 116), (4, 64, 40, 5, 65, 41), (11, 36, 16, 5, 37, 17), (11, 36, 12, 5, 37, 13),
    # 15
    (5, 109, 87, 1, 110, 88), (5, 65, 41, 5, 66, 42), (5, 54, 24, 7, 55, 25), (11, 36, 12, 7, 37, 13),
    # 16
    (5, 122, 98, 1, 123, 99), (7, 73, 45, 3, 74, 46), (15, 43, 19, 2, 44, 20), (3, 45, 15, 13, 46, 16),
    # 17
    (1, 135, 107, 5, 136, 108), (10, 74, 46, 1, 75, 47), (1, 50, 22, 15, 51, 23), (2, 42, 14, 17, 43, 15),
    # 18
    (5, 150, 120, 1, 151, 121), (9, 69, 43, 4, 70, 44), (17, 50, 22, 1, 51, 23), (2, 42, 14, 19, 43, 15),
    # 19
    (3, 141, 113, 4, 142, 114), (3, 70, 44, 11, 71, 45), (17, 47, 21, 4, 48, 22), (9, 39, 13, 16, 40, 14),
    # 20
    (3, 135, 107, 5, 136, 108), (3, 67, 41, 13, 68, 42), (15, 54, 24, 5, 55, 25), (15, 43, 15, 10, 44, 16),
    # 21
    (4, 144, 116, 4, 145, 117), (17, 68, 42), (17, 50, 22, 6, 51, 23), (19, 46, 16, 6, 47, 17),
    # 22
    (2, 139, 111, 7, 140, 112), (17, 74, 46), (7, 54, 24, 16, 55, 25), (34, 37, 13),
    # 23
    (4, 151, 121, 5, 152, 122), (4, 75, 47, 14, 76, 48), (11, 54, 24, 14, 55, 25), (16, 45, 15, 14, 46, 16),
    # 24
    (6, 147, 117, 4, 148, 118), (6, 73, 45, 14, 74, 46), (11, 54, 24, 16, 55, 25), (30, 46, 16, 2, 47, 17),
    # 25
    (8, 132, 106, 4, 133, 107), (8, 75, 47, 13, 76, 48), (7, 54, 24, 22, 55, 25), (22, 45, 15, 13, 46, 16),
    # 26
    (10, 142, 114, 2, 143, 115), (19, 74, 46, 4, 75, 47), (28, 50, 22, 6, 51, 23), (33, 46, 16, 4, 47, 17),
    # 27
    (8, 152, 122, 4, 153, 123), (22, 73, 45, 3, 74, 46), (8, 53, 23, 26, 54, 24), (12, 45, 15, 28, 46, 16),
    # 28
    (3, 147, 117, 10, 148, 118), (3, 73, 45, 23, 74, 46), (4, 54, 24, 31, 55, 25), (11, 45, 15, 31, 46, 16),
    # 29
    (7, 146, 116, 7, 147, 117), (21, 73, 45, 7, 74, 46), (1, 53, 23, 37, 54, 24), (19, 45, 15, 26, 46, 16),
    # 30
    (5, 145, 115, 10, 146, 116), (19, 75, 47, 10, 76, 48), (15, 54, 24, 25, 55, 25), (23, 45, 15, 25, 46, 16),
    # 31
    (13, 145, 115, 3, 146, 116), (2, 74, 46, 29, 75, 47), (42, 54, 24, 1, 55, 25), (23, 45, 15, 28, 46, 16),
    # 32
    (17, 145, 115), (10, 74, 46, 23, 75, 47), (10, 54, 24, 35, 55, 25), (19, 45, 15, 35, 46, 16),
    # 33
    (17, 145, 115, 1, 146, 116), (14, 74, 46, 21, 75, 47), (29, 54, 24, 19, 55, 25), (11, 45, 15, 46, 46, 16),
    # 34
    (13, 145, 115, 6, 146, 116), (14, 74, 46, 23, 75, 47), (44, 54, 24, 7, 55, 25), (59, 46, 16, 1, 47, 17),
    # 35
    (12, 151, 121, 7, 152, 122), (12, 75, 47, 26, 76, 48), (39, 54, 24, 14, 55, 25), (22, 45, 15, 41, 46, 16),
    # 36
    (6, 151, 121, 14, 152, 122), (6, 75, 47, 34, 76, 48), (46, 54, 24, 10, 55, 25), (2, 45, 15, 64, 46, 16),
    # 37
    (17, 152, 122, 4, 153, 123), (29, 74, 46, 14, 75, 47), (49, 54, 24, 10, 55, 25), (24, 45, 15, 46, 46, 16),
    # 38
    (4, 152, 122, 18, 153, 123), (13, 74, 46, 32, 75, 47), (48, 54, 24, 14, 55, 25), (42, 45, 15, 32, 46, 16),
    # 39
    (20, 147, 117, 4, 148, 118), (40, 75, 47, 7, 76, 48), (43, 54, 24, 22, 55, 25), (10, 45, 15, 67, 46, 16),
    # 40
    (19, 148, 118, 6, 149, 119), (18, 75, 47, 31, 76, 48), (34, 54, 24, 34, 55, 25), (20, 45, 15, 61, 46, 16),
)

# 每个版本的对齐图形中心坐标
_PATTERN_POSITION_TABLE = (
    [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34], [6, 22, 38], [6, 24, 42],
    [6, 26, 46], [6, 28, 50], [6, 30, 54], [6, 32, 58], [6, 34, 62],
    [6, 26, 46, 66], [6, 26, 48, 70], [6, 26, 50, 74], [6, 30, 54, 78],
    [6, 30, 56, 82], [6, 30, 58, 86], [6, 34, 62, 90], [6, 28, 50, 72, 94],
    [6, 26, 50, 74, 98], [6, 30, 54, 78, 102], [6, 28, 54, 80, 106],
    [6, 32, 58, 84, 110], [6, 30, 58, 86, 114], [6, 34, 62, 90, 118],
    [6, 26, 50, 74, 98, 122], [6, 30, 54, 78, 102, 126], [6, 26, 52, 78, 104, 130],
    [6, 30, 56, 82, 108, 134], [6, 34, 60, 86, 112, 138], [6, 30, 58, 86, 114, 142],
    [6, 34, 62, 90, 118, 146], [6, 30, 54, 78, 102, 126, 150],
    [6, 24, 50, 76, 102, 128, 154], [6, 28, 54, 80, 106, 132, 158],
    [6, 32, 58, 84, 110, 136, 162], [6, 26, 54, 82, 110, 138, 166],
    [6, 30, 58, 86, 114, 142, 170],
)

_G15 = 0b10100110111
_G18 = 0b1111100100101
_G15_MASK = 0b101010000010010

_PAD0 = 0xEC
_PAD1 = 0x11

# GF(256)，本原多项式 0x11D
_EXP = [0] * 256
_LOG = [0] * 256
for _i in range(8):
    _EXP[_i] = 1 << _i
for _i in range(8, 256):
    _EXP[_i] = _EXP[_i - 4] ^ _EXP[_i - 5] ^ _EXP[_i - 6] ^ _EXP[_i - 8]
for _i in range(255):
    _LOG[_EXP[_i]] = _i


def _gexp(n: int) -> int:
    return _EXP[n % 255]


def _glog(n: int) -> int:
    if n < 1:
        raise ValueError(f"glog({n})")
    return _LOG[n]


class DataOverflowError(ValueError):
    """数据超出所选版本容量。"""


# --------------------------------------------------------------------------
# Reed-Solomon
# --------------------------------------------------------------------------


def _rs_blocks(version: int, error_correction: int) -> List[tuple]:
    """返回 [(总码字数, 数据码字数), ...]，按块展开。"""
    offset = _RS_BLOCK_OFFSET[error_correction]
    spec = _RS_BLOCK_TABLE[(version - 1) * 4 + offset]
    blocks: List[tuple] = []
    for i in range(0, len(spec), 3):
        count, total, data = spec[i], spec[i + 1], spec[i + 2]
        blocks.extend([(total, data)] * count)
    return blocks


def _rs_generator_poly(ec_count: int) -> List[int]:
    """生成多项式 (x - a^0)(x - a^1)...(x - a^(ec_count-1))，系数按降幂。"""
    poly = [1]
    for i in range(ec_count):
        factor = [1, _gexp(i)]
        result = [0] * (len(poly) + 1)
        for a, ca in enumerate(poly):
            for b, cb in enumerate(factor):
                if ca == 0 or cb == 0:
                    continue
                result[a + b] ^= _gexp(_glog(ca) + _glog(cb))
        poly = result
    return poly


def _rs_encode(data: Sequence[int], ec_count: int) -> List[int]:
    """对一组数据码字求纠错码字。"""
    gen = _rs_generator_poly(ec_count)
    remainder = list(data) + [0] * ec_count
    for i in range(len(data)):
        coef = remainder[i]
        if coef == 0:
            continue
        for j, g in enumerate(gen):
            remainder[i + j] ^= _gexp(_glog(g) + _glog(coef))
    return remainder[len(data):]


# --------------------------------------------------------------------------
# 位缓冲
# --------------------------------------------------------------------------


class _BitBuffer:
    __slots__ = ("bits",)

    def __init__(self) -> None:
        self.bits: List[int] = []

    def put(self, value: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            self.bits.append((value >> i) & 1)

    def __len__(self) -> int:
        return len(self.bits)


# --------------------------------------------------------------------------
# 主编码器
# --------------------------------------------------------------------------


class QRCode:
    """一个编码完成的 QR 符号：版本 + 矩阵。"""

    def __init__(self, version: int, modules: List[List[bool]]) -> None:
        self.version = version
        self.modules = modules

    @property
    def size(self) -> int:
        return len(self.modules)

    # -- 输出 ---------------------------------------------------------------

    def to_ascii(self, border: int = 2, invert: bool = False) -> str:
        """终端友好输出：用两个字符宽表示一个模块，深色用 '██'。"""
        dark, light = ("  ", "██") if invert else ("██", "  ")
        lines = []
        width = self.size + border * 2
        blank = light * width
        for _ in range(border):
            lines.append(blank)
        for row in self.modules:
            line = light * border
            line += "".join(dark if cell else light for cell in row)
            line += light * border
            lines.append(line)
        for _ in range(border):
            lines.append(blank)
        return "\n".join(lines)

    def to_png_bytes(self, scale: int = 8, border: int = 4) -> bytes:
        """需要 Pillow；不可用时抛 RuntimeError，由调用方降级。"""
        try:
            from io import BytesIO

            from PIL import Image, ImageDraw
        except Exception as exc:  # pragma: no cover - 环境相关
            raise RuntimeError("需要 Pillow 才能导出 PNG") from exc

        quiet = border * scale
        total = self.size * scale + quiet * 2
        img = Image.new("RGB", (total, total), "white")
        draw = ImageDraw.Draw(img)
        for y, row in enumerate(self.modules):
            for x, cell in enumerate(row):
                if cell:
                    x0 = quiet + x * scale
                    y0 = quiet + y * scale
                    draw.rectangle([x0, y0, x0 + scale - 1, y0 + scale - 1], fill="black")
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


def _encode_data(data: bytes, version: int, error_correction: int) -> List[int]:
    """构造数据码字序列（含纠错）。"""
    blocks = _rs_blocks(version, error_correction)
    bit_limit = sum(data_count * 8 for _, data_count in blocks)
    if len(data) > (bit_limit - 4 - 8) // 8:
        raise DataOverflowError("数据超出该版本容量")

    buf = _BitBuffer()
    buf.put(MODE_8BIT_BYTE, 4)
    buf.put(len(data), 16 if version >= 10 else 8)
    for byte in data:
        buf.put(byte, 8)

    if len(buf) > bit_limit:
        raise DataOverflowError("数据超出该版本容量")

    for _ in range(min(bit_limit - len(buf), 4)):
        buf.put(0, 1)
    while len(buf) % 8:
        buf.put(0, 1)
    fill = (bit_limit - len(buf)) // 8
    for i in range(fill):
        buf.put(_PAD0 if i % 2 == 0 else _PAD1, 8)

    codewords = [
        sum(buf.bits[i + j] << (7 - j) for j in range(8))
        for i in range(0, len(buf.bits), 8)
    ]

    data_blocks: List[List[int]] = []
    ec_blocks: List[List[int]] = []
    offset = 0
    for total, data_count in blocks:
        chunk = codewords[offset:offset + data_count]
        offset += data_count
        data_blocks.append(chunk)
        ec_blocks.append(_rs_encode(chunk, total - data_count))

    out: List[int] = []
    max_data = max(len(b) for b in data_blocks)
    for i in range(max_data):
        for b in data_blocks:
            if i < len(b):
                out.append(b[i])
    max_ec = max(len(b) for b in ec_blocks)
    for i in range(max_ec):
        for b in ec_blocks:
            if i < len(b):
                out.append(b[i])
    return out


def _bch_format(data: int) -> int:
    d = data << 10
    while d.bit_length() - _G15.bit_length() >= 0:
        d ^= _G15 << (d.bit_length() - _G15.bit_length())
    return ((data << 10) | d) ^ _G15_MASK


def _mask_bit(pattern: int, i: int, j: int) -> bool:
    if pattern == 0:
        return (i + j) % 2 == 0
    if pattern == 1:
        return i % 2 == 0
    if pattern == 2:
        return j % 3 == 0
    if pattern == 3:
        return (i + j) % 3 == 0
    if pattern == 4:
        return (i // 2 + j // 3) % 2 == 0
    if pattern == 5:
        return (i * j) % 2 + (i * j) % 3 == 0
    if pattern == 6:
        return ((i * j) % 2 + (i * j) % 3) % 2 == 0
    return ((i * j) % 3 + (i + j) % 2) % 2 == 0


def _lost_point(modules: List[List[bool]]) -> int:
    """掩码评价：ISO/IEC 18004 的 N1..N4。"""
    n = len(modules)
    score = 0

    # N1：同色连续 >=5
    for line in list(modules) + [list(col) for col in zip(*modules)]:
        run = 1
        for k in range(1, n):
            if line[k] == line[k - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2

    # N2：2x2 同色块
    for i in range(n - 1):
        for j in range(n - 1):
            c = modules[i][j]
            if c == modules[i][j + 1] == modules[i + 1][j] == modules[i + 1][j + 1]:
                score += 3

    # N3：1:1:3:1:1 定位图形样式
    pat_a = [True, False, True, True, True, False, True, False, False, False, False]
    pat_b = [False, False, False, False, True, False, True, True, True, False, True]
    for line in list(modules) + [list(col) for col in zip(*modules)]:
        for k in range(n - 10):
            window = line[k:k + 11]
            if window == pat_a or window == pat_b:
                score += 40

    # N4：深色比例偏离 50%
    dark = sum(sum(1 for c in row if c) for row in modules)
    percent = dark * 100 / (n * n)
    score += int(abs(percent - 50) / 5) * 10
    return score


def _place_patterns(version: int) -> tuple:
    """返回 (矩阵, 功能模块掩码)。"""
    size = version * 4 + 17
    modules = [[False] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    def set_module(r: int, c: int, value: bool) -> None:
        modules[r][c] = value
        reserved[r][c] = True

    def place_finder(row: int, col: int) -> None:
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = row + r, col + c
                if not (0 <= rr < size and 0 <= cc < size):
                    continue
                if r in (0, 6) or c in (0, 6):
                    val = 0 <= r <= 6 and 0 <= c <= 6
                elif 2 <= r <= 4 and 2 <= c <= 4:
                    val = True
                else:
                    val = False
                set_module(rr, cc, val)

    # 三个定位图形
    place_finder(0, 0)
    place_finder(0, size - 7)
    place_finder(size - 7, 0)

    # 定时图形
    for i in range(8, size - 8):
        bit = i % 2 == 0
        set_module(6, i, bit)
        set_module(i, 6, bit)

    # 对齐图形
    positions = _PATTERN_POSITION_TABLE[version - 1]
    for r in positions:
        for c in positions:
            if (r == 6 and c == 6) or (r == 6 and c == size - 7) or (r == size - 7 and c == 6):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    val = max(abs(dr), abs(dc)) != 1
                    set_module(r + dr, c + dc, val)

    # 固定深色模块
    set_module(size - 8, 8, True)

    # 预留格式信息区（内容稍后填）
    for i in range(9):
        if not reserved[8][i]:
            set_module(8, i, False)
        if not reserved[i][8]:
            set_module(i, 8, False)
    for i in range(8):
        set_module(8, size - 1 - i, False)
        set_module(size - 1 - i, 8, False)
    reserved[size - 8][8] = True
    return modules, reserved


def _place_data(modules: List[List[bool]], reserved: List[List[bool]], codewords: List[int]) -> None:
    size = len(modules)
    bits: List[int] = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)

    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:  # 跳过定时图形列
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if not reserved[row][c]:
                    bit = bits[idx] if idx < len(bits) else 0
                    modules[row][c] = bool(bit)
                    idx += 1
        upward = not upward
        col -= 2


def _place_format(modules: List[List[bool]], error_correction: int, mask: int) -> None:
    size = len(modules)
    data = (error_correction << 3) | mask
    bits = _bch_format(data)

    def bit(i: int) -> bool:
        return bool((bits >> i) & 1)

    # 左上：竖向 0..5 + 横向 7,8（跳过定时图形）
    for i in range(6):
        modules[i][8] = bit(i)
    modules[7][8] = bit(6)
    modules[8][8] = bit(7)
    modules[8][7] = bit(8)
    for i in range(9, 15):
        modules[8][14 - i] = bit(i)

    # 右上 / 左下副本
    for i in range(8):
        modules[size - 1 - i][8] = bit(i)
    for i in range(8, 15):
        modules[8][size - 15 + i] = bit(i)
    modules[size - 8][8] = True  # 深色模块


def make_qr(data, error_correction: int = ERROR_CORRECT_M, mask: int = None) -> QRCode:
    """把 bytes/str 编码成 QRCode。"""
    if isinstance(data, str):
        payload = data.encode("utf-8")
    else:
        payload = bytes(data)
    if not payload:
        raise ValueError("空数据无法编码")

    chosen_version = None
    for version in range(1, 41):
        blocks = _rs_blocks(version, error_correction)
        bit_limit = sum(data_count * 8 for _, data_count in blocks)
        length_bits = 16 if version >= 10 else 8
        if len(payload) * 8 + 4 + length_bits <= bit_limit:
            chosen_version = version
            break
    if chosen_version is None:
        raise DataOverflowError("数据过长，超出 QR 版本 40 容量")

    codewords = _encode_data(payload, chosen_version, error_correction)

    best = None
    candidates = range(8) if mask is None else [mask]
    for candidate in candidates:
        modules, reserved = _place_patterns(chosen_version)
        _place_data(modules, reserved, codewords)
        # 掩码只作用于非功能模块
        for r in range(len(modules)):
            for c in range(len(modules)):
                if not reserved[r][c] and _mask_bit(candidate, r, c):
                    modules[r][c] = not modules[r][c]
        _place_format(modules, error_correction, candidate)
        score = _lost_point(modules)
        if best is None or score < best[0]:
            best = (score, candidate, modules)
    return QRCode(chosen_version, best[2])


def write_png(qr: QRCode, path: str, scale: int = 8, border: int = 4) -> str:
    """把 QR 写成 PNG 文件，返回路径。"""
    with open(path, "wb") as fh:
        fh.write(qr.to_png_bytes(scale=scale, border=border))
    return path
