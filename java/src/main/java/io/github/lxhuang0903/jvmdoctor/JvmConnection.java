package io.github.lxhuang0903.jvmdoctor;

import com.sun.management.HotSpotDiagnosticMXBean;
import com.sun.tools.attach.AttachNotSupportedException;
import com.sun.tools.attach.VirtualMachine;
import java.io.IOException;
import java.lang.management.GarbageCollectorMXBean;
import java.lang.management.ManagementFactory;
import java.lang.management.MemoryMXBean;
import java.lang.management.MemoryPoolMXBean;
import java.lang.management.RuntimeMXBean;
import java.lang.management.ThreadMXBean;
import java.util.List;
import javax.management.MBeanServerConnection;
import javax.management.remote.JMXConnector;
import javax.management.remote.JMXConnectorFactory;
import javax.management.remote.JMXServiceURL;

/**
 * attach 到目标 JVM 并拿到各个 MXBean。对应 Python 侧的 {@code _jvm.py}：
 * 只解决「怎么把连接建对」，不含 tool/resource 的设计决策。
 *
 * <p>路径是 attach → {@code startLocalManagementAgent()} → JMX：
 * <ol>
 *   <li>{@link VirtualMachine#attach(String)} 握手（会在目标 JVM 里永久创建一个
 *       {@code Attach Listener} 线程 —— 这是可观察的副作用，声明 tool 时要算进去）
 *   <li>{@code startLocalManagementAgent()}（JDK 9+）让目标临时起一个本地 JMX 端点，
 *       目标不需要预先配 {@code -Dcom.sun.management.jmxremote}
 *   <li>连上去，拿 MXBean 代理
 * </ol>
 *
 * <p>⚠️ attach 的三条硬约束和 Python 侧完全一样，换语言不豁免：
 * 同一个用户、同一个 PID namespace、目标没开 {@code -XX:+DisableAttachMechanism}。
 * 跨容器必然失败 —— 那是设计，不是 bug。
 *
 * <p>每次调用建一条新连接后关掉。缓存连接能省掉重复 attach 的开销，但要处理目标退出、
 * 连接失效、并发这些生命周期问题 —— 那是设计决策，留给调用方。
 */
public final class JvmConnection implements AutoCloseable {

    private final JMXConnector connector;
    private final MBeanServerConnection conn;

    private JvmConnection(JMXConnector connector, MBeanServerConnection conn) {
        this.connector = connector;
        this.conn = conn;
    }

    /** 一个可见的 JVM。{@code display} 是主类 + 命令行参数。 */
    public record Jvm(String pid, String display) {}

    /**
     * 当前用户可见的 JVM。不需要 attach，静态调用。
     *
     * <p>{@code displayName()} 带命令行参数（等价于 {@code jcmd -l}，比 {@code jps -l} 强 ——
     * 后者对 {@code java Foo.java} 和 {@code -jar} 启动的进程只显示一个没信息量的主类名）。
     *
     * <p>会把调用方自己也列出来，所以要滤掉。
     *
     * <p>⚠️ 实测：开了 {@code -XX:+DisableAttachMechanism} 的 JVM<b>不会出现在这个列表里</b>
     * （{@code jcmd -l} 同样看不到）。也就是说模型无法通过这个出口发现它们 —— 那种 pid
     * 只可能由用户直接给出。
     */
    public static List<Jvm> list() {
        String self = String.valueOf(ProcessHandle.current().pid());
        return VirtualMachine.list().stream()
                .filter(d -> !self.equals(d.id()))
                .map(d -> new Jvm(d.id(), d.displayName() == null || d.displayName().isBlank()
                        ? "(未知)" : d.displayName()))
                .toList();
    }

    /**
     * 连上目标 JVM。
     *
     * @throws JvmException 已经翻译成模型能据以改变行为的话，直接返回给模型即可
     */
    public static JvmConnection open(String pid) throws JvmException {
        VirtualMachine vm = null;
        try {
            vm = VirtualMachine.attach(pid);
            String url = vm.startLocalManagementAgent();
            JMXConnector c = JMXConnectorFactory.connect(new JMXServiceURL(url));
            return new JvmConnection(c, c.getMBeanServerConnection());
        } catch (Exception e) {
            throw explain(pid, e);
        } finally {
            if (vm != null) {
                try {
                    vm.detach();
                } catch (IOException ignored) {
                    // detach 失败不影响已经建好的 JMX 连接
                }
            }
        }
    }

    // ── MXBean ────────────────────────────────────────────────────────────
    // 这些返回的是类型化对象，不是要解析的文本 —— Python 侧整个解析层在这里消失。

    /** 线程快照。{@code dumpAllThreads(true, true)} 连锁信息一起带回来。 */
    public ThreadMXBean threads() throws JvmException {
        return proxy(ManagementFactory.THREAD_MXBEAN_NAME, ThreadMXBean.class);
    }

    /** 堆 / 非堆总量。 */
    public MemoryMXBean memory() throws JvmException {
        return proxy(ManagementFactory.MEMORY_MXBEAN_NAME, MemoryMXBean.class);
    }

    /** 启动参数、system properties。{@code getInputArguments()} 只含用户显式传的。 */
    public RuntimeMXBean runtime() throws JvmException {
        return proxy(ManagementFactory.RUNTIME_MXBEAN_NAME, RuntimeMXBean.class);
    }

    /**
     * 各内存池的 used/committed/max。池的名字随 GC 变（{@code G1 Eden Space} /
     * {@code PS Eden Space}），但结构一样 —— 不用像 Python 侧那样为
     * {@code GC.heap_info} 写三套解析。
     */
    public List<MemoryPoolMXBean> memoryPools() throws JvmException {
        return platformBeans(MemoryPoolMXBean.class);
    }

    /** 每个收集器的次数与累计耗时，对应 jstat 的 YGC/YGCT/FGC/FGCT，但没有 {@code -} 哨兵。 */
    public List<GarbageCollectorMXBean> garbageCollectors() throws JvmException {
        return platformBeans(GarbageCollectorMXBean.class);
    }

    /**
     * JVM flag。{@code getVMOption(name)} 的 {@code getOrigin()} 直接是枚举
     * （DEFAULT / ERGONOMIC / VM_CREATION …），不用从 {@code {ergonomic}} 里抠。
     *
     * <p>⚠️ 但它<b>不能枚举全部 flag</b>：{@code getDiagnosticOptions()} 只返回可运行时
     * 修改的那批。要查具体某个 flag 只能按名字取 —— 所以 Python 侧那张「按 GC 分档的
     * flag 白名单」在这边是必需品，不是优化。
     */
    public HotSpotDiagnosticMXBean hotspotDiagnostic() throws JvmException {
        return proxy("com.sun.management:type=HotSpotDiagnostic", HotSpotDiagnosticMXBean.class);
    }

    private <T> T proxy(String name, Class<T> type) throws JvmException {
        try {
            return ManagementFactory.newPlatformMXBeanProxy(conn, name, type);
        } catch (IOException e) {
            throw new JvmException("读取 " + type.getSimpleName() + " 失败：" + e.getMessage(), e);
        }
    }

    private <T extends java.lang.management.PlatformManagedObject> List<T> platformBeans(Class<T> type)
            throws JvmException {
        try {
            return ManagementFactory.getPlatformMXBeans(conn, type);
        } catch (IOException e) {
            throw new JvmException("读取 " + type.getSimpleName() + " 失败：" + e.getMessage(), e);
        }
    }

    @Override
    public void close() throws IOException {
        connector.close();
    }

    // ── 错误翻译 ──────────────────────────────────────────────────────────
    // 和 Python 侧 _jvm._explain() 同一套判据：把异常翻成「模型读完知道下一步干什么」
    // 的话。直接把 stack trace 甩回去，模型只会原地重试同一个调用。

    private static JvmException explain(String pid, Exception e) {
        String msg = String.valueOf(e.getMessage()).toLowerCase();

        // 生产环境加固。和下面几种「换个参数再试」不同：本机制整个不可用，
        // 换任何命令都一样，模型应该停手。放最前面。
        if (msg.contains("does not support the attach mechanism")) {
            return new JvmException(
                    "PID " + pid + " 启动时关掉了 attach 机制（-XX:+DisableAttachMechanism），"
                            + "attach / JMX 本地代理全部不可用，换命令重试也没有意义。"
                            + "这台 JVM 只能走它自己暴露的远程 JMX 端点或监控接口。", e);
        }

        // 同一个「pid 不存在」，各平台措辞不同，只能枚举 —— Python 侧踩过一模一样的坑
        if (msg.contains("no such process")
                || msg.contains("non existent jvm pid")
                || msg.contains("not ready to participate")
                || e instanceof NumberFormatException) {
            return new JvmException(
                    "PID " + pid + " 不存在、已退出，或者它根本不是 JVM。"
                            + "先读 resource jvm://processes 列一遍当前进程再试。", e);
        }

        if (msg.contains("access denied")
                || msg.contains("operation not permitted")
                || msg.contains("well-known file is not secure")) {
            return new JvmException(
                    "没权限 attach 到 PID " + pid + "。attach 要求和目标 JVM 同一个用户、"
                            + "同一个 PID namespace。跨容器或跨机器诊断要改用远程 JMX，不是 attach。", e);
        }

        if (e instanceof AttachNotSupportedException) {
            return new JvmException(
                    "无法 attach 到 PID " + pid + "：" + e.getMessage()
                            + "。确认它是 HotSpot JVM，且和本进程同用户同 namespace。", e);
        }

        return new JvmException("连接 PID " + pid + " 失败：" + e, e);
    }

    /** 已经翻译成人话的失败。message 可以直接返回给模型。 */
    public static class JvmException extends Exception {
        public JvmException(String message, Throwable cause) {
            super(message, cause);
        }
    }
}
