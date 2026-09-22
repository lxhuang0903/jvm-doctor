"""jvm-doctor —— 诊断运行中 JVM 的 MCP Server。"""

from .server import mcp

__all__ = ["main", "mcp"]


def main() -> None:
    """命令行入口。客户端把它当子进程拉起，走 stdio 传输。

    ⚠️ stdio 模式下 stdout 是协议通道，任何 print() 都会污染它、直接让客户端
       解析失败。要打日志只能往 stderr 写。
    """
    mcp.run(transport="stdio")
