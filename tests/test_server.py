"""jvm-doctor 的本地回归测试。不走 MCP 客户端，改完直接跑：

    uv run tests/test_server.py

自己起两个靶子（G1 + Parallel），跑完杀掉。不依赖已经在跑的进程。

⚠️ 这里只覆盖「逻辑对不对」。三件事它抓不到，要靠真客户端或 MCP Inspector：
     · print() 污染 stdout（本测试不走 stdio 传输）
     · docstring 变成的 description 发出去长什么样
     · 客户端侧的 stale server —— 本测试每次都是新进程，天然没有
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from jvm_doctor import server
from jvm_doctor.jcmd_flag import parse_jcmd_vm_flags_all

JAVA = os.getenv("JAVA_HOME", str(Path.home() / ".sdkman/candidates/java/current"))
JAVA_BIN = str(Path(JAVA) / "bin" / "java")
DEMO = str(ROOT / "demo-app" / "DemoApp.java")

_passed, _failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}")
        if detail:
            for line in str(detail).splitlines()[:6]:
                print(f"       {line}")


# ═══════════════════════════════════════════════════════════════════
# 靶子
# ═══════════════════════════════════════════════════════════════════

def start_target(port: int, *jvm_args: str, timeout: float = 60.0):
    """起一个 demo-app，返回 (pid, Popen)。源码直跑模式要现编译，慢。"""
    proc = subprocess.Popen(
        [JAVA_BIN, *jvm_args, DEMO, str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(f"靶子退出了，args={jvm_args}")
            continue
        if m := re.search(r"PID\s*[:：]\s*(\d+)", line):
            return int(m.group(1)), proc
    proc.kill()
    raise RuntimeError(f"靶子 {timeout}s 没起来，args={jvm_args}")


# ═══════════════════════════════════════════════════════════════════
# 1. 解析器 —— 不需要 JVM
# ═══════════════════════════════════════════════════════════════════
# 每条都是 jcmd VM.flags -all 真实出现过的畸形行。

PARSER_SAMPLE = """27979:
[Global flags]
     bool UseParallelGC                            = true            {product} {command line}
    uintx ReservedCodeCacheSize                    = 251658240       {pd product} {ergonomic}
     bool UseCompressedOops                        = true            {product lp64_product} {ergonomic}
    ccstr AllocateHeapAt                           =                 {product} {default}
   size_t MaxMetaspaceSize                         = 18446744073709551615  {product} {default}
   double MaxRAMPercentage                         = 25.000000       {product} {default}
"""

PARSER_CASES = [
    # (name, flag, 断言函数)
    ("来源标记含空格", "UseParallelGC", lambda f: f.source == "command line"),
    ("类别含空格",     "ReservedCodeCacheSize", lambda f: f.category == "pd product"),
    ("类别双词",       "UseCompressedOops", lambda f: f.category == "product lp64_product"),
    ("ccstr 空值保留空串（不是 None）", "AllocateHeapAt",
     lambda f: f.value == "" and f.value is not None),
    ("bool 转成真 bool",   "UseParallelGC", lambda f: f.value is True),
    ("2⁶⁴−1 不溢出",       "MaxMetaspaceSize", lambda f: f.value == 18446744073709551615),
    ("double 转 float",    "MaxRAMPercentage", lambda f: f.value == 25.0),
]


def test_parser():
    print("\n【解析器】")
    m = parse_jcmd_vm_flags_all(PARSER_SAMPLE)
    check("跳过 pid 行 + [Global flags] 行", len(m) == 6, f"解析出 {len(m)} 条，期望 6")
    for name, flag_name, assertion in PARSER_CASES:
        f = m.get(flag_name)
        check(name, f is not None and assertion(f), f"{flag_name} = {f}")


# ═══════════════════════════════════════════════════════════════════
# 2. flags() —— 通用不变量，两个 GC 都要满足
# ═══════════════════════════════════════════════════════════════════

SOURCES = ("{default}", "{ergonomic}", "{command line}")
GC_NAMES = ("UseG1GC", "UseParallelGC", "UseSerialGC", "UseZGC")

# 白名单之外的字段，绝不能被当字节数换算
NOT_BYTES = ["MaxGCPauseMillis", "GCTimeRatio", "G1ReservePercent",
             "InitiatingHeapOccupancyPercent", "ConcGCThreads", "ParallelGCThreads",
             "NewRatio", "SurvivorRatio", "MaxTenuringThreshold", "ZUncommitDelay"]


def test_flags_common(pid: int, label: str):
    print(f"\n【flags() · {label}】")
    out = server.flags(str(pid))
    lines = [ln for ln in out.splitlines() if "=" in ln]

    check("返回非空且不是异常字符串", len(lines) > 5, out[:200])

    # 每行都要带来源 —— docstring 承诺了这个，不能只承诺不给
    no_src = [ln for ln in lines if not any(s in ln for s in SOURCES)]
    check("每行都带来源标记", not no_src, "\n".join(no_src))

    # GC 类型是理解其余字段的前提，必须在最前面
    head = [ln.split("=")[0] for ln in lines[:4]]
    check("GC 类型排在最前", all(n in GC_NAMES for n in head), f"前四行：{head}")

    # 非字节字段不许带容量单位
    bad = [ln for ln in lines
           if ln.split("=")[0] in NOT_BYTES and re.search(r"\d\s*(B|[KMGT]iB)\b", ln)]
    check("非字节字段没被误换算", not bad, "\n".join(bad))

    # 1024 进制就得叫 MiB/GiB
    wrong_unit = [ln for ln in lines if re.search(r"\d\s*[KMGT]B\b", ln)]
    check("容量单位用 MiB/GiB 不用 MB/GB", not wrong_unit, "\n".join(wrong_unit))

    d = {ln.split("=", 1)[0]: ln.split("=", 1)[1] for ln in lines}

    # 哨兵值：命中就给语义，且原值不能丢
    if "MaxMetaspaceSize" in d:
        v = d["MaxMetaspaceSize"]
        check("哨兵 MaxMetaspaceSize 翻译成语义", "无上限" in v, v)
        check("哨兵保留原值", "18446744073709551615" in v, v)
        check("哨兵不被字节换算", "EiB" not in v and "TiB" not in v, v)
    if "MaxDirectMemorySize" in d and d["MaxDirectMemorySize"].startswith("0"):
        check("哨兵 MaxDirectMemorySize 翻译", "跟随Xmx" in d["MaxDirectMemorySize"],
              d["MaxDirectMemorySize"])

    # 字节字段：换算值和原值都在
    if "MaxHeapSize" in d:
        v = d["MaxHeapSize"]
        check("字节字段保留原值 + 换算", bool(re.match(r"\d+\s*\(\d+(\.\d+)?GiB\)", v)), v)

    return d


def test_flags_g1(pid: int):
    d = test_flags_common(pid, "G1")
    print("  ── G1 专属 ──")
    check("残留字段 NewSize 被排除", "NewSize" not in d, str(list(d))[:200])
    check("残留字段 OldSize 被排除", "OldSize" not in d)
    check("G1 核心旋钮 MaxGCPauseMillis 在（默认值也要补出来）",
          "MaxGCPauseMillis" in d)
    check("同名不同义的 MaxNewSize 标了软上限",
          "软上限" in d.get("MaxNewSize", ""), d.get("MaxNewSize"))


def test_flags_parallel(pid: int):
    d = test_flags_common(pid, "Parallel")
    print("  ── Parallel 专属 ──")
    check("分代字段 NewSize 在", "NewSize" in d, str(list(d))[:200])
    check("分代字段 OldSize 在", "OldSize" in d)
    check("MaxNewSize 不该标软上限（Parallel 下是硬上限）",
          "软上限" not in d.get("MaxNewSize", ""), d.get("MaxNewSize"))
    check("UseAdaptiveSizePolicy 在（它决定上面那些是不是真配置）",
          "UseAdaptiveSizePolicy" in d)


# ═══════════════════════════════════════════════════════════════════
# 3. properties() —— 这是安全边界，回归了不会报错只会悄悄多放行
# ═══════════════════════════════════════════════════════════════════

# 白名单挡不住就出事的（黑名单也挡不住这些 —— 不含 password/secret/token）
MUST_NOT_LEAK = ["proxyHost", "proxyPort", "nonProxyHosts", "user.name", "user.home"]
# 挡掉就没诊断价值的
MUST_KEEP = ["java.version", "java.vm.name", "os.name"]


def test_properties(pid: int):
    print("\n【properties()】")
    out = server.properties(str(pid))

    leaked = [k for k in MUST_NOT_LEAK if k in out]
    check("敏感项没泄露", not leaked, f"泄露了：{leaked}")

    missing = [k for k in MUST_KEEP if k not in out]
    check("高价值项没被误挡", not missing, f"缺了：{missing}")

    # 模型要能分辨「这项不存在」和「这项被挡了」
    check("有过滤说明", bool(re.search(r"\d+\s*条", out)), out.splitlines()[0] if out else "")

    # 说明里的数字要和实际行数对得上，不然是在骗模型
    if m := re.search(r"放行\s*(\d+)\s*条", out):
        claimed = int(m.group(1))
        actual = len([ln for ln in out.splitlines() if "=" in ln])
        check("放行数和实际行数一致", claimed == actual, f"说放行 {claimed}，实际 {actual} 行")


# ═══════════════════════════════════════════════════════════════════
# 4. 错误路径 —— 模型读完要知道下一步干什么
# ═══════════════════════════════════════════════════════════════════

def test_error_path():
    print("\n【错误路径】")
    for name, fn in (("flags", server.flags), ("properties", server.properties)):
        try:
            out = fn("999999")
        except Exception as e:  # noqa: BLE001
            check(f"{name}(不存在的 pid) 不抛异常", False, f"{type(e).__name__}: {e}")
            continue
        check(f"{name}(不存在的 pid) 不抛异常", True)
        check(f"{name} 的报错是人话，不是 Python 异常字符串",
              "不存在" in out and "object" not in out, out[:120])


# ═══════════════════════════════════════════════════════════════════

def main():
    test_parser()
    test_error_path()

    targets = []
    try:
        print("\n起靶子（源码直跑要现编译，稍等）…")
        g1_pid, g1_proc = start_target(18090)
        targets.append(g1_proc)
        par_pid, par_proc = start_target(18091, "-XX:+UseParallelGC")
        targets.append(par_proc)
        print(f"  G1 pid={g1_pid}  Parallel pid={par_pid}")

        test_flags_g1(g1_pid)
        test_flags_parallel(par_pid)
        test_properties(g1_pid)
    finally:
        for p in targets:
            p.kill()

    print(f"\n{'─' * 46}\n通过 {_passed}，失败 {_failed}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
