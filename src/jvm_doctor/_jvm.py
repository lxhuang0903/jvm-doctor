"""调 JDK 诊断工具的底层封装。只解决「怎么把命令跑对」。

tool/resource 的划分、输出归并、权限分档都在 server.py。

"""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# 坑 1：JDK 工具不一定在 PATH 上
# ---------------------------------------------------------------------------
# 用 sdkman / jenv / asdf 管理 JDK 时，jcmd 只在当前 shell 的 PATH 里；
# MCP server 被 Claude Desktop 之类的客户端拉起时，拿到的是一个很干净的
# 环境，PATH 里往往没有 JDK。所以不能直接 subprocess.run("jcmd")。
#
# 找法按优先级：JAVA_HOME → PATH → sdkman 的 current 软链。
_SDKMAN = Path.home() / ".sdkman/candidates/java/current/bin"


def _find(tool: str) -> str:
    if jh := os.getenv("JAVA_HOME"):
        p = Path(jh) / "bin" / tool
        if p.exists():
            return str(p)
    if p := shutil.which(tool):
        return p
    p = _SDKMAN / tool
    if p.exists():
        return str(p)
    raise RuntimeError(
        f"找不到 {tool}。设置 JAVA_HOME 指向一个 JDK（不是 JRE —— JRE 不带诊断工具），"
        f"或者把 JDK 的 bin 加进 PATH。"
    )


# ---------------------------------------------------------------------------
# 坑 2：jps -l 看不到启动参数
# ---------------------------------------------------------------------------
# 实测：`java DemoApp.java`（JDK 11+ 的源码直跑模式）在 jps -l 里显示成
#     27979 jdk.compiler/com.sun.tools.javac.launcher.Main
# 主类名完全没有信息量。用 -jar 启动的应用同理，一堆进程长得一模一样。
#
# jcmd -l 会带上命令行参数：
#     27979 jdk.compiler/...Main gate-4-mcp-server/demo-app/DemoApp.java 18080
# 所以列进程一律用 jcmd -l，不要用 jps。
#
# ---------------------------------------------------------------------------
# 坑 3：attach 需要同一个用户
# ---------------------------------------------------------------------------
# jcmd/jstack 靠 attach 机制（往目标进程的 /tmp/.java_pid<N> 建 socket），
# 要求调用方和目标 JVM 是同一个 uid，且在同一个 PID namespace 里。
# 跨容器必然失败 —— 这不是 bug，是设计。远程诊断要走 JMX，不是 attach。


@dataclass(frozen=True)
class Jvm:
    pid: int
    display: str  # 主类 + 命令行参数，jcmd -l 的原样输出（去掉 pid）


class JvmError(RuntimeError):
    """调 JDK 工具失败。message 已经是能直接给模型看的人话。"""


def run(tool: str, *args: str, timeout: float = 15.0) -> str:
    """跑一个 JDK 工具，返回 stdout。

    不用 shell=True —— 参数里有 pid 和用户传入的字符串，走 shell 就是注入。

    timeout 不是可选项：jmap 在大堆上可以卡几分钟，jcmd 在目标 JVM
    停在安全点时会一直等。没有超时的 MCP tool 会把整个 agent 挂死。
    """
    cmd = [_find(tool), *args]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as e:
        raise JvmError(
            f"{tool} 超过 {timeout}s 没返回。目标 JVM 可能停在安全点上，"
            f"或者堆太大。换一个更轻的命令，或者调大 timeout。"
        ) from e

    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise JvmError(_explain(tool, args, out.strip()))
    return r.stdout


def _explain(tool: str, args: tuple[str, ...], raw: str) -> str:
    """把 JDK 工具的报错翻译成模型能据以改变行为的话。

    直接把 raw 甩回去，模型只会重试同一个命令 —— 它不知道
    「No such process」意味着该先列一遍进程。
    """
    low = raw.lower()
    pid = args[0] if args else "?"

    # 实测：macOS/JDK21 上给一个不存在的 pid，报的不是 "No such process"，
    # 而是 AttachNotSupportedException ... state is not ready to participate
    # in attach handshake。光看字面完全猜不到是「进程不存在」。
    if "not ready to participate" in low or "no such process" in low:
        return f"PID {pid} 不存在、已退出，或者它根本不是 JVM。先列一遍当前 JVM 进程再试。"

    # 目标进程属于别的用户 / 别的 PID namespace
    if any(
        s in low
        for s in ("access denied", "operation not permitted", "well-known file is not secure")
    ):
        return (
            f"没权限 attach 到 PID {pid}。attach 要求和目标 JVM 同一个用户、"
            f"同一个 PID namespace。跨容器或跨机器诊断要改用 JMX，不是 attach。"
        )

    # 命令名打错 —— 模型很容易把 jcmd 的子命令记岔（GC.heap_info vs GC.heapInfo）
    if "unknown diagnostic" in low or "unknown command" in low or "invalid command" in low:
        return (
            f"jcmd 子命令不对：{' '.join(args[1:]) or '(空)'}。"
            f"用 `jcmd {pid} help` 列出这个 JVM 实际支持的命令 —— 不同 JDK 版本差别很大。"
        )

    return f"{tool} 执行失败：{raw[:500]}"


def list_jvms() -> list[Jvm]:
    """当前用户可见的 JVM。用 jcmd -l，因为它带命令行参数（见坑 2）。"""
    out = run("jcmd", "-l", timeout=10)
    jvms = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, display = line.partition(" ")
        if not pid.isdigit():
            continue
        # 把 jcmd 自己过滤掉 —— 它每次都在列表里，纯噪音
        if "sun.tools.jcmd.JCmd" in display:
            continue
        jvms.append(Jvm(pid=int(pid), display=display or "(未知)"))
    return jvms
