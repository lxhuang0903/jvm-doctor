"""jvm-doctor —— 诊断运行中 JVM 的 MCP Server。

把 jcmd / jstack 的原始输出，压成模型读得懂、也读得起的形状。三件事是核心：

  1. 归并    一个 400 线程的 dump 是 300+ KB / 15 万 token，直接甩给模型
             既烧钱又读不懂。按栈签名归并成「20 个线程卡在同一个栈」。
             实测 116 KB / 216 线程 -> 6.4 KB；输出体积只随「不同栈的种类数」
             增长，与线程总数无关。

  2. 分档    jmap -dump:live 在大堆上会触发 Full GC，把 JVM 冻住几秒到几分钟。
             模型不知道这件事。危险性要同时进 tool description（让模型知道）
             和代码（兜住模型不听话的情况）—— ToolAnnotations 只是 hint，
             协议不强制客户端遵守。

  3. 脱敏    VM.system_properties 里常有 spring.datasource.password。
             这里走白名单：只放行已知安全的 JDK 标准属性，其余隐藏，并告诉模型
             「隐藏了 N 条」——「这项不存在」和「这项被挡了」对模型是两回事。
"""

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ._jvm import JvmError, list_jvms, run
from .jcmd_flag import parse_jcmd_vm_flags_all
from .jstack_parser import JStackParser

mcp = MCPServer(
    name="jvm-doctor",
    instructions=(
        """诊断运行中的 JVM。
        pid 从 resource jvm://processes 读。
        启动参数读 jvm://<pid>/flags，system properties 读 jvm://<pid>/properties
        （这两个是 template，不出现在 resources/list 里，按上面的格式直接拼 URI 读）。
        拿到 pid 后再按需调 thread_dump / gc_stat / heap_histogram
        """
    ),
)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Resources —— 模型可以「读」的上下文
# ═══════════════════════════════════════════════════════════════════════════

@mcp.resource("jvm://processes")
def processes() -> str:
    """当前用户可见的 JVM 进程列表。限制仅返回10条数据。"""
    try:
        jvmlist = list_jvms()[:10]
        result = []
        for jvm in jvmlist:
            pid = jvm.pid
            display = jvm.display
            result.append(f"pid={pid}, display={display}")
        
        return '\n'.join(result)

    except JvmError as e:
        return str(e)

def bytes_convert(size_bytes: int):
    """
    字节自动转换为合适单位 B/KiB/MiB/GiB/TiB/PiB/EiB/ZiB/YiB（二进制1024进制）
    :param size_bytes: 字节数（整数）
    :return: (数值, 单位)，数值自动去除末尾.0，如8.0→8
    """
    if size_bytes < 0:
        raise ValueError("字节数不能为负数")
    # 补齐全部常用二进制单位
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB", "ZiB", "YiB"]
    index = 0
    size = size_bytes
    while size >= 1024 and index < len(units)-1:
        size /= 1024
        index += 1

    rounded_size = round(size, 2)
    if rounded_size.is_integer():
        rounded_size = int(rounded_size)
    return rounded_size, units[index]

def handle_sizet(flags):

    sentinel_flags_map = {
        "MaxMetaspaceSize": { "value": 18446744073709551615, "postfix": "无上限" },
        "MaxDirectMemorySize": { "value": 0, "postfix": "跟随Xmx" },
        "ActiveProcessorCount": { "value": -1, "postfix": "自动" },
        "ZCollectionInterval": { "value": 0, "postfix": "不定时触发" },
        "ParallelGCThreads": { "value": 0, "postfix": "单线程" }
    }

    should_conver_byte_flags = ['MaxHeapSize','InitialHeapSize','NewSize','OldSize','MaxNewSize','MetaspaceSize','MaxMetaspaceSize','MaxDirectMemorySize','ReservedCodeCacheSize','G1HeapRegionSize','SoftMaxHeapSize']
    percent_flags = ['InitiatingHeapOccupancyPercent','G1HeapWastePercent','G1ReservePercent','MaxRAMPercentage']

    useG1GC = False
    # 转化数值 拼接来源
    for flag in flags:

        if flag.name in sentinel_flags_map and flag.value == sentinel_flags_map[flag.name]["value"]:
            flag.format_value = f'{flag.format_value}({sentinel_flags_map[flag.name]["postfix"]})'
        elif flag.name in should_conver_byte_flags:
            bytecount, unit = bytes_convert(flag.value)
            flag.format_value = f"{flag.value} ({bytecount!s}{unit!s})"
        if flag.name in percent_flags:
            flag.format_value = f'{flag.format_value}%'
        flag.format_value = str(flag.format_value) + "{" + flag.source + "}"
        if flag.name  == 'UseG1GC' and flag.value:
            useG1GC = True

    if useG1GC:
        for flag in flags:
            if flag.name == 'MaxNewSize' and flag.source != 'command line':
                flag.format_value = flag.format_value.replace("{", "(软上限){")


@mcp.resource("jvm://{pid}/flags")
def flags(pid: str) -> str:
    """获取JVM启动参数
    - 返回的是 517 个里挑出来的子集23个,不是全部
    - 字段集按 GC 分支,所以先看 UseG1GC / UseParallelGC 那几行
    - G1 下的 MaxNewSize 是软上限,不是实际新生代大小
    - 每个值后边标注来值的来源 
        - {command line} 用户显式传的
        - {ergonomic} JVM 按机器规格 / 当前 GC 推导
        - {default} 编译期默认值,从未被计算
    """
    try:
        jcmd_resp = run('jcmd', pid, "VM.flags -all")
        all_flags_map = parse_jcmd_vm_flags_all(jcmd_resp)
        gc_flags = filter_gc_relative_flags(all_flags_map)
        handle_sizet(gc_flags)

        return "\n".join([flag.name + '=' + str(flag.format_value) for flag in gc_flags])
    except JvmError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return str(e)

def check_support_gc(all_flags_map):
    return 'UseParallelGC' in all_flags_map or 'UseG1GC' in all_flags_map or 'UseZGC' in all_flags_map or 'UseSerialGC' in all_flags_map

def filter_gc_relative_flags(all_flags_map):
    if not check_support_gc(all_flags_map):
        raise JvmError('This app use unkonwn gc')

    gc_relative_flags = ['UseParallelGC', 'UseG1GC', 'UseZGC', 'UseSerialGC']
    if all_flags_map['UseParallelGC'].value:
        parallelGC_flags = ['NewSize', 'OldSize', 'MaxNewSize', 'NewRatio','SurvivorRatio', 'MaxTenuringThreshold', 'UseAdaptiveSizePolicy', 'ParallelGCThreads', 'GCTimeRatio']
        gc_relative_flags.extend(parallelGC_flags)
    elif all_flags_map['UseG1GC'].value:
        g1_flags = ['MaxGCPauseMillis', 'G1HeapRegionSize', 'MaxNewSize', 'InitiatingHeapOccupancyPercent','G1HeapWastePercent','G1ReservePercent','ConcGCThreads','GCTimeRatio']
        gc_relative_flags.extend(g1_flags)
    elif all_flags_map['UseZGC'].value:
        zgc_flags = ['ZGenerational', 'SoftMaxHeapSize', 'ZUncommit', 'ZUncommitDelay', 'ZCollectionInterval', 'ZAllocationSpikeTolerance']
        gc_relative_flags.extend(zgc_flags)
    elif all_flags_map['UseSerialGC'].value:
        serial_gc_flags = ['NewSize', 'OldSize', 'MaxNewSize', 'ParallelGCThreads', 'ConcGCThreads', 'MaxGCPauseMillis']
        gc_relative_flags.extend(serial_gc_flags)
    else:
        raise JvmError('This app use unkonwn gc')

    gc_relative_flags.extend(['MaxHeapSize', 'InitialHeapSize', 'MaxMetaspaceSize', 'MetaspaceSize', 'MaxDirectMemorySize', 'ReservedCodeCacheSize', 'UseCompressedOops', 'MaxRAMPercentage', 'ActiveProcessorCount', 'HeapDumpOnOutOfMemoryError', 'AlwaysPreTouch'])
    result = [flag for flag_name, flag in all_flags_map.items() if flag_name in gc_relative_flags]
    result = sorted(result, key=lambda x: 0 if x.name in ['UseParallelGC', 'UseG1GC', 'UseZGC', 'UseSerialGC'] else 1)
    return result




@mcp.resource("jvm://{pid}/properties")
def properties(pid: str) -> str:
    """System properties。"""
    #
    # ⚠️ 这是整个 server 最危险的一个出口。真实应用里这里会有：
    #      spring.datasource.password=...
    #      aws.secretKey=...
    #      javax.net.ssl.keyStorePassword=...
    #
    # 脱敏两种做法，这里选白名单：
    #   黑名单：key 命中 password/secret/token/credential/key 就打码
    #           —— 简单，但漏一个就出事
    #   白名单：只放行已知安全的前缀（java.version / os.name / user.timezone…）
    #           —— 安全，但会挡掉有诊断价值的业务配置
    #
    # - 这个 property 不存在(应用没配)
    # - 被你挡掉了(应用配了,但不给看)

    try:
        lines = run('jcmd', pid, "VM.system_properties").splitlines()
        whitelist = ['java.version', 'file.encoding', 'java.vm.name', 'java.vm.version', 'os.name', 'java.io.tmpdir', 'java.home', 'user.dir','os.arch']
        result = []
        for line in lines[2:]:
            property_name = line.split('=')[0]
            if property_name in whitelist:
                result.append(line)
        summary = f'{len(lines)}条中按白名单放行{len(result)}条，其余{len(lines)-len(result)}条未列入白名单已隐藏\n'
        properties = "\n".join(result)
        return summary + properties 
        
    except JvmError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return str(e)

    


# ═══════════════════════════════════════════════════════════════════════════
# 2. Tools —— 模型可以「做」的动作
# ═══════════════════════════════════════════════════════════════════════════
# ToolAnnotations 是协议自带的「危险性声明」，不用自己发明：
#
#   read_only_hint=True    只读，随便调
#   idempotent_hint=True   重复调用无额外副作用
#   destructive_hint=True  有破坏性 / 会显著影响目标进程
#
# 客户端（Claude Code / Desktop）会据此决定要不要弹审批。
# 但 ⚠️ hint 只是「提示」，协议不强制客户端遵守 —— 所以危险的操作
# 在代码里也必须自己兜一层，不能只靠 annotation。这就是三档的意义。


@mcp.tool(
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=False),
)
def thread_dump(pid: int) -> str:
    """抓一次线程快照，返回归并后的视图与死锁检测结果。"""

    try:
        dump_content = run('jstack', str(pid))
        parser = JStackParser(dump_content)
        parser.parse()
        report = parser.summary_report()
        return report
        
    except JvmError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return str(e)


@mcp.tool(
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=False),
)
def gc_stat(pid: int) -> str:
    """堆各代使用率、GC 次数与累计停顿。"""
    # TODO: jcmd <pid> GC.heap_info，或 jstat -gcutil <pid>
    #   两个输出形状完全不同，选一个或者合并。
    #   注意 GC 算法不同输出也不同（G1 / ZGC / Parallel 的分代概念都不一样），
    #   demo-app 跑的是 G1。
    raise NotImplementedError


@mcp.tool(
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=False),
)
def heap_histogram(pid: int, top: int = 20) -> str:
    """堆内对象占用 Top-N。"""
    # TODO: jcmd <pid> GC.class_histogram
    #
    # ⚠️ 这个比看起来危险：class_histogram 在部分 JVM 上会触发一次 STW。
    #    demo-app 这种小堆无感，几十 G 的堆上是另一回事。
    #    所以它算「② 采样档」：只读，但有开销 —— 要不要限频？
    #
    # top 默认 20：全量直方图有几千行，模型只看得懂前几十行。
    #   但别忘了加上「其余 N 类共占 X MB」，否则模型不知道自己看的是局部。
    raise NotImplementedError


# 第三档（侵入操作）—— 目前不提供，理由见 README
#
#   heap_dump(pid, path)   jmap -dump:live
#     · 大堆上触发 Full GC，冻住 JVM 几秒到几分钟
#     · 产出的文件可能几十 GB，写满磁盘
#     · annotations 要 destructive_hint=True
#     · 光靠 annotation 不够：客户端可以无视 hint，所以代码里也要拦
#
#   MCP 是 stdio 协议，server 没有 UI，没法就单次调用向人发起审批。


# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # stdio 是 MCP 的默认传输，客户端把 server 当子进程拉起。
    #
    # ⚠️ stdio 模式下 stdout 是协议通道，任何 print() 都会污染它、直接让
    #    客户端解析失败。要打日志只能往 stderr 写。这个坑第一次必踩。
    mcp.run(transport="stdio")
