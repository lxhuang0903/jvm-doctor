// 有病的 Java 应用 —— jvm-doctor 的测试靶子。
//
// 刻意做成【单文件、零依赖】：JDK 21 可以直接 `java DemoApp.java` 跑起来，
//
//   java DemoApp.java              # 前台跑，会打印自己的 PID
//   curl localhost:8080/deadlock   # 制造死锁（jstack 会报 Found one Java-level deadlock）
//   curl localhost:8080/exhaust    # 制造线程堆积（N 个线程 BLOCKED 在同一个 monitor）
//   curl localhost:8080/leak       # 每次分配 20MB 不释放（堆直方图里 byte[] 会飙上去）
//   curl localhost:8080/status     # 看当前制造了多少病
//
// 这三个端点对应三类最常见的生产故障，也是 jvm-doctor 三个 tool 各自要诊断的目标。

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

public class DemoApp {

    // ---- 病灶 1：死锁 ------------------------------------------------------
    // 两把锁 + 两个线程 + 相反的加锁顺序。中间的 sleep 是为了让竞态必然发生，
    // 否则先跑完的线程会直接释放锁，死锁只是概率事件。
    private static final Object LOCK_A = new Object();
    private static final Object LOCK_B = new Object();
    private static final AtomicInteger deadlockRounds = new AtomicInteger();

    // ---- 病灶 2：线程堆积 --------------------------------------------------
    // 一个持有者长时间占着 monitor，后面的 worker 全部 BLOCKED 在同一处。
    // 线程栈会出现大量「几乎一模一样」的栈 —— 这正是 jstack 输出需要被归并的原因。
    private static final Object POOL_MONITOR = new Object();
    private static final AtomicInteger blockedWorkers = new AtomicInteger();

    // ---- 病灶 3：内存泄漏 --------------------------------------------------
    // static 集合只进不出，GC 回收不了。经典的「缓存忘了设上限」。
    private static final List<byte[]> LEAK = new ArrayList<>();

    public static void main(String[] args) throws IOException {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 8080;

        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/health",   ex -> respond(ex, "ok"));
        server.createContext("/status",   DemoApp::status);
        server.createContext("/deadlock", DemoApp::deadlock);
        server.createContext("/exhaust",  DemoApp::exhaust);
        server.createContext("/leak",     DemoApp::leak);
        server.setExecutor(null);   // 默认单线程，故障线程都是我们自己起的，便于识别
        server.start();

        long pid = ProcessHandle.current().pid();
        System.out.printf("""
                demo-app 已启动
                  PID  : %d
                  端口 : %d

                制造故障：
                  curl localhost:%d/deadlock
                  curl localhost:%d/exhaust?workers=20
                  curl localhost:%d/leak?mb=20
                %n""", pid, port, port, port, port);
    }

    // ---------------------------------------------------------------------

    private static void deadlock(HttpExchange ex) throws IOException {
        int round = deadlockRounds.incrementAndGet();

        Thread t1 = new Thread(() -> {
            synchronized (LOCK_A) {
                sleep(200);                       // 给 t2 时间拿到 LOCK_B
                synchronized (LOCK_B) { sleep(1); }
            }
        }, "deadlock-" + round + "-A");

        Thread t2 = new Thread(() -> {
            synchronized (LOCK_B) {
                sleep(200);                       // 给 t1 时间拿到 LOCK_A
                synchronized (LOCK_A) { sleep(1); }
            }
        }, "deadlock-" + round + "-B");

        t1.start();
        t2.start();
        respond(ex, "已制造第 " + round + " 组死锁（线程 deadlock-" + round + "-A / -B）\n"
                  + "约 0.3 秒后生效，此时 jstack 应报 Found one Java-level deadlock\n");
    }

    private static void exhaust(HttpExchange ex) throws IOException {
        int workers = intParam(ex, "workers", 20);

        // 持有者：抓住 monitor 不放，逼后面所有 worker 排队
        Thread holder = new Thread(() -> {
            synchronized (POOL_MONITOR) { sleep(10 * 60 * 1000); }
        }, "monitor-holder");
        holder.setDaemon(true);
        holder.start();
        sleep(50);                                 // 确保 holder 先拿到锁

        for (int i = 0; i < workers; i++) {
            Thread w = new Thread(() -> {
                blockedWorkers.incrementAndGet();
                synchronized (POOL_MONITOR) { sleep(1); }
                blockedWorkers.decrementAndGet();
            }, "worker-" + i);
            w.setDaemon(true);
            w.start();
        }

        respond(ex, "已起 " + workers + " 个 worker，全部会 BLOCKED 在同一个 monitor 上\n"
                  + "它们的线程栈几乎完全相同 —— 这正是 thread_dump 需要归并的场景\n");
    }

    private static void leak(HttpExchange ex) throws IOException {
        int mb = intParam(ex, "mb", 20);
        synchronized (LEAK) {
            for (int i = 0; i < mb; i++) {
                LEAK.add(new byte[1024 * 1024]);   // 1MB 一块，避免一次大分配走 humongous 路径
            }
        }
        respond(ex, "已泄漏 " + mb + " MB，累计 " + LEAK.size() + " MB\n"
                  + "堆直方图里 byte[] 应排第一\n");
    }

    private static void status(HttpExchange ex) throws IOException {
        Runtime rt = Runtime.getRuntime();
        respond(ex, """
                死锁组数     : %d   （每组 2 个线程）
                阻塞 worker  : %d
                已泄漏       : %d MB
                线程总数     : %d
                堆 已用/最大 : %d MB / %d MB
                """.formatted(
                deadlockRounds.get(), blockedWorkers.get(), LEAK.size(),
                Thread.activeCount(),
                (rt.totalMemory() - rt.freeMemory()) / 1048576, rt.maxMemory() / 1048576));
    }

    // ---------------------------------------------------------------------

    private static int intParam(HttpExchange ex, String key, int fallback) {
        String q = ex.getRequestURI().getQuery();
        if (q == null) return fallback;
        for (String kv : q.split("&")) {
            String[] p = kv.split("=", 2);
            if (p.length == 2 && p[0].equals(key)) {
                try { return Integer.parseInt(p[1]); } catch (NumberFormatException ignored) { }
            }
        }
        return fallback;
    }

    private static void respond(HttpExchange ex, String body) throws IOException {
        byte[] b = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "text/plain; charset=utf-8");
        ex.sendResponseHeaders(200, b.length);
        try (OutputStream os = ex.getResponseBody()) { os.write(b); }
    }

    private static void sleep(long ms) {
        try { Thread.sleep(ms); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
    }
}
