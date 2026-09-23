package io.github.lxhuang0903.jvmdoctor;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * jvm-doctor 的 Java 侧实现。
 *
 * <p>和 Python 侧是同一份需求、两条技术路径：Python 侧解析 jcmd / jstack 的文本输出，
 * 这边走 MXBean 拿结构化对象 —— {@code ThreadMXBean.dumpAllThreads()} 直接给
 * {@code ThreadInfo[]}，{@code findDeadlockedThreads()} 直接给死锁线程 id，
 * {@code VMOption.getOrigin()} 是枚举而不是花括号里的字符串。整个解析层消失。
 *
 * <p>但归并、截断、给模型返回什么，两边一样要自己写 —— 那部分不是语言问题，
 * 是 agent 工程问题。
 */
@SpringBootApplication
public class JvmDoctorApplication {

    public static void main(String[] args) {
        SpringApplication.run(JvmDoctorApplication.class, args);
    }
}
