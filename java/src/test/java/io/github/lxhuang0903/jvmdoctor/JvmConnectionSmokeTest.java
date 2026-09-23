package io.github.lxhuang0903.jvmdoctor;

import java.lang.management.ThreadInfo;

/** 脚手架自检：对一个真实 pid 把各个 MXBean 都摸一遍。用法：… JvmConnectionSmokeTest <pid> */
public class JvmConnectionSmokeTest {
    public static void main(String[] args) throws Exception {
        String pid = args[0];
        try (JvmConnection c = JvmConnection.open(pid)) {
            var tmx = c.threads();
            ThreadInfo[] infos = tmx.dumpAllThreads(true, true);
            long[] dead = tmx.findDeadlockedThreads();
            System.out.println("  线程数        " + infos.length + "（只含 Java 线程，没有 GC 原生线程）");
            System.out.println("  死锁线程      " + (dead == null ? "无" : dead.length + " 个"));
            System.out.println("  堆            " + c.memory().getHeapMemoryUsage());
            System.out.println("  内存池        " + c.memoryPools().size() + " 个");
            c.garbageCollectors().forEach(g ->
                System.out.println("    GC " + g.getName() + "  次数=" + g.getCollectionCount()
                                   + "  累计=" + g.getCollectionTime() + "ms"));
            var opt = c.hotspotDiagnostic().getVMOption("MaxHeapSize");
            System.out.println("  MaxHeapSize   " + opt.getValue() + "  origin=" + opt.getOrigin());
            System.out.println("  用户传的参数   " + c.runtime().getInputArguments());
        } catch (JvmConnection.JvmException e) {
            System.out.println("  翻译后的报错：" + e.getMessage());
        }
    }
}
