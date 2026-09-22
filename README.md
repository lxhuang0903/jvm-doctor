# jvm-doctor

## 概述
这是一个诊断运行中 JVM 的 MCP Server。封装了资源和工具便于为LLM提供运行时JVM信息，以便LLM进行诊断

针对thread_dump做了合并归档
端到端实测触发 200 worker → jstack → thread_dump):116,102 B → 7,156 B,最大组 [计数=200] worker-0 BLOCKED

原始   21,195 → 116,102 B     涨 5.5 倍
输出    7,153 →   7,156 B     涨 3 字节

归并后的输出体积正比于「不同栈的种类数」,与线程总数无关。
线程越多、重复度越高,压缩比越好 —— 36 线程时 0.337,216 线程时 0.062。
生产环境 400+ 线程的线程池场景会更极端

## 资源和工具
uv sync(拆完依赖之后才成立)
2 resource + 3 tool,各自干嘛、危险性分档
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

#### gc_stat
堆各代使用率、GC 次数与累计停顿

#### heap_histogram
堆内对象占用 Top-N







