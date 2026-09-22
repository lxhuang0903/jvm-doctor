import re
from dataclasses import dataclass


@dataclass
class FlagInfo:
    """单个JVM Flag信息对象"""
    flag_type: str      # bool / intx / uintx / ccstr ...
    name: str
    raw_value: str      # 原始字符串，保留原样，区分空字符串/无值
    category: str       # 第一组{}内，如 pd product / product lp64_product
    source: str         # 第二组{}内，如 command line / ergonomic
    value: object       # 自动转换后的Python类型（bool/int/float/str）
    format_value: object


def parse_jcmd_vm_flags_all(text: str) -> dict[str, FlagInfo]:
    """
    解析 jcmd <pid> VM.flags -all 完整输出字符串
    :param text: jcmd命令返回的全部原始输出
    :return: dict[flag_name, FlagInfo]
    """
    result: dict[str, FlagInfo] = {}
    lines = text.splitlines()

    # 跳过头部：找到 "[Global flags]" 之后的行才开始解析
    start_idx = 0
    for idx, line in enumerate(lines):
        if "[Global flags]" in line:
            start_idx = idx + 1
            break

    # 正则规则：
    # 分组1：类型，分组2：flag名称，分组3：=和第一个{之间的原始值，分组4：第一{}，分组5：第二{}
    # 逻辑：<type> <name> = <val> {category} {source}
    # 捕获：type, name, raw_val, {category_content}, {source_content}
    pat = re.compile(
        r"""
        ^\s*                # 行首空白
        (?P<flag_type>\w+)  # 类型 bool/uintx/ccstr/size_t...
        \s+
        (?P<name>[\w]+)     # flag名字，如 UseParallelGC
        \s*=\s*             # =号，两边允许空格
        (?P<raw_val>.*?)    # = 之后，直到第一个 { 之前的全部内容（含空格，非贪婪）
        \s*\{(?P<category>.+?)\}  # 第一个花括号内部内容
        \s*\{(?P<source>.+?)\}    # 第二个花括号内部内容
        \s*$                # 行尾空白
        """,
        re.VERBOSE
    )

    for line in lines[start_idx:]:
        stripped_line = line.strip()
        if not stripped_line:
            continue
        m = pat.match(line)
        if not m:
            continue

        gd = m.groupdict()
        flag_type = gd["flag_type"]
        name = gd["name"]
        raw_val = gd["raw_val"].rstrip()
        category = gd["category"].strip()
        source = gd["source"].strip()

        # 类型转换
        py_val: object
        if flag_type == "bool":
            py_val = raw_val.lower() == "true"
        elif flag_type in ("int", "intx", "uint", "uintx", "size_t", "uint64_t"):
            if raw_val == "":
                py_val = None
            else:
                py_val = int(raw_val)
        elif flag_type == "double":
            py_val = float(raw_val)
        elif flag_type in ("ccstr", "ccstrlist"):
            # raw_val为空字符串就保留空串，不是None，区分语义
            py_val = raw_val
        else:
            # 未知类型直接存原始字符串
            py_val = raw_val

        flag = FlagInfo(
            flag_type=flag_type,
            name=name,
            raw_value=raw_val,
            category=category,
            source=source,
            value=py_val,
            format_value=py_val
        )
        result[name] = flag
    return result


# ---------------------- 测试用例 ----------------------
if __name__ == "__main__":
    test_input = """36914:
[Global flags]
bool   UseParallelGC        = true         {product} {command line}
uintx  ReservedCodeCacheSize= 251658240    {pd product} {ergonomic}
bool   UseCompressedOops    = true         {product lp64_product} {ergonomic}
ccstr  AllocateHeapAt       =              {product} {default}
size_t MaxMetaspaceSize     = 18446744073709551615 {product} {default}
"""
    flags_dict = parse_jcmd_vm_flags_all(test_input)
    for fname, finfo in flags_dict.items():
        print(f"==== {fname} ====")
        print(f"type: {finfo.flag_type}")
        print(f"raw_value: |{finfo.raw_value}|")
        print(f"value: {finfo.value}, type={type(finfo.value)}")
        print(f"category: {finfo.category}")
        print(f"source: {finfo.source}")
        print()