# CentaurAI 微信网页观察器（Firefox）

该扩展只观察 `https://wx.qq.com/` 中 `#chatArea` 当前真实渲染的消息。它不会自动
点击会话、滚动历史、拦截网络、读取 Cookie、注入微信协议或下载登录态附件。

## 本地试用

1. 启动 Pocket API，在“微信网页版观察器”来源页生成一次性配对码。
2. 安装 Native Host：

   ```bash
   ./tools/native-host/install-native-host.sh
   ```

3. Firefox 打开 `about:debugging#/runtime/this-firefox`，选择“临时载入附加组件”，
   加载本目录的 `manifest.json`。
4. 点击 Firefox 工具栏中的“CentaurAI 微信网页观察器”，输入来源 ID 和一次性
   配对码。配对码直接交给 Native Host，不写入扩展 storage。
5. 顶层标签页打开 `https://wx.qq.com/` 并由本人扫码、在手机确认登录。

普通 Firefox 正式长期安装要求 Mozilla 签名的 XPI；临时加载适合本机试点，重启
Firefox 后需要重新加载。扩展重新加载不会清除 Native Host 中已经换取的
collector token。

## 权限

manifest 只有：

- `nativeMessaging`：把经过约束的事件发送给本机 Host。
- `storage`：只保存非秘密的来源 ID 和本机 API 地址，方便重新打开弹窗。
- `https://wx.qq.com/*`：唯一允许运行 content script 的网页来源。

扩展没有 `cookies`、`webRequest`、`debugger`、浏览历史、下载或任意站点权限。

## 覆盖语义

- 只采集 `#chatArea` 内可见、带有效 `data-cm.msgId` 的消息节点。
- 明确跳过 `#prerender`、`display:none`、`visibility:hidden`、`aria-hidden` 和没有
  布局矩形的节点。
- 当前未打开的会话只汇报未读会话数量，不读取摘要或正文。
- 页面关闭、电脑休眠、未打开会话、登录失效及 DOM 结构变化都会产生覆盖缺口。
- 找不到可靠会话标识或名称时不提交消息，避免把内容归到错误会话。
- 网页观察结果是 `observed` 旁路证据，不是微信官方完整归档。

## 开发与测试

```bash
cd apps/wechat-observer-extension
npm run check
npm test
```

正式发布前需要把同一固定扩展 ID 的包提交 Mozilla 签名，并在 Firefox 与微信页面
真实版本上完成 DOM fixture 回归；不允许增加反检测或风控绕过逻辑。
