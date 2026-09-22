import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ===================== 配置项 =====================
MAX_STACK_FRAMES = 10  # 栈深度截断：只保留前N帧
SKIP_NOISE_BLOCKS = True
# =================================================

@dataclass
class LockInfo:
    typ: str       # locked / waiting to lock / waiting on / parking to wait for
    address: str
    clazz: str

@dataclass
class ThreadItem:
    name: str
    is_vm_gc_thread: bool          # True=GC/VM原生线程（无State，无java栈）
    state_raw: Optional[str] = None # 完整状态字符串 "TIMED_WAITING (parking)"
    stack_frames: List[str] = None
    locks: List[LockInfo] = None

    def __post_init__(self):
        if self.stack_frames is None:
            self.stack_frames = []
        if self.locks is None:
            self.locks = []

    def get_normalized_stack_signature(self) -> Tuple[str, ...]:
        """
        归一化栈签名：
        - 有栈帧：正常归一化栈帧，剥离模块版本前缀，截断
        - 无栈帧：使用 ("__NO_STACK__", thread_name)，每个线程独立签名，不会合并
        """
        if not self.stack_frames:
            # 没有栈帧，用线程名区分，避免全部合并到空签名
            return ("__NO_STACK__", self.name)

        sig_frames = []
        for frame in self.stack_frames[:MAX_STACK_FRAMES]:
            # 剥离 java.base@21.0.11/ 这类模块版本前缀
            clean = re.sub(r"[a-zA-Z0-9\.]+@[\d\.]+/", "", frame)
            sig_frames.append(clean.strip())
        return tuple(sig_frames)


@dataclass
class DeadlockInfo:
    raw_text: str
    thread_names: List[str]


class JStackParser:
    def __init__(self, raw_text: str):
        self.raw = raw_text
        self.lines = [line.rstrip("\n") for line in self.raw.splitlines()]
        self.threads: List[ThreadItem] = []
        self.deadlock: Optional[DeadlockInfo] = None
        self._deadlock_start_idx: Optional[int] = None
        # 正则：匹配引号结束后，存在 #数字（Java线程标记）
        self.re_has_java_tid = re.compile(r'"[^"]+"\s+#\d+')

    def parse(self):
        # Step1: 找到死锁起始位置，死锁区域不参与主线程解析，避免重复统计
        for idx, line in enumerate(self.lines):
            if "Found one Java-level deadlock" in line:
                self._deadlock_start_idx = idx
                break

        # Step2: 分割成块，以空行分割，过滤噪音块
        blocks: List[List[str]] = []
        current_block: List[str] = []
        for idx, line in enumerate(self.lines):
            # 到达死锁区域，停止主线程块解析
            if self._deadlock_start_idx is not None and idx >= self._deadlock_start_idx:
                if current_block:
                    blocks.append(current_block)
                    current_block = []
                break
            if line.strip() == "":
                if current_block:
                    blocks.append(current_block)
                    current_block = []
            else:
                current_block.append(line)
        if current_block:
            blocks.append(current_block)

        # Step3 逐个解析块
        for blk in blocks:
            first_line = blk[0].strip()
            # 跳过噪音块
            if re.match(r"Threads class SMR info|_java_thread_list|JNI global refs|Full thread dump|^[0-9]{4}-[0-9]{2}-[0-9]{2}", first_line):
                continue
            if not first_line.startswith('"'):
                continue

            # 【核心结构性判断】
            head_line = blk[0]
            has_hash_id = bool(self.re_has_java_tid.search(head_line))
            if has_hash_id:
                # Java线程：头行引号外带有 #数字，一定存在Thread.State行
                th = self._parse_java_thread(blk)
                self.threads.append(th)
            else:
                # VM/GC原生线程：引号外没有#id，无State、无java栈
                th = self._parse_vm_gc_thread(blk)
                self.threads.append(th)

        # Step4 单独解析死锁段落
        if self._deadlock_start_idx is not None:
            deadlock_lines = self.lines[self._deadlock_start_idx:]
            deadlock_raw = "\n".join(deadlock_lines)
            dead_thread_names = re.findall(r'"([^"]+)":', deadlock_raw)
            self.deadlock = DeadlockInfo(raw_text=deadlock_raw, thread_names=dead_thread_names)

    def _parse_vm_gc_thread(self, block: List[str]) -> ThreadItem:
        """GC/VM原生线程，没有state、没有java栈"""
        head_line = block[0]
        m = re.match(r'"([^"]+)"', head_line)
        name = m.group(1) if m else head_line
        return ThreadItem(
            name=name,
            is_vm_gc_thread=True
        )

    def _parse_java_thread(self, block: List[str]) -> ThreadItem:
        """解析普通Java线程（带State + 栈帧+锁行）"""
        head_line = block[0]
        m = re.match(r'"([^"]+)"', head_line)
        thread_name = m.group(1) if m else "unknown"
        state_raw: Optional[str] = None
        stack_frames = []
        locks: List[LockInfo] = []

        for line in block[1:]:
            line_stripped = line.strip()
            # 匹配 Thread.State 行
            state_match = re.match(r"java\.lang\.Thread\.State:\s+(.*)", line_stripped)
            if state_match:
                state_raw = state_match.group(1)
                continue
            # 栈帧行: \tat xxx
            if line.startswith("\tat "):
                stack_frames.append(line_stripped)
                continue
            # 锁标记行: 以\t- 开头
            lock_match = re.match(r"\t-\s+(waiting to lock|locked|waiting on|parking to wait for)\s+<(0x[0-9a-f]+)>\s*\(a\s*(.*?)\)", line)
            if lock_match:
                lock_type, addr, clazz = lock_match.groups()
                locks.append(LockInfo(typ=lock_type, address=addr, clazz=clazz))
                continue
        return ThreadItem(
            name=thread_name,
            is_vm_gc_thread=False,
            state_raw=state_raw,
            stack_frames=stack_frames,
            locks=locks
        )

    def get_merged_stack_groups(self) -> List[dict]:
        """按栈签名归并，相同栈合并计数；无栈线程各自独立分组"""
        group_map: Dict[Tuple, List[ThreadItem]] = defaultdict(list)
        for th in self.threads:
            sig = th.get_normalized_stack_signature()
            group_map[sig].append(th)
        result = []
        for sig, thread_list in group_map.items():
            sample = thread_list[0]
            is_no_stack = sig[0] == "__NO_STACK__"
            result.append({
                "count": len(thread_list),
                "sample_thread_name": sample.name,
                "thread_state": sample.state_raw,
                "is_no_stack": is_no_stack,
                "is_vm_gc_thread": sample.is_vm_gc_thread,
                "stack_signature": list(sig),
                "sample_locks": [l.__dict__ for l in sample.locks]
            })
        # 按count降序
        result.sort(key=lambda x: x["count"], reverse=True)
        return result

    def group_by_state(self) -> Dict[str, List[ThreadItem]]:
        """按线程状态分组，VM/GC原生线程单独放一组"""
        groups = defaultdict(list)
        for th in self.threads:
            if th.is_vm_gc_thread:
                groups["VM/GC Native Thread"].append(th)
            else:
                state = th.state_raw if th.state_raw else "UNKNOWN"
                groups[state].append(th)
        return groups

    def summary_report(self) -> str:
        """生成文本汇总报告"""
        lines = []
        lines.append("=" * 70)
        lines.append("JSTACK 解析汇总报告")
        lines.append("=" * 70)
        lines.append(f"总线程数（含GC/VM）: {len(self.threads)}")
        if self.deadlock:
            lines.append(f"\n🔥【检测到死锁！】涉及线程: {self.deadlock.thread_names}")
            lines.append("死锁原始文本：")
            lines.append(self.deadlock.raw_text)
        else:
            lines.append("\n✅ 未检测到Java死锁")

        lines.append("\n----- 按线程状态分组 -----")
        state_groups = self.group_by_state()
        for state, ths in state_groups.items():
            lines.append(f"{state} : {len(ths)} threads")

        lines.append(f"\n----- 栈签名合并结果（相同栈合并计数，最多前{MAX_STACK_FRAMES}帧）-----")
        merged = self.get_merged_stack_groups()
        for item in merged:
            tag_parts = []
            if item["is_vm_gc_thread"]:
                tag_parts.append("【VM原生线程】")
            if item["is_no_stack"]:
                tag_parts.append("【无栈线程】")
            tag = " ".join(tag_parts)
            lines.append(f"\n[计数={item['count']}] {tag}样例线程名={item['sample_thread_name']} 状态={item['thread_state']}")
            if not item["is_no_stack"]:
                for f in item["stack_signature"]:
                    lines.append(f"    {f}")
            else:
                lines.append("    > 该线程无Java栈帧，不参与栈阻塞分析")
        return "\n".join(lines)


# ===================== 使用示例 =====================
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        sys.exit("用法: python -m jvm_doctor.jstack_parser <jstack 输出文件>")
    with open(sys.argv[1], encoding="utf-8") as f:
        parser = JStackParser(f.read())
    parser.parse()
    print(parser.summary_report())
