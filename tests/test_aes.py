"""AES-128-ECB 自检：标准向量 + 边界 + 异常 + 往返 + 性能。

为什么先钉标准向量，再做"加密再解密"的往返：
    往返一致只证明"加解密互为逆运算"，证明不了它真的是 AES —— 一个 S 盒写错
    但两处同时写错的实现照样往返成功，却会和任何别的 AES 实现对不上。CDN 上传
    是跨实现对账（对面是服务端 SDK），所以先用 FIPS-197 C.1 与 NIST SP 800-38A
    F.1.1 的已知答案把算法本身钉死，再补边界、异常路径与性能。

为什么要测 1 MiB 计时：
    纯 Python 是这里的硬约束（零第三方依赖），但"慢到不能用"同样是失败的实现。
    加密 1 MiB 超过 3 秒即判 FAIL，把"上传前本地加密"钉在不拖后腿的量级上。

运行：python tests/test_aes.py
"""

from __future__ import annotations

import os
import random
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from clawbot import aes  # noqa: E402

FAILURES: list = []

# --- 已知答案向量 -----------------------------------------------------------

# FIPS-197 Appendix C.1：AES-128 单分组。
FIPS_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
FIPS_PT = bytes.fromhex("00112233445566778899aabbccddeeff")
FIPS_CT = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")

# 本模块接口恒定带 PKCS#7，所以解密方向需要一个"填充合法"的密文做已知答案：
# 上面的向量明文再加一整块 0x10 填充后的 ECB 密文。第二块由标准 AES 实现
# 独立算出（可复核），并与 SP 800-38A 的跨实现比对互相印证。
FIPS_PADDED_CT = bytes.fromhex(
    "69c4e0d86a7b0430d8cdb78070b4c55a"
    "954f64f2e4e86e9eee82d20216684899"
)

# NIST SP 800-38A F.1.1 ECB-AES128.Encrypt：四段独立分组，无 IV。
SP_KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
SP_PAIRS = (
    ("6bc1bee22e409f96e93d7e117393172a", "3ad77bb40d7a3660a89ecaf32466ef97"),
    ("ae2d8a571e03ac9c9eb76fac45af8e51", "f5d3d58503b9699de785895a96fdbaaf"),
    ("30c81c46a35ce411e5fbc1191a0a52ef", "43b1cd7f598ece23881b00e3ed030688"),
    ("f69f2445df4f9b17ad2b417be66c3710", "7b0c785e27e8ad3f8223207104725dd4"),
)


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def raise_kind(fn, *args) -> str:
    """调用 fn(*args)，返回 'ValueError' / 其它异常名 / 'no-raise'。"""
    try:
        fn(*args)
    except ValueError:
        return "ValueError"
    except Exception as exc:  # noqa: BLE001 - 测试要把"抛错类型不对"也暴露出来
        return type(exc).__name__
    return "no-raise"


def test_fips_vector() -> None:
    print("1) FIPS-197 C.1 已知答案：")

    got = aes.encrypt_ecb(FIPS_PT, FIPS_KEY)
    check("加密 == 期望密文（含末尾整块填充）", got == FIPS_PADDED_CT, got.hex())
    check("首个分组 == FIPS-197 C.1 密文", got[:16] == FIPS_CT, got[:16].hex())
    check("解密方向 == 原明文", aes.decrypt_ecb(FIPS_PADDED_CT, FIPS_KEY) == FIPS_PT)


def test_sp800_38a() -> None:
    print("2) NIST SP 800-38A F.1.1 ECB-AES128：")

    plaintext = b"".join(bytes.fromhex(pt) for pt, _ in SP_PAIRS)
    ciphertext = b"".join(bytes.fromhex(ct) for _, ct in SP_PAIRS)

    got = aes.encrypt_ecb(plaintext, SP_KEY)
    check("整体加密 == 四段密文拼接（ECB 无链式状态）", got[:64] == ciphertext, got[:64].hex())
    check("整体解密 == 四段明文拼接", aes.decrypt_ecb(got, SP_KEY) == plaintext)

    for index, (pt_hex, ct_hex) in enumerate(SP_PAIRS, start=1):
        one = aes.encrypt_ecb(bytes.fromhex(pt_hex), SP_KEY)
        check(f"第 {index} 组单独加密的首块 == 期望密文", one[:16] == bytes.fromhex(ct_hex), one[:16].hex())


def test_pkcs7() -> None:
    print("3) PKCS#7 填充边界：")

    empty_padded = aes.pkcs7_pad(b"")
    check("空数据补齐到 16 字节", len(empty_padded) == 16, str(len(empty_padded)))
    check("空数据的填充全为 0x10", empty_padded == bytes([16]) * 16, empty_padded.hex())

    for size, expect_pad in ((15, 1), (16, 16), (17, 15)):
        data = bytes(range(size))
        padded = aes.pkcs7_pad(data)
        added = len(padded) - size
        check(f"{size} 字节 → 填充 {expect_pad} 字节", added == expect_pad, str(added))
        check(
            f"{size} 字节 → 尾部 {expect_pad} 个字节都等于 {expect_pad}",
            padded[-expect_pad:] == bytes([expect_pad]) * expect_pad,
            padded[-expect_pad:].hex(),
        )
        check(f"{size} 字节 pad→unpad 往返一致", aes.pkcs7_unpad(padded) == data)


def test_padded_size() -> None:
    print("4) padded_size 与实际密文长度一致：")

    for size in (0, 1, 15, 16, 17, 31, 32, 1000):
        data = bytes(size)
        expected = aes.padded_size(size)
        actual = len(aes.encrypt_ecb(data, SP_KEY))
        check(f"len(encrypt_ecb) == padded_size（{size}）", actual == expected, f"{actual} != {expected}")
        # 契约里的两种写法必须等价：ceil((size+1)/16)*16
        check(
            f"padded_size 等价 ceil((size+1)/16)*16（{size}）",
            expected == -(-(size + 1) // 16) * 16,
            str(expected),
        )

    check("padded_size(0) == 16（空数据也补一整块）", aes.padded_size(0) == 16, str(aes.padded_size(0)))


def test_errors() -> None:
    print("5) 异常路径：")

    good_key = SP_KEY
    valid_ct = aes.encrypt_ecb(b"hello clawbot", good_key)

    check("密文长度 15 → ValueError", raise_kind(aes.decrypt_ecb, bytes(15), good_key) == "ValueError")
    check("密文长度 17 → ValueError", raise_kind(aes.decrypt_ecb, bytes(17), good_key) == "ValueError")
    check("空密文 → ValueError", raise_kind(aes.decrypt_ecb, b"", good_key) == "ValueError")

    # FIPS-197 的单分组向量在 ECB 下解密得到明文 ...eeff，末字节 0xff 不可能是
    # 合法填充，因此这是"能确定必然抛错"的坏填充用例，不依赖概率。
    check(
        "未加填充的 FIPS 分组（末字节 0xff）→ ValueError",
        raise_kind(aes.decrypt_ecb, FIPS_CT, FIPS_KEY) == "ValueError",
    )
    check("全 0x00 假密文块 → ValueError", raise_kind(aes.decrypt_ecb, bytes(16), good_key) == "ValueError")

    # 手工构造的坏填充：填充字节 0、填充字节越界、尾部与声称长度不符。
    check("全 0x00 的 16 字节填充 → ValueError", raise_kind(aes.pkcs7_unpad, bytes(16)) == "ValueError")
    check(
        "填充字节 0x11（越界）→ ValueError",
        raise_kind(aes.pkcs7_unpad, bytes(15) + bytes([17])) == "ValueError",
    )
    check(
        "填充字节与内容不符 → ValueError",
        raise_kind(aes.pkcs7_unpad, bytes(13) + b"\x05\x04\x03") == "ValueError",
    )
    check("长度非 16 倍数 → ValueError", raise_kind(aes.pkcs7_unpad, bytes(15)) == "ValueError")

    for bad_len in (0, 15, 17, 32):
        check(
            f"密钥 {bad_len} 字节 → ValueError",
            raise_kind(aes.encrypt_ecb, b"data", bytes(bad_len)) == "ValueError",
        )
        check(
            f"解密时密钥 {bad_len} 字节 → ValueError",
            raise_kind(aes.decrypt_ecb, valid_ct, bytes(bad_len)) == "ValueError",
        )

    check("正常密文仍可解密（异常路径没误伤）", aes.decrypt_ecb(valid_ct, good_key) == b"hello clawbot")


def test_random_roundtrip() -> None:
    print("6) 随机数据往返（种子固定）：")

    rnd = random.Random(1234)
    sizes = (0, 1, 15, 16, 17, 31, 32, 33, 63, 64, 65, 127, 128, 255, 256,
             1000, 4096, 65535, 65536, (1 << 20) - 3)
    mismatched = []
    for size in sizes:
        data = rnd.randbytes(size) if size else b""
        key = rnd.randbytes(16)
        if aes.decrypt_ecb(aes.encrypt_ecb(data, key), key) != data:
            mismatched.append(size)
    check(f"{len(sizes)} 种长度 encrypt→decrypt 完全一致", not mismatched, f"不一致的长度：{mismatched}")

    # ECB 的固有性质：相同分组加密结果相同，用来确认没有意外引入链式状态。
    block = bytes(range(16))
    doubled = aes.encrypt_ecb(block + block, SP_KEY)
    check("相同明文分组 → 相同密文分组（ECB 语义）", doubled[:16] == doubled[16:32], doubled.hex())


def test_performance() -> None:
    print("7) 1 MiB 性能：")

    data = random.Random(99).randbytes(1 << 20)

    started = time.perf_counter()
    encrypted = aes.encrypt_ecb(data, SP_KEY)
    enc_seconds = time.perf_counter() - started

    started = time.perf_counter()
    decrypted = aes.decrypt_ecb(encrypted, SP_KEY)
    dec_seconds = time.perf_counter() - started

    print(f"  1 MiB 加密耗时 {enc_seconds:.2f}s")
    print(f"  1 MiB 解密耗时 {dec_seconds:.2f}s")

    check("1 MiB 加密 ≤ 3.0s", enc_seconds <= 3.0, f"{enc_seconds:.2f}s")
    check("1 MiB 解密 ≤ 3.0s", dec_seconds <= 3.0, f"{dec_seconds:.2f}s")
    check("1 MiB 往返一致", decrypted == data)


def test_cryptography_cross_check() -> None:
    print("8) 可选交叉验证：")

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        print("  skip cryptography 未安装，跳过交叉验证")
        return

    def reference(key: bytes, data: bytes) -> bytes:
        encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return encryptor.update(data) + encryptor.finalize()

    # 参考实现不做填充，所以喂给它的是我们补齐后的数据。
    check(
        "FIPS 向量补齐后的密文与 cryptography 一致",
        aes.encrypt_ecb(FIPS_PT, FIPS_KEY) == reference(FIPS_KEY, aes.pkcs7_pad(FIPS_PT)),
    )

    rnd = random.Random(4321)
    mismatched = []
    for size in (0, 1, 15, 16, 17, 100, 1000, 4096, 65536):
        key = rnd.randbytes(16)
        data = rnd.randbytes(size) if size else b""
        if aes.encrypt_ecb(data, key) != reference(key, aes.pkcs7_pad(data)):
            mismatched.append(size)
    check("多种长度下与 cryptography 的 ECB 结果一致", not mismatched, f"不一致的长度：{mismatched}")

    key = rnd.randbytes(16)
    data = rnd.randbytes(4096)
    ciphertext = reference(key, aes.pkcs7_pad(data))
    check("cryptography 加密 → 本模块解密 一致", aes.decrypt_ecb(ciphertext, key) == data)


def main() -> int:
    print("AES-128-ECB 自检：")
    print(f"  后端标识：{aes.backend()}（BLOCK_SIZE={aes.BLOCK_SIZE}）")
    check("backend() == 'pure-python'", aes.backend() == "pure-python", aes.backend())
    check("BLOCK_SIZE == 16", aes.BLOCK_SIZE == 16, str(aes.BLOCK_SIZE))
    print()

    test_fips_vector()
    test_sp800_38a()
    test_pkcs7()
    test_padded_size()
    test_errors()
    test_random_roundtrip()
    test_performance()
    test_cryptography_cross_check()

    print()
    if FAILURES:
        print(f"FAIL（{len(FAILURES)} 项）：")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
