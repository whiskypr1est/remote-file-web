package com.fileweb.desktop;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.Toast;

/**
 * 远程桌面 · 安卓平板办公客户端（WebView 壳）
 * ============================================================================
 * 这个 App 本身不实现任何业务：它只做一件事 —— 用 WebView 加载局域网里那台
 * 服务器上的「远程文件管理」网页，并补上网页在浏览器之外拿不到的那几样能力。
 *
 * 为什么是壳、而不是原生重写
 * --------------------------
 * 网页那一套界面本来就是照「鼠标 + 键盘」写的（窗口、任务栏、右键菜单、
 * 双击进入、Ctrl+C/V），而办公场景正好是**平板 + 蓝牙鼠标键盘** ——
 * 也就是说它已经是**对的形式**，不需要重做 UI。原生重写等于再写一个前端，
 * 而且要在两个地方各维护一份。
 *
 * 壳必须自己实现的东西（不写就会静默失败）
 * ----------------------------------------
 *   1. onShowFileChooser —— 网页里的 <input type=file>（照片上传、「选择照片…」）
 *      在 WebView 里**默认点了毫无反应**，必须接上系统的文件选择器；
 *   2. DownloadListener —— 网页里的「下载」在 WebView 里**默认也是毫无反应**。
 *      ★ 而且不能简单地交给系统：下载请求需要带着**会话 Cookie**，
 *        否则服务端会返回 401。这里用 CookieManager 取出 Cookie 后
 *        作为请求头交给 DownloadManager；
 *   3. 明文 HTTP 许可（清单里的 usesCleartextTraffic）—— Android 9+ 默认禁止，
 *      不开的话 http:// 局域网地址根本连不上；
 *   4. 服务器地址配置（首次运行 + 连不上时），并用 SharedPreferences 记住。
 *
 * 反过来说，**网页工程一行都不用改**。唯一一处「配合」是「返回键先关最前面的
 * 窗口」——那也是壳这边注入的一小段 JS，没有落到网页代码里。
 */
public class MainActivity extends Activity {

    /* ---- 偏好 ---- */
    private static final String PREFS = "remote_desktop";
    private static final String KEY_URL = "server_url";
    private static final String KEY_KEEP_AWAKE = "keep_awake";

    private static final int REQ_FILE_CHOOSER = 1001;
    private static final int REQ_STORAGE = 1002;

    private WebView webView;
    private ValueCallback<Uri[]> fileCallback;

    /** 返回键是不是「长按」过（长按 = 打开服务器地址对话框） */
    private boolean backLongPressed = false;

    /* ======================================================================
       生命周期
       ====================================================================== */

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        applyWindowFlags();
        buildWebView();

        String url = prefs().getString(KEY_URL, "");
        if (url == null || url.trim().isEmpty()) {
            // 第一次运行：先问地址
            showServerDialog(true);
        } else {
            webView.loadUrl(url);
            toast(getString(R.string.tip_back));
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        // 网页里有轮询（任务管理器、后台任务面板）。切到后台就把它停掉，
        // 不然放桌上一天会白白耗电。
        if (webView != null) {
            webView.onPause();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (webView != null) {
            webView.onResume();
        }
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }

    /* ======================================================================
       窗口：全屏 + 保持常亮
       ====================================================================== */

    private void applyWindowFlags() {
        Window window = getWindow();

        // 全屏：把状态栏隐掉，桌面就多出一条。
        // ★ 时间/电量不会因此看不到 —— 网页的任务栏托盘里本来就有实时时钟与日期。
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            window.setDecorFitsSystemWindows(false);
            WindowInsetsController controller = window.getInsetsController();
            if (controller != null) {
                controller.hide(WindowInsets.Type.statusBars());
                controller.setSystemBarsBehavior(
                    WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
        } else {
            window.setFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN,
                            WindowManager.LayoutParams.FLAG_FULLSCREEN);
        }

        // 办公时保持常亮（可在地址对话框里关掉）
        if (prefs().getBoolean(KEY_KEEP_AWAKE, true)) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }
    }

    /* ======================================================================
       WebView
       ====================================================================== */

    private void buildWebView() {
        FrameLayout root = new FrameLayout(this);
        root.setLayoutParams(new ViewGroup.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        webView = new WebView(this);
        root.addView(webView, new FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);          // 整个应用都是 JS 驱动的
        settings.setDomStorageEnabled(true);          // localStorage / sessionStorage
        settings.setDatabaseEnabled(true);
        settings.setUseWideViewPort(true);            // 认网页里的 viewport meta
        settings.setLoadWithOverviewMode(false);
        // 缩放：允许（Ctrl+滚轮 / 双指），但不要那套会一直浮在屏幕上的 +/- 按钮
        settings.setSupportZoom(true);
        settings.setBuiltInZoomControls(true);
        settings.setDisplayZoomControls(false);
        // 音视频预览：不要让「必须先有用户手势」把播放器拦住
        settings.setMediaPlaybackRequiresUserGesture(false);
        // 安全：这个应用不需要从本地文件读东西
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true);         // 文件选择器回的是 content://

        // 会话 Cookie 归 WebView 自己管，默认就开着；这里显式写出来是为了
        // 「登录状态能保持」这件事在代码里看得见。
        CookieManager.getInstance().setAcceptCookie(true);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                // 只关心主文档失败（子资源 404 不该弹窗）
                if (request != null && request.isForMainFrame()) {
                    showConnectError(error != null ? String.valueOf(error.getDescription()) : "");
                }
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            /**
             * ★ 没有这一段，网页里所有 <input type=file> 都是**点了没反应** ——
             *   照片应用的「从我的电脑上传 / 选择照片…」以及资源管理器的
             *   「上传文件」全都走这条路。
             */
            @Override
            public boolean onShowFileChooser(WebView view,
                                             ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (fileCallback != null) {
                    fileCallback.onReceiveValue(null);
                }
                fileCallback = callback;

                Intent intent;
                try {
                    // createIntent 会把网页上的 accept 类型与 multiple 带过来
                    intent = params.createIntent();
                } catch (Exception e) {
                    intent = new Intent(Intent.ACTION_GET_CONTENT);
                    intent.setType("*/*");
                }
                intent.addCategory(Intent.CATEGORY_OPENABLE);

                try {
                    startActivityForResult(intent, REQ_FILE_CHOOSER);
                } catch (ActivityNotFoundException e) {
                    fileCallback = null;
                    toast(getString(R.string.err_no_picker));
                    return false;
                }
                return true;
            }
        });

        /**
         * ★ 下载：网页里的「下载 / 打包下载」在 WebView 里默认什么都不做。
         *
         *   关键在 Cookie —— 这个服务的下载接口都要登录，而 DownloadManager
         *   跑在另一个进程里、**拿不到 WebView 的会话 Cookie**，
         *   不手动带上就会 401。所以这里从 CookieManager 取出来塞进请求头。
         */
        webView.setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) ->
            handleDownload(url, userAgent, contentDisposition, mimeType));
    }

    /* ======================================================================
       下载
       ====================================================================== */

    private void handleDownload(String url, String userAgent,
                                String contentDisposition, String mimeType) {
        // Android 9 及以下要写公共「下载」目录得有权限；10+ 由系统代写，不需要。
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q && !canWritePublicStorage()) {
            requestPermissions(
                new String[]{android.Manifest.permission.WRITE_EXTERNAL_STORAGE}, REQ_STORAGE);
            toast("请允许存储权限，然后再点一次下载");
            return;
        }

        try {
            String fileName = DownloadName.sanitize(DownloadName.fromDisposition(contentDisposition));
            String cookie = CookieManager.getInstance().getCookie(url);

            DownloadManager.Request request = new DownloadManager.Request(Uri.parse(url));
            if (mimeType != null && !mimeType.isEmpty()) {
                request.setMimeType(mimeType);
            }
            if (cookie != null && !cookie.isEmpty()) {
                request.addRequestHeader("Cookie", cookie);
            }
            if (userAgent != null && !userAgent.isEmpty()) {
                request.addRequestHeader("User-Agent", userAgent);
            }
            request.setTitle(fileName);
            request.setNotificationVisibility(
                DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            request.setDestinationInExternalPublicDir(
                Environment.DIRECTORY_DOWNLOADS, fileName);

            DownloadManager manager = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
            if (manager == null) {
                toast(getString(R.string.msg_download_failed, "系统下载服务不可用"));
                return;
            }
            manager.enqueue(request);
            toast(getString(R.string.msg_downloading));
        } catch (Exception e) {
            toast(getString(R.string.msg_download_failed, String.valueOf(e.getMessage())));
        }
    }

    private boolean canWritePublicStorage() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            return true;
        }
        return checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
            == PackageManager.PERMISSION_GRANTED;
    }

    /* ======================================================================
       服务器地址
       ====================================================================== */

    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private void showServerDialog(boolean firstRun) {
        View view = getLayoutInflater().inflate(R.layout.dialog_server, null);
        final EditText input = view.findViewById(R.id.server_input);
        final CheckBox keepAwake = view.findViewById(R.id.keep_awake);

        String current = prefs().getString(KEY_URL, "");
        input.setText(ServerAddress.stripScheme(current));
        keepAwake.setChecked(prefs().getBoolean(KEY_KEEP_AWAKE, true));

        AlertDialog.Builder builder = new AlertDialog.Builder(this)
            .setTitle(firstRun ? R.string.dialog_title_first : R.string.dialog_title)
            .setView(view)
            .setPositiveButton(R.string.action_connect, null);   // 真正的处理在 onShow 里

        if (!firstRun) {
            builder.setNeutralButton(R.string.action_reload, (d, w) -> {
                if (webView != null && webView.getUrl() != null) {
                    webView.reload();
                }
            });
            builder.setNegativeButton(R.string.action_cancel, null);
        }

        final AlertDialog dialog = builder.create();
        // 首次运行时不允许点外面关掉（否则会停在一个空白页面手足无措）
        dialog.setCancelable(!firstRun);

        dialog.setOnShowListener(d -> dialog.getButton(AlertDialog.BUTTON_POSITIVE)
            .setOnClickListener(v -> {
                String url = ServerAddress.normalize(input.getText().toString());
                if (url == null) {
                    // 地址不合法时**不关对话框**，让人直接改
                    toast(getString(R.string.err_bad_address));
                    return;
                }
                prefs().edit()
                    .putString(KEY_URL, url)
                    .putBoolean(KEY_KEEP_AWAKE, keepAwake.isChecked())
                    .apply();
                applyWindowFlags();
                if (webView != null) {
                    webView.loadUrl(url);
                }
                dialog.dismiss();
            }));

        dialog.show();
        input.requestFocus();
    }

    /* ======================================================================
       连不上时的提示
       ====================================================================== */

    private void showConnectError(String reason) {
        if (isFinishing()) {
            return;
        }
        String url = prefs().getString(KEY_URL, "");
        AlertDialog dialog = new AlertDialog.Builder(this)
            .setTitle(R.string.err_connect)
            .setMessage(getString(R.string.err_connect_hint, url)
                + (reason == null || reason.isEmpty() ? "" : "\n\n(" + reason + ")"))
            .setPositiveButton(R.string.action_retry, (d, w) -> {
                if (webView != null) {
                    webView.reload();
                }
            })
            .setNeutralButton(R.string.action_change, (d, w) -> showServerDialog(false))
            .create();
        dialog.setOnCancelListener(d -> toast(getString(R.string.tip_back)));
        dialog.show();
    }

    /* ======================================================================
       返回键：先关窗口，再退出
       ====================================================================== */

    /**
     * 找桌面上**最前面**的那个窗口并关掉。
     *
     * ★ 这是壳这边注入的一小段 JS，**网页工程一行都不用改**。
     *   winbox 用行内样式的 z-index 表达前后关系（网页自己的
     *   sessionstate.js 里也是这么读的，见那里的 zIndexOf）。
     */
    private static final String CLOSE_TOP_WINDOW_JS =
        "(function(){try{"
        + "var list=document.querySelectorAll('.winbox');"
        + "if(!list.length)return false;"
        + "var top=null,best=-1;"
        + "for(var i=0;i<list.length;i++){"
        + "var m=/z-index:\\s*(-?\\d+)/.exec(list[i].getAttribute('style')||'');"
        + "var z=m?parseInt(m[1],10):0;"
        + "if(z>=best){best=z;top=list[i];}}"
        + "if(!top)return false;"
        + "var btn=top.querySelector('.wb-close');"
        + "if(!btn)return false;"
        + "btn.click();return true;"
        + "}catch(e){return false;}})()";

    private void handleBack() {
        if (webView == null) {
            finish();
            return;
        }
        // 1) 网页自己还有历史（登录页、深链接）就先回退
        if (webView.canGoBack()) {
            webView.goBack();
            return;
        }
        // 2) 桌面上开着窗口就先关掉最前面那个（与 Windows 的习惯一致）
        webView.evaluateJavascript(CLOSE_TOP_WINDOW_JS, value -> {
            if (!"true".equals(value)) {
                finish();   // 3) 什么都没有了才退出 App
            }
        });
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            if (event.getRepeatCount() == 0) {
                backLongPressed = false;
                event.startTracking();     // 不 startTracking 就收不到 onKeyLongPress
            }
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    public boolean onKeyLongPress(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            // 长按返回 = 改服务器地址（不占界面空间，也不用加菜单）
            backLongPressed = true;
            showServerDialog(false);
            return true;
        }
        return super.onKeyLongPress(keyCode, event);
    }

    @Override
    public boolean onKeyUp(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            if (!backLongPressed) {
                handleBack();
            }
            backLongPressed = false;
            return true;
        }
        return super.onKeyUp(keyCode, event);
    }

    /* ======================================================================
       文件选择器的回执
       ====================================================================== */

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQ_FILE_CHOOSER) {
            if (fileCallback == null) {
                super.onActivityResult(requestCode, resultCode, data);
                return;
            }
            Uri[] results = null;
            if (resultCode == RESULT_OK && data != null) {
                if (data.getClipData() != null) {
                    // 多选（网页里的 multiple）
                    int count = data.getClipData().getItemCount();
                    results = new Uri[count];
                    for (int i = 0; i < count; i++) {
                        results[i] = data.getClipData().getItemAt(i).getUri();
                    }
                } else if (data.getData() != null) {
                    results = new Uri[]{data.getData()};
                }
            }
            // ★ 无论成功、取消还是没选，都**必须**回调一次，
            //   否则网页那边的 <input type=file> 会一直卡在「正在选择」状态，
            //   之后再点选择就没反应了。
            fileCallback.onReceiveValue(results);
            fileCallback = null;
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    /* ======================================================================
       小工具
       ====================================================================== */

    private void toast(String text) {
        Toast.makeText(this, text, Toast.LENGTH_SHORT).show();
    }
}
