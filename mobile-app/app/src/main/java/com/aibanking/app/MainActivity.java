package com.aibanking.app;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.os.Bundle;
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
import java.net.MalformedURLException;
import java.net.URL;

/**
 * AI Banking 手机 App 壳:只做一件事——把电脑上 :3000 的手机端页面装进 WebView。
 * 不打包前端:前端改动无需重新发 APK;数据联动靠页面自身与 8800 后端
 * (同源推导 API 地址 + x-bank-token 会话存 localStorage)。
 *
 * 首次启动出现配置屏,输入电脑地址后记住,之后直连;主帧加载失败回到配置屏。
 */
public class MainActivity extends Activity {

    private static final String PREFS = "cfg";
    private static final String KEY_SERVER = "server_url";

    private WebView webView;
    private View setupPanel;
    private EditText inputUrl;
    private TextView errorText;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        webView = findViewById(R.id.webview);
        setupPanel = findViewById(R.id.setup_panel);
        inputUrl = findViewById(R.id.input_url);
        errorText = findViewById(R.id.error_text);
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

        btnConnect.setOnClickListener(v -> connect());

        String saved = getSharedPreferences(PREFS, MODE_PRIVATE).getString(KEY_SERVER, null);
        if (saved != null) {
            inputUrl.setText(saved);
            hideSetup();
            webView.loadUrl(saved);
        } else {
            showSetup(null);
        }
    }

    private void connect() {
        String raw = inputUrl.getText().toString().trim();
        if (raw.isEmpty()) {
            inputUrl.setError("请输入电脑地址");
            return;
        }
        if (!raw.startsWith("http://") && !raw.startsWith("https://")) {
            raw = "http://" + raw;
        }
        // 容忍整段 URL(带路径/查询),只取 origin
        try {
            URL u = new URL(raw);
            int port = u.getPort();
            raw = u.getProtocol() + "://" + u.getHost() + (port == -1 ? "" : ":" + port);
        } catch (MalformedURLException ignored) {
            // 解析不了就按原样加载,WebView 会给出错误页
        }
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(KEY_SERVER, raw).apply();
        errorText.setVisibility(View.GONE);
        hideSetup();
        webView.loadUrl(raw);
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
