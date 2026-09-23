package io.github.lxhuang0903.jvmdoctor;

import com.sun.management.HotSpotDiagnosticMXBean;
import com.sun.management.VMOption;
import com.sun.tools.attach.VirtualMachine;
import com.sun.tools.attach.VirtualMachineDescriptor;
import org.springframework.ai.mcp.annotation.McpArg;
import org.springframework.ai.mcp.annotation.McpResource;
import org.springframework.ai.mcp.annotation.McpTool;
import org.springframework.ai.mcp.annotation.McpToolParam;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.stream.Collectors;

/**
 * 脚手架自检：证明 tool 和 resource 两条注册路径都通。
 *
 * <p>真正的实现替换掉这里即可。注意和 Python 侧一样，resource 的参数从 URI 里抠出来，
 * 天然是 String；tool 的参数走 JSON Schema，声明成什么类型就会被强制转成什么。
 */
@Component
public class SmokeEndpoints {

    /** 对应 Python 侧的 jvm://processes。VirtualMachine.list() 直接给对象，不用解析 jcmd -l。 */
    @McpResource(uri = "jvm://processes", name = "processes",
            description = "当前用户可见的 JVM 进程列表")
    public String processes() {
        List<VirtualMachineDescriptor> vms = VirtualMachine.list();
        return vms.stream()
                .map(vm -> "pid=" + vm.id() + ", display=" + vm.displayName())
                .collect(Collectors.joining("\n"));
    }

    
    @McpResource(uri = "jvm://{pid}/flags", name = "flags",
            description = "获取JVM启动参数 - 返回的是 517 个里挑出来的子集23个,不是全部- 字段集按 GC 分支,所以先看 UseG1GC / UseParallelGC 那几行 - G1 下的 MaxNewSize 是软上限,不是实际新生代大小 - 每个值后边标注来值的来源")
    public String flags(@McpArg(name = "pid") String pid){

        List<VMOption> result = new ArrayList<>();
        List<String> defaultFlags = List.of("MaxHeapSize", "InitialHeapSize", "MaxMetaspaceSize", "MetaspaceSize", "MaxDirectMemorySize", "ReservedCodeCacheSize", "UseCompressedOops", "MaxRAMPercentage", "ActiveProcessorCount", "HeapDumpOnOutOfMemoryError", "AlwaysPreTouch");
        try (JvmConnection jvmConnection = JvmConnection.open(pid)) {
            HotSpotDiagnosticMXBean hotSpotDiagnosticMXBean = jvmConnection.hotspotDiagnostic();
            if (Objects.nonNull(getVMOption(hotSpotDiagnosticMXBean, "UseParallelGC")) &&  "true".equals(getVMOption(hotSpotDiagnosticMXBean, "UseParallelGC").getValue())) {
                List<String> parallelGCFlags = List.of("NewSize", "OldSize", "MaxNewSize", "NewRatio","SurvivorRatio", "MaxTenuringThreshold", "UseAdaptiveSizePolicy", "ParallelGCThreads", "GCTimeRatio");
                List<VMOption> vMoptions = getVMoptions(hotSpotDiagnosticMXBean, parallelGCFlags);
                result.addAll(vMoptions);
            } else if (Objects.nonNull(getVMOption(hotSpotDiagnosticMXBean, "UseG1GC")) &&  "true".equals(getVMOption(hotSpotDiagnosticMXBean, "UseG1GC").getValue())) {
                List<String> g1Flags = List.of("MaxGCPauseMillis", "G1HeapRegionSize", "MaxNewSize", "InitiatingHeapOccupancyPercent","G1HeapWastePercent","G1ReservePercent","ConcGCThreads","GCTimeRatio");
                List<VMOption> vMoptions = getVMoptions(hotSpotDiagnosticMXBean, g1Flags);
                result.addAll(vMoptions);
            } else if (Objects.nonNull(getVMOption(hotSpotDiagnosticMXBean, "UseZGC")) &&  "true".equals(getVMOption(hotSpotDiagnosticMXBean, "UseZGC").getValue())) {
                List<String> zgcFlags = List.of("ZGenerational", "SoftMaxHeapSize", "ZUncommit", "ZUncommitDelay", "ZCollectionInterval", "ZAllocationSpikeTolerance");
                List<VMOption> vMoptions = getVMoptions(hotSpotDiagnosticMXBean, zgcFlags);
                result.addAll(vMoptions);
            } else if (Objects.nonNull(getVMOption(hotSpotDiagnosticMXBean, "UseSerialGC")) &&  "true".equals(getVMOption(hotSpotDiagnosticMXBean, "UseSerialGC").getValue())) {
                List<String> serialGcFlags = List.of("NewSize", "OldSize", "MaxNewSize", "ParallelGCThreads", "ConcGCThreads", "MaxGCPauseMillis");
                List<VMOption> vMoptions = getVMoptions(hotSpotDiagnosticMXBean, serialGcFlags);
                result.addAll(vMoptions);
            }

            result.addAll(getVMoptions(hotSpotDiagnosticMXBean, defaultFlags));
            return result.stream().map(this::showVMOption).collect(Collectors.joining("\n"));
        } catch (JvmConnection.JvmException | IOException e) {
            return e.getMessage();
        }
    }

    private String showVMOption(VMOption vMoption) {
        return vMoption.getName() + "=" + vMoption.getValue() + "{" + vMoption.getOrigin().name() + "}";
    }

    private List<VMOption> getVMoptions(HotSpotDiagnosticMXBean hotSpotDiagnosticMXBean, List<String> optionNames) {
        return optionNames.stream().map(name -> getVMOption(hotSpotDiagnosticMXBean, name)).filter(Objects::nonNull).toList();
    }

    private VMOption getVMOption(HotSpotDiagnosticMXBean hotSpotDiagnosticMXBean, String optionName) {
        try {
            return hotSpotDiagnosticMXBean.getVMOption(optionName);
        } catch (Exception e) {
            return null;
        }
    }

    @McpResource(uri = "jvm://{pid}/properties", name = "properties",
            description = "System properties")
    public String properties(@McpArg(name = "pid") String pid){
        try (JvmConnection jvmConnection = JvmConnection.open(pid)) {
            Map<String, String> systemProperties = jvmConnection.runtime().getSystemProperties();
            List<String> whitelist = List.of("java.version", "file.encoding", "java.vm.name", "java.vm.version", "os.name", "java.io.tmpdir", "java.home", "user.dir", "os.arch");
            return systemProperties.entrySet().stream().filter(entry -> whitelist.contains(entry.getKey())).map(entry -> entry.getKey() + "=" + entry.getValue()).collect(Collectors.joining("\n"));
        } catch (Exception e) {
            return e.getMessage();
        }
    }

    /** 脚手架冒烟用，确认 tool 注册链路通。实现 thread_dump 时删掉。 */
    @McpTool(name = "ping", description = "自检用：原样回显，确认 tool 链路可用")
    public String ping(@McpToolParam(description = "任意文本") String text) {
        return "pong: " + text;
    }

    
}
