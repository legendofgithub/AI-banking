# 手机 App 壳(Android)

一个零依赖的 WebView 壳:打开后自动连接电脑上 `http://<电脑IP>:3000` 的手机端页面。
不打包前端代码——前端改动**不需要**重新发 APK;数据与 PC 端 agent 联动
靠页面自身与 8800 后端(同源推导 API 地址、`x-bank-token` 会话存
localStorage、`/api/sync/*` 双向轮询),App 层零逻辑参与。

## 电脑地址怎么来(三层递进)

1. **自动发现**(默认):App 打开向局域网广播 UDP 包,电脑上的
   `scripts/discover_responder.py`(由启动器托管)回自己的地址,免扫码免打字。
   依赖:入站 UDP 58713 防火墙规则(名称「AI Banking Mobile Discover 58713」,
   限本地子网;换机演示需重建一次,需管理员权限)。
2. **扫码配对**:路由器开了 AP 隔离时广播会被拦。电脑端「手机 App」弹窗有
   「配对码」(`aibanking://pair?server=…`),手机系统相机扫码 → 深链拉起
   App 并自动写入地址。
3. **手动输入**:配置屏兜底,地址会记住。

## 结构

```
mobile-app/
  app/src/main/
    AndroidManifest.xml      INTERNET + 明文流量 + aibanking://pair 深链(singleTask)
    java/.../MainActivity.java  发现/深链/手动三层取地址 + WebView(返回键=网页后退)
    res/                     布局/主题/图标(mipmap 自适应)
  build.gradle / settings.gradle   AGP 8.5.2, Gradle 8.9, minSdk 24 / target 35
scripts/discover_responder.py     UDP 58713 应答器(纯 stdlib)
.github/workflows/build-android-app.yml   云端构建(APK 产物)
```

## 构建与分发(GitHub Actions,本机零安装)

1. push 仓库后,Actions 里跑 **build-android-app**(改 `mobile-app/**` 或工作流文件会自动触发,也可手动 Run workflow)。
2. 构建完自动发布到固定 tag「app」的 Release,**公网扫码直链**(永远指向最新构建,发新版不用换二维码):
   `https://github.com/legendofgithub/AI-banking/releases/latest/download/bankagent.apk`
   对外分发、打印二维码都用这个地址——不依赖任何人的本机;下载页主按钮也指向它。
3. App 内自更新与局域网兜底仍用本地副本:从 Release 资产(或 run 的 Artifacts)取
   `bankagent.apk` 与 `version.json`,放入 `webui/public/app/`(该目录已 gitignore,不入库)。
4. `curl -I http://127.0.0.1:3000/app/bankagent.apk` 确认 200 且
   Content-Type 为 `application/vnd.android.package-archive`(放入新文件后必须重启演示栈)。

## 演示前检查

- 电脑与手机同一 Wi-Fi;电脑防火墙已放行 TCP 3000/8800 与 **UDP 58713**。
- **建议给电脑设静态 IP**(演示 Wi-Fi 的 DHCP 会换地址);换 IP 后重启一次演示栈
  (应答器随启动器刷新地址),App 里旧地址连不上时删掉重装或在配置屏改。
- 装过 App 的手机覆盖安装即可,配置与登录态保留。
- App 内语音输入不可用(WebView 不支持 webkitSpeechRecognition),文本输入是主路径。
- 首次安装:任意网络扫码(公网 Release 直链)→ APK 下载安装(Play Protect
  选「仍然安装」)→ 打开 App 后连接**与电脑同一 Wi-Fi**,自动发现电脑 → 登录页一键填入演示账号。

## 登录态说明

token 有效期 7 天(`agent/auth.py` SESSION_DAYS),存在 WebView 的
localStorage;过期或清除 App 数据后重新用演示账号一键登录即可。
