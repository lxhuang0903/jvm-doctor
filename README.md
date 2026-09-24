# jvm-doctor

[![CI](https://github.com/lxhuang0903/jvm-doctor/actions/workflows/ci.yml/badge.svg)](https://github.com/lxhuang0903/jvm-doctor/actions/workflows/ci.yml)

## 概述
这是一个诊断运行中 JVM 的 MCP Server。封装了资源和工具便于为LLM提供运行时JVM信息，以便LLM进行诊断

针对thread_dump做了合并归档
端到端实测触发（ 200 worker → jstack → thread_dump):116,102 B → 7,156 B,最大组 [计数=200] worker-0 BLOCKED


│                 │     20 worker     │      200 worker      │
├──────────────────┼───────────────────┼──────────────────────┤
│ 原始 jstack      │ 21,195 B / 352 行 │ 116,102 B / 1,837 行 │
├──────────────────┼───────────────────┼──────────────────────┤
│ thread_dump 输出 │ 7,153 B / 193 行  │ 7,156 B / 193 行     │
├──────────────────┼───────────────────┼──────────────────────┤
│ 压缩比           │ 0.337             │ 0.062(约 16:1)       │
├──────────────────┼───────────────────┼──────────────────────┤
│ Java 线程        │ 36                │ 216                  │
├──────────────────┼───────────────────┼──────────────────────┤
│ 归并组           │ 35                │ 35                   │
├──────────────────┼───────────────────┼──────────────────────┤
│ 最大组           │ x20               │ x200                 │
├──────────────────┼───────────────────┼──────────────────────┤
│ 死锁段           │ ✅ 完整           │ ✅ 完整              │
└──────────────────┴───────────────────┴──────────────────────┘

归并后的输出体积正比于「不同栈的种类数」,与线程总数无关。
线程越多、重复度越高,压缩比越好 —— 36 线程时 0.337,216 线程时 0.062。
生产环境 400+ 线程的线程池场景会更极端

## 环境依赖
- JDK 21 （必须是 JDK 不是 JRE），需正确配置JAVA_HOME
- uv
- attach 要同用户，同pid namespace ，不支持容器

## 资源和工具
### Resources
#### jvm://processes
当前用户可见的 JVM 进程列表。限制仅返回10条数据

#### jvm://{pid}/flags
获取JVM启动参数
- 返回的是 517 个里挑出来的子集23个,不是全部
- 字段集按 GC 分支,所以先看 UseG1GC / UseParallelGC 那几行
- G1 下的 MaxNewSize 是软上限,不是实际新生代大小
- 每个值后边标注来值的来源 
   - {command line} 用户显式传的
   - {ergonomic} JVM 按机器规格 / 当前 GC 推导
   - {default} 编译期默认值,从未被计算

#### jvm://{pid}/properties
考虑到为了避免敏感数据泄露给LLM，采用了白名单机制，仅根据白名单返回配置

### Tools
#### thread_dump
抓一次线程快照，返回归并后的视图与死锁检测结果

#### gc_stat（TBD）
堆各代使用率、GC 次数与累计停顿

#### heap_histogram（TBD）
堆内对象占用 Top-N

## 安装使用

### 客户端配置
#### Claude Code 集成
.mcp.json文件里面配置mcp

{
    "mcpServers": {
      "jvm-doctor": {
        "command": "uvx",
        "args": [
          "--from",
          "https://github.com/lxhuang0903/jvm-doctor.git",
          "jvm-doctor"
        ]
      }
    }
}

#### 其他（TBD）

### 验证
#### 靶子应用

项目源代码中提供了测试靶子DemoApp，用来模拟实际应用几种常见的异常场景

curl -O https://raw.githubusercontent.com/lxhuang0903/jvm-doctor/main/demo-app/DemoApp.java

JDK21下直接执行 java DemoApp.java 8080

   java DemoApp.java              # 前台跑，会打印自己的 PID
   curl localhost:8080/deadlock   # 制造死锁（jstack 会报 Found one Java-level deadlock）
   curl localhost:8080/exhaust    # 制造线程堆积（N 个线程 BLOCKED 在同一个 monitor）
   curl localhost:8080/leak       # 每次分配 20MB 不释放（堆直方图里 byte[] 会飙上去）
   curl localhost:8080/status     # 看当前制造了多少病

针对LLM提问，如“查看pid为1234的线程栈信息”

## 调试
MCP Inspector

npx @modelcontextprotocol/inspector uvx --from https://github.com/lxhuang0903/jvm-doctor.git jvm-doctor





## Java 侧实现

`java/` 下是同一份需求的 Java 实现，走 Spring AI 的 MCP server starter。
目的不是把 Python 侧翻译一遍，而是对比两条技术路径。

```bash
cd java && mvn package -DskipTests
java -jar target/jvm-doctor-java-0.1.0.jar     # stdio，等客户端连
```

出口和 Python 侧对齐：resource `jvm://processes` / `jvm://{pid}/flags` /
`jvm://{pid}/properties`，tool `thread_dump`。

依赖 Spring Boot 4.1.1 + Spring AI 2.0.1。注意 `@McpTool` / `@McpResource`
这套注解在 `spring-ai-mcp-annotations` 里，1.0.0 没有，2.0.x 才有；查版本要读
`maven-metadata.xml`，Maven Central 的搜索接口给的 latestVersion 是陈的。

### 同一份需求，两条路径

Python 侧解析 `jcmd` / `jstack` 的文本输出，Java 侧走 MXBean 拿结构化对象。
解析层整个消失：

| Python 侧 | Java 侧 |
| --- | --- |
| 解析 jstack 352 行文本 | `ThreadMXBean.dumpAllThreads()` → `ThreadInfo[]` |
| 找 `Found one Java-level deadlock` 段落 | `findDeadlockedThreads()` → `long[]` |
| 正则抠 `- waiting to lock <0x..>` | `ThreadInfo.getLockInfo()` |
| 区分 GC 原生线程（判有没有 `#id`） | 不返回 —— `ThreadInfo` 只有 Java 线程 |
| 解析 `VM.flags -all` 517 行 | `getVMOption(name)` 按名字取 |
| 从 `{ergonomic}` 花括号抠来源 | `VMOption.getOrigin()` 是枚举 |
| 对比 `VM.flags` 与 `VM.command_line` 区分用户设的 | `getInputArguments()` 天然只含用户传的 |
| `GC.heap_info` 三套语法 + `jstat` 的 `-` 哨兵 | `MemoryPoolMXBean` / `GarbageCollectorMXBean` |

但**归并、截断、返回什么给模型，两边一样要自己写** —— 那部分不是语言问题，
是 agent 工程问题。框架帮你做掉的是协议和解析，做不掉「工具返回什么」。

### 两条路径的真实差异

**`HotSpotDiagnosticMXBean` 不能枚举全部 flag。** `getDiagnosticOptions()` 只返回
可运行时修改的那批，查具体 flag 只能按名字取。所以「按 GC 分档的 flag 白名单」
在 Java 侧是必需品，不是优化 —— 同一个设计决策，Python 侧是为了精简输出，
Java 侧是 API 逼的。

**哨兵值的形态不同。** 同一个「无上限」，两条路径给的值不一样：

| flag | jcmd（Python 侧） | JMX（Java 侧） |
| --- | --- | --- |
| `MaxGCPauseMillis` 无目标 | `18446744073709551614` | `-2` |
| `MaxNewSize` ZGC 下 | `18446744073709551615` | `-1` |

JMX 把 unsigned 64 位截断成了有符号值。`-2` 比 `2⁶⁴−2` 更难识别 —— 后者一看就是
极值，前者像个合法的小负数，模型更容易误读成「暂停目标 -2 毫秒」。

**参数名在 Java 里默认丢失。** `@McpResource(uri = "jvm://{pid}/flags")` 靠参数名
绑定 `{pid}`，而 Java 编译默认不保留参数名，要靠 `-parameters`（Spring Boot parent
已经配了 `<maven.compiler.parameters>true</maven.compiler.parameters>`）。接进
公司已有的 parent pom、或用 Gradle / IDE 直接编译时会失效。更稳的写法是在
`@McpArg(name = "pid")` / `@McpToolParam(name = "...")` 里显式写名字。
Python 侧没有这个问题 —— 参数名在运行时永远可读。

### 交叉验证：两条路径等价

同一个靶子（先 `curl /deadlock` 和 `/exhaust?workers=200`），两个实现各跑一次
`thread_dump`：

| | Python（解析 jstack 文本） | Java（ThreadMXBean） |
| --- | --- | --- |
| 原始输入 | 120,197 B / 1,882 行 | 不适用（拿的是对象） |
| 报告 | 9,651 B / 229 行 | 7,710 B / 153 行 |
| **最大归并组** | **x200** | **x200** |
| **死锁检出** | **deadlock-1-A / B** | **deadlock-1-A / B** |
| Java 线程 | 219 | 216 |
| GC / VM 原生线程 | 18 | **0** |

**最大归并组和死锁两边一致 —— 这就是「归并判据等价」的证据。** 组数或最大组对不上，
说明有一边的签名算错了，而且能立刻定位是哪一边。

剩下的差异都能解释：219 vs 216 是两次调用之间有几个临时 worker 超时退出了；
原生线程 18 vs 0 是机制差异（`ThreadMXBean` 只返回 Java 线程），这也是 Java 侧报告
更短的主要原因。

Java 侧这个出口还省掉两件事：死锁的等待关系是 `getLockName()` + `getLockOwnerName()`
直接拼的，不用从 `Found one Java-level deadlock` 段落里解析 monitor 地址再和主体的栈
对应（Python 侧还得处理「死锁段的栈会重复出现」这个坑）；栈深度截断是
`dumpAllThreads(true, true, 10)` 的第三个参数，JMX 自带。

但**归并本身两边都要自己写** —— 包括那个「无栈线程必须用线程名兜底」的判据：
Signal Dispatcher / Attach Listener / DestroyJavaVM 都没有栈，只用空签名会把它们
错误地合并成「N 个线程卡在同一处」（Python 侧实测合出过一组 x8）。MXBean 给对象，
不给结论。

### 已知限制：stdout 污染没有机制保护

stdio 传输下 **stdout 就是协议通道**，MCP SDK 直接拿 `System.out` 写 JSON-RPC 帧
（反编译 `StdioServerTransportProvider` 可以看到 `getstatic System.out`）。任何别的
东西写进 stdout —— 一行日志、一句 `System.out.println` 调试、某个库的启动横幅 ——
都会插进帧之间，客户端解析失败、连接直接断，而且报错在客户端那侧，从 server
这边完全看不出原因。

这边靠 `application.properties` 压住：

```properties
logging.pattern.console=
logging.file.name=./jvm-doctor-java.log
```

**但这是约定，不是机制。** 任何人加一行 `System.out.println`，约定就破了。

试过在 `main()` 第一行把 `System.out` 换成指向 stderr 的流，结果是**协议帧自己也跟着
跑进了 stderr** —— SDK 在它初始化那一刻读全局的 `System.out`，谁先改谁赢。要真正
做到「协议独占 stdout」，只能自己声明 transport bean，把抢在前面存下来的真 stdout
交给它：

```java
PrintStream real = System.out;                    // 先抢到真 stdout
System.setOut(new PrintStream(new FileOutputStream(FileDescriptor.err), true));
return new StdioServerTransportProvider(mapper, System.in, real);
```

代价是绕过 Spring AI 的自动配置，所以这里选择不做，文档化。

Python 侧有同样的问题（SDK 也是直接拿 `sys.stdout`），只是 Python 默认不会自动往
stdout 打日志，不容易触发。**这是 stdio 传输的固有缺陷，不是某个 SDK 的疏忽。**
