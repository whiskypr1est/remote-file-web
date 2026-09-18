package com.fileweb.desktop;

import java.net.URLDecoder;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 下载文件名的解析与清洗 —— **纯逻辑，不依赖任何 Android API**。
 * ============================================================================
 * 与 ServerAddress 同样拆出来，是为了能在电脑上用 JDK 直接跑用例。
 * 这两件事出错的症状都不明显（文件名变成乱码、或者下载直接失败），
 * 而且都跟中文有关，正是最容易漏测的地方。
 */
public final class DownloadName {

    private static final Pattern STAR = Pattern.compile(
        "filename\\*\\s*=\\s*UTF-8''([^;]+)", Pattern.CASE_INSENSITIVE);
    private static final Pattern QUOTED = Pattern.compile(
        "filename\\s*=\\s*\"([^\"]+)\"", Pattern.CASE_INSENSITIVE);
    private static final Pattern PLAIN = Pattern.compile(
        "filename\\s*=\\s*([^;]+)", Pattern.CASE_INSENSITIVE);

    /** Windows 与 POSIX 都不接受的文件名字符，统一换成下划线 */
    private static final char[] BAD_CHARS = {'\\', '/', ':', '*', '?', '"', '<', '>', '|'};

    private DownloadName() {
    }

    /**
     * 从 Content-Disposition 里取文件名。
     *
     * ★ 必须优先认 RFC 5987 的 `filename*=UTF-8''%E4%B8%AD%E6%96%87.jpg`：
     *   HTTP 头只能是 Latin-1，所以本服务对**中文文件名**正是走这个形式
     *   （见 fileweb/http_utils.py 的 content_disposition）。
     *   只读 filename="..." 的话，中文名会落到那个 ASCII 兜底名上。
     */
    public static String fromDisposition(String disposition) {
        if (disposition == null || disposition.isEmpty()) {
            return null;
        }
        Matcher star = STAR.matcher(disposition);
        if (star.find()) {
            try {
                return URLDecoder.decode(star.group(1).trim(), "UTF-8");
            } catch (Exception ignored) {
                // 解不出来就往后退到普通 filename
            }
        }
        Matcher quoted = QUOTED.matcher(disposition);
        if (quoted.find()) {
            return quoted.group(1).trim();
        }
        Matcher plain = PLAIN.matcher(disposition);
        if (plain.find()) {
            return plain.group(1).trim().replace("\"", "");
        }
        return null;
    }

    /**
     * 落盘用的文件名。
     *
     * 去掉路径分隔符是必须的：DownloadManager 拿到带路径的名字会直接拒绝，
     * 而 Content-Disposition 是服务端给的（理论上可信，但下载落盘这一步
     * 不该依赖上游「不会出错」）。
     */
    public static String sanitize(String name) {
        String result = (name == null || name.trim().isEmpty()) ? "download" : name.trim();
        for (char bad : BAD_CHARS) {
            result = result.replace(bad, '_');
        }
        result = result.trim();
        return result.isEmpty() ? "download" : result;
    }
}
