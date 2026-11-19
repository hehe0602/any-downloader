/* 高速下载器前端逻辑（Vue3）。
 * 后端通过 window.appBridge(event) 推送事件；
 * 前端通过 window.pywebview.api.* 调用后端（返回 Promise）。
 */
const { createApp } = Vue;

function defaultForm() {
  return {
    url: "",
    saveDir: "",
    filename: "",
    concurrency: 64,
    chunkSize: 4,
    userAgent: "",
    referer: "",
    cookie: "",
    useHttp2: false,
    // cURL 一键导入
    curlText: "",
    extraHeaders: {},
    verifyTls: true,
    // 内置浏览器会话（WebView2）凭据
    sessionSite: "quark",
    autoCredentials: true,
    useClientHints: true,
    // 只解析 A 记录（强制 IPv4）：某些 CDN 的 IPv6 路径被限速
    preferIpv4: false,
    // 并发自适应：低并发起步，按实测吞吐找最优并发数
    adaptiveConcurrency: true,
  };
}

/* 分享链接解析面板的空状态：直链 / 分享 / 未登录 / 提取码 / 错误 */
function emptyShare() {
  return {
    kind: "",
    site: "",
    site_name: "",
    share_title: "",
    share_url: "",
    pwd_id: "",
    stoken: "",
    referer: "",
    // 当前目录下展示的条目
    files: [],
    // 勾选的文件 key 列表 + key->文件字典（可跨多个目录累积）
    selected: [],
    selectedMap: {},
    // 面包屑（根目录 fid 固定为 "0"）
    crumbs: [{ fid: "0", name: "根目录" }],
    currentDir: "0",
    // 已浏览目录的列表缓存：fid -> files
    dirCache: {},
    message: "",
    note: "",
    login_url: "",
    notes: [],
    passcode: "",
    resolving: false,
    dirLoading: false,
  };
}

const STATUS_TEXT = {
  queued: "等待中",
  connecting: "连接中",
  downloading: "下载中",
  paused: "已暂停",
  completed: "已完成",
  canceled: "已停止",
  error: "错误",
};

const app = createApp({
  data() {
    return {
      showForm: false,
      showAdvanced: false,
      tasks: {},
      order: [],
      maxCells: 200,
      toast: null,
      toastTimer: null,
      form: defaultForm(),
      // 本地偏好（目录 / 并发 / 分块），启动时从后端回填
      settings: { save_dir: "", concurrency: 64, chunk_size: 4 },
      // 分享链接解析状态（直链为空状态；分享 / 未登录 / 提取码 / 文件列表 / 错误）
      share: emptyShare(),
      resolveTimer: null,
      sessionSites: [],
      session: {
        site: "",
        site_name: "",
        logged_in: false,
        cookie_count: 0,
        cookie_names: [],
        window_open: false,
        note: "",
      },
      sessionTimer: null,
      // 登录引导面板可见时的登录态轮询（扫码成功后自动重解析）
      loginWatcher: null,
      diags: {},
      diagBusy: {},
    };
  },
  computed: {
    taskList() {
      return this.order.map((id) => this.tasks[id]).filter(Boolean);
    },

    sessionHint() {
      const s = this.session;
      if (s.note) return s.note;
      if (s.logged_in) {
        return `${s.site_name || "网盘"}已登录会话可用（${s.cookie_count} 个 Cookie），提交时自动注入`;
      }
      if (s.cookie_count || s.has_cookie) {
        return `${s.site_name || "网盘"}已读到会话 Cookie（${s.cookie_count || 0} 个），提交时先用现有会话`;
      }
      return "未登录：点「打开网盘登录」扫码一次，之后长期免 cURL";
    },
  },
  methods: {
    api() {
      return window.pywebview && window.pywebview.api;
    },

    toggleForm() {
      this.showForm = !this.showForm;
    },

    /* ---------------- 本地偏好：记住上次目录 / 并发 / 分块 ---------------- */
    async bootSettings() {
      try {
        const s = await this.api().get_settings();
        this.settings = {
          save_dir: s.save_dir || "",
          concurrency: s.concurrency || 64,
          chunk_size: s.chunk_size || 4,
          prefer_ipv4: !!s.prefer_ipv4,
          adaptive_concurrency: s.adaptive_concurrency !== false,
        };
        this.form.concurrency = this.settings.concurrency;
        this.form.chunkSize = this.settings.chunk_size;
        this.form.preferIpv4 = this.settings.prefer_ipv4;
        this.form.adaptiveConcurrency = this.settings.adaptive_concurrency;
        this.form.saveDir =
          this.settings.save_dir || (await this.api().get_default_dir());
      } catch (e) {
        try {
          this.form.saveDir = await this.api().get_default_dir();
        } catch (e2) {
          /* 取默认目录失败就留空，由后端兜底 */
        }
      }
    },

    async _persistSettings() {
      try {
        this.settings = await this.api().save_settings({
          save_dir: this.form.saveDir,
          concurrency: this.form.concurrency,
          chunk_size: this.form.chunkSize,
          prefer_ipv4: !!this.form.preferIpv4,
          adaptive_concurrency: !!this.form.adaptiveConcurrency,
        });
      } catch (e) {
        /* 写设置失败不阻断下载 */
      }
    },

    async browseFolder() {
      const dir = await this.api().browse_folder();
      if (dir) {
        this.form.saveDir = dir;
        await this._persistSettings();
      }
    },

    /* ---------------- 分享链接解析 ---------------- */
    resetShare() {
      this.stopLoginWatch();
      this.share = emptyShare();
    },

    scheduleResolve() {
      clearTimeout(this.resolveTimer);
      const url = (this.form.url || "").trim();
      if (!url) {
        this.resetShare();
        return;
      }
      this.resolveTimer = setTimeout(() => this.resolveLink(), 500);
    },

    async resolveLink() {
      const url = (this.form.url || "").trim();
      if (!url) {
        this.resetShare();
        return;
      }
      if (this.share.resolving) return;
      this.share.resolving = true;
      try {
        const res = await this.api().resolve_link({
          url,
          passcode: this.share.passcode,
        });
        this.applyResolveResult(res);
      } catch (e) {
        const next = emptyShare();
        next.kind = "error";
        next.message = "解析失败：" + (e && e.message ? e.message : e);
        next.passcode = this.share.passcode;
        this.share = next;
      } finally {
        this.share.resolving = false;
      }
    },

    applyResolveResult(res) {
      if (!res || !res.kind) return;
      const next = emptyShare();
      next.message = res.message || "";
      next.note = res.note || "";
      next.passcode = this.share.passcode;

      switch (res.kind) {
        case "direct":
          next.kind = "";
          break;
        case "share":
          next.kind = "share";
          next.site = res.site || "";
          next.site_name = res.site_name || "";
          next.share_title = res.share_title || "";
          next.share_url = res.share_url || this.form.url;
          next.pwd_id = res.pwd_id || "";
          next.stoken = res.stoken || "";
          next.referer = res.referer || "";
          next.files = res.files || [];
          next.notes = res.notes || [];
          next.currentDir = "0";
          next.crumbs = [{ fid: "0", name: "根目录" }];
          next.dirCache = { "0": next.files };
          // 勾选状态从空开始，由用户勾选文件
          next.selected = [];
          next.selectedMap = {};
          break;
        case "need_login":
          next.kind = "need_login";
          next.site = res.site || "";
          next.site_name = res.site_name || "";
          next.login_url = res.login_url || "";
          break;
        case "passcode_required":
        case "passcode_wrong":
        case "error":
          next.kind = res.kind;
          break;
        default:
          next.kind = "error";
          next.message = res.message || "未知解析结果";
      }
      this.share = next;
      // 登录引导面板开着时轮询登录态，扫码成功后自动重解析
      if (next.kind === "need_login") this.startLoginWatch();
      else this.stopLoginWatch();
    },

    /* ---------------- 文件夹下钻 / 多选 ---------------- */
    /* 勾选 / 取消勾选一个文件（可跨目录累积） */
    toggleFile(f) {
      if (!f || f.is_dir) return;
      const idx = this.share.selected.indexOf(f.key);
      if (idx >= 0) {
        this.share.selected.splice(idx, 1);
        delete this.share.selectedMap[f.key];
      } else {
        this.share.selected.push(f.key);
        this.share.selectedMap[f.key] = f;
      }
      this.syncFilenameField();
    },

    /* 勾选数量变化时同步文件名输入框：恰好 1 个时回填，其余清空 */
    syncFilenameField() {
      const n = this.share.selected.length;
      if (n === 1) {
        const f = this.share.selectedMap[this.share.selected[0]];
        if (f) this.form.filename = f.name;
      } else {
        this.form.filename = "";
      }
    },

    isAllCurrentSelected() {
      const files = this.share.files.filter((f) => !f.is_dir);
      return (
        files.length > 0 &&
        files.every((f) => this.share.selected.indexOf(f.key) >= 0)
      );
    },

    toggleSelectAll() {
      const files = this.share.files.filter((f) => !f.is_dir);
      const all = this.isAllCurrentSelected();
      for (const f of files) {
        const idx = this.share.selected.indexOf(f.key);
        if (all) {
          if (idx >= 0) this.share.selected.splice(idx, 1);
          delete this.share.selectedMap[f.key];
        } else if (idx < 0) {
          this.share.selected.push(f.key);
          this.share.selectedMap[f.key] = f;
        }
      }
      this.syncFilenameField();
    },

    selectedCount() {
      return this.share.selected.length;
    },

    selectedSize() {
      return Object.values(this.share.selectedMap).reduce(
        (sum, f) => sum + (f.size || 0),
        0
      );
    },

    /* 点文件夹进入 */
    async enterDir(f) {
      if (!f || !f.is_dir || this.share.dirLoading) return;
      await this.loadDir(f.key, true, f.name);
    },

    /* 点面包屑回到某一级 */
    async goCrumb(index) {
      const crumb = this.share.crumbs[index];
      if (!crumb || crumb.fid === this.share.currentDir || this.share.dirLoading)
        return;
      await this.loadDir(crumb.fid, false, "", index);
    },

    async loadDir(fid, push, name, crumbIndex = -1) {
      this.share.dirLoading = true;
      try {
        const cached = this.share.dirCache[fid];
        if (cached) {
          this.share.files = cached;
        } else {
          const res = await this.api().list_share_dir({
            site: this.share.site,
            share_url: this.share.share_url,
            share_title: this.share.share_title,
            pwd_id: this.share.pwd_id,
            stoken: this.share.stoken,
            referer: this.share.referer,
            dir_fid: fid,
          });
          // need_login / error 等非 share 状态交回面板状态机
          if (!res || res.kind !== "share") {
            this.applyResolveResult(res || { kind: "error", message: "读取目录失败" });
            return;
          }
          this.share.files = res.files || [];
          if (res.notes && res.notes.length) this.share.notes = res.notes;
          this.share.dirCache[fid] = this.share.files;
        }
        this.share.currentDir = fid;
        if (push) {
          this.share.crumbs.push({ fid, name });
        } else if (crumbIndex >= 0) {
          this.share.crumbs = this.share.crumbs.slice(0, crumbIndex + 1);
        }
      } catch (e) {
        this.showToast(
          "读取目录失败：" + (e && e.message ? e.message : e),
          "error"
        );
      } finally {
        this.share.dirLoading = false;
      }
    },

    async openShareLogin() {
      try {
        await this.api().open_session(
          this.share.site || this.form.sessionSite,
          this.form.url
        );
        this.showToast("登录窗口已打开，扫码完成后点「我已登录，重新解析」", "info");
        setTimeout(() => this.refreshSession(false), 1200);
      } catch (e) {
        this.showToast(
          "打开登录窗口失败：" + (e && e.message ? e.message : e),
          "error"
        );
      }
    },

    /* ---------------- 内置浏览器会话（WebView2） ---------------- */
    async loadSessionSites() {
      try {
        this.sessionSites = (await this.api().session_sites()) || [];
      } catch (e) {
        this.sessionSites = [];
      }
    },

    /* 按当前链接的域名自动切换站点并刷新登录态（防抖，避免每敲一个字符就问一次） */
    scheduleSessionRefresh() {
      clearTimeout(this.sessionTimer);
      this.sessionTimer = setTimeout(() => this.refreshSession(true), 350);
    },

    async refreshSession(autoSite = false) {
      if (!this.api()) return null;
      const url = (this.form.url || "").trim();
      const wasLoggedIn = !!(this.session && this.session.logged_in);
      try {
        const state = await this.api().session_state(url, autoSite ? "" : this.form.sessionSite);
        this.session = state || this.session;
        if (autoSite && state && state.site) this.form.sessionSite = state.site;
        // 扫码登录完成后（未登录 → 已登录）而面板还停在登录引导上：自动重解析一次，
        // 不用用户再手点「我已登录，重新解析」。只做一次，避免失败时反复打接口。
        if (
          state &&
          state.logged_in &&
          !wasLoggedIn &&
          this.share.kind === "need_login" &&
          !this.share.resolving
        ) {
          this.resolveLink();
        }
        return state;
      } catch (e) {
        /* 静默：状态查询失败不应打断输入 */
        return null;
      }
    },

    /* 登录引导面板开着时轮询登录态：扫码成功后自动进入解析 */
    startLoginWatch() {
      this.stopLoginWatch();
      this.loginWatcher = setInterval(() => {
        if (!this.api() || this.share.kind !== "need_login") {
          this.stopLoginWatch();
          return;
        }
        if (this.share.resolving) return;
        this.refreshSession(true);
      }, 2500);
    },

    stopLoginWatch() {
      if (this.loginWatcher) {
        clearInterval(this.loginWatcher);
        this.loginWatcher = null;
      }
    },

    async openSession() {
      try {
        const r = await this.api().open_session(this.form.sessionSite, (this.form.url || "").trim());
        if (r && r.site) this.form.sessionSite = r.site;
        this.showToast(
          `${(r && r.name) || "网盘"}登录窗口已打开，扫码登录后点「读取会话凭据」`,
          "info"
        );
        setTimeout(() => this.refreshSession(false), 1200);
      } catch (e) {
        this.showToast("打开登录窗口失败：" + (e && e.message ? e.message : e), "error");
      }
    },

    async captureSession() {
      const url = (this.form.url || "").trim();
      if (!url) return this.showToast("请先填写下载直链，再读取会话凭据", "error");
      try {
        const c = await this.api().capture_credentials(url, this.form.sessionSite);
        if (c && c.cookie) {
          this.form.cookie = c.cookie;
          if (c.user_agent) this.form.userAgent = c.user_agent;
          if (c.referer) this.form.referer = c.referer;
          // 用真实 Edge UA 时不再发 Chrome 客户端提示
          this.form.useClientHints = false;
          this.session = Object.assign({}, this.session, c);
          this.showToast(
            c.logged_in
              ? `已载入 ${c.site_name || "网盘"} 会话：${c.cookie_count} 个 Cookie`
              : `${c.site_name || "网盘"} 未识别到登录标记，已载入 ${c.cookie_count} 个 Cookie，先用现有会话`,
            c.logged_in ? "success" : "info"
          );
        } else {
          this.session = Object.assign({}, this.session, c || {});
          this.showToast((c && c.note) || "未能读取会话凭据", "error");
        }
      } catch (e) {
        this.showToast("读取会话凭据失败：" + (e && e.message ? e.message : e), "error");
      }
    },

    /* 直链诊断：Cookie × Referer 四种组合各打一次，直接告诉到底缺什么 */
    async diagnoseLink() {
      const url = (this.form.url || "").trim();
      if (!url) return this.showToast("请先填写下载直链", "error");
      this.showToast("正在诊断直链…", "info");
      try {
        const r = await this.api().diagnose_link({
          url,
          cookie: this.form.cookie,
          referer: this.form.referer,
          user_agent: this.form.userAgent,
          extra_headers: this.form.extraHeaders,
          use_client_hints: this.form.useClientHints,
          verify_tls: this.form.verifyTls,
          send_default_referer: true,
        });
        const brief = (r.combos || [])
          .map((c) => `${this.comboText(c.label)}=${c.status || "ERR"}`)
          .join("｜");
        this.showToast(`${brief}。${r.verdict}`, r.best ? "success" : "error");
      } catch (e) {
        this.showToast("诊断失败：" + (e && e.message ? e.message : e), "error");
      }
    },

    /* 针对某个已存在任务诊断（用它自己的配置，避免表单值与任务不一致） */
    async diagnoseTask(t) {
      if (this.diagBusy[t.id]) return;
      this.diagBusy = Object.assign({}, this.diagBusy, { [t.id]: true });
      this.showToast("诊断中：会分别尝试 4 种请求头组合…", "info");
      try {
        const r = await this.api().diagnose_task(t.id);
        const prev = this.diags[t.id] || {};
        this.diags = Object.assign({}, this.diags, {
          [t.id]: Object.assign({}, prev, r),
        });
        this.showToast(r.verdict, r.best ? "success" : "error");
      } catch (e) {
        this.showToast("诊断失败：" + (e && e.message ? e.message : e), "error");
      } finally {
        const next = Object.assign({}, this.diagBusy);
        delete next[t.id];
        this.diagBusy = next;
      }
    },

    /* 深度诊断：请求头组合（4 组）+ 传输栈（httpx / chrome-tls / curl.exe / WebView2） */
    async deepDiagnose(t) {
      if (this.diagBusy[t.id]) return;
      this.diagBusy = Object.assign({}, this.diagBusy, { [t.id]: true });
      this.showToast("深度诊断进行中：4 种请求头组合 + 4 种传输栈，约需 10 秒…", "info");
      try {
        let combos = {};
        let transport = null;
        let browser = null;
        try {
          combos = await this.api().diagnose_task(t.id);
        } catch (e) {
          combos = { combos: [], verdict: "请求头组合诊断失败：" + e };
        }
        try {
          transport = await this.api().transport_probe(t.id);
        } catch (e) {
          transport = { transports: [], error: String(e) };
        }
        try {
          browser = await this.api().browser_probe(t.id);
        } catch (e) {
          browser = { name: "WebView2(Chromium)", error: String(e) };
        }

        const list = [].concat(
          (transport && transport.transports) || [],
          browser && browser.name ? [browser] : []
        );
        const deepVerdict = this.transportVerdict(list);
        const cfOk = list.some(
          (r) =>
            (r.name || "").indexOf("curl_cffi") >= 0 &&
            (r.status === 200 || r.status === 206)
        );
        this.diags = Object.assign({}, this.diags, {
          [t.id]: Object.assign({}, combos, {
            transports: list,
            deepVerdict,
            suggestTransport: cfOk ? "curl_cffi" : "",
            curlBackend: (transport && transport.curl_backend) || "",
          }),
        });
        const anyOk = list.some((r) => r && (r.status === 200 || r.status === 206));
        this.showToast(deepVerdict, anyOk ? "success" : "error");
      } catch (e) {
        this.showToast("深度诊断失败：" + (e && e.message ? e.message : e), "error");
      } finally {
        const next = Object.assign({}, this.diagBusy);
        delete next[t.id];
        this.diagBusy = next;
      }
    },

    transportVerdict(rows) {
      const ok = (r) => r && (r.status === 200 || r.status === 206);
      if (!rows.length) return "传输层探测没有返回结果。";
      const passing = rows.filter(ok);
      const failing = rows.filter((r) => r && !ok(r));
      const brief = (list) =>
        list.map((r) => `${this.trName(r.name)} ${r.status || "ERR"}`).join("｜");
      if (!passing.length) {
        return (
          `四种传输栈全部被拒（${brief(rows)}）：连浏览器栈都过不去，` +
          "说明这条直链本身已失效或被服务端绑定，建议重新取一条直链再试。"
        );
      }
      if (passing.some((r) => (r.name || "").indexOf("WebView2") >= 0) && failing.length) {
        return (
          `浏览器栈能下、${failing.map((r) => this.trName(r.name)).join("、")} 不行` +
          `（${brief(rows)}）→ 服务端在拦非浏览器 TLS 指纹，问题在传输层，不是 Cookie/Referer。`
        );
      }
      if (passing.some((r) => (r.name || "").indexOf("curl_cffi") >= 0)) {
        return (
          `curl_cffi（Chrome TLS 指纹）能过（${brief(rows)}）→ 就是 TLS 指纹拦截，` +
          "点「改用 Chrome 指纹重试」即可。"
        );
      }
      if (passing.some((r) => (r.name || "").indexOf("curl") >= 0)) {
        return `curl.exe(Schannel) 能过（${brief(rows)}）→ 可以改用系统 TLS 栈下载。`;
      }
      return `可用传输栈：${passing.map((r) => this.trName(r.name)).join("、")}（${brief(rows)}）。`;
    },

    trName(name) {
      const map = {
        "httpx(当前)": "httpx",
        "httpx(Chrome TLS)": "httpx+ChromeTLS",
        "curl.exe(Schannel)": "curl(Schannel)",
        "curl_cffi(Chrome)": "curl_cffi(Chrome)",
        "WebView2(Chromium)": "浏览器栈",
      };
      return map[name] || name;
    },

    async retryWithTransport(t) {
      const d = this.diags[t.id];
      const kind = d && d.suggestTransport;
      if (!kind) return;
      try {
        await this.api().retry_with_transport(t.id, kind);
        this.showToast("已切换到 Chrome TLS 指纹后端并重试", "success");
      } catch (e) {
        this.showToast("切换失败：" + (e && e.message ? e.message : e), "error");
      }
    },

    async applyFix(t) {
      const d = this.diags[t.id];
      if (!d || !d.best) return;
      const useReferer = d.best === "cookie+referer" || d.best === "referer";
      try {
        await this.api().apply_probe_fix(t.id, useReferer);
        const next = Object.assign({}, this.diags);
        delete next[t.id];
        this.diags = next;
        this.showToast("已按诊断结果调整并重试", "success");
      } catch (e) {
        this.showToast("修正失败：" + (e && e.message ? e.message : e), "error");
      }
    },

    comboText(label) {
      const map = {
        "cookie+referer": "Cookie+Referer",
        cookie: "仅 Cookie",
        referer: "仅 Referer",
        plain: "裸请求",
      };
      return map[label] || label;
    },

    /* 解析浏览器 Copy as cURL，自动回填 URL / UA / Referer / Cookie / 额外头 */
    async parseCurl() {
      const raw = (this.form.curlText || "").trim();
      if (!raw) return this.showToast("请先粘贴 cURL 命令", "error");
      try {
        const r = await this.api().parse_curl(raw);
        if (r.url) this.form.url = r.url;
        if (r.user_agent) this.form.userAgent = r.user_agent;
        if (r.referer) this.form.referer = r.referer;
        if (r.cookie) this.form.cookie = r.cookie;
        if (r.filename) this.form.filename = r.filename;
        if (r.use_http2 != null) this.form.useHttp2 = !!r.use_http2;
        this.form.verifyTls = !!r.verify_tls;
        this.form.extraHeaders = r.extra_headers || {};
        const n = Object.keys(this.form.extraHeaders).length;
        const note = n ? `，另载入 ${n} 个额外请求头` : "";
        if (r.warnings && r.warnings.length) {
          this.showToast(`已填充${note}；提示：${r.warnings.join("；")}`, "info");
        } else {
          this.showToast(`cURL 解析完成${note}`, "success");
        }
      } catch (e) {
        this.showToast("解析失败：" + (e && e.message ? e.message : e), "error");
      }
    },

    async submitForm() {
      const f = this.form;
      if (!f.url.trim()) return this.showToast("请填写下载链接", "error");
      if (!f.saveDir.trim()) return this.showToast("请选择保存目录", "error");

      if (this.share.kind === "need_login") {
        // 登录态是启发式判断：先复查一次会话，别让已经登录的用户卡在登录引导上
        const st = await this.refreshSession(true);
        if (!st || (!st.logged_in && !st.has_cookie)) {
          return this.showToast("请先完成登录，再点「我已登录，重新解析」", "error");
        }
        await this.resolveLink();
        if (this.share.kind === "need_login") {
          return this.showToast(
            this.share.message || "仍未识别到登录态，请重新扫码登录",
            "error"
          );
        }
        if (this.share.kind === "error") return;
      }
      if (
        this.share.kind === "passcode_required" ||
        this.share.kind === "passcode_wrong"
      ) {
        return this.showToast("请先输入提取码完成解析", "error");
      }

      let sharePayload = null;
      if (this.share.kind === "share") {
        if (!this.share.selected.length) {
          return this.showToast("请勾选要下载的文件（可进入文件夹后勾选）", "error");
        }
        sharePayload = {
          site: this.share.site,
          share_url: this.share.share_url || f.url,
          share_title: this.share.share_title,
          pwd_id: this.share.pwd_id,
          stoken: this.share.stoken,
          referer: this.share.referer,
          // 只回传勾选的文件，后端按 selected 签发
          files: Object.values(this.share.selectedMap),
          selected: this.share.selected.slice(),
        };
      }

      try {
        const opts = {
          url: f.url,
          save_dir: f.saveDir,
          filename: f.filename,
          concurrency: f.concurrency,
          chunk_size: f.chunkSize,
          user_agent: f.userAgent,
          referer: f.referer,
          cookie: f.cookie,
          extra_headers: f.extraHeaders,
          use_http2: f.useHttp2,
          verify_tls: f.verifyTls,
          auto_credentials: f.autoCredentials,
          use_client_hints: f.useClientHints,
          prefer_ipv4: f.preferIpv4,
          adaptive_concurrency: f.adaptiveConcurrency,
        };
        if (sharePayload) opts.share = sharePayload;

        const t = await this.api().add_download(opts);

        // 后端检测到未登录：已自动开窗，把面板切到登录引导
        if (t && t.need_login) {
          this.applyResolveResult({
            kind: "need_login",
            site: t.site,
            site_name: t.site_name,
            login_url: t.login_url,
            message: t.message,
            note: t.note,
          });
          this.showToast(t.message || "需要先登录网盘", "warn");
          return;
        }
        if (t && t.error) {
          // 分享面板里保留失败原因（例如「转存失败：网盘空间不足」），别一闪就没
          if (this.share.kind === "share") this.share.message = t.error;
          this.showToast(t.error, "error");
          return;
        }

        await this._persistSettings();
        // 分享批量：后端一次返回多个任务快照；直链：单个快照
        const snapshots = t && t.created ? t.created : [t];
        for (const snap of snapshots) this.upsertFull(snap);
        this.form = defaultForm();
        // 保留最近一次的目录与参数（重启后由设置回填）
        this.form.saveDir = this.settings.save_dir;
        this.form.concurrency = this.settings.concurrency;
        this.form.chunkSize = this.settings.chunk_size;
        this.form.preferIpv4 = !!this.settings.prefer_ipv4;
        this.form.adaptiveConcurrency = this.settings.adaptive_concurrency !== false;
        this.resetShare();
        this.showForm = false;
        this.showAdvanced = false;
        this.showToast(
          snapshots.length > 1 ? `已创建 ${snapshots.length} 个下载任务` : "任务已开始",
          "success"
        );
      } catch (e) {
        this.showToast("创建失败：" + (e && e.message ? e.message : e), "error");
      }
    },

    async pause(t) {
      try {
        await this.api().pause_task(t.id);
      } catch (e) {
        this.showToast("暂停失败：" + e, "error");
      }
    },

    async resume(t) {
      try {
        await this.api().resume_task(t.id, {});
      } catch (e) {
        this.showToast("操作失败：" + e, "error");
      }
    },

    /* 下载中热改并发数（会关掉该任务的自适应，以手动值为准）。
     * 适用场景：某些 CDN 把「一个直链太多并发」当成风控，降并发反而更快。 */
    async applyConcurrency(t) {
      const value = Number(t._concInput || t.current_concurrency || t.concurrency || 64);
      if (!isFinite(value) || value < 1) return this.showToast("并发数不合法", "error");
      try {
        const snap = await this.api().set_task_concurrency(t.id, Math.floor(value));
        if (snap && snap.id) this.upsertFull(snap);
        this.showToast(`并发数已改为 ${Math.floor(value)}`, "success");
      } catch (e) {
        this.showToast("改并发失败：" + (e && e.message ? e.message : e), "error");
      }
    },

    async cancel(t) {
      if (!confirm(`确定停止任务「${t.filename || t.url}」吗？\n进度会保留，之后可继续。`))
        return;
      try {
        await this.api().cancel_task(t.id);
      } catch (e) {
        this.showToast("停止失败：" + e, "error");
      }
    },

    async remove(t) {
      const name = t.filename || t.url;
      if (!confirm(`确定移除任务「${name}」吗？\n\n确定 = 仅移除记录（保留文件）；取消 = 返回。`))
        return;
      let deleteFiles = false;
      if (t.status !== "completed") {
        deleteFiles = confirm("是否同时删除已下载的文件与进度？\n\n确定 = 删除文件；取消 = 保留文件。");
      }
      try {
        await this.api().remove_task(t.id, deleteFiles);
        this.order = this.order.filter((id) => id !== t.id);
        delete this.tasks[t.id];
      } catch (e) {
        this.showToast("移除失败：" + e, "error");
      }
    },

    async openFolder(t) {
      try {
        const target = t.status === "completed" ? t.output_path : t.save_dir;
        await this.api().open_folder(target);
      } catch (e) {
        this.showToast("无法打开目录：" + e, "error");
      }
    },

    /* ---------------- 后端事件 ---------------- */
    onBackendEvent(ev) {
      if (ev.type === "task_update") {
        const t = ev.task;
        const existing = this.tasks[t.id];
        if (existing) {
          if (t.completed_delta) {
            for (const i of t.completed_delta) existing._done.add(i);
          }
          const done = existing._done;
          Object.assign(existing, t);
          existing._done = done;
        } else {
          this.upsertFull(t);
        }
      } else if (ev.type === "task_removed") {
        this.order = this.order.filter((id) => id !== ev.id);
        delete this.tasks[ev.id];
      } else if (ev.type === "toast") {
        this.showToast(ev.message, ev.level || "info");
      }
    },

    upsertFull(t) {
      if (!this.tasks[t.id]) {
        t._done = new Set(t.completed || []);
        // 输入框默认填**实际生效**的并发（自适应收敛后是 8，不是设定值 64），
        // 否则用户盲点「改并发」会把 64 写回去、反而取消了刚收敛的结果。
        t._concInput = t.current_concurrency || t.concurrency;
        this.tasks[t.id] = t;
        this.order.push(t.id);
      } else {
        const done = this.tasks[t.id]._done;
        for (const i of t.completed || []) done.add(i);
        Object.assign(this.tasks[t.id], t);
        this.tasks[t.id]._done = done;
        if (!this.tasks[t.id]._concInput) {
          this.tasks[t.id]._concInput = t.current_concurrency || t.concurrency;
        }
      }
    },

    async loadAll() {
      const list = await this.api().list_tasks();
      for (const t of list) this.upsertFull(t);
    },

    /* ---------------- 分块网格 ---------------- */
    visibleCells(t) {
      const n = Math.min(t.chunk_count, this.maxCells);
      return Array.from({ length: n }, (_, i) => i);
    },

    cellClass(t, i) {
      if (t._done && t._done.has(i)) return "cell-done";
      if (t.active && t.active.includes(i)) return "cell-active";
      return "cell-pending";
    },

    /* ---------------- 格式化 ---------------- */
    hostOf(url) {
      try {
        return new URL(url).host;
      } catch {
        return "";
      }
    },

    statusText(s) {
      return STATUS_TEXT[s] || s;
    },

    formatBytes(n) {
      if (!n) return "0 B";
      const units = ["B", "KB", "MB", "GB", "TB"];
      let i = 0;
      n = Number(n);
      while (n >= 1024 && i < units.length - 1) {
        n /= 1024;
        i++;
      }
      return (i === 0 ? Math.round(n) : n.toFixed(1)) + " " + units[i];
    },

    formatEta(s) {
      if (s == null) return "--:--";
      s = Number(s);
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      const ss = Math.floor(s % 60);
      const pad = (x) => String(x).padStart(2, "0");
      return h ? `${h}:${pad(m)}:${pad(ss)}` : `${pad(m)}:${pad(ss)}`;
    },

    /* 速度是后端算好的「近 60 秒滑动平均」；不在传输中时后端会归零，
     * 这里统一显示「—」，避免暂停/停止/完成后挂着 "0 B/s" 让人以为卡住了。 */
    speedText(t) {
      if (t.status !== "downloading" && t.status !== "connecting") return "—";
      const v = Number(t.speed);
      if (!isFinite(v) || v < 0) return "—";
      return this.formatBytes(v) + "/s";
    },

    etaText(t) {
      if (t.status !== "downloading" && t.status !== "connecting") return "—";
      return this.formatEta(t.eta);
    },

    /* 并发显示：自适应收敛 / 被限速时「实际生效」会低于设定值，两个都显示 */
    concText(t) {
      const cur = Number(t.current_concurrency || t.concurrency || 0);
      const set = Number(t.concurrency || 0);
      if (!cur) return "—";
      if (set && cur !== set) return `${cur} / ${set}`;
      return t.adaptive ? `${cur}（自适应）` : `${cur}`;
    },

    showToast(message, type = "info") {
      this.toast = { message, type };
      clearTimeout(this.toastTimer);
      this.toastTimer = setTimeout(() => {
        this.toast = null;
      }, 3200);
    },
  },

  watch: {
    /* 粘贴链接后：自动识别站点刷新登录态 + 防抖解析分享链接 */
    "form.url"(value) {
      if (value && value.trim()) {
        this.scheduleSessionRefresh();
        this.scheduleResolve();
      } else {
        this.resetShare();
      }
    },
  },

  mounted() {
    /* pywebview 的 js_api 是页面加载后异步注入的：早于它调用会拿到 undefined，
     * 因此等 pywebviewready（或已注入则立即执行）。 */
    const boot = () => {
      if (!this.api()) return;
      this.bootSettings()
        .then(() => this.loadAll().catch(() => {}))
        .then(() => this.loadSessionSites())
        .then(() => this.refreshSession(true));
    };

    if (this.api()) {
      boot();
      return;
    }
    window.addEventListener("pywebviewready", boot, { once: true });
    setTimeout(() => {
      if (!this.api()) this.showToast("请通过 main.py 启动应用", "error");
    }, 3000);
  },
});

const vm = app.mount("#app");
window.appBridge = (ev) => vm.onBackendEvent(ev);
// 暴露实例便于前端调试与自动化冒烟（不影响打包逻辑）
window.__vm = vm;
