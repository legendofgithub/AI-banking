/* ============================================================
   AI 银行助手 · 移动端高保真原型 —— 页面定义
   375×812 单列画板;页面顺序与「提示词.md」第三节一致
   ============================================================ */
(function () {
  'use strict';

  /* ------------------------- 图标 ------------------------- */
  const I = {
    ham: '<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 5h14M3 10h14M3 15h14"/></svg>',
    back: '<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12.5 4.5 7 10l5.5 5.5"/></svg>',
    send: '<svg width="19" height="19" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10 17 3.5 11.5 17l-2.6-5.2L3 10Z"/></svg>',
    plus: '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10 4v12M4 10h12"/></svg>',
    phone: '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="5.5" y="2" width="9" height="16" rx="2.4"/><path d="M9 16.2h2" stroke-linecap="round"/></svg>',
    lock: '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3.8" y="8.5" width="12.4" height="9" rx="2.2"/><path d="M6.8 8.5V6.6a3.2 3.2 0 0 1 6.4 0v1.9"/></svg>',
    idcard: '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="2.5" y="4.5" width="15" height="11" rx="2.2"/><circle cx="7.4" cy="9.6" r="1.7"/><path d="M4.8 13.4c.5-1.2 1.5-1.8 2.6-1.8s2.1.6 2.6 1.8M13 8.4h3M13 11.2h3" stroke-linecap="round"/></svg>',
    user: '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="10" cy="7" r="3.2"/><path d="M4 17c.9-2.7 3.2-4.2 6-4.2s5.1 1.5 6 4.2" stroke-linecap="round"/></svg>',
    mail: '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="2.5" y="4.5" width="15" height="11" rx="2.2"/><path d="m3.4 6 6.6 5 6.6-5" stroke-linecap="round"/></svg>',
    shield: '<svg width="34" height="34" viewBox="0 0 32 32" fill="none"><path d="M16 3.2 27 7v9.2c0 6-4.6 10.7-11 12.6C9.6 26.9 5 22.2 5 16.2V7l11-3.8Z" fill="#C8A45C" fill-opacity=".22" stroke="#C8A45C" stroke-width="1.6"/><path d="m11.6 15.8 3 3 5.8-6" stroke="#C8A45C" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    shieldWhite: '<svg width="36" height="36" viewBox="0 0 32 32" fill="none"><path d="M16 3.2 27 7v9.2c0 6-4.6 10.7-11 12.6C9.6 26.9 5 22.2 5 16.2V7l11-3.8Z" fill="#fff" fill-opacity=".18" stroke="#fff" stroke-width="1.6"/><path d="m11.6 15.8 3 3 5.8-6" stroke="#C8A45C" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    warn: '<svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M10 3.2 18 16.8H2L10 3.2Z" stroke-linejoin="round"/><path d="M10 8.4v3.4M10 14.2h.01"/></svg>',
    check: '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.6 4.4 4.4L19 7.4"/></svg>',
    checkSm: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.6 4.4 4.4L19 7.4"/></svg>',
    close: '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="m5.5 5.5 9 9M14.5 5.5l-9 9"/></svg>',
    clock: '<svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="10" cy="10" r="7.2"/><path d="M10 6v4.4l3 1.8" stroke-linecap="round"/></svg>',
    cash: '<svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="2.4" y="5.4" width="15.2" height="9.2" rx="2"/><circle cx="10" cy="10" r="2.2"/></svg>',
    chart: '<svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 16V9M10 16V4M16 16v-5"/></svg>',
    card: '<svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="2.4" y="4.6" width="15.2" height="10.8" rx="2.2"/><path d="M2.4 8.6h15.2" stroke-linecap="round"/></svg>',
    lockSm: '<svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="4" y="8.8" width="12" height="8.6" rx="2"/><path d="M7 8.8V7a3 3 0 0 1 6 0v1.8"/></svg>',
    arrowRight: '<svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7.5 4.5 13 10l-5.5 5.5"/></svg>',
    bolt: '<svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor"><path d="M11.6 2 4.5 11.2h4.2L8 18l7.4-9.4h-4.3L11.6 2Z"/></svg>'
  };

  /* ---------------------- 基础片段 ---------------------- */
  const sb = (dark) => `
    <div class="statusbar${dark ? ' on-dark' : ''}">
      <span>9:41</span>
      <span class="sb-right">
        <svg width="17" height="11" viewBox="0 0 17 11" fill="currentColor" aria-hidden="true"><rect x="0" y="7" width="3" height="4" rx="1"/><rect x="4.6" y="5" width="3" height="6" rx="1"/><rect x="9.2" y="2.6" width="3" height="8.4" rx="1"/><rect x="13.8" y="0" width="3" height="11" rx="1"/></svg>
        <svg width="16" height="11" viewBox="0 0 16 11" fill="currentColor" aria-hidden="true"><path d="M8 9.6l1.6-1.7a2.3 2.3 0 0 0-3.2 0L8 9.6Zm0-3.9c1.3 0 2.5.5 3.4 1.3l1.1-1.2A6.9 6.9 0 0 0 8 3.8a6.9 6.9 0 0 0-4.5 1.9l1.1 1.2A4.9 4.9 0 0 1 8 5.7Zm0-3.5c2.2 0 4.2.8 5.7 2.1l1.1-1.2A10.9 10.9 0 0 0 8 .2C5.1.2 2.5 1.3.6 3.1l1.1 1.2A9 9 0 0 1 8 2.2Z"/></svg>
        <span class="sb-batt"></span>
      </span>
    </div>`;

  const nav = (o) => {
    o = o || {};
    const left = o.left === 'none' ? '<span style="width:44px"></span>'
      : o.left === 'ham'
        ? `<button class="icon-btn" data-goto="p12" data-act="goto:p12" aria-label="会话历史">${I.ham}</button>`
        : `<button class="icon-btn" data-act="${o.leftAct || 'back'}" aria-label="返回">${I.back}</button>`;
    const right = o.right || '<span style="width:44px"></span>';
    return `<div class="nav${o.dark ? ' on-dark' : ''}">${left}<div class="nav-title">${o.title || ''}</div>${right}</div>`;
  };

  const composer = (ph, act) => `
    <div class="composer">
      <div class="composer-row">
        <input class="composer-input" placeholder="${ph || '试试:给张三转 500'}" data-act="input">
        <button class="send-btn" data-act="${act || 'send'}" aria-label="发送">${I.send}</button>
      </div>
      <div class="composer-safe"></div>
    </div>`;

  const fab = () => `
    <button class="fab-alert" data-goto="p11" data-act="goto:p11">
      <span class="red-dot"></span>${I.warn}
      <span>你有 <b>1 笔待确认转账 ¥500</b> · 去确认</span>
    </button>`;

  const homeBar = (dark) => `<div class="home-bar${dark ? ' on-dark' : ''}"><i></i></div>`;

  const userMsg = (t) => `<div class="msg user"><div class="bubble-user">${t}</div></div>`;
  const aiMsg = (t) => `<div class="msg"><div class="avatar">AI</div><div class="bubble-ai">${t}</div></div>`;
  const chips = (list, first) => `
    <div class="chips">${list.map((c, i) => `<button class="chip" data-goto="${first && first[i] ? first[i] : 'p3'}" data-act="chip">${c}</button>`).join('')}</div>`;

  const keypad = (inline) => `
    <div class="keypad${inline ? ' keypad-inline' : ''}">
      ${[1, 2, 3, 4, 5, 6, 7, 8, 9].map((n) => `<button class="key" data-act="key" data-key="${n}">${n}</button>`).join('')}
      <button class="key" data-act="noop"></button>
      <button class="key" data-act="key" data-key="0">0</button>
      <button class="key wide" data-act="del">删除</button>
    </div>`;

  const pwdCells = (filled, err) => `
    <div class="pwd-cells">
      ${[1, 2, 3, 4, 5, 6].map((n) => `
        <div class="pwd-cell${n <= filled ? (err ? ' err' : ' filled') : (n === filled + 1 ? ' active' : '')}">${n <= filled ? '<i></i>' : ''}</div>
      `).join('')}
    </div>`;

  const chatHead = (title, o) => {
    o = o || {};
    return sb(o.dark) + nav({
      title: title,
      left: o.left === undefined ? 'back' : o.left,
      right: o.right,
      dark: o.dark
    });
  };

  const demoFastBtn = `<button class="pill-btn-gold" data-goto="p13b" data-act="ff">${I.bolt}演示快进</button>`;

  /* ---------------------- 共享卡片片段 ---------------------- */
  const transferCard = () => `
    <div class="card-gold">
      <div class="cc-head">
        <div class="cc-title">${I.cash}转账确认</div>
        <span class="cc-badge">待确认</span>
      </div>
      <div class="amount-line"><div class="amount"><small>¥</small>500.00</div></div>
      <div class="kv"><span class="k">收款人</span><span class="v">张三</span></div>
      <div class="kv"><span class="k">备注</span><span class="v">房租</span></div>
      <div class="kv"><span class="k">转账单号</span><span class="v">#1024</span></div>
      <div class="hint gold" style="margin-top:10px">${I.lockSm}确认后将要求输入 6 位支付密码</div>
      <div class="card-actions">
        <button class="btn btn-gold" data-goto="p5" data-act="passcode"
          data-pwd-sub="向 张三 转账 ¥500.00" data-pwd-next="p4s">确认转账</button>
        <button class="btn btn-ghost" data-act="cancel">取消</button>
      </div>
    </div>`;

  const subsCard = () => `
    <div class="card">
      <div class="card-title"><span class="dot"></span>订阅 / 代扣总览</div>
      <div class="card-sub">3 个自动续费,月合计 ¥69</div>
      <div style="margin-top:6px">
        ${[
          ['腾讯视频', '¥30/月', '下次扣费 11-01', '年化 ¥360'],
          ['网易云音乐', '¥18/月', '下次扣费 10-28', '年化 ¥216'],
          ['iCloud', '¥21/月', '下次扣费 10-30', '年化 ¥252']
        ].map((r) => `
          <div class="list-row">
            <div class="lr-main">
              <div class="lr-name">${r[0]} <span style="font-size:12px;color:var(--muted);font-weight:500">${r[1]}</span></div>
              <div class="lr-meta">${r[2]} · ${r[3]}</div>
            </div>
            <button class="btn btn-ghost btn-sm" data-goto="p9b" data-act="goto:p9b">取消代扣</button>
          </div>`).join('')}
      </div>
      <div class="total-row"><span>月合计 <b>¥69</b></span><span>年合计 <b>¥828</b></span></div>
    </div>`;

  /* ============================================================
     页面集合
     ============================================================ */
  const screens = [

    /* ---------------- P1 登录 ---------------- */
    {
      id: 'p1', no: 'P1', group: '账号入口', title: '登录页',
      note: '墨蓝渐变 + 银行盾牌;手机号/密码双输入;底部「立即实名注册」入口',
      html: `
      <div class="login-screen">
        ${sb(true)}
        <div class="login-body">
          <div class="login-brand">
            <div class="logo">${I.shieldWhite}</div>
            <h1>AI 银行助手</h1>
            <p>动钱的事,我会先和你确认</p>
          </div>
          <div class="login-field">
            ${I.phone}
            <input placeholder="手机号 / 登录账号" value="139" data-act="input">
          </div>
          <div class="login-field">
            ${I.lock}
            <input type="password" placeholder="登录密码" value="••••••••" data-act="input">
            <span class="eye" data-act="noop">显示</span>
          </div>
          <button class="btn btn-gold btn-block login-submit" data-goto="p3" data-act="goto:p3">登录</button>
          <div class="login-links">没有账号?<b data-goto="p2" data-act="goto:p2">立即实名注册</b></div>
          <div class="login-note">
            演示账号:陈明 · <code>139****2233</code> / 密码 <code>Demo@12345</code><br>
            支付密码 <code>888888</code>(仅本地演示数据,不连接真实银行)
          </div>
        </div>
        ${homeBar(true)}
      </div>`
    },

    /* ---------------- P2 实名注册 ---------------- */
    {
      id: 'p2', no: 'P2', group: '账号入口', title: '实名注册页',
      note: '注册即实名:姓名 / 18 位身份证 / 手机号(即账号)/ 邮箱选填 / 设密码',
      html: `
      ${chatHead('实名注册')}
      <div class="body">
        <div class="field-group">
          <div class="field-label">真实姓名 <span class="must">*</span></div>
          <div class="field">${I.user}<input placeholder="与身份证一致" data-act="input"></div>
        </div>
        <div class="field-group">
          <div class="field-label">身份证号 <span class="must">*</span></div>
          <div class="field">${I.idcard}<input placeholder="仅支持有效身份证号" data-act="input"><span class="suffix">18 位</span></div>
        </div>
        <div class="field-group">
          <div class="field-label">手机号 <span class="must">*</span> 必填,即登录账号</div>
          <div class="field">${I.phone}<input placeholder="11 位手机号" data-act="input"></div>
        </div>
        <div class="field-group">
          <div class="field-label">邮箱 <span style="color:var(--muted)">选填</span></div>
          <div class="field">${I.mail}<input placeholder="name@example.com" data-act="input"></div>
        </div>
        <div class="field-group">
          <div class="field-label">设置登录密码 <span class="must">*</span></div>
          <div class="field">${I.lock}<input type="password" placeholder="8-20 位,含字母与数字" value="••••••••" data-act="input"></div>
        </div>
        <div class="field-group">
          <div class="field-label">再次输入密码 <span class="must">*</span></div>
          <div class="field">${I.lock}<input type="password" placeholder="再次输入登录密码" value="••••••••" data-act="input"></div>
        </div>
      </div>
      <div style="flex:0 0 auto;padding:10px 14px 0;background:var(--bg)">
        <button class="btn btn-primary btn-block" data-goto="p3" data-act="goto:p3">提交实名注册</button>
        <div class="form-note" style="margin:10px 0 12px">
          注册即实名:同一张身份证仅可注册一个账户;身份证编号将做合法性校验(GB 11643 校验码)。
        </div>
      </div>
      <div class="safe-bottom on-bg"></div>`
    },

    /* ---------------- P3 对话主界面(空态) ---------------- */
    {
      id: 'p3', no: 'P3', group: '对话核心', title: '对话主界面 · 空态',
      note: '核心页:汉堡会话抽屉 / 演示快进;问候语 + 横滑建议胶囊;底部输入栏',
      html: `
      <div style="display:flex;flex-direction:column;height:100%;background:var(--bg)">
        ${chatHead('AI 银行助手', { left: 'ham', right: demoFastBtn })}
        <div class="body">
          <div class="hero" style="margin-top:24px">
            <div class="hero-logo">${I.shield}</div>
            <h2>你好,陈明!</h2>
            <p>我是你的 AI 银行助手,动钱的事我会先和你确认。<br>转账、账单、理财、卡片、订阅,一句话就行。</p>
          </div>
          <div style="flex:1"></div>
          <div class="chips">
            ${[
              ['我要转账', 'p4'], ['帮我准备生日', 'p10a'], ['账单分析', 'p6'],
              ['理财操作', 'p7a'], ['卡片管理', 'p8a'], ['订阅代扣管理', 'p9a']
            ].map((c) => `<button class="chip" data-goto="${c[1]}" data-act="goto:${c[1]}">${c[0]}</button>`).join('')}
          </div>
          <div class="hint" style="justify-content:center;text-align:center;padding:0 10px">
            「给妈妈转 500 块」这样说话就行;所有动钱操作都会先给你一张确认卡。
          </div>
        </div>
        ${fab()}
        ${composer()}
        ${homeBar()}
      </div>`
    },

    /* ---------------- P4 转账确认流 ---------------- */
    {
      id: 'p4', no: 'P4', group: '动钱闸门', title: '对话中 · 转账确认卡',
      note: '用户口语指令 → 助手核对 → 浅金确认卡(确认 + 取消两个出口)',
      html: `
      <div style="display:flex;flex-direction:column;height:100%;background:var(--bg)">
        ${chatHead('AI 银行助手', { left: 'ham', right: demoFastBtn })}
        <div class="body" style="padding-top:4px">
          ${userMsg('给张三转 500,备注房租')}
          ${aiMsg('好的,请核对以下信息:')}
          <div class="msg-cards">${transferCard()}</div>
          ${aiMsg('没问题就点「确认转账」,我会再要一次 6 位支付密码;钱不会在你点确认之前动。')}
        </div>
        ${fab()}
        ${composer()}
        ${homeBar()}
      </div>`
    },

    /* ---------------- P4-S 转账成功 ---------------- */
    {
      id: 'p4s', no: 'P4-S', group: '动钱闸门', title: '转账成功(密码正确后)',
      note: '密码校验通过 → 卡片转为绿色成功态,展示单号/时间/去向',
      html: `
      <div style="display:flex;flex-direction:column;height:100%;background:var(--bg)">
        ${chatHead('AI 银行助手', { left: 'ham', right: demoFastBtn })}
        <div class="body" style="padding-top:4px">
          ${userMsg('给张三转 500,备注房租')}
          <div class="card-gold" style="border-color:#bfe4cc;background:var(--green-bg);box-shadow:none">
            <div class="success-hero">
              <div class="check-circle">${I.check}</div>
              <div style="font-size:14px;font-weight:700;color:var(--green)">转账成功</div>
              <div class="success-amount">¥500.00</div>
              <div style="font-size:12px;color:var(--muted);margin-top:4px">收款人 张三 · 备注 房租</div>
            </div>
            <div class="kv" style="border-bottom:0"><span class="k">转账单号</span><span class="v">#1024</span></div>
            <div class="kv"><span class="k">交易时间</span><span class="v">2026-10-14 09:41</span></div>
            <div class="kv"><span class="k">付款账户</span><span class="v">借记卡 尾号 6688</span></div>
            <div class="kv" style="border-bottom:0"><span class="k">可用余额</span><span class="v">¥8,120.36</span></div>
            <div class="card-actions">
              <button class="btn btn-ghost" data-goto="p6" data-act="goto:p6">看账单</button>
              <button class="btn btn-ghost" data-goto="p3" data-act="goto:p3">完成</button>
            </div>
          </div>
          ${aiMsg('已完成。要我顺手看看这个月的支出情况吗?')}
        </div>
        ${composer()}
        ${homeBar()}
      </div>`
    },

    /* ---------------- P5 支付密码弹层 ---------------- */
    {
      id: 'p5', no: 'P5', group: '动钱闸门', title: '支付密码弹层',
      note: '6 格密码 + 数字键盘 + 剩余次数提示;连续错 4 次锁定转账功能',
      html: `
      <div style="display:flex;flex-direction:column;height:100%;background:var(--bg)">
        ${chatHead('AI 银行助手', { left: 'ham', right: demoFastBtn })}
        <div class="body" style="padding-top:4px">
          ${userMsg('给张三转 500,备注房租')}
          ${aiMsg('好的,请核对以下信息:')}
          <div class="msg-cards">${transferCard()}</div>
        </div>
        ${composer()}
        <div class="mask">
          <div class="sheet">
            <div class="sheet-title">请输入支付密码</div>
            <div class="sheet-sub">向 <b>张三</b> 转账 <b>¥500.00</b></div>
            ${pwdCells(4)}
            <div class="hint">${I.lockSm}剩余尝试次数:<b style="color:var(--red)">3 次</b>,连续错 4 次将锁定转账功能</div>
            ${keypad(true)}
          </div>
        </div>
        ${homeBar()}
      </div>`
    },

    /* ---------------- P6 账单分析 ---------------- */
    {
      id: 'p6', no: 'P6', group: '六大场景', title: '账单分析卡',
      note: '只读不动钱:总览四宫格 + 分类条形图 + 异常检测(重复扣款标红)',
      html: `
      <div style="display:flex;flex-direction:column;height:100%;background:var(--bg)">
        ${chatHead('AI 银行助手', { left: 'ham', right: demoFastBtn })}
        <div class="body" style="padding-top:4px">
          ${userMsg('上个月花了多少?')}
          ${aiMsg('9 月你一共花了 ¥6,480,储蓄率 46%,我顺手查了三类异常:')}
          <div class="msg-cards">
            <div class="card">
              <div class="card-title"><span class="dot"></span>9 月总览</div>
              <div class="stat-grid" style="margin-top:10px">
                <div class="stat"><div class="s-label">支出</div><div class="s-value">¥6,480</div></div>
                <div class="stat"><div class="s-label">收入</div><div class="s-value">¥12,000</div></div>
                <div class="stat"><div class="s-label">储蓄率</div><div class="s-value">46%</div></div>
                <div class="stat"><div class="s-label">环比</div><div class="s-value down">↓ 8%</div></div>
              </div>
            </div>
            <div class="card">
              <div class="card-title">${I.chart}消费分类</div>
              <div class="bar-row" style="margin-top:10px">
                ${[['房租', 3000, 3000], ['餐饮', 1850, 3000], ['购物', 980, 3000], ['交通', 420, 3000], ['订阅', 230, 3000]]
                  .map((b) => `
                  <div class="bar-line">
                    <span class="bl-label">${b[0]}</span>
                    <span class="bar-track"><span class="bar-fill" style="width:${Math.round(b[1] / b[2] * 100)}%"></span></span>
                    <span class="bl-val">¥${b[1].toLocaleString('en-US')}</span>
                  </div>`).join('')}
              </div>
            </div>
            <div class="card">
              <div class="card-title">${I.warn}异常检测</div>
              <div style="margin-top:8px">
                <div class="list-row" style="padding-top:8px">
                  <span class="tag tag-red">重复扣款</span>
                  <div class="lr-main"><div class="lr-meta" style="color:var(--red)">美团 9/3 与 9/4 各扣 ¥39,疑似重复</div></div>
                </div>
                <div class="list-row">
                  <span class="tag tag-gray">大额离群</span>
                  <div class="lr-main"><div class="lr-meta">无</div></div>
                </div>
                <div class="list-row">
                  <span class="tag tag-gray">凌晨大额</span>
                  <div class="lr-main"><div class="lr-meta">无</div></div>
                </div>
              </div>
            </div>
          </div>
        </div>
        ${composer()}
        ${homeBar()}
      </div>`
    },

    /* ---------------- P7a 理财申购 ---------------- */
    {
      id: 'p7a', no: 'P7a', group: '六大场景', title: '理财申购确认卡',
      note: '申购同样走确认卡 + 支付密码;展示产品风险等级与付款账户',
      html: `
      <div style="display:flex;flex-direction:column;height:100%;background:var(--bg)">
        ${chatHead('AI 银行助手', { left: 'ham', right: demoFastBtn })}
        <div class="body" style="padding-top:4px">
          ${userMsg('拿一万块买个稳一点的理财')}
          ${aiMsg('给你挑了一支符合你风险等级(C3)的:')}
          <div class="card-gold">
            <div class="cc-head">
              <div class="cc-title">${I.chart}申购确认</div>
              <span class="cc-badge">风险 R2</span>
            </div>
            <div class="amount-line"><div class="amount"><small>¥</small>10,000.00</div></div>
            <div class="kv"><span class="k">产品</span><span class="v">安享 90 天定开债</span></div>
            <div class="kv"><span class="k">参考年化</span><span class="v">3.2%</span></div>
            <div class="kv"><span class="k">锁定期</span><span class="v">90 天</span></div>
            <div class="kv"><span class="k">产品风险</span><span class="v">R2(低于你的 C3)</span></div>
            <div class="kv"><span class="k">付款账户</span><span class="v">借记卡 尾号 6688</span></div>
            <div class="hint gold" style="margin-top:10px">${I.lockSm}确认后需输入 6 位支付密码,申购成功后进入锁定期</div>
            <div class="card-actions">
              <button class="btn btn-gold" data-goto="p5" data-act="passcode"
                data-pwd-title="请输入支付密码" data-pwd-sub="申购 安享 90 天定开债 ¥10,000.00" data-pwd-next="p7a">确认申购</button>
              <button class="btn btn-ghost" data-act="cancel">取消</button>
            </div>
          </div>
        </div>
        ${composer()}
        ${homeBar()}
      </div>`
    },

    /* ---------------- P7b 风险拦截 ---------------- */
    {
      id: 'p7b', no: 'P7b', group: '六大场景', title: '理财风险拦截态',
      note: '强制规则:产品风险高于用户测评等级直接拒购,无「继续」出口',
      html: `
      <div style="display:flex;flex-direction:column;height:100%;background:var(--bg)">
        ${chatHead('AI 银行助手', { left: 'ham', right: demoFastBtn })}
        <div class="body" style="padding-top:4px">
          ${userMsg('有没有收益高点的?想买那个 R4 的量化产品')}
          ${aiMsg('这个我不能帮你买,原因是硬性的:')}
          <div class="card-alert">
            <div class="cc-head"><div class="cc-title">${I.warn}风险等级超限</div><span class="tag tag-red">已拦截</span></div>
            <div class="alert-strong" style="margin-top:10px">
              该产品风险等级 <b>R4</b> 高于你的测评等级 <b>C3</b>,无法购买。<br>此为强制规则,不可人工跳过。
            </div>
            <div class="kv" style="border-bottom:0;border-top:1px dashed #f0cccc;margin-top:10px;padding-top:10px">
              <span class="k">你的风险测评</span><span class="v">C3 · 稳健型</span>
            </div>
            <div class="kv" style="border-bottom:0"><span class="k">该产品风险</span><span class="v" style="color:var(--red)">R4 · 中高</span></div>
            <div class="card-actions">
              <button class="btn btn-ghost" data-goto="p7a" data-act="goto:p7a">看符合我的产品</button>
            </div>
          </div>
          ${aiMsg('需要的话,我可以按 C3 以内的范围再筛一遍给你。')}
        </div>
        ${composer()}
        ${homeBar()}
      </div>`
    },

    /* ---------------- P8a 限额调整 ---------------- */
    {
      id: 'p8a', no: 'P8a', group: '六大场景', title: '卡片管理 · 调整限额',
      note: '限额调整属于动钱相关操作 → 同样确认卡 + 支付密码',
      html: `
      <div style="display:flex;flex-direction:column;height:100%;background:var(--bg)">
        ${chatHead('AI 银行助手', { left: 'ham', right: demoFastBtn })}
        <div class="body" style="padding-top:4px">
          ${userMsg('把尾号 6688 这张卡的单笔限额提到 1 万')}
          ${aiMsg('好,请核对:')}
          <div class="bank-card">
            <div class="bc-top"><span>借记卡</span><span>AI BANK</span></div>
            <div class="bc-no">**** **** **** 6688</div>
            <div class="bc-foot"><span>陈明</span><span>单笔限额 ¥5,000</span></div>
          </div>
          <div class="card-gold">
            <div class="cc-head"><div class="cc-title">${I.card}调整单笔限额</div><span class="cc-badge">待确认</span></div>
            <div class="kv" style="margin-top:8px"><span class="k">卡号</span><span class="v">借记卡 尾号 6688</span></div>
            <div class="kv"><span class="k">当前单笔限额</span><span class="v">¥5,000</span></div>
            <div class="amount-line" style="padding-top:10px">
              <span class="k" style="font-size:13px;color:var(--muted)">调整为</span>
              <div class="amount"><small>¥</small>10,000.00</div>
            </div>
            <div class="hint gold">${I.lockSm}调整后立即生效,确认需输入 6 位支付密码</div>
            <div class="card-actions">
              <button class="btn btn-gold" data-goto="p5" data-act="passcode"
                data-pwd-sub="调整尾号 6688 单笔限额至 ¥10,000" data-pwd-next="p8a">确认调整</button>
              <button class="btn btn-ghost" data-act="cancel">取消</button>
            </div>
          </div>
        </div>
        ${composer()}
        ${homeBar()}
      </div>`
    },

    /* ---------------- P8b 挂失警示 ---------------- */
    {
      id: 'p8b', no: 'P8b', group: '六大场景', title: '卡片管理 · 挂失警示',
      note: '不可逆操作:红色描边 + 红字二次确认,按钮文案明确后果',
      html: `
      <div style="display:flex;flex-direction:column;height:100%;background:var(--bg)">
        ${chatHead('AI 银行助手', { left: 'ham', right: demoFastBtn })}
        <div class="body" style="padding-top:4px">
          ${userMsg('我信用卡好像丢了,帮我挂失')}
          ${aiMsg('挂失不可撤销,请务必再确认一次:')}
          <div class="card-alert">
            <div class="cc-head"><div class="cc-title">${I.warn}挂失确认(不可恢复)</div><span class="tag tag-red">高风险</span></div>
            <div class="alert-strong" style="margin-top:10px">
              挂失后卡片立即且永久失效,无法由任何人恢复,请再次确认。<br>
              卡号:信用卡 尾号 2251
            </div>
            <div class="card-actions">
              <button class="btn btn-red" data-goto="p13a" data-act="goto:p13a">确认挂失</button>
              <button class="btn btn-ghost" data-act="cancel">取消</button>
            </div>
          </div>
          ${aiMsg('如果只是找不到,建议先做「临时冻结」,找到后可自行解冻。')}
        </div>
        ${composer()}
        ${homeBar()}
      </div>`
    },

    /* ---------------- P9a 订阅总览 ---------------- */
    {
      id: 'p9a', no: 'P9a', group: '六大场景', title: '订阅 / 代扣总览',
      note: '三行订阅明示下次扣费与年化;每行一个「取消代扣」出口',
      html: `
      <div style="display:flex;flex-direction:column;height:100%;background:var(--bg)">
        ${chatHead('AI 银行助手', { left: 'ham', right: demoFastBtn })}
        <div class="body" style="padding-top:4px">
          ${userMsg('我都有哪些自动续费?')}
          ${aiMsg('你名下 3 个自动扣费,合计月 ¥69 / 年 ¥828:')}
          <div class="msg-cards">${subsCard()}</div>
          ${aiMsg('要取消哪个?取消属于动钱操作,我会先给你一张确认卡,再要支付密码。')}
        </div>
        ${composer()}
        ${homeBar()}
      </div>`
    },

    /* ---------------- P9b 取消代扣确认 ---------------- */
    {
      id: 'p9b', no: 'P9b', group: '六大场景', title: '取消代扣确认卡',
      note: '破坏性操作照样两步走:确认卡 → 支付密码;同时给「保留」出口',
      html: `
      <div style="display:flex;flex-direction:column;height:100%;background:var(--bg)">
        ${chatHead('AI 银行助手', { left: 'ham', right: demoFastBtn })}
        <div class="body" style="padding-top:4px">
          ${userMsg('腾讯视频那个会员不用了,取消自动续费')}
          <div class="card-gold">
            <div class="cc-head"><div class="cc-title">${I.clock}取消代扣确认</div><span class="cc-badge">待确认</span></div>
            <div class="kv" style="margin-top:8px"><span class="k">商户</span><span class="v">腾讯视频</span></div>
            <div class="kv"><span class="k">当前周期</span><span class="v">¥30/月</span></div>
            <div class="kv"><span class="k">下次扣费</span><span class="v">11-01</span></div>
            <div class="kv"><span class="k">取消后</span><span class="v">下一期不再扣款</span></div>
            <div class="hint gold" style="margin-top:10px">${I.lockSm}取消代扣需输入支付密码验证,会员权益保留至当前周期结束</div>
            <div class="card-actions">
              <button class="btn btn-gold" data-goto="p5" data-act="passcode"
                data-pwd-title="取消代扣验证" data-pwd-sub="取消 腾讯视频 自动扣费(¥30/月)" data-pwd-next="p9a">确认取消(输支付密码)</button>
              <button class="btn btn-ghost" data-goto="p9a" data-act="goto:p9a">保留</button>
            </div>
          </div>
          ${aiMsg('确认前我再提醒一句:取消后 11-01 不会再扣,会员到期即止。')}
        </div>
        ${composer()}
        ${homeBar()}
      </div>`
    },

    /* ---------------- P10a 生日联动计划 ---------------- */
    {
      id: 'p10a', no: 'P10a', group: '六大场景', title: '跨场景联动 · 建计划',
      note: '预算只锁定为待确认转账,绝不自动扣款;每个动作到期都要再过一次密码',
      html: `
      <div style="display:flex;flex-direction:column;height:100%;background:var(--bg)">
        ${chatHead('AI 银行助手', { left: 'ham', right: demoFastBtn })}
        <div class="body" style="padding-top:4px">
          ${userMsg('我老婆下周三生日')}
          ${aiMsg('那我给你排个计划,钱只预留不自动付:')}
          <div class="card-gold">
            <div class="cc-head"><div class="cc-title">${I.bolt}跨场景联动计划</div><span class="cc-badge">妻子生日</span></div>
            <div class="kv" style="margin-top:8px"><span class="k">事件</span><span class="v">妻子生日 10-22(周三)</span></div>
            <div class="kv"><span class="k">预算预留</span><span class="v">¥1,000</span></div>
            <div class="hint gold" style="margin-bottom:8px">${I.lockSm}仅锁定为待确认转账,绝不自动扣款</div>
            <div class="timeline">
              <div class="tl-item">
                <div class="tl-rail"><span class="tl-dot"></span><span class="tl-line"></span></div>
                <div class="tl-body"><div class="tl-title">10-20 买鲜花 ¥300</div><div class="tl-meta">生日前 2 天提醒,需支付密码</div></div>
              </div>
              <div class="tl-item">
                <div class="tl-rail"><span class="tl-dot"></span></div>
                <div class="tl-body"><div class="tl-title">10-21 订生日蛋糕 ¥260</div><div class="tl-meta">生日前 1 天提醒,需支付密码</div></div>
              </div>
            </div>
            <div class="hint">合计 2 个动作 ¥560,剩余 ¥440 留在预算池</div>
            <div class="card-actions">
              <button class="btn btn-gold" data-goto="p10b" data-act="ff-jump">确认计划</button>
              <button class="btn btn-ghost" data-act="cancel">取消</button>
            </div>
          </div>
        </div>
        ${composer()}
        ${homeBar()}
      </div>`
    },

    /* ---------------- P10b 联动到期 ---------------- */
    {
      id: 'p10b', no: 'P10b', group: '六大场景', title: '跨场景联动 · 到期提醒',
      note: '演示快进到 10-20 后的到期待办;点「现在购买」再走支付密码',
      html: `
      <div style="display:flex;flex-direction:column;height:100%;background:var(--bg)">
        ${chatHead('AI 银行助手', { left: 'ham', right: demoFastBtn })}
        <div class="body" style="padding-top:4px">
          ${aiMsg('你把系统日期拨到了 10-20,联动计划有一条到期了:')}
          <div class="card-gold">
            <div class="cc-head"><div class="cc-title">${I.clock}联动到期提醒</div><span class="cc-badge">今天到期</span></div>
            <div class="amount-line">
              <div>
                <div style="font-size:14px;font-weight:600">买鲜花</div>
                <div class="hint">10-20 · 妻子生日计划第 1 步</div>
              </div>
              <div class="amount"><small>¥</small>300.00</div>
            </div>
            <div class="hint gold">${I.lockSm}今天到期;不点就不会扣钱,过期后动作自动失效</div>
            <div class="card-actions">
              <button class="btn btn-gold" data-goto="p5" data-act="passcode"
                data-pwd-sub="购买鲜花 ¥300.00(生日联动)" data-pwd-next="p4s">现在购买(需支付密码)</button>
              <button class="btn btn-ghost" data-goto="p10a" data-act="goto:p10a">跳过</button>
            </div>
          </div>
          ${aiMsg('后面还有一条:10-21 的生日蛋糕 ¥260。')}
        </div>
        ${composer()}
        ${homeBar()}
      </div>`
    },

    /* ---------------- P11 待确认转账页 ---------------- */
    {
      id: 'p11', no: 'P11', group: '动钱闸门', title: '待确认转账页(独立页)',
      note: '定时/待确认单据集中入口;展开即输入支付密码;定时到期只转待确认不自动扣款',
      html: `
      ${chatHead('待确认转账')}
      <div class="body">
        <div class="seg">
          <button class="on">全部 2</button><button>待确认 1</button><button>定时 1</button>
        </div>
        <div class="card-gold">
          <div class="cc-head">
            <div class="cc-title">${I.cash}张三</div><span class="tag tag-gold">待确认</span>
          </div>
          <div class="amount-line"><div class="amount"><small>¥</small>500.00</div></div>
          <div class="kv"><span class="k">备注</span><span class="v">房租</span></div>
          <div class="kv"><span class="k">发起时间</span><span class="v">今天 09:41</span></div>
          <div class="hint gold" style="margin:10px 0 4px">${I.lockSm}输入 6 位支付密码后才会真正扣款</div>
          ${pwdCells(2)}
          ${keypad(true)}
          <div class="hint" style="margin-top:8px">连续输错 4 次锁定转账功能,输对一次即清零计数;对话窗口内错 2 次自动取消。</div>
        </div>
        <div class="card">
          <div class="list-row" style="padding-top:0">
            <div class="lr-main">
              <div class="lr-name">房东 李四</div>
              <div class="lr-meta">房租 · 定时转账</div>
            </div>
            <div style="text-align:right">
              <div class="lr-amount">¥3,000.00</div>
              <span class="tag tag-blue" style="margin-top:4px">${I.clock}定时 10-16 09:00</span>
            </div>
          </div>
        </div>
        <div class="hint" style="text-align:center;padding:0 8px">
          定时转账到期后仅转为「待确认」,不会自动扣款;需你亲自输入支付密码。
        </div>
      </div>
      <div class="safe-bottom on-bg"></div>`
    },

    /* ---------------- P12 会话抽屉 ---------------- */
    {
      id: 'p12', no: 'P12', group: '对话核心', title: '会话历史抽屉',
      note: '左侧滑出;顶部用户信息 + 退出登录;会话列表高亮当前会话',
      html: `
      <div style="display:flex;flex-direction:column;height:100%;background:var(--bg)">
        ${chatHead('AI 银行助手', { left: 'ham', right: demoFastBtn })}
        <div class="body" style="padding-top:4px">
          <div class="hero hero-small"><h2 style="font-size:18px">你好,陈明!</h2><p>我是你的 AI 银行助手。</p></div>
        </div>
        ${composer()}
        <div class="drawer-mask" data-act="goto:p3"></div>
        <div class="drawer">
          <div class="drawer-head">
            <div class="drawer-user">
              <div class="drawer-avatar">陈</div>
              <div>
                <div class="drawer-name">陈明</div>
                <div class="drawer-phone">139****2233</div>
              </div>
            </div>
            <button class="drawer-logout" data-act="goto:p1">退出登录</button>
          </div>
          <div class="drawer-body">
            <button class="btn btn-ghost btn-block" style="flex:0 0 auto;margin:4px 0 8px" data-goto="p3" data-act="goto:p3">${I.plus} 新对话</button>
            <div class="drawer-section">会话历史</div>
            <button class="session-item" data-goto="p4" data-act="goto:p4"><span class="si-title">给妈妈转账</span><span class="si-time">10-14</span></button>
            <button class="session-item" data-goto="p6" data-act="goto:p6"><span class="si-title">九月账单分析</span><span class="si-time">10-02</span></button>
            <button class="session-item active" data-goto="p10a" data-act="goto:p10a"><span class="si-title">生日准备</span><span class="si-time">刚刚</span></button>
            <button class="session-item" data-goto="p7a" data-act="goto:p7a"><span class="si-title">理财咨询</span><span class="si-time">09-27</span></button>
            <button class="session-item" data-goto="p9a" data-act="goto:p9a"><span class="si-title">订阅代扣管理</span><span class="si-time">09-20</span></button>
          </div>
          <div class="drawer-foot">
            <div class="hint" style="justify-content:center">本地演示数据 · 不连接真实银行</div>
          </div>
        </div>
        ${homeBar()}
      </div>`
    },

    /* ---------------- P13a 转账功能锁定 ---------------- */
    {
      id: 'p13a', no: 'P13a', group: '演示专用', title: '锁定提示态',
      note: '支付密码连续错 4 次 → 转账功能锁定,仅管理员后台可解;按钮置灰',
      html: `
      <div style="display:flex;flex-direction:column;height:100%;background:var(--bg)">
        ${chatHead('AI 银行助手', { left: 'ham', right: demoFastBtn })}
        <div class="body" style="padding-top:4px">
          ${userMsg('给张三转 500,备注房租')}
          ${aiMsg('好的,请核对以下信息:')}
          <div class="card" style="border-color:#e6e8ec;box-shadow:none;background:#f2f3f6">
            <div class="card-title" style="color:#7a8494">${I.lock}转账功能已锁定</div>
            <div style="font-size:13px;color:#7a8494;margin-top:8px;line-height:1.6">
              支付密码连续错误 <b>4 次</b>,转账功能已锁定。<br>仅管理员可在后台解锁,解锁后剩余次数重新计为 3 次。
            </div>
            <div class="kv" style="border-bottom:0;border-top:1px dashed #dfe2e7;margin-top:10px;padding-top:10px">
              <span class="k" style="color:#8b93a1">锁定时间</span><span class="v" style="color:#8b93a1">2026-10-14 09:43</span>
            </div>
            <div class="card-actions">
              <button class="btn disabled" disabled>知道了</button>
            </div>
          </div>
          ${aiMsg('转账、定时转账、取消代扣等动钱操作都已暂停;账单查询不受影响。')}
        </div>
        ${composer()}
        ${homeBar()}
      </div>`
    },

    /* ---------------- P13b 演示快进 ---------------- */
    {
      id: 'p13b', no: 'P13b', group: '演示专用', title: '演示快进弹窗',
      note: '现场演示专用:把系统日期拨到 10-20,触发联动到期',
      html: `
      <div style="display:flex;flex-direction:column;height:100%;background:var(--bg)">
        ${chatHead('AI 银行助手', { left: 'ham', right: demoFastBtn })}
        <div class="body" style="padding-top:4px">
          <div class="hero hero-small"><h2 style="font-size:18px">你好,陈明!</h2><p>我是你的 AI 银行助手。</p></div>
          <div class="chips">
            <button class="chip" data-goto="p4" data-act="goto:p4">我要转账</button>
            <button class="chip" data-goto="p10a" data-act="goto:p10a">帮我准备生日</button>
          </div>
        </div>
        ${composer()}
        <div class="mask">
          <div class="dialog">
            <h3>演示快进</h3>
            <p>把系统日期拨到 <b>10-20</b>(生日前 2 天),用于现场演示跨场景联动到期触发。<br>
              快进只影响演示时钟,不改变任何账户余额与已建单据。</p>
            <div class="dialog-actions">
              <button class="btn btn-gold" data-goto="p10b" data-act="goto:p10b">快进</button>
              <button class="btn btn-ghost" data-act="cancel">关闭</button>
            </div>
          </div>
        </div>
        ${homeBar()}
      </div>`
    }
  ];

  /* ---------------------- 交互连线表 ---------------------- */
  const links = [
    ['「立即实名注册」文字点按', 'P1 登录页', 'P2 实名注册页', 'p1', 'p2'],
    ['登录成功', 'P1 登录页', 'P3 对话主界面', 'p1', 'p3'],
    ['建议胶囊「我要转账」', 'P3 对话主界面', 'P4 转账确认流', 'p3', 'p4'],
    ['「确认转账」按钮', 'P4 转账确认流', 'P5 支付密码弹层', 'p4', 'p5'],
    ['密码输满 6 位(正确)', 'P5 支付密码弹层', 'P4-S 转账成功(绿色态)', 'p5', 'p4s'],
    ['密码错误 4 次', 'P5 支付密码弹层', 'P13a 转账功能锁定', 'p5', 'p13a'],
    ['右下角「去确认」胶囊', 'P3 / P4', 'P11 待确认转账页', 'p3', 'p11'],
    ['返回箭头', 'P11 待确认转账页', 'P3 对话主界面', 'p11', 'p3'],
    ['汉堡按钮', 'P3 对话主界面', 'P12 会话历史抽屉', 'p3', 'p12'],
    ['「确认取消(输支付密码)」', 'P9b 取消代扣确认', 'P5 支付密码弹层', 'p9b', 'p5'],
    ['「现在购买(需支付密码)」', 'P10b 联动到期', 'P5 支付密码弹层', 'p10b', 'p5'],
    ['「演示快进」按钮', 'P3 顶栏', 'P13b 快进弹窗 → P10b 到期态', 'p3', 'p13b'],
    ['「确认计划」', 'P10a 建计划', 'P3(助手回复「计划已建立,到期我会提醒你」)', 'p10a', 'p3']
  ];

  /* ---------------------- 验收清单 ---------------------- */
  const checklist = [
    ['全部页面为 375×812 手机画板,无桌面端双栏布局残留', true],
    ['所有确认卡为浅金底 / 金描边,警示(挂失、超限、锁定)为红色系,风格统一', true],
    ['动钱类卡片都有「确认 + 取消」两个出口,没有「一步直接扣款」的流程', true],
    ['支付密码统一是 6 格 + 数字键盘,带剩余次数提示', true],
    ['每页底部输入栏 / 键盘均避开安全区(34px),按钮热区 ≥ 44px', true],
    ['敏感信息全部脱敏:手机号 139****2233、卡号只露尾号', true],
    ['演示数据口径一致:陈明 / 张三 / 腾讯视频 ¥30 / 生日 10-22 / 预算 ¥1,000', true],
    ['交互链接按第四节连完,重点走通:转账流 P3→P4→P5、联动流 P3→P13→P10', true]
  ];

  window.PROTO = { screens: screens, links: links, checklist: checklist, icons: I, composer: composer, fab: fab, homeBar: homeBar, keypad: keypad, pwdCells: pwdCells, sb: sb, nav: nav };
})();
