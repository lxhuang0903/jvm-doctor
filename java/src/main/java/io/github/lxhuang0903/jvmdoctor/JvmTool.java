package io.github.lxhuang0903.jvmdoctor;

import java.io.IOException;
import java.lang.management.MonitorInfo;
import java.lang.management.ThreadInfo;
import java.lang.management.ThreadMXBean;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.stream.Collectors;
import org.springframework.ai.mcp.annotation.McpTool;
import org.springframework.ai.mcp.annotation.McpToolParam;
import org.springframework.stereotype.Component;

/**
 * MCP tool 出口。
 *
 * <p>归并判据和 Python 侧 {@code jstack_parser.py} 一致：按栈签名分组、无栈线程用线程名
 * 兜底、死锁段单独提出来。但拿到数据的方式完全不同 —— 那边解析 jstack 的 352 行文本，
 * 这边 {@code dumpAllThreads()} 直接给 {@code ThreadInfo[]}，整个解析层不存在。
 */
@Component
public class JvmTool {

    /** 栈深度截断，对应 Python 侧的 MAX_STACK_FRAMES。 */
    private static final int MAX_STACK_FRAMES = 10;

    @McpTool(name = "thread_dump", description = """
            抓一次线程快照，返回归并后的视图与死锁检测结果。
            相同栈的线程合并成一条 + 计数，栈深度截断到前 10 帧。
            ⚠️ 会 attach 到目标 JVM：在目标进程里永久创建一个 Attach Listener 线程，
            并触发一次 safepoint（线程多 / 栈深时耗时更久）。不改业务数据，但不是零成本。""")
    public String threadDump(@McpToolParam(description = "java进程id") String pid) {
        try (JvmConnection jvmConnection = JvmConnection.open(pid)) {
            ThreadMXBean tmx = jvmConnection.threads();
            // 第三个参数是栈深度上限 —— JMX 自带截断，不用像 Python 侧那样自己切
            ThreadInfo[] threadInfos = tmx.dumpAllThreads(true, true, MAX_STACK_FRAMES);
            return report(threadInfos, tmx.findDeadlockedThreads());
        } catch (JvmConnection.JvmException | IOException e) {
            return e.getMessage();
        }
    }

    // ── 报告 ──────────────────────────────────────────────────────────────

    private String report(ThreadInfo[] threads, long[] deadlocked) {
        List<String> out = new ArrayList<>();
        out.add("=".repeat(70));
        out.add("THREAD DUMP 汇总报告（来源：ThreadMXBean，不是 jstack 文本）");
        out.add("=".repeat(70));
        out.add("Java 线程数: " + threads.length + "（MXBean 不返回 GC / VM 原生线程）");

        out.add("");
        out.addAll(deadlockSection(threads, deadlocked));

        out.add("");
        out.add("----- 按线程状态分组 -----");
        countBy(threads, t -> t.getThreadState().name())
                .forEach((state, n) -> out.add(state + " : " + n + " threads"));

        out.add("");
        out.add("----- 栈签名合并结果（相同栈合并计数，最多前 " + MAX_STACK_FRAMES + " 帧）-----");
        out.addAll(mergedSection(threads));

        return String.join("\n", out);
    }

    /**
     * 死锁段。{@code findDeadlockedThreads()} 只给线程 id，等待关系要从
     * {@code getLockName()} / {@code getLockOwnerName()} 拼 —— 这两个字段正是
     * Python 侧要从 {@code - waiting to lock <0x..>} 里用正则抠的东西。
     */
    private List<String> deadlockSection(ThreadInfo[] threads, long[] deadlocked) {
        if (deadlocked == null || deadlocked.length == 0) {
            return List.of("✅ 未检测到 Java 死锁");
        }
        Map<Long, ThreadInfo> byId = Arrays.stream(threads)
                .collect(Collectors.toMap(ThreadInfo::getThreadId, t -> t, (a, b) -> a));

        List<String> lines = new ArrayList<>();
        List<String> names = Arrays.stream(deadlocked)
                .mapToObj(id -> byId.containsKey(id) ? byId.get(id).getThreadName() : "tid-" + id)
                .toList();
        lines.add("🔥【检测到死锁】涉及 " + deadlocked.length + " 个线程: " + names);
        for (long id : deadlocked) {
            ThreadInfo t = byId.get(id);
            if (t == null) {
                continue;
            }
            lines.add("  \"" + t.getThreadName() + "\" 等待 " + t.getLockName()
                    + "，持有者是 \"" + t.getLockOwnerName() + "\"");
            for (MonitorInfo m : t.getLockedMonitors()) {
                lines.add("      已持有 " + m + "  at " + m.getLockedStackFrame());
            }
            for (StackTraceElement f : t.getStackTrace()) {
                lines.add("      at " + normalize(f.toString()));
            }
        }
        return lines;
    }

    private List<String> mergedSection(ThreadInfo[] threads) {
        Map<List<String>, List<ThreadInfo>> groups = new LinkedHashMap<>();
        for (ThreadInfo t : threads) {
            groups.computeIfAbsent(signature(t), k -> new ArrayList<>()).add(t);
        }

        List<String> lines = new ArrayList<>();
        groups.entrySet().stream()
                .sorted((a, b) -> Integer.compare(b.getValue().size(), a.getValue().size()))
                .forEach(e -> {
                    List<ThreadInfo> members = e.getValue();
                    ThreadInfo sample = members.getFirst();
                    boolean noStack = sample.getStackTrace().length == 0;
                    lines.add("");
                    lines.add("[计数=" + members.size() + "] " + (noStack ? "【无栈线程】" : "")
                            + "样例线程名=" + sample.getThreadName()
                            + " 状态=" + sample.getThreadState());
                    if (noStack) {
                        return;
                    }
                    for (String frame : e.getKey()) {
                        lines.add("    " + frame);
                    }
                    // 等待的锁 —— BLOCKED 的那一组里这条是诊断关键
                    if (sample.getLockName() != null) {
                        lines.add("    - 等待锁 " + sample.getLockName()
                                + (sample.getLockOwnerName() == null ? ""
                                   : "，持有者 \"" + sample.getLockOwnerName() + "\""));
                    }
                });
        return lines;
    }

    /**
     * 栈签名。和 Python 侧同一套判据：
     * <ul>
     *   <li>有栈：归一化后的帧列表（已由 dumpAllThreads 截断）
     *   <li>无栈：{@code ["__NO_STACK__", 线程名]} —— <b>必须带线程名</b>。
     *       Signal Dispatcher / Attach Listener / DestroyJavaVM / Notification Thread
     *       都没有栈，只用空列表当签名会把它们错误地合并成「N 个线程卡在同一处」。
     *       Python 侧实测踩过这个坑（当时错误地合出了一组 x8）。
     * </ul>
     *
     * <p>锁对象的身份不进签名：{@code getLockName()} 形如 {@code java.lang.Object@531e9784}，
     * 那个 hash 每次 dump 都可能变，进了签名就没法跨时间对比同一组线程是否一直卡着。
     */
    private List<String> signature(ThreadInfo t) {
        StackTraceElement[] stack = t.getStackTrace();
        if (stack.length == 0) {
            return List.of("__NO_STACK__", t.getThreadName());
        }
        return Arrays.stream(stack).map(f -> normalize(f.toString())).toList();
    }

    /**
     * 剥掉模块版本前缀。{@code StackTraceElement.toString()} 在 JDK 9+ 带
     * {@code java.base@21.0.11/}，不剥的话换个 JDK 小版本，签名全变、归并全散。
     */
    private String normalize(String frame) {
        return frame.replaceAll("[A-Za-z0-9.]+@[\\d.]+/", "");
    }

    private Map<String, Long> countBy(ThreadInfo[] threads, Function<ThreadInfo, String> key) {
        return Arrays.stream(threads)
                .collect(Collectors.groupingBy(key, LinkedHashMap::new, Collectors.counting()));
    }
}
