import com.fileweb.desktop.DownloadName;
import com.fileweb.desktop.ServerAddress;

/**
 * 服务器地址与下载文件名的用例
 * ============================================================================
 * 跑法（纯 JDK，不需要安卓设备、不需要 JUnit、不需要联网）：
 *
 *     javac -encoding UTF-8 -d "%TEMP%\rd-check" ^
 *         android-client\app\src\main\java\com\fileweb\desktop\ServerAddress.java ^
 *         android-client\app\src\main\java\com\fileweb\desktop\DownloadName.java ^
 *         android-client\tools\CheckPureLogic.java
 *     java -cp "%TEMP%\rd-check" CheckPureLogic
 *
 * 为什么值得单独写这一份
 * ----------------------
 * 这两段逻辑出错都是「不吭声」的：
 *   * 地址规范化错了 —— 用户看到的是「连不上服务器」，会去怀疑网络、怀疑服务没开；
 *   * 文件名解析错了 —— 下载下来的文件叫 `download` 或一串乱码。
 * 而它们又恰好是**纯字符串处理**，完全没必要等到装到平板上才发现问题。
 *
 * 所以 ServerAddress / DownloadName 刻意不引用任何 android.* ，
 * 于是能在电脑上直接跑 —— 测的是**与 App 里同一份实现**，不是重写一遍。
 */

public final class CheckPureLogic {

    private static int total = 0;
    private static int failed = 0;

    private static void eq(String label, String actual, String expected) {
        total++;
        boolean ok = (actual == null) ? (expected == null) : actual.equals(expected);
        if (ok) {
            System.out.println("  ok   " + label + "  →  " + actual);
        } else {
            failed++;
            System.out.println("  FAIL " + label);
            System.out.println("         实际 = " + actual);
            System.out.println("         期望 = " + expected);
        }
    }

    public static void main(String[] args) {
        System.out.println("=== 服务器地址规范化（只填 IP 也要能连上）===");
        eq("只填 IP", ServerAddress.normalize("192.168.1.100"), "http://192.168.1.100:8000/");
        eq("IP + 端口", ServerAddress.normalize("192.168.1.100:8000"), "http://192.168.1.100:8000/");
        eq("带 scheme 无尾斜杠", ServerAddress.normalize("http://192.168.1.100:8000"),
            "http://192.168.1.100:8000/");
        eq("带 scheme 有尾斜杠", ServerAddress.normalize("http://192.168.1.100:8000/"),
            "http://192.168.1.100:8000/");
        eq("前后空格", ServerAddress.normalize("  10.0.0.5  "), "http://10.0.0.5:8000/");
        eq("主机名", ServerAddress.normalize("localhost"), "http://localhost:8000/");
        eq("https 与自定义端口", ServerAddress.normalize("https://nas.local:9000"),
            "https://nas.local:9000/");
        eq("自定义端口要保留", ServerAddress.normalize("192.168.1.100:9999"),
            "http://192.168.1.100:9999/");
        eq("IPv6", ServerAddress.normalize("[::1]:8000"), "http://[::1]:8000/");
        // ★ 不强行补尾斜杠：用户粘的是具体页面时，补成 /index.html/ 会 404
        eq("带子路径", ServerAddress.normalize("192.168.1.100:8000/some/path"),
            "http://192.168.1.100:8000/some/path");

        System.out.println("=== 不合法的输入要**拒绝**，而不是猜 ===");
        eq("空串", ServerAddress.normalize(""), null);
        eq("全空格", ServerAddress.normalize("   "), null);
        eq("只有 scheme", ServerAddress.normalize("http://"), null);
        eq("端口不是数字", ServerAddress.normalize("192.168.1.100:abc"), null);
        eq("null", ServerAddress.normalize(null), null);

        System.out.println("=== 地址框回填（显示成好敲的形式）===");
        eq("去掉 scheme 与尾斜杠", ServerAddress.stripScheme("http://192.168.1.100:8000/"),
            "192.168.1.100:8000");
        eq("https 同样处理", ServerAddress.stripScheme("https://nas.local:9000/"),
            "nas.local:9000");
        eq("空串", ServerAddress.stripScheme(""), "");
        eq("null", ServerAddress.stripScheme(null), "");

        System.out.println("=== 下载文件名：中文走 RFC 5987（本服务就是这么发的）===");
        eq("filename* 优先（中文）",
            DownloadName.fromDisposition(
                "attachment; filename=\"report.docx\"; "
                + "filename*=UTF-8''%E6%8A%A5%E5%91%8A.docx"),
            "报告.docx");
        eq("只有引号形式", DownloadName.fromDisposition("attachment; filename=\"a.txt\""),
            "a.txt");
        eq("无引号形式", DownloadName.fromDisposition("attachment; filename=a.txt"), "a.txt");
        eq("没有 filename", DownloadName.fromDisposition("attachment"), null);
        eq("空串", DownloadName.fromDisposition(""), null);
        eq("null", DownloadName.fromDisposition(null), null);

        System.out.println("=== 落盘文件名：不能带路径分隔符 ===");
        eq("中文名原样保留", DownloadName.sanitize("报告.docx"), "报告.docx");
        eq("路径穿越被压平", DownloadName.sanitize("../../etc/passwd"), ".._.._etc_passwd");
        eq("Windows 非法字符", DownloadName.sanitize("a:b*c?.txt"), "a_b_c_.txt");
        eq("null 兜底", DownloadName.sanitize(null), "download");
        eq("空白兜底", DownloadName.sanitize("   "), "download");

        System.out.println();
        System.out.println("共 " + total + " 条，失败 " + failed + " 条");
        if (failed > 0) {
            System.exit(1);
        }
    }
}
