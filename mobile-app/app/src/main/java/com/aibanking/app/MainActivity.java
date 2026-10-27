package com.aibanking.app;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.text.TextUtils;
import android.view.View;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InterfaceAddress;
import java.net.MalformedURLException;
import java.net.NetworkInterface;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.util.Enumeration;
import java.util.HashSet;
import java.util.Set;

/**
 * AI Banking 手机 App 壳:把电脑上 :3000 的手机端页面装进 WebView。
 * 不打包前端:前端改动无需重新发 APK;数据联动靠页面自身与 8800 后端
 * (同源推导 API 地址 + x-bank-token 会话存 localStorage)。
 *
 * 电脑地址的三层获取方式(依次兜底):
 * 1. 自动发现:向局域网广播 UDP 探测,电脑上的 discover_responder 应答,
 *    免扫码免打字(路由器 AP 隔离会拦广播,此路不通走下一层);
 * 2. 扫码配对:电脑端弹窗的「配对码」是 aibanking://pair?server=… 深链,
 *    系统相机扫码后直接打开本 App 并写入地址;
 * 3. 手动输入:配置屏兜底。
 */
public class MainActivity extends Activity {

    private static final String PREFS = "cfg";
    private static final String KEY_SERVER = "server_url";

    /** 与 scripts/discover_responder.py 约定的发现协议 */
    private static final int DISCOVERY_PORT = 58713;
    private static final String DISCOVERY_MAGIC = "AIBANK_DISCOVER_V1";
    private static final String DISCOVERY_PREFIX = "AIBANK_V1|";

    private WebView webView;
    private View setupPanel;
    private EditText inputUrl;
    private TextView errorText;
    private TextView statusText;
    private volatile boolean discoveryRunning = false;
    private boolean deepLinkHandled = false;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        webView = findViewById(R.id.webview);
        setupPanel = findViewById(R.id.setup_panel);
        inputUrl = findViewById(R.id.input_url);
        errorText = findViewById(R.id.error_text);
        statusText = findViewById(R.id.status_text);
        Button btnConnect = findViewById(R.id.btn_connect);

        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        // localStorage 持久化 bank-token(7 天会话),装一次登录一次
        s.setDomStorageEnabled(true);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        // 忽略系统字体缩放,保证 375 布局不被放大撑破
        s.setTextZoom(100);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String host = request.getUrl().getHost();
                // 管理台 8789 只绑电脑回环,App 内不可达,直接拦下
                if ("127.0.0.1".equals(host) || "localhost".equals(host)) {
                    Toast.makeText(MainActivity.this, "后台入口仅电脑端可用",
                            Toast.LENGTH_SHORT).show();
                    return true;
                }
                return false;
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                if (request.isForMainFrame()) {
                    showSetup("连接失败(" + error.getErrorCode() + "):检查电脑端演示是否已启动、"
                            + "地址是否正确、手机与电脑是否同一 Wi-Fi");
                }
            }
        });
        webView.setWebChromeClient(new WebChromeClient());

        btnConnect.setOnClickListener(v -> {
            stopDiscovery();
            connect();
        });

        // 深链(aibanking://pair?server=…)优先;否则已配置直连,否则发现+配置屏
        if (!handleDeepLink(getIntent())) {
            String saved = getSharedPreferences(PREFS, MODE_PRIVATE)
                    .getString(KEY_SERVER, null);
            if (saved != null) {
                inputUrl.setText(saved);
                hideSetup();
                webView.loadUrl(saved);
            } else {
                showSetup(null);
                startDiscovery();
            }
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        // App 已在运行时系统相机扫码会走这里(singleTask)
        handleDeepLink(intent);
    }

    /** 解析 aibanking://pair?server=http://x:3000;命中返回 true 并直接连接。 */
    private boolean handleDeepLink(Intent intent) {
        if (intent == null || intent.getData() == null) {
            return false;
        }
        Uri data = intent.getData();
        if (!"aibanking".equals(data.getScheme()) || !"pair".equals(data.getHost())) {
            return false;
        }
        String server = data.getQueryParameter("server");
        if (TextUtils.isEmpty(server)) {
            Toast.makeText(this, "配对码里没有电脑地址", Toast.LENGTH_SHORT).show();
            return true;
        }
        deepLinkHandled = true;
        stopDiscovery();
        connectTo(normalizeOrigin(server));
        return true;
    }

    // ---- 自动发现:UDP 广播探测,电脑端 discover_responder 应答 ----

    private void startDiscovery() {
        if (discoveryRunning) {
            return;
        }
        discoveryRunning = true;
        statusText.setText("正在搜索同一 Wi-Fi 下的电脑…");
        statusText.setVisibility(View.VISIBLE);
        new Thread(() -> {
            String found = null;
            DatagramSocket socket = null;
            try {
                Set<InetAddress> targets = new HashSet<>();
                targets.add(InetAddress.getByName("255.255.255.255"));
                Enumeration<NetworkInterface> nis = NetworkInterface.getNetworkInterfaces();
                while (nis != null && nis.hasMoreElements()) {
                    NetworkInterface ni = nis.nextElement();
                    if (!ni.isUp() || ni.isLoopback()) {
                        continue;
                    }
                    for (InterfaceAddress ia : ni.getInterfaceAddresses()) {
                        InetAddress bc = ia.getBroadcast();
                        if (bc != null) {
                            targets.add(bc);
                        }
                    }
                }
                socket = new DatagramSocket();
                socket.setSoTimeout(900);
                byte[] req = DISCOVERY_MAGIC.getBytes("UTF-8");
                long deadline = System.currentTimeMillis() + 6000;
                while (System.currentTimeMillis() < deadline && found == null) {
                    for (InetAddress target : targets) {
                        try {
                            socket.send(new DatagramPacket(req, req.length, target,
                                    DISCOVERY_PORT));
                        } catch (Exception ignored) {
                            // 单个广播地址失败(如无路由)不影响其它地址
                        }
                    }
                    byte[] buf = new byte[256];
                    DatagramPacket pkt = new DatagramPacket(buf, buf.length);
                    try {
                        socket.receive(pkt);
                        String msg = new String(pkt.getData(), 0, pkt.getLength(),
                                "UTF-8");
                        if (msg.startsWith(DISCOVERY_PREFIX)) {
                            found = msg.substring(DISCOVERY_PREFIX.length()).trim();
                        }
                    } catch (SocketTimeoutException ignored) {
                        // 一轮没人应答,继续发下一轮
                    }
                }
            } catch (Exception ignored) {
                // 发现失败不致命,落到手动/扫码兜底
            } finally {
                if (socket != null) {
                    socket.close();
                }
            }
            final String server = found;
            runOnUiThread(() -> {
                if (isFinishing() || isDestroyed() || deepLinkHandled) {
                    return;
                }
                discoveryRunning = false;
                if (setupPanel.getVisibility() != View.VISIBLE) {
                    return; // 用户已手动连上或深链已处理
                }
                if (server == null) {
                    statusText.setText("没自动发现电脑(路由器可能开了 AP 隔离)。"
                            + "可直接输入地址,或在电脑端弹窗用系统相机扫「配对码」。");
                    return;
                }
                if (TextUtils.isEmpty(inputUrl.getText())) {
                    statusText.setText("已发现电脑,正在连接…");
                    connectTo(normalizeOrigin(server));
                } else {
                    statusText.setText("已发现 " + server + ",点「连接」即可");
                }
            });
        }, "aibank-discover").start();
    }

    private void stopDiscovery() {
        discoveryRunning = false; // 线程发现标志变化后在 UI 回调里自行退出
    }

    // ---- 连接 ----

    private void connect() {
        String raw = inputUrl.getText().toString().trim();
        if (raw.isEmpty()) {
            inputUrl.setError("请输入电脑地址");
            return;
        }
        connectTo(normalizeOrigin(raw));
    }

    private void connectTo(String origin) {
        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                .putString(KEY_SERVER, origin).apply();
        errorText.setVisibility(View.GONE);
        statusText.setVisibility(View.GONE);
        hideSetup();
        webView.loadUrl(origin);
    }

    /** 容忍整段 URL(带路径/查询),只取 origin;补协议。 */
    private String normalizeOrigin(String raw) {
        if (!raw.startsWith("http://") && !raw.startsWith("https://")) {
            raw = "http://" + raw;
        }
        try {
            URL u = new URL(raw);
            int port = u.getPort();
            return u.getProtocol() + "://" + u.getHost()
                    + (port == -1 ? "" : ":" + port);
        } catch (MalformedURLException ignored) {
            return raw; // 解析不了就按原样加载,WebView 会给出错误页
        }
    }

    private void showSetup(String error) {
        setupPanel.setVisibility(View.VISIBLE);
        if (error != null) {
            errorText.setText(error);
            errorText.setVisibility(View.VISIBLE);
        }
    }

    private void hideSetup() {
        setupPanel.setVisibility(View.GONE);
    }

    @Override
    public void onBackPressed() {
        if (setupPanel.getVisibility() != View.VISIBLE && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
