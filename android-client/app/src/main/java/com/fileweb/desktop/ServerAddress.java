package com.fileweb.desktop;

import java.net.URI;
import java.net.URISyntaxException;

/**
 * 服务器地址的规范化 —— **纯逻辑，不依赖任何 Android API**。
 * ============================================================================
 * 为什么要单独一个类
 * ------------------
 * 这里最容易出错，而且一出错就是「**App 根本连不上**」这种致命症状；
 * 偏偏它又完全是一段纯字符串处理。把它从 MainActivity 里拆出来（不引用
 * android.* ），就能**在电脑上用 JDK 直接跑用例**验证，不必先装到平板上试。
 *
 * （网页工程里 phototime.js 是同一个理由：把纯逻辑拆出来，测的是与线上
 *   同一份实现，而不是在测试里重写一遍。）
 *
 * ★ 刻意用 java.net.URI 而不是 android.net.Uri：后者要在安卓上才能跑，
 *   前者是标准 Java，两端行为一致，于是这段逻辑可测。
 *   代价是 IPv6 的处理：java.net.URI 的 getHost() 对 IPv6 会**带方括号**返回，
 *   而 android.net.Uri 不带 —— 这里统一先剥掉、再按需要加回去，两种情形结果相同。
 */
public final class ServerAddress {

    /** 只填 IP 时补上的默认端口，与本服务的默认端口一致 */
    public static final String DEFAULT_PORT = "8000";

    private ServerAddress() {
    }

    /**
     * 把用户敲的东西补成完整 URL；不合法返回 null。
     *
     * 常见输入是 `192.168.1.100` 或 `192.168.1.100:8000` ——
     * 不该逼用户把 scheme 与端口敲全，所以自动补 `http://` 与 `:8000`。
     */
    public static String normalize(String raw) {
        if (raw == null) {
            return null;
        }
        String text = raw.trim();
        if (text.isEmpty()) {
            return null;
        }
        if (!text.matches("(?i)^https?://.*")) {
            text = "http://" + text;
        }

        URI uri;
        try {
            uri = new URI(text);
        } catch (URISyntaxException e) {
            // 有空格、非法字符等等：直接判定填得不对，而不是猜
            return null;
        }

        String host = uri.getHost();
        if (host == null || host.isEmpty()) {
            // 端口写成非数字（如 192.168.1.5:abc）时 getHost() 也会是 null ——
            // 那确实该让用户改，而不是当成主机名用
            return null;
        }
        if (host.startsWith("[") && host.endsWith("]")) {
            host = host.substring(1, host.length() - 1);
        }
        if (host.isEmpty()) {
            return null;
        }

        String scheme = uri.getScheme() == null ? "http" : uri.getScheme().toLowerCase();
        int port = uri.getPort();
        String path = uri.getRawPath();

        StringBuilder builder = new StringBuilder();
        builder.append(scheme).append("://");
        builder.append(host.contains(":") ? ("[" + host + "]") : host);
        builder.append(':').append(port > 0 ? String.valueOf(port) : DEFAULT_PORT);

        // 路径照原样带上（正常部署是 "/"）。★ 刻意**不**强行补尾斜杠：
        // 用户若粘的是 .../index.html，补成 /index.html/ 会直接 404。
        if (path == null || path.isEmpty() || "/".equals(path)) {
            builder.append('/');
        } else {
            builder.append(path);
        }
        return builder.toString();
    }

    /** 地址框里显示成 `192.168.1.100:8000`，比带 scheme 的好敲也好改 */
    public static String stripScheme(String url) {
        if (url == null) {
            return "";
        }
        String text = url.trim();
        if (text.startsWith("http://")) {
            text = text.substring(7);
        } else if (text.startsWith("https://")) {
            text = text.substring(8);
        }
        while (text.endsWith("/")) {
            text = text.substring(0, text.length() - 1);
        }
        return text;
    }
}
