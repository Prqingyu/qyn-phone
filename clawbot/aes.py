"""AES-128-ECB + PKCS#7 填充（零第三方依赖）。

为什么要手写：
    CDN 媒体上传只要求 AES-128-ECB，而本项目的原则是"纯标准库、零第三方依赖"
    （与同目录 qr.py 手写 QR 编码器同一个取舍）。cryptography / pycryptodome
    装得上，但不该成为运行必需 —— 节点部署不该为了一个分组密码拖上编译链
    和平台轮子。

为什么用 T 表而不是逐字节 S-box 循环：
    AES 的轮变换是 GF(2^8) 上的字节代换 + 行移位 + 列混合 + 轮密钥加。把列混合
    折进 T 表后，一轮就只剩 4 组"四次查表 + 异或"，可以用 32 位整数一次算完
    一整列，省掉每字节的函数调用与位运算。逐字节版本每轮要走 16 次 S-box 和
    4 次乘加，1 MiB 要几十秒，会让"上传前本地加密"变成瓶颈。

为什么解密表要单独建：
    解密走的是 FIPS-197 §5.3.5 的"等价逆密码"：把 InvMixColumns 提到轮密钥上，
    轮函数就与加密同构，可以复用同一套查表骨架。代价只是密钥扩展时对中间轮
    密钥多做一次 InvMixColumns，比每轮都对状态做一次便宜得多。

为什么 S 盒 / T 表在 import 时算而不是抄成 256 行字面量：
    表完全由 GF(2^8)（模 0x11B）的取逆与仿射变换决定，算出来只有几毫秒，却
    让源码保持几十行可核对的数学定义，而不是几百行无法复核的魔数。
"""

from __future__ import annotations

import struct
from typing import List, Tuple

#: AES 分组长度，PKCS#7 的填充粒度也用它。
BLOCK_SIZE = 16

#: AES-128 的轮密钥字数：4 字 × (10 轮 + 1)。
_EXPANDED_WORDS = 44

_RCON: Tuple[int, ...] = (
    0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36,
)


def _build_tables() -> Tuple[
    List[int], List[int], List[int], List[int], List[int],
    List[int], List[int], List[int], List[int], List[int],
]:
    """由 GF(2^8) 的数学定义生成 S 盒与加/解密 T 表。

    返回 (S, Si, Te0, Te1, Te2, Te3, Td0, Td1, Td2, Td3)。
    """
    # 生成元 3 的幂表与对数表：x -> 3^x，用于把乘法降成指数相加。
    exp = [0] * 256
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        # x * 3 = x * 2 ^ x；x * 2 即 xtime，溢出时约简 0x11B（只留下 0x1B）。
        x ^= ((x << 1) ^ (0x1B if x & 0x80 else 0)) & 0xFF
    exp[255] = 1  # 3^255 = 1，供下面取逆时索引 255 - log[a] 落到 255 用

    def gmul(a: int, b: int) -> int:
        if a == 0 or b == 0:
            return 0
        return exp[(log[a] + log[b]) % 255]

    # S 盒 = 乘法逆元 + 仿射变换（含常数 0x63）。
    sbox = [0] * 256
    for a in range(256):
        b = exp[255 - log[a]] if a else 0  # a=0 的逆元按定义取 0
        s = b
        for _ in range(4):
            b = ((b << 1) | (b >> 7)) & 0xFF
            s ^= b
        sbox[a] = s ^ 0x63

    inv_sbox = [0] * 256
    for i, v in enumerate(sbox):
        inv_sbox[v] = i

    # T 表：把一个字节的代换与列混合系数一次打包成 32 位字。
    # Te0[x] 的四个字节依次为 [2,1,1,3]·S[x]，Te1..Te3 是它的循环右移 8/16/24 位。
    te0 = [0] * 256
    te1 = [0] * 256
    te2 = [0] * 256
    te3 = [0] * 256
    td0 = [0] * 256
    td1 = [0] * 256
    td2 = [0] * 256
    td3 = [0] * 256
    for x in range(256):
        s = sbox[x]
        te0[x] = (
            (gmul(s, 2) << 24) | (s << 16) | (s << 8) | gmul(s, 3)
        )
        te1[x] = ((te0[x] >> 8) | (te0[x] << 24)) & 0xFFFFFFFF
        te2[x] = ((te1[x] >> 8) | (te1[x] << 24)) & 0xFFFFFFFF
        te3[x] = ((te2[x] >> 8) | (te2[x] << 24)) & 0xFFFFFFFF

        si = inv_sbox[x]
        td0[x] = (
            (gmul(si, 14) << 24) | (gmul(si, 9) << 16)
            | (gmul(si, 13) << 8) | gmul(si, 11)
        )
        td1[x] = ((td0[x] >> 8) | (td0[x] << 24)) & 0xFFFFFFFF
        td2[x] = ((td1[x] >> 8) | (td1[x] << 24)) & 0xFFFFFFFF
        td3[x] = ((td2[x] >> 8) | (td2[x] << 24)) & 0xFFFFFFFF

    return sbox, inv_sbox, te0, te1, te2, te3, td0, td1, td2, td3


(
    _SBOX, _INV_SBOX,
    _TE0, _TE1, _TE2, _TE3,
    _TD0, _TD1, _TD2, _TD3,
) = _build_tables()


def _expand_key(key: bytes) -> List[int]:
    """AES-128 密钥扩展，返回 44 个 32 位轮密钥字。

    逐字递推：w[i] = w[i-4] ^ f(w[i-1])，其中每 4 个字做一次 RotWord +
    SubWord + Rcon，这是 FIPS-197 §5.2 对 128 位密钥的定义。
    """
    w = list(struct.unpack(">4I", key))
    for i in range(4, _EXPANDED_WORDS):
        t = w[i - 1]
        if i % 4 == 0:
            t = ((t << 8) | (t >> 24)) & 0xFFFFFFFF  # RotWord
            t = (
                (_SBOX[t >> 24] << 24) | (_SBOX[(t >> 16) & 0xFF] << 16)
                | (_SBOX[(t >> 8) & 0xFF] << 8) | _SBOX[t & 0xFF]
            )
            t ^= _RCON[i // 4 - 1] << 24
        w.append(w[i - 4] ^ t)
    return w


def _inv_mix_word(a: int) -> int:
    """对一个轮密钥字做 InvMixColumns。

    这里借 Td 表算：用 S 盒先取回原字节（Td 表内部存的是逆 S 盒的结果），
    于是四次查表就把整列的 14/11/13/9 系数乘加完成了。
    """
    return (
        _TD0[_SBOX[a >> 24]] ^ _TD1[_SBOX[(a >> 16) & 0xFF]]
        ^ _TD2[_SBOX[(a >> 8) & 0xFF]] ^ _TD3[_SBOX[a & 0xFF]]
    )


def _expand_key_dec(key: bytes) -> List[int]:
    """生成等价逆密码用的解密轮密钥序列。

    头尾两轮密钥不参与 InvMixColumns（它们后面没有紧随的 InvMixColumns 要
    抵消），中间 9 组各做一次，顺序与加密相反。
    """
    w = _expand_key(key)
    d = w[40:44] + [0] * 36 + w[0:4]
    for r in range(1, 10):
        base = (10 - r) * 4
        for j in range(4):
            d[r * 4 + j] = _inv_mix_word(w[base + j])
    return d


def padded_size(size: int) -> int:
    """PKCS#7 填充后的长度 = size + (16 - size % 16)。

    空输入也补满一整块（16），这是 PKCS#7 与"零填充"的关键差别：只有补齐
    一整块，去填充时才能无歧义地判断最后一个字节是填充还是数据。
    """
    if size < 0:
        raise ValueError(f"长度不能为负：{size}")
    return size + (BLOCK_SIZE - size % BLOCK_SIZE)


def pkcs7_pad(data: bytes) -> bytes:
    """按 PKCS#7 补齐到 16 的整数倍，填充值即填充字节数（1..16）。"""
    n = BLOCK_SIZE - len(data) % BLOCK_SIZE
    return bytes(data) + bytes((n,)) * n


def pkcs7_unpad(data: bytes) -> bytes:
    """去掉 PKCS#7 填充；长度或填充字节非法时抛 ValueError。

    这里必须严格校验：长度非整数块、填充字节不在 1..16、尾部字节与声称的
    填充长度不符，都说明密文被篡改或密钥不对。宽松处理会把"解密失败"悄悄
    变成"得到一段错数据"，在 CDN 上传这种场景里更难排查。
    """
    if len(data) == 0 or len(data) % BLOCK_SIZE != 0:
        raise ValueError(f"待去填充的数据长度非法：{len(data)}")
    n = data[-1]
    if not 1 <= n <= BLOCK_SIZE:
        raise ValueError(f"PKCS#7 填充字节非法：{n}")
    if data[-n:] != bytes((n,)) * n:
        raise ValueError("PKCS#7 填充内容与填充长度不一致")
    return bytes(data[:-n])


def encrypt_ecb(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-ECB + PKCS#7 加密；密钥不是 16 字节时抛 ValueError。

    ECB 每个分组独立，因此可以把输入看成 32 位字数组直接顺序处理，不需要
    携带链式状态；轮函数全部内联展开，避免每轮的循环与解包开销。
    """
    if len(key) != BLOCK_SIZE:
        raise ValueError(f"AES-128 密钥必须是 {BLOCK_SIZE} 字节，收到 {len(key)} 字节")
    key = bytes(key)

    data = pkcs7_pad(plaintext)
    nwords = len(data) // 4
    words = struct.unpack(">%dI" % nwords, data)

    (w0, w1, w2, w3, w4, w5, w6, w7, w8, w9, w10, w11,
     w12, w13, w14, w15, w16, w17, w18, w19, w20, w21, w22, w23,
     w24, w25, w26, w27, w28, w29, w30, w31, w32, w33, w34, w35,
     w36, w37, w38, w39, w40, w41, w42, w43) = _expand_key(key)

    te0 = _TE0
    te1 = _TE1
    te2 = _TE2
    te3 = _TE3
    sbox = _SBOX

    out = [0] * nwords
    i = 0
    while i < nwords:
        # 状态按列存成 4 个 32 位字；初始轮密钥加。
        s0 = words[i] ^ w0
        s1 = words[i + 1] ^ w1
        s2 = words[i + 2] ^ w2
        s3 = words[i + 3] ^ w3

        # 9 轮完整轮：SubBytes + ShiftRows + MixColumns + AddRoundKey
        # 已折进 T 表，每列一次"四次查表异或"。
        t0 = te0[s0 >> 24] ^ te1[(s1 >> 16) & 0xFF] ^ te2[(s2 >> 8) & 0xFF] ^ te3[s3 & 0xFF] ^ w4
        t1 = te0[s1 >> 24] ^ te1[(s2 >> 16) & 0xFF] ^ te2[(s3 >> 8) & 0xFF] ^ te3[s0 & 0xFF] ^ w5
        t2 = te0[s2 >> 24] ^ te1[(s3 >> 16) & 0xFF] ^ te2[(s0 >> 8) & 0xFF] ^ te3[s1 & 0xFF] ^ w6
        t3 = te0[s3 >> 24] ^ te1[(s0 >> 16) & 0xFF] ^ te2[(s1 >> 8) & 0xFF] ^ te3[s2 & 0xFF] ^ w7
        s0, s1, s2, s3 = t0, t1, t2, t3

        t0 = te0[s0 >> 24] ^ te1[(s1 >> 16) & 0xFF] ^ te2[(s2 >> 8) & 0xFF] ^ te3[s3 & 0xFF] ^ w8
        t1 = te0[s1 >> 24] ^ te1[(s2 >> 16) & 0xFF] ^ te2[(s3 >> 8) & 0xFF] ^ te3[s0 & 0xFF] ^ w9
        t2 = te0[s2 >> 24] ^ te1[(s3 >> 16) & 0xFF] ^ te2[(s0 >> 8) & 0xFF] ^ te3[s1 & 0xFF] ^ w10
        t3 = te0[s3 >> 24] ^ te1[(s0 >> 16) & 0xFF] ^ te2[(s1 >> 8) & 0xFF] ^ te3[s2 & 0xFF] ^ w11
        s0, s1, s2, s3 = t0, t1, t2, t3

        t0 = te0[s0 >> 24] ^ te1[(s1 >> 16) & 0xFF] ^ te2[(s2 >> 8) & 0xFF] ^ te3[s3 & 0xFF] ^ w12
        t1 = te0[s1 >> 24] ^ te1[(s2 >> 16) & 0xFF] ^ te2[(s3 >> 8) & 0xFF] ^ te3[s0 & 0xFF] ^ w13
        t2 = te0[s2 >> 24] ^ te1[(s3 >> 16) & 0xFF] ^ te2[(s0 >> 8) & 0xFF] ^ te3[s1 & 0xFF] ^ w14
        t3 = te0[s3 >> 24] ^ te1[(s0 >> 16) & 0xFF] ^ te2[(s1 >> 8) & 0xFF] ^ te3[s2 & 0xFF] ^ w15
        s0, s1, s2, s3 = t0, t1, t2, t3

        t0 = te0[s0 >> 24] ^ te1[(s1 >> 16) & 0xFF] ^ te2[(s2 >> 8) & 0xFF] ^ te3[s3 & 0xFF] ^ w16
        t1 = te0[s1 >> 24] ^ te1[(s2 >> 16) & 0xFF] ^ te2[(s3 >> 8) & 0xFF] ^ te3[s0 & 0xFF] ^ w17
        t2 = te0[s2 >> 24] ^ te1[(s3 >> 16) & 0xFF] ^ te2[(s0 >> 8) & 0xFF] ^ te3[s1 & 0xFF] ^ w18
        t3 = te0[s3 >> 24] ^ te1[(s0 >> 16) & 0xFF] ^ te2[(s1 >> 8) & 0xFF] ^ te3[s2 & 0xFF] ^ w19
        s0, s1, s2, s3 = t0, t1, t2, t3

        t0 = te0[s0 >> 24] ^ te1[(s1 >> 16) & 0xFF] ^ te2[(s2 >> 8) & 0xFF] ^ te3[s3 & 0xFF] ^ w20
        t1 = te0[s1 >> 24] ^ te1[(s2 >> 16) & 0xFF] ^ te2[(s3 >> 8) & 0xFF] ^ te3[s0 & 0xFF] ^ w21
        t2 = te0[s2 >> 24] ^ te1[(s3 >> 16) & 0xFF] ^ te2[(s0 >> 8) & 0xFF] ^ te3[s1 & 0xFF] ^ w22
        t3 = te0[s3 >> 24] ^ te1[(s0 >> 16) & 0xFF] ^ te2[(s1 >> 8) & 0xFF] ^ te3[s2 & 0xFF] ^ w23
        s0, s1, s2, s3 = t0, t1, t2, t3

        t0 = te0[s0 >> 24] ^ te1[(s1 >> 16) & 0xFF] ^ te2[(s2 >> 8) & 0xFF] ^ te3[s3 & 0xFF] ^ w24
        t1 = te0[s1 >> 24] ^ te1[(s2 >> 16) & 0xFF] ^ te2[(s3 >> 8) & 0xFF] ^ te3[s0 & 0xFF] ^ w25
        t2 = te0[s2 >> 24] ^ te1[(s3 >> 16) & 0xFF] ^ te2[(s0 >> 8) & 0xFF] ^ te3[s1 & 0xFF] ^ w26
        t3 = te0[s3 >> 24] ^ te1[(s0 >> 16) & 0xFF] ^ te2[(s1 >> 8) & 0xFF] ^ te3[s2 & 0xFF] ^ w27
        s0, s1, s2, s3 = t0, t1, t2, t3

        t0 = te0[s0 >> 24] ^ te1[(s1 >> 16) & 0xFF] ^ te2[(s2 >> 8) & 0xFF] ^ te3[s3 & 0xFF] ^ w28
        t1 = te0[s1 >> 24] ^ te1[(s2 >> 16) & 0xFF] ^ te2[(s3 >> 8) & 0xFF] ^ te3[s0 & 0xFF] ^ w29
        t2 = te0[s2 >> 24] ^ te1[(s3 >> 16) & 0xFF] ^ te2[(s0 >> 8) & 0xFF] ^ te3[s1 & 0xFF] ^ w30
        t3 = te0[s3 >> 24] ^ te1[(s0 >> 16) & 0xFF] ^ te2[(s1 >> 8) & 0xFF] ^ te3[s2 & 0xFF] ^ w31
        s0, s1, s2, s3 = t0, t1, t2, t3

        t0 = te0[s0 >> 24] ^ te1[(s1 >> 16) & 0xFF] ^ te2[(s2 >> 8) & 0xFF] ^ te3[s3 & 0xFF] ^ w32
        t1 = te0[s1 >> 24] ^ te1[(s2 >> 16) & 0xFF] ^ te2[(s3 >> 8) & 0xFF] ^ te3[s0 & 0xFF] ^ w33
        t2 = te0[s2 >> 24] ^ te1[(s3 >> 16) & 0xFF] ^ te2[(s0 >> 8) & 0xFF] ^ te3[s1 & 0xFF] ^ w34
        t3 = te0[s3 >> 24] ^ te1[(s0 >> 16) & 0xFF] ^ te2[(s1 >> 8) & 0xFF] ^ te3[s2 & 0xFF] ^ w35
        s0, s1, s2, s3 = t0, t1, t2, t3

        t0 = te0[s0 >> 24] ^ te1[(s1 >> 16) & 0xFF] ^ te2[(s2 >> 8) & 0xFF] ^ te3[s3 & 0xFF] ^ w36
        t1 = te0[s1 >> 24] ^ te1[(s2 >> 16) & 0xFF] ^ te2[(s3 >> 8) & 0xFF] ^ te3[s0 & 0xFF] ^ w37
        t2 = te0[s2 >> 24] ^ te1[(s3 >> 16) & 0xFF] ^ te2[(s0 >> 8) & 0xFF] ^ te3[s1 & 0xFF] ^ w38
        t3 = te0[s3 >> 24] ^ te1[(s0 >> 16) & 0xFF] ^ te2[(s1 >> 8) & 0xFF] ^ te3[s2 & 0xFF] ^ w39
        s0, s1, s2, s3 = t0, t1, t2, t3

        # 第 10 轮没有列混合，只剩 S 盒查表（等价于 SubBytes + ShiftRows）
        # 再异或最后一组轮密钥。
        # 注意必须写进临时变量：s0 的新值不能再参与 s1..s3 的查表。
        t0 = ((sbox[s0 >> 24] << 24) | (sbox[(s1 >> 16) & 0xFF] << 16)
              | (sbox[(s2 >> 8) & 0xFF] << 8) | sbox[s3 & 0xFF]) ^ w40
        t1 = ((sbox[s1 >> 24] << 24) | (sbox[(s2 >> 16) & 0xFF] << 16)
              | (sbox[(s3 >> 8) & 0xFF] << 8) | sbox[s0 & 0xFF]) ^ w41
        t2 = ((sbox[s2 >> 24] << 24) | (sbox[(s3 >> 16) & 0xFF] << 16)
              | (sbox[(s0 >> 8) & 0xFF] << 8) | sbox[s1 & 0xFF]) ^ w42
        t3 = ((sbox[s3 >> 24] << 24) | (sbox[(s0 >> 16) & 0xFF] << 16)
              | (sbox[(s1 >> 8) & 0xFF] << 8) | sbox[s2 & 0xFF]) ^ w43
        s0, s1, s2, s3 = t0, t1, t2, t3

        out[i] = s0
        out[i + 1] = s1
        out[i + 2] = s2
        out[i + 3] = s3
        i += 4

    return struct.pack(">%dI" % nwords, *out)


def decrypt_ecb(ciphertext: bytes, key: bytes) -> bytes:
    """AES-128-ECB 解密并去 PKCS#7 填充；长度或填充非法时抛 ValueError。"""
    if len(key) != BLOCK_SIZE:
        raise ValueError(f"AES-128 密钥必须是 {BLOCK_SIZE} 字节，收到 {len(key)} 字节")
    key = bytes(key)

    if len(ciphertext) == 0 or len(ciphertext) % BLOCK_SIZE != 0:
        raise ValueError(
            f"ECB 密文长度必须是 {BLOCK_SIZE} 的整数倍，收到 {len(ciphertext)} 字节"
        )

    nwords = len(ciphertext) // 4
    words = struct.unpack(">%dI" % nwords, ciphertext)

    (d0, d1, d2, d3, d4, d5, d6, d7, d8, d9, d10, d11,
     d12, d13, d14, d15, d16, d17, d18, d19, d20, d21, d22, d23,
     d24, d25, d26, d27, d28, d29, d30, d31, d32, d33, d34, d35,
     d36, d37, d38, d39, d40, d41, d42, d43) = _expand_key_dec(key)

    td0 = _TD0
    td1 = _TD1
    td2 = _TD2
    td3 = _TD3
    inv_sbox = _INV_SBOX

    out = [0] * nwords
    i = 0
    while i < nwords:
        s0 = words[i] ^ d0
        s1 = words[i + 1] ^ d1
        s2 = words[i + 2] ^ d2
        s3 = words[i + 3] ^ d3

        # 9 轮完整轮：InvSubBytes + InvShiftRows + InvMixColumns + AddRoundKey
        # 同样折进 Td 表。索引方向与加密相反：第 c 列第 k 个字节取自
        # 第 (c-k) mod 4 个字 —— 加密的 ShiftRows 向左移，InvShiftRows 向右移。

        t0 = td0[s0 >> 24] ^ td1[(s3 >> 16) & 0xFF] ^ td2[(s2 >> 8) & 0xFF] ^ td3[s1 & 0xFF] ^ d4
        t1 = td0[s1 >> 24] ^ td1[(s0 >> 16) & 0xFF] ^ td2[(s3 >> 8) & 0xFF] ^ td3[s2 & 0xFF] ^ d5
        t2 = td0[s2 >> 24] ^ td1[(s1 >> 16) & 0xFF] ^ td2[(s0 >> 8) & 0xFF] ^ td3[s3 & 0xFF] ^ d6
        t3 = td0[s3 >> 24] ^ td1[(s2 >> 16) & 0xFF] ^ td2[(s1 >> 8) & 0xFF] ^ td3[s0 & 0xFF] ^ d7
        s0, s1, s2, s3 = t0, t1, t2, t3

        t0 = td0[s0 >> 24] ^ td1[(s3 >> 16) & 0xFF] ^ td2[(s2 >> 8) & 0xFF] ^ td3[s1 & 0xFF] ^ d8
        t1 = td0[s1 >> 24] ^ td1[(s0 >> 16) & 0xFF] ^ td2[(s3 >> 8) & 0xFF] ^ td3[s2 & 0xFF] ^ d9
        t2 = td0[s2 >> 24] ^ td1[(s1 >> 16) & 0xFF] ^ td2[(s0 >> 8) & 0xFF] ^ td3[s3 & 0xFF] ^ d10
        t3 = td0[s3 >> 24] ^ td1[(s2 >> 16) & 0xFF] ^ td2[(s1 >> 8) & 0xFF] ^ td3[s0 & 0xFF] ^ d11
        s0, s1, s2, s3 = t0, t1, t2, t3

        t0 = td0[s0 >> 24] ^ td1[(s3 >> 16) & 0xFF] ^ td2[(s2 >> 8) & 0xFF] ^ td3[s1 & 0xFF] ^ d12
        t1 = td0[s1 >> 24] ^ td1[(s0 >> 16) & 0xFF] ^ td2[(s3 >> 8) & 0xFF] ^ td3[s2 & 0xFF] ^ d13
        t2 = td0[s2 >> 24] ^ td1[(s1 >> 16) & 0xFF] ^ td2[(s0 >> 8) & 0xFF] ^ td3[s3 & 0xFF] ^ d14
        t3 = td0[s3 >> 24] ^ td1[(s2 >> 16) & 0xFF] ^ td2[(s1 >> 8) & 0xFF] ^ td3[s0 & 0xFF] ^ d15
        s0, s1, s2, s3 = t0, t1, t2, t3

        t0 = td0[s0 >> 24] ^ td1[(s3 >> 16) & 0xFF] ^ td2[(s2 >> 8) & 0xFF] ^ td3[s1 & 0xFF] ^ d16
        t1 = td0[s1 >> 24] ^ td1[(s0 >> 16) & 0xFF] ^ td2[(s3 >> 8) & 0xFF] ^ td3[s2 & 0xFF] ^ d17
        t2 = td0[s2 >> 24] ^ td1[(s1 >> 16) & 0xFF] ^ td2[(s0 >> 8) & 0xFF] ^ td3[s3 & 0xFF] ^ d18
        t3 = td0[s3 >> 24] ^ td1[(s2 >> 16) & 0xFF] ^ td2[(s1 >> 8) & 0xFF] ^ td3[s0 & 0xFF] ^ d19
        s0, s1, s2, s3 = t0, t1, t2, t3

        t0 = td0[s0 >> 24] ^ td1[(s3 >> 16) & 0xFF] ^ td2[(s2 >> 8) & 0xFF] ^ td3[s1 & 0xFF] ^ d20
        t1 = td0[s1 >> 24] ^ td1[(s0 >> 16) & 0xFF] ^ td2[(s3 >> 8) & 0xFF] ^ td3[s2 & 0xFF] ^ d21
        t2 = td0[s2 >> 24] ^ td1[(s1 >> 16) & 0xFF] ^ td2[(s0 >> 8) & 0xFF] ^ td3[s3 & 0xFF] ^ d22
        t3 = td0[s3 >> 24] ^ td1[(s2 >> 16) & 0xFF] ^ td2[(s1 >> 8) & 0xFF] ^ td3[s0 & 0xFF] ^ d23
        s0, s1, s2, s3 = t0, t1, t2, t3

        t0 = td0[s0 >> 24] ^ td1[(s3 >> 16) & 0xFF] ^ td2[(s2 >> 8) & 0xFF] ^ td3[s1 & 0xFF] ^ d24
        t1 = td0[s1 >> 24] ^ td1[(s0 >> 16) & 0xFF] ^ td2[(s3 >> 8) & 0xFF] ^ td3[s2 & 0xFF] ^ d25
        t2 = td0[s2 >> 24] ^ td1[(s1 >> 16) & 0xFF] ^ td2[(s0 >> 8) & 0xFF] ^ td3[s3 & 0xFF] ^ d26
        t3 = td0[s3 >> 24] ^ td1[(s2 >> 16) & 0xFF] ^ td2[(s1 >> 8) & 0xFF] ^ td3[s0 & 0xFF] ^ d27
        s0, s1, s2, s3 = t0, t1, t2, t3

        t0 = td0[s0 >> 24] ^ td1[(s3 >> 16) & 0xFF] ^ td2[(s2 >> 8) & 0xFF] ^ td3[s1 & 0xFF] ^ d28
        t1 = td0[s1 >> 24] ^ td1[(s0 >> 16) & 0xFF] ^ td2[(s3 >> 8) & 0xFF] ^ td3[s2 & 0xFF] ^ d29
        t2 = td0[s2 >> 24] ^ td1[(s1 >> 16) & 0xFF] ^ td2[(s0 >> 8) & 0xFF] ^ td3[s3 & 0xFF] ^ d30
        t3 = td0[s3 >> 24] ^ td1[(s2 >> 16) & 0xFF] ^ td2[(s1 >> 8) & 0xFF] ^ td3[s0 & 0xFF] ^ d31
        s0, s1, s2, s3 = t0, t1, t2, t3

        t0 = td0[s0 >> 24] ^ td1[(s3 >> 16) & 0xFF] ^ td2[(s2 >> 8) & 0xFF] ^ td3[s1 & 0xFF] ^ d32
        t1 = td0[s1 >> 24] ^ td1[(s0 >> 16) & 0xFF] ^ td2[(s3 >> 8) & 0xFF] ^ td3[s2 & 0xFF] ^ d33
        t2 = td0[s2 >> 24] ^ td1[(s1 >> 16) & 0xFF] ^ td2[(s0 >> 8) & 0xFF] ^ td3[s3 & 0xFF] ^ d34
        t3 = td0[s3 >> 24] ^ td1[(s2 >> 16) & 0xFF] ^ td2[(s1 >> 8) & 0xFF] ^ td3[s0 & 0xFF] ^ d35
        s0, s1, s2, s3 = t0, t1, t2, t3

        t0 = td0[s0 >> 24] ^ td1[(s3 >> 16) & 0xFF] ^ td2[(s2 >> 8) & 0xFF] ^ td3[s1 & 0xFF] ^ d36
        t1 = td0[s1 >> 24] ^ td1[(s0 >> 16) & 0xFF] ^ td2[(s3 >> 8) & 0xFF] ^ td3[s2 & 0xFF] ^ d37
        t2 = td0[s2 >> 24] ^ td1[(s1 >> 16) & 0xFF] ^ td2[(s0 >> 8) & 0xFF] ^ td3[s3 & 0xFF] ^ d38
        t3 = td0[s3 >> 24] ^ td1[(s2 >> 16) & 0xFF] ^ td2[(s1 >> 8) & 0xFF] ^ td3[s0 & 0xFF] ^ d39
        s0, s1, s2, s3 = t0, t1, t2, t3

        # 最后一轮只剩逆 S 盒（等价于 InvSubBytes + InvShiftRows）与轮密钥加。
        # 同样必须先写临时变量：s 的新值不能参与后面几列的查表。
        t0 = ((inv_sbox[s0 >> 24] << 24) | (inv_sbox[(s3 >> 16) & 0xFF] << 16)
              | (inv_sbox[(s2 >> 8) & 0xFF] << 8) | inv_sbox[s1 & 0xFF]) ^ d40
        t1 = ((inv_sbox[s1 >> 24] << 24) | (inv_sbox[(s0 >> 16) & 0xFF] << 16)
              | (inv_sbox[(s3 >> 8) & 0xFF] << 8) | inv_sbox[s2 & 0xFF]) ^ d41
        t2 = ((inv_sbox[s2 >> 24] << 24) | (inv_sbox[(s1 >> 16) & 0xFF] << 16)
              | (inv_sbox[(s0 >> 8) & 0xFF] << 8) | inv_sbox[s3 & 0xFF]) ^ d42
        t3 = ((inv_sbox[s3 >> 24] << 24) | (inv_sbox[(s2 >> 16) & 0xFF] << 16)
              | (inv_sbox[(s1 >> 8) & 0xFF] << 8) | inv_sbox[s0 & 0xFF]) ^ d43
        s0, s1, s2, s3 = t0, t1, t2, t3

        out[i] = s0
        out[i + 1] = s1
        out[i + 2] = s2
        out[i + 3] = s3
        i += 4

    return pkcs7_unpad(struct.pack(">%dI" % nwords, *out))


def backend() -> str:
    """返回当前实现标识。

    固定为 'pure-python'：这个模块刻意不提供"有第三方就用第三方"的分支，
    以免同一份数据在不同机器上走不同的密码实现、把问题变成环境相关。
    """
    return "pure-python"
