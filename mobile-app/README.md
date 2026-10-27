# 手机 App 壳(Android)

一个零依赖的 WebView 壳:打开后加载电脑上 `http://<电脑IP>:3000` 的手机端页面。
不打包前端代码——前端改动**不需要**重新发 APK;数据与 PC 端 agent 联动
靠页面自身与 8800 后端(同源推导 API 地址、`x-bank-token` 会话存
localStorage、`/api/sync/*` 双向轮询),App 层零逻辑参与。

## 结构

```
mobile-app/
  app/src/main/
    AndroidManifest.xml      INTERNET + usesCleartextTraffic(局域网 HTTP)
    java/.../MainActivity.java  配置屏 + WebView(返回键=网页后退)
    res/                     布局/主题/图标(复用 webui icon-512)
  build.gradle / settings.gradle   AGP 8.5.2, Gradle 8.9, minSdk 24 / target 35
.github/workflows/build-android-app.yml   云端构建(APK 产物)
```

## 构建(GitHub Actions,本机零安装)

1. push 仓库后,Actions 里跑 **build-android-app**(改 `mobile-app/**` 会自动触发,也可手动 Run workflow)。
2. 构建完在该 run 的 Artifacts 下载 `bankagent-apk`,或本机:
   `gh run download -n bankagent-apk -D /tmp/apk`
3. 把 `app-debug.apk` 复制为 `webui/public/app/bankagent.apk`(该路径已 gitignore,不入库)。
4. `curl -I http://127.0.0.1:3000/app/bankagent.apk` 确认 200 且
   Content-Type 为 `application/vnd.android.package-archive`。

## 演示前检查

- 电脑与手机同一 Wi-Fi;电脑防火墙已放行 3000/8800(规则「AI Banking Mobile Demo」已存在)。
- **建议给电脑设静态 IP**(演示 Wi-Fi 的 DHCP 会换地址);IP 变了只需在 App 配置屏改地址,App 内登录态不受影响。
- 换新 APK 时手机直接覆盖安装即可,配置与登录态保留。
- App 内语音输入不可用(WebView 不支持 webkitSpeechRecognition),文本输入是主路径。
- 首次安装:浏览器扫码 → `/download` 页下载 APK → 允许「安装未知应用」→
  Play Protect 提示时选「仍然安装」→ 打开 App 填电脑 IP → 登录页一键填入演示账号。

## 登录态说明

token 有效期 7 天(`agent/auth.py` SESSION_DAYS),存在 WebView 的
localStorage;过期或清除 App 数据后重新用演示账号一键登录即可。
