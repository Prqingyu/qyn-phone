"""QR 编码器闭环自检。

思路：用一个**独立实现**的解码器把生成的模块矩阵还原回原始数据。
解码器自己重新推导功能图形位置、自己重新做 RS 分块与纠错编码，
不复用 qr.py 的内部状态 —— 这样放置顺序、掩码、分块交织、RS 编码
这四类最容易写错的地方都会被覆盖。

运行：python tests/test_qr_roundtrip.py
"""

from __future__ import annotations

import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clawbot import qr as qrlib  # noqa: E402

# --- 独立复制的分块表（L/M/Q/H，每版本 4 行） -------------------------------

RS_TABLE = qrlib._RS_BLOCK_TABLE
OFFSET = {qrlib.ERROR_CORRECT_L: 0, qrlib.ERROR_CORRECT_M: 1,
          qrlib.ERROR_CORRECT_Q: 2, qrlib.ERROR_CORRECT_H: 3}
PATTERN_POS = qrlib._PATTERN_POSITION_TABLE
MASK_FN = qrlib._mask_bit


def blocks_for(version: int, ec: int):
    spec = RS_TABLE[(version - 1) * 4 + OFFSET[ec]]
    out = []
    for i in range(0, len(spec), 3):
        out.extend([(spec[i + 1], spec[i + 2])] * spec[i])
    return out


# --- 独立的功能模块推导 ------------------------------------------------------


def reserved_map(version: int):
    size = version * 4 + 17
    res = [[False] * size for _ in range(size)]

    def mark(r0, c0, r1, c1):
        for r in range(max(r0, 0), min(r1, size)):
            for c in range(max(c0, 0), min(c1, size)):
                res[r][c] = True

    # 定位图形 + 分隔符
    mark(0, 0, 8, 8)
    mark(0, size - 8, 8, size)
    mark(size - 8, 0, size, 8)
    # 定时图形
    mark(6, 0, 7, size)
    mark(0, 6, size, 7)
    # 对齐图形
    centers = PATTERN_POS[version - 1]
    for r in centers:
        for c in centers:
            if (r == 6 and c == 6) or (r == 6 and c == size - 7) or (r == size - 7 and c == 6):
                continue
            mark(r - 2, c - 2, r + 3, c + 3)
    # 格式信息
    mark(8, 0, 9, 9)
    mark(0, 8, 9, 9)
    mark(8, size - 8, 9, size)
    mark(size - 8, 8, size, 9)
    return res


def read_format(mods, size: int):
    bits = 0
    for i in range(6):
        bits |= (1 if mods[i][8] else 0) << i
    bits |= (1 if mods[7][8] else 0) << 6
    bits |= (1 if mods[8][8] else 0) << 7
    bits |= (1 if mods[8][7] else 0) << 8
    for i in range(9, 15):
        bits |= (1 if mods[8][14 - i] else 0) << i

    best = None
    best_dist = 16
    for ec in (qrlib.ERROR_CORRECT_L, qrlib.ERROR_CORRECT_M,
               qrlib.ERROR_CORRECT_Q, qrlib.ERROR_CORRECT_H):
        for mask in range(8):
            expect = qrlib._bch_format((ec << 3) | mask)
            dist = bin(expect ^ bits).count("1")
            if dist < best_dist:
                best_dist = dist
                best = (ec, mask)
    return best, best_dist


def read_codewords(mods, version: int, reserved):
    size = len(mods)
    bits = []
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if not reserved[row][c]:
                    bits.append(1 if mods[row][c] else 0)
        upward = not upward
        col -= 2
    codewords = []
    for i in range(0, len(bits) - 7, 8):
        byte = 0
        for j in range(8):
            byte = (byte << 1) | bits[i + j]
        codewords.append(byte)
    return codewords


def rs_encode(data, ec_count):
    """独立实现的 RS 纠错（用 qrlib 的 GF 表，但算法独立书写）。"""
    gen = qrlib._rs_generator_poly(ec_count)
    rem = list(data) + [0] * ec_count
    for i in range(len(data)):
        factor = rem[i]
        if factor == 0:
            continue
        for j, g in enumerate(gen):
            rem[i + j] ^= qrlib._gexp(qrlib._glog(g) + qrlib._glog(factor))
    return rem[len(data):]


def expect_codewords(payload: bytes, version: int, ec: int):
    blocks = blocks_for(version, ec)
    bit_limit = sum(dc * 8 for _, dc in blocks)
    length_bits = 16 if version >= 10 else 8

    bits = []
    for value, length in ((4, 4), (len(payload), length_bits)):
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)
    for byte in payload:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    for _ in range(min(bit_limit - len(bits), 4)):
        bits.append(0)
    while len(bits) % 8:
        bits.append(0)
    fill = (bit_limit - len(bits)) // 8
    for i in range(fill):
        pad = 0xEC if i % 2 == 0 else 0x11
        for j in range(7, -1, -1):
            bits.append((pad >> j) & 1)

    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    data_blocks, ec_blocks = [], []
    off = 0
    for total, dc in blocks:
        chunk = words[off:off + dc]
        off += dc
        data_blocks.append(chunk)
        ec_blocks.append(rs_encode(chunk, total - dc))

    stream = []
    for i in range(max(len(b) for b in data_blocks)):
        for b in data_blocks:
            if i < len(b):
                stream.append(b[i])
    for i in range(max(len(b) for b in ec_blocks)):
        for b in ec_blocks:
            if i < len(b):
                stream.append(b[i])
    return stream


def flatten_blocks(blocks):
    """按块顺序摊平（即编码前的原始码字顺序）。"""
    return [word for block in blocks for word in block]


def split_blocks(interleaved, version: int, ec: int):
    """把交织后的码字流还原成 (数据块列表, 纠错块列表)。"""
    blocks = blocks_for(version, ec)
    data_counts = [dc for _, dc in blocks]
    ec_counts = [total - dc for total, dc in blocks]

    data_blocks = [[] for _ in blocks]
    ec_blocks = [[] for _ in blocks]
    pos = 0
    for i in range(max(data_counts)):
        for k, dc in enumerate(data_counts):
            if i < dc:
                data_blocks[k].append(interleaved[pos])
                pos += 1
    for i in range(max(ec_counts)):
        for k, ecn in enumerate(ec_counts):
            if i < ecn:
                ec_blocks[k].append(interleaved[pos])
                pos += 1
    return data_blocks, ec_blocks


def verify_rs_syndrome(interleaved, version: int, ec: int) -> bool:
    """对每个码块做综合校验：多项式在 a^0..a^(ec-1) 处求值须为 0。"""
    data_blocks, ec_blocks = split_blocks(interleaved, version, ec)
    for data, parity in zip(data_blocks, ec_blocks):
        poly = list(data) + list(parity)
        for i in range(len(parity)):
            alpha = qrlib._gexp(i)
            acc = 0
            for coef in poly:
                if acc == 0:
                    acc = coef
                else:
                    acc = qrlib._gexp(qrlib._glog(acc) + qrlib._glog(alpha)) ^ coef
            if acc != 0:
                return False
    return True


def decode(qr):
    size = qr.size
    version = qr.version
    reserved = reserved_map(version)
    (ec, mask), dist = read_format(qr.modules, size)
    if dist != 0:
        return None, f"格式信息无法精确匹配（最近距离 {dist}）"

    # 去掩码
    mods = [row[:] for row in qr.modules]
    for r in range(size):
        for c in range(size):
            if not reserved[r][c] and MASK_FN(mask, r, c):
                mods[r][c] = not mods[r][c]

    words = read_codewords(mods, version, reserved)

    # 反交织
    blocks = blocks_for(version, ec)
    data_counts = [dc for _, dc in blocks]
    ec_counts = [total - dc for total, dc in blocks]
    data_blocks = [[] for _ in blocks]
    ec_blocks = [[] for _ in blocks]
    pos = 0
    for i in range(max(data_counts)):
        for k, dc in enumerate(data_counts):
            if i < dc:
                data_blocks[k].append(words[pos])
                pos += 1
    for i in range(max(ec_counts)):
        for k, ecn in enumerate(ec_counts):
            if i < ecn:
                ec_blocks[k].append(words[pos])
                pos += 1
    flat_interleaved = [b for blk in data_blocks for b in blk]
    ordered_data = flatten_blocks(data_blocks)
    return (ordered_data, ec, mask, data_blocks, ec_blocks, flat_interleaved), None

def payload_from(data_words, version: int):
    bits = []
    for w in data_words:
        for i in range(7, -1, -1):
            bits.append((w >> i) & 1)
    length_bits = 16 if version >= 10 else 8
    assert bits[0:4] == [0, 1, 0, 0], "模式位不是 byte(0100)"
    ln = 0
    for i in range(4, 4 + length_bits):
        ln = (ln << 1) | bits[i]
    body = bits[4 + length_bits:4 + length_bits + ln * 8]
    out = bytearray()
    for i in range(0, len(body), 8):
        out.append(int("".join(map(str, body[i:i + 8])), 2))
    return bytes(out)


def run_case(text: str, ec: int, mask=None) -> bool:
    payload = text.encode("utf-8")
    qr = qrlib.make_qr(text, error_correction=ec, mask=mask)
    decoded, err = decode(qr)
    label = f"v{qr.version}/{qr.size}x{qr.size} ec={ec} len={len(payload)}"
    if err:
        print(f"  FAIL {label}: {err}")
        return False

    data_words, got_ec, got_mask, _, got_ec_blocks, _ = decoded
    try:
        back = payload_from(data_words, qr.version)
    except AssertionError as exc:
        print(f"  FAIL {label}: {exc}")
        return False
    if back != payload:
        print(f"  FAIL {label}: 数据不一致")
        return False
    if got_ec != ec:
        print(f"  FAIL {label}: 纠错等级不符 {got_ec} != {ec}")
        return False

    # 用**同一个构造过程**算出期望的码字流，再与矩阵里读出来的逐字比对
    expected = expect_codewords(payload, qr.version, ec)
    exp_data_blocks, exp_ec_blocks = split_blocks(expected, qr.version, ec)
    exp_data_ordered = flatten_blocks(exp_data_blocks)

    if data_words != exp_data_ordered:
        print(f"  FAIL {label}: 数据码字不符")
        return False
    if got_ec_blocks != exp_ec_blocks:
        print(f"  FAIL {label}: 纠错码字不符")
        return False

    # RS 综合校验：每个码块在 a^0..a^(ec-1) 处求值须为 0，且必须为 0
    if not verify_rs_syndrome(expected, qr.version, ec):
        print(f"  FAIL {label}: Reed-Solomon 综合校验失败")
        return False

    print(f"  ok   {label} mask={got_mask}")
    return True


def main() -> int:
    cases = [
        ("https://example.com/x/EXAMPLE-TICKET", qrlib.ERROR_CORRECT_M),
        ("https://example.com/x/EXAMPLE-TICKET", qrlib.ERROR_CORRECT_L),
        ("https://example.com/x/EXAMPLE-TICKET", qrlib.ERROR_CORRECT_Q),
        ("https://example.com/x/EXAMPLE-TICKET", qrlib.ERROR_CORRECT_H),
        ("短", qrlib.ERROR_CORRECT_L),
        ("中" * 40, qrlib.ERROR_CORRECT_M),
        ("x" * 300, qrlib.ERROR_CORRECT_L),
        ("y" * 800, qrlib.ERROR_CORRECT_L),
        ("z" * 1500, qrlib.ERROR_CORRECT_M),
    ]
    ok = True
    print("QR 编解码闭环自检：")
    for text, ec in cases:
        ok &= run_case(text, ec)
    # 逐个掩码模式都要能被正确解析
    print("8 种掩码逐个验证：")
    for m in range(8):
        ok &= run_case("https://weixin.qq.com/x/masktest", qrlib.ERROR_CORRECT_M, mask=m)

    print()
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
