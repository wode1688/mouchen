"use strict";

const TOKEN_KEY = "mouchen.web.accessToken";
const DEVICE_KEY = "mouchen.web.installationId";
const LOCALE_PREVIEW_KEY = "mouchen.web.localePreview";
const DEFAULT_LOCALE = "zh-CN";

function normalizeLocale(value) {
  return String(value || "").toLowerCase().startsWith("en") ? "en-US" : DEFAULT_LOCALE;
}

function initialLocale() {
  try {
    return normalizeLocale(sessionStorage.getItem(LOCALE_PREVIEW_KEY) || navigator.language);
  } catch (_error) {
    return normalizeLocale(navigator.language);
  }
}

const STATIC_EN = Object.freeze({
  "AI替身：主动理解局势、发现问题并给出可执行建议。": "My AI Twin proactively understands your situation, finds important problems, and suggests a clear next step.",
  "AI替身网页版需要启用 JavaScript。": "My AI Twin Web requires JavaScript.",
  "AI替身首页": "My AI Twin home",
  "AI替身": "My AI Twin",
  "谋": "M",
  "主动发现，审慎建言": "Proactive insight, careful counsel",
  "你的个人 AI 幕僚": "Your personal AI chief of staff",
  "重要的事，": "Important things",
  "不必等你想起来。": "should not wait to be remembered.",
  "AI替身会结合你的目标和授权信息，主动发现偏差、风险与机会，并给出第一步行动。": "My AI Twin connects your goals with the information you authorize, identifies deviations, risks, and opportunities, and gives you a concrete first move.",
  "隐私原则": "Privacy principles",
  "每个账号的数据相互隔离": "Each account's data is isolated",
  "云分析与完整原文传输默认关闭": "Cloud analysis and full-text sharing are off by default",
  "建议不会未经确认代你执行": "Nothing is executed on your behalf without confirmation",
  "选择语言": "Choose language",
  "账号操作": "Account actions",
  "登录": "Sign in",
  "邀请码注册": "Register with invite",
  "欢迎回来": "Welcome back",
  "登录后可查看与你账号绑定的目标和建言。": "Sign in to see the goals and counsel linked to your account.",
  "用户名": "Username",
  "密码": "Password",
  "显示密码": "Show password",
  "显示": "Show",
  "登录AI替身": "Sign in to My AI Twin",
  "创建账号": "Create account",
  "注册需要管理员发放的一次性邀请码。": "Registration requires a one-time invitation code from an administrator.",
  "3–64 个字符，不能包含空格。": "3–64 characters, with no spaces.",
  "至少 10 个字符，请使用独立密码。": "Use at least 10 characters and a unique password.",
  "确认密码": "Confirm password",
  "邀请码": "Invitation code",
  "注册并登录": "Create account and sign in",
  "为保护账号，本网页关闭后需要重新登录。访问令牌只保存在当前浏览器会话中。": "For your protection, closing this page signs you out. The access token exists only in this browser session.",
  "AI替身总览": "My AI Twin overview",
  "个人局势台": "Personal situation desk",
  "主导航": "Main navigation",
  "总": "O",
  "总览": "Overview",
  "言": "C",
  "建言": "Counsel",
  "标": "G",
  "目标": "Goals",
  "设": "S",
  "设置": "Settings",
  "正在连接": "Connecting",
  "AI替身 · 个人工作台": "My AI Twin · Personal workspace",
  "会话校验中": "Checking session",
  "刷新": "Refresh",
  "退出": "Sign out",
  "待处理建言": "Active counsel",
  "需要你决定的建议": "Suggestions awaiting your decision",
  "重要提醒": "Important alerts",
  "即时送达的建言": "Counsel delivered immediately",
  "目标坐标": "Goal anchors",
  "AI替身判断的依据": "What grounds My AI Twin's judgment",
  "服务状态": "Service status",
  "检查中": "Checking",
  "模型通道检查中": "Checking model connection",
  "当前最值得关注": "What matters most now",
  "AI替身的判断": "My AI Twin's judgment",
  "立即复盘": "Review now",
  "正在读取局势……": "Reading the situation…",
  "你的原话": "In your own words",
  "尚未建立目标": "No goals yet",
  "证据 · 动作 · 结果": "Evidence · Action · Outcome",
  "全部建言": "All counsel",
  "筛选": "Filter",
  "全部": "All",
  "待处理": "Active",
  "已采纳": "Adopted",
  "已结束": "Closed",
  "正在读取建言……": "Loading counsel…",
  "现行坐标": "Current anchors",
  "目标版本": "Goal versions",
  "录入你的原话": "Record your own words",
  "新增目标": "Add goal",
  "目标名称": "Goal name",
  "例如：完成第一个企业 AI 付费试点": "Example: complete the first paid enterprise AI pilot",
  "领域": "Domain",
  "例如：事业": "Example: business",
  "写下你判断成功与否的标准。AI替身只依据你的目标纠偏。": "Write the standard you will use to judge success. My AI Twin corrects course only against your goals.",
  "每周计划投入（小时）": "Planned weekly time (hours)",
  "可选": "Optional",
  "保存目标版本": "Save goal version",
  "本次浏览器会话": "This browser session",
  "AI 与数据授权": "AI and data permissions",
  "两个开关默认关闭。开启前请确认你理解相应数据会被发送给服务器配置的模型服务商。": "Both controls are off by default. Before enabling them, confirm that you understand the relevant data will be sent to the model provider configured by the server.",
  "允许云模型分析": "Allow cloud model analysis",
  "发送完成分析所需的最小上下文。关闭时，网页不会主动批准云分析。": "Send the minimum context needed for analysis. When off, the web app will not approve cloud analysis.",
  "开启云分析前请确认": "Confirm before enabling cloud analysis",
  "相关任务片段会离开当前设备，并由服务器配置的模型服务商处理。请勿发送无权共享的他人信息。": "Relevant task excerpts will leave this device and be processed by the model provider configured on the server. Do not share information you are not authorized to send.",
  "取消": "Cancel",
  "我理解并同意": "I understand and agree",
  "允许发送完整原始上下文": "Allow full original context",
  "可能包括未脱敏的屏幕、输入、语音或对话原文。风险更高，仅在确有需要时开启。": "This may include unredacted screen text, input, speech, or conversations. It carries greater risk and should be enabled only when necessary.",
  "这是高敏感授权": "This is a high-sensitivity permission",
  "完整原文可能包含身份、账号、通信对象和商业信息。发送后无法通过本设备撤回第三方已经接收的数据。": "Full original content may contain identity, account, contact, and business information. Once sent, this device cannot retract data already received by a third party.",
  "我确认内容属于我，或我有权发送，并接受完整原文离开设备。": "I confirm that the content is mine or that I am authorized to send it, and I accept that the full original content will leave this device.",
  "确认开启": "Enable",
  "授权只保留在当前浏览器标签会话；关闭页面后自动恢复为关闭。": "Permissions apply only to this browser tab session and turn off automatically when the page closes.",
  "账号": "Account",
  "当前会话": "Current session",
  "界面与建言语言": "Interface and counsel language",
  "此设置属于账号，并会同步到其他设备。已有建言和证据原文不会被机械翻译。": "This account setting syncs to your other devices. Existing counsel and original evidence are not mechanically translated.",
  "设备": "Device",
  "有效期": "Expires",
  "访问令牌不会写入本地长期存储，也不会被 Service Worker 缓存。": "The access token is never written to persistent local storage or cached by the Service Worker.",
  "退出当前账号": "Sign out of this account",
  "账号安全": "Account security",
  "密码与个人数据": "Password and personal data",
  "修改密码会撤销其他设备的旧会话。导出文件只在本次操作中生成，不会被网页离线缓存。": "Changing your password revokes old sessions on other devices. Export files are created only for this operation and are never cached offline by the web app.",
  "当前密码": "Current password",
  "新密码": "New password",
  "再次输入新密码": "Re-enter new password",
  "修改密码并撤销旧会话": "Change password and revoke old sessions",
  "导出个人数据": "Export personal data",
  "下载目标、事件、建言、反馈和出云审计，不含密码或访问令牌。": "Download goals, events, counsel, feedback, and cloud audit records. Passwords and access tokens are excluded.",
  "验证并下载": "Verify and download",
  "危险操作": "Danger zone",
  "永久删除账号": "Permanently delete account",
  "删除会撤销所有设备会话，并级联删除服务器中属于此账号的数据。此操作无法撤销。": "Deletion revokes all device sessions and removes this account's data from the server. This cannot be undone.",
  "再次输入当前密码": "Re-enter current password",
  "我确认永久删除账号和服务器中的个人数据。": "I confirm permanent deletion of this account and its personal data on the server.",
  "能力边界": "Capability boundaries",
  "AI替身不会做什么": "What My AI Twin will not do",
  "正在读取能力声明……": "Loading capability boundaries…",
  "iPhone 使用": "Using iPhone",
  "添加到主屏幕": "Add to Home Screen",
  "在 Safari 中打开本页面。": "Open this page in Safari.",
  "点击底部“分享”。": "Tap Share at the bottom.",
  "选择“添加到主屏幕”。": "Choose Add to Home Screen.",
  "安装后以独立窗口运行；账号数据仍从服务器读取，不会写入离线缓存。": "After installation it runs in its own window. Account data still comes from the server and is not stored in the offline cache.",
  "纠正AI替身的方向": "Correct My AI Twin's direction",
  "你希望以后怎么建议？": "How should My AI Twin advise you in the future?",
  "这段话会作为建言反馈保存，请不要填写密码或验证码。": "This note will be saved as feedback. Do not enter passwords or verification codes.",
  "例如：这类事情只在影响企业 AI 业务时提醒我。": "Example: only remind me about this when it affects my enterprise AI business.",
  "发送方向": "Send direction",
});

const VIEW_TITLES = Object.freeze({
  overview: ["总览", "Overview"],
  advice: ["建言", "Counsel"],
  goals: ["目标", "Goals"],
  settings: ["设置", "Settings"],
});
const STATUS_LABELS = Object.freeze({
  active: ["待处理", "Active"],
  adopted: ["已采纳", "Adopted"],
  dismissed: ["已结束", "Closed"],
  withdrawn: ["已撤回", "Withdrawn"],
  verified: ["已核验", "Verified"],
});
const BOUNDARY_LABELS = Object.freeze({
  no_sandbox_bypass: ["不绕过 Android 或 iOS 的应用沙箱", "Does not bypass the Android or iOS app sandbox"],
  no_secure_window_capture: ["不捕获系统标记为安全的窗口", "Does not capture windows marked secure by the operating system"],
  no_password_capture: ["不采集密码框内容", "Does not collect password-field content"],
  no_covert_recording: ["不进行隐藏录音", "Does not record covertly"],
  no_unconfirmed_external_mutation: ["未经确认不修改外部系统", "Does not change external systems without confirmation"],
  no_wechat_private_db_access: ["不直接读取微信等应用的私有数据库", "Does not directly read private databases belonging to WeChat or other apps"],
});

function bilingual(pair) {
  return Array.isArray(pair) ? pair[state.locale === "en-US" ? 1 : 0] : String(pair ?? "");
}

function tr(zh, en) {
  return state.locale === "en-US" ? en : zh;
}

const state = {
  locale: initialLocale(),
  session: null,
  health: null,
  goals: [],
  advice: [],
  capabilities: null,
  consent: {cloud: false, raw: false},
  guidanceAdviceId: null,
  refreshing: false,
  localeGeneration: 0,
  localeMutationPending: false,
  epoch: 0,
  requestControllers: new Set(),
};

const staticTextSources = new WeakMap();
const staticAttributeSources = new WeakMap();

function replaceStaticText(source) {
  if (state.locale !== "en-US") return source;
  const match = String(source).match(/^(\s*)(.*?)(\s*)$/s);
  if (!match || !match[2]) return source;
  const translated = STATIC_EN[match[2]];
  return translated ? `${match[1]}${translated}${match[3]}` : source;
}

function applyStaticLanguage() {
  document.documentElement.lang = state.locale;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    if (["SCRIPT", "STYLE"].includes(node.parentElement?.tagName)) continue;
    if (!staticTextSources.has(node)) staticTextSources.set(node, node.nodeValue || "");
    node.nodeValue = replaceStaticText(staticTextSources.get(node));
  }
  document.querySelectorAll("[placeholder], [aria-label], [title], meta[content]").forEach(element => {
    let originals = staticAttributeSources.get(element);
    if (!originals) {
      originals = {};
      ["placeholder", "aria-label", "title", "content"].forEach(name => {
        if (element.hasAttribute(name)) originals[name] = element.getAttribute(name);
      });
      staticAttributeSources.set(element, originals);
    }
    Object.entries(originals).forEach(([name, value]) => {
      element.setAttribute(name, state.locale === "en-US" && STATIC_EN[value] ? STATIC_EN[value] : value);
    });
  });
  const authSelector = byId("authLanguageSelect");
  const accountSelector = byId("accountLanguageSelect");
  if (authSelector) authSelector.value = state.locale;
  if (accountSelector) accountSelector.value = state.locale;
  const description = document.querySelector('meta[name="description"]');
  if (description) {
    description.content = tr(
      "AI替身：主动理解局势、发现问题并给出可执行建议。",
      "My AI Twin proactively understands your situation, finds important problems, and suggests a clear next step.",
    );
  }
  const appleTitle = document.querySelector('meta[name="apple-mobile-web-app-title"]');
  if (appleTitle) appleTitle.content = tr("AI替身", "My AI Twin");
  const manifest = document.querySelector('link[rel="manifest"]');
  if (manifest) {
    manifest.href = state.locale === "en-US"
      ? "/static/manifest-en.webmanifest"
      : "/static/manifest.webmanifest";
  }
}

function applyLocale(locale, {rerender = true} = {}) {
  state.locale = normalizeLocale(locale);
  try {
    sessionStorage.setItem(LOCALE_PREVIEW_KEY, state.locale);
  } catch (_error) {
    // Language still applies for the current in-memory page.
  }
  applyStaticLanguage();
  if (state.session && rerender) {
    renderSession();
    renderAll();
    showView(location.hash.slice(1) || "overview", false);
  } else if (!state.session) {
    document.title = tr("登录 · AI替身", "Sign in · My AI Twin");
  }
}

class StaleSessionError extends Error {
  constructor() {
    super(tr("登录状态已经变化", "The sign-in state has changed"));
    this.name = "StaleSessionError";
  }
}

const byId = id => document.getElementById(id);
const accessToken = () => sessionStorage.getItem(TOKEN_KEY) || "";
const newId = () => typeof crypto.randomUUID === "function"
  ? crypto.randomUUID()
  : `${Date.now().toString(36)}-${crypto.getRandomValues(new Uint32Array(2)).join("-")}`;

function installationId() {
  try {
    const existing = localStorage.getItem(DEVICE_KEY);
    if (existing) return existing;
    const created = `web-${newId()}`;
    localStorage.setItem(DEVICE_KEY, created);
    return created;
  } catch (_error) {
    let fallback = sessionStorage.getItem(DEVICE_KEY);
    if (!fallback) {
      fallback = `web-${newId()}`;
      sessionStorage.setItem(DEVICE_KEY, fallback);
    }
    return fallback;
  }
}

function deviceName() {
  const mobile = /iPhone|iPad|Android/i.test(navigator.userAgent);
  return mobile ? tr("手机网页", "Mobile web") : tr("电脑网页", "Desktop web");
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

function formatDate(value, includeYear = false) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return "—";
  return parsed.toLocaleString(state.locale, {
    ...(includeYear ? {year: "numeric"} : {}),
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatLevel(value) {
  return typeof value === "number" ? `L${value}` : String(value || "L1").toUpperCase();
}

function toast(message) {
  const element = byId("toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove("show"), 2800);
}

function setBusy(button, busy, busyLabel) {
  if (!button) return;
  if (busy) {
    button.dataset.originalLabel = button.textContent;
    button.textContent = busyLabel;
    button.disabled = true;
  } else {
    const original = button.dataset.originalLabel || button.textContent;
    button.textContent = state.locale === "en-US" && STATIC_EN[original]
      ? STATIC_EN[original]
      : original;
    button.disabled = false;
  }
}

async function errorMessage(response) {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
    if (body.detail?.message) return body.detail.message;
  } catch (_error) {
    // Non-JSON errors intentionally fall back to a generic message.
  }
  return tr(`请求失败（${response.status}）`, `Request failed (${response.status})`);
}

function cloudApprovalHeaders() {
  return {
    "X-Proactive-Cloud-Approved": state.consent.cloud ? "true" : "false",
    "X-Raw-Cloud-Approved": state.consent.cloud && state.consent.raw ? "true" : "false",
  };
}

function beginSessionTransition() {
  state.epoch += 1;
  state.requestControllers.forEach(controller => controller.abort());
  state.requestControllers.clear();
  state.refreshing = false;
  return state.epoch;
}

function requireSameOriginPath(path) {
  if (typeof path !== "string" || !path.startsWith("/") || path.startsWith("//") || path.includes("\\")) {
    throw new Error(tr("请求地址必须是本站相对路径", "The request must use a same-site relative path"));
  }
  const url = new URL(path, window.location.origin);
  if (url.origin !== window.location.origin) {
    throw new Error(tr("拒绝向其他站点发送数据", "Refused to send data to another site"));
  }
  return url.href;
}

async function apiRequest(path, options = {}) {
  const {
    authenticated = true,
    cloudAction = false,
    headers: suppliedHeaders = {},
    signal: suppliedSignal,
    ...fetchOptions
  } = options;
  const headers = new Headers(suppliedHeaders);
  const token = accessToken();
  const requestEpoch = state.epoch;
  const controller = new AbortController();
  const assertCurrent = () => {
    if (requestEpoch !== state.epoch || (authenticated && token !== accessToken())) {
      throw new StaleSessionError();
    }
  };

  if (suppliedSignal) {
    if (suppliedSignal.aborted) controller.abort();
    else suppliedSignal.addEventListener("abort", () => controller.abort(), {once: true});
  }
  if (authenticated) {
    if (!token) throw new Error(tr("请先登录", "Please sign in first"));
    headers.set("Authorization", `Bearer ${token}`);
  }
  headers.set("Accept-Language", state.locale);
  if (fetchOptions.body && !(fetchOptions.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (cloudAction) {
    Object.entries(cloudApprovalHeaders()).forEach(([name, value]) => headers.set(name, value));
  }

  state.requestControllers.add(controller);
  try {
    const response = await fetch(requireSameOriginPath(path), {
      ...fetchOptions,
      headers,
      signal: controller.signal,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
    });
    assertCurrent();
    if (response.redirected || new URL(response.url).origin !== window.location.origin) {
      throw new Error(tr("服务器返回了不安全的跳转", "The server returned an unsafe redirect"));
    }
    if (response.status === 401 && authenticated) {
      expireSession(tr("登录已失效，请重新登录。", "Your session has expired. Please sign in again."));
      throw new Error(tr("登录已失效", "Session expired"));
    }
    if (!response.ok) {
      const message = await errorMessage(response);
      assertCurrent();
      throw new Error(message);
    }
    if (response.status === 204) return null;
    const contentType = response.headers.get("content-type") || "";
    const result = contentType.includes("application/json")
      ? await response.json()
      : await response.text();
    assertCurrent();
    return result;
  } catch (error) {
    if (error?.name === "AbortError") throw new StaleSessionError();
    throw error;
  } finally {
    state.requestControllers.delete(controller);
  }
}

function showAuth(message = "", success = false) {
  byId("appScreen").hidden = true;
  byId("authScreen").hidden = false;
  const messageBox = byId("authMessage");
  messageBox.textContent = message;
  messageBox.hidden = !message;
  messageBox.classList.toggle("success", success);
  document.title = tr("登录 · AI替身", "Sign in · My AI Twin");
}

function showApp() {
  byId("authScreen").hidden = true;
  byId("appScreen").hidden = false;
  renderSession();
  showView(location.hash.slice(1) || "overview", false);
}

function expireSession(message) {
  beginSessionTransition();
  sessionStorage.removeItem(TOKEN_KEY);
  state.session = null;
  state.goals = [];
  state.advice = [];
  state.consent.cloud = false;
  state.consent.raw = false;
  resetConsentControls();
  showAuth(message);
}

async function validateSession() {
  if (!accessToken()) return false;
  try {
    const session = await apiRequest("/v1/session");
    state.session = session;
    applyLocale(session.locale || state.locale, {rerender: false});
    return true;
  } catch (error) {
    if (error instanceof StaleSessionError) throw error;
    if (accessToken()) {
      expireSession(tr("无法验证登录，请重新登录。", "Unable to verify the session. Please sign in again."));
    }
    return false;
  }
}

async function establishSession(token) {
  beginSessionTransition();
  sessionStorage.setItem(TOKEN_KEY, token);
  const valid = await validateSession();
  if (!valid) throw new Error(tr("服务器未接受本次登录", "The server did not accept this sign-in"));
  showApp();
  await refreshAll();
}

function switchAuthTab(tab) {
  const login = tab === "login";
  byId("loginForm").hidden = !login;
  byId("registerForm").hidden = login;
  byId("loginTab").classList.toggle("active", login);
  byId("registerTab").classList.toggle("active", !login);
  byId("loginTab").setAttribute("aria-selected", String(login));
  byId("registerTab").setAttribute("aria-selected", String(!login));
  byId("authMessage").hidden = true;
  (login ? byId("loginUsername") : byId("registerUsername")).focus();
}

async function handleLogin(event) {
  event.preventDefault();
  const button = byId("loginSubmit");
  setBusy(button, true, tr("正在登录……", "Signing in…"));
  byId("authMessage").hidden = true;
  try {
    beginSessionTransition();
    sessionStorage.removeItem(TOKEN_KEY);
    const payload = await apiRequest("/v1/auth/login", {
      authenticated: false,
      method: "POST",
      body: JSON.stringify({
        username: byId("loginUsername").value,
        password: byId("loginPassword").value,
        device_id: installationId(),
        device_name: deviceName(),
      }),
    });
    byId("loginPassword").value = "";
    await establishSession(payload.access_token);
  } catch (error) {
    if (error instanceof StaleSessionError) return;
    showAuth(error.message === "invalid credentials"
      ? tr("用户名或密码不正确。", "The username or password is incorrect.")
      : error.message);
  } finally {
    setBusy(button, false);
  }
}

async function handleRegister(event) {
  event.preventDefault();
  const password = byId("registerPassword").value;
  const confirmation = byId("registerPasswordConfirm").value;
  if (password !== confirmation) {
    showAuth(tr("两次输入的密码不一致。", "The passwords do not match."));
    return;
  }
  const button = byId("registerSubmit");
  setBusy(button, true, tr("正在创建账号……", "Creating account…"));
  byId("authMessage").hidden = true;
  try {
    beginSessionTransition();
    sessionStorage.removeItem(TOKEN_KEY);
    const payload = await apiRequest("/v1/auth/register", {
      authenticated: false,
      method: "POST",
      body: JSON.stringify({
        username: byId("registerUsername").value,
        password,
        registration_code: byId("registrationCode").value.trim(),
        device_id: installationId(),
        device_name: deviceName(),
        locale: state.locale,
      }),
    });
    byId("registerForm").reset();
    await establishSession(payload.access_token);
    toast(tr("账号已创建", "Account created"));
  } catch (error) {
    if (error instanceof StaleSessionError) return;
    const generic = ["registration unavailable", "Conflict"].includes(error.message)
      ? tr(
        "无法注册：用户名不可用、邀请码无效或已使用。",
        "Unable to register: the username is unavailable or the invitation code is invalid or already used.",
      )
      : error.message;
    showAuth(generic);
  } finally {
    setBusy(button, false);
  }
}

async function logout() {
  const token = accessToken();
  expireSession(tr(
    "你已安全退出。页面中没有保留访问令牌。",
    "You have signed out safely. No access token remains on this page.",
  ));
  if (token) {
    try {
      await fetch(requireSameOriginPath("/v1/auth/logout"), {
        method: "POST",
        headers: {Authorization: `Bearer ${token}`},
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
      });
    } catch (_error) {
      // Local credential removal is mandatory even if the server is unreachable.
    }
  }
}

async function changePassword(event) {
  event.preventDefault();
  const currentPassword = byId("currentPassword").value;
  const newPassword = byId("newPassword").value;
  if (newPassword !== byId("newPasswordConfirm").value) {
    toast(tr("两次输入的新密码不一致", "The new passwords do not match"));
    return;
  }
  const button = byId("changePasswordButton");
  setBusy(button, true, tr("正在修改……", "Updating…"));
  try {
    const payload = await apiRequest("/v1/account/password", {
      method: "POST",
      body: JSON.stringify({current_password: currentPassword, new_password: newPassword}),
    });
    byId("changePasswordForm").reset();
    await establishSession(payload.access_token);
    toast(tr("密码已修改，其他旧会话已撤销", "Password changed and other old sessions revoked"));
  } catch (error) {
    if (!(error instanceof StaleSessionError)) toast(error.message);
  } finally {
    setBusy(button, false);
  }
}

async function exportAccount() {
  const button = byId("exportAccountButton");
  const passwordInput = byId("exportAccountPassword");
  if (!passwordInput.value) {
    toast(tr("请输入当前密码后导出", "Enter your current password before exporting"));
    passwordInput.focus();
    return;
  }
  setBusy(button, true, tr("正在导出……", "Exporting…"));
  try {
    const payload = await apiRequest("/v1/account/export", {
      method: "POST",
      body: JSON.stringify({current_password: passwordInput.value}),
    });
    passwordInput.value = "";
    const blob = new Blob([JSON.stringify(payload, null, 2)], {type: "application/json"});
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const day = new Date().toISOString().slice(0, 10);
    link.href = url;
    link.download = `mouchen-account-${day}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
    toast(tr("个人数据已导出", "Personal data exported"));
  } catch (error) {
    if (!(error instanceof StaleSessionError)) toast(error.message);
  } finally {
    setBusy(button, false);
  }
}

async function deleteAccount(event) {
  event.preventDefault();
  if (!byId("deleteAccountConfirm").checked) {
    toast(tr("请先确认永久删除", "Confirm permanent deletion first"));
    return;
  }
  if (!window.confirm(tr(
    "永久删除账号和服务器中的个人数据？此操作无法撤销。",
    "Permanently delete this account and its personal data from the server? This cannot be undone.",
  ))) return;
  const button = byId("deleteAccountButton");
  setBusy(button, true, tr("正在删除……", "Deleting…"));
  try {
    await apiRequest("/v1/account", {
      method: "DELETE",
      body: JSON.stringify({current_password: byId("deleteAccountPassword").value}),
    });
    byId("deleteAccountForm").reset();
    expireSession(tr(
      "账号和服务器中的个人数据已删除。",
      "The account and its personal data have been deleted from the server.",
    ));
  } catch (error) {
    if (!(error instanceof StaleSessionError)) toast(error.message);
  } finally {
    setBusy(button, false);
  }
}

async function changeAccountLocale(event) {
  const selector = event.currentTarget;
  const previous = state.locale;
  const requested = normalizeLocale(selector.value);
  if (!state.session || requested === previous) {
    applyLocale(requested);
    return;
  }
  state.localeGeneration += 1;
  const mutationGeneration = state.localeGeneration;
  state.localeMutationPending = true;
  selector.disabled = true;
  try {
    const preferences = await apiRequest("/v1/account/preferences", {
      method: "PUT",
      body: JSON.stringify({locale: requested}),
    });
    if (mutationGeneration !== state.localeGeneration || !state.session) {
      throw new StaleSessionError();
    }
    state.session.locale = preferences.locale;
    applyLocale(preferences.locale);
    toast(tr("语言已同步到账号", "Language synced to your account"));
  } catch (error) {
    if (!(error instanceof StaleSessionError) && mutationGeneration === state.localeGeneration) {
      applyLocale(previous);
    }
    if (!(error instanceof StaleSessionError)) toast(error.message);
  } finally {
    if (mutationGeneration === state.localeGeneration) {
      state.localeMutationPending = false;
      state.localeGeneration += 1;
    }
    selector.disabled = false;
  }
}

function renderSession() {
  if (!state.session) return;
  const username = state.session.username || tr("当前账号", "Current account");
  const expiry = formatDate(state.session.expires_at, true);
  byId("accountName").textContent = username;
  byId("sessionExpiry").textContent = tr(`有效至 ${expiry}`, `Valid until ${expiry}`);
  byId("settingsUsername").textContent = username;
  byId("settingsDevice").textContent = state.session.device_id || "—";
  byId("settingsExpiry").textContent = expiry;
}

function setConnection(online) {
  byId("systemState").textContent = online
    ? tr("服务在线", "Service online")
    : tr("连接失败", "Connection failed");
  byId("systemDot").classList.toggle("offline", !online);
  byId("healthText").textContent = online ? tr("在线", "Online") : tr("离线", "Offline");
  byId("healthText").style.color = online ? "var(--green)" : "var(--red)";
}

function showView(requested, updateHash = true) {
  const view = VIEW_TITLES[requested] ? requested : "overview";
  document.querySelectorAll("[data-view-panel]").forEach(panel => {
    panel.hidden = panel.dataset.viewPanel !== view;
  });
  document.querySelectorAll("[data-view]").forEach(button => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    active ? button.setAttribute("aria-current", "page") : button.removeAttribute("aria-current");
  });
  byId("pageTitle").textContent = bilingual(VIEW_TITLES[view]);
  document.title = `${bilingual(VIEW_TITLES[view])} · ${tr("AI替身", "My AI Twin")}`;
  if (updateHash && location.hash !== `#${view}`) history.pushState(null, "", `#${view}`);
}

async function refreshAll() {
  if (state.refreshing || !state.session) return;
  const refreshEpoch = state.epoch;
  const refreshToken = accessToken();
  const refreshLocaleGeneration = state.localeGeneration;
  state.refreshing = true;
  setBusy(byId("refreshButton"), true, tr("刷新中", "Refreshing"));
  try {
    const [health, goals, advice, capabilities, preferences] = await Promise.all([
      apiRequest("/health", {authenticated: false}),
      apiRequest("/v1/goals"),
      apiRequest("/v1/advice?limit=200"),
      apiRequest("/v1/capabilities"),
      apiRequest("/v1/account/preferences"),
    ]);
    if (refreshEpoch !== state.epoch || refreshToken !== accessToken()) {
      throw new StaleSessionError();
    }
    Object.assign(state, {health, goals, advice, capabilities});
    if (
      preferences?.locale
      && preferences.locale !== state.locale
      && !state.localeMutationPending
      && refreshLocaleGeneration === state.localeGeneration
    ) {
      state.session.locale = preferences.locale;
      applyLocale(preferences.locale);
    }
    setConnection(true);
    renderAll();
  } catch (error) {
    if (error instanceof StaleSessionError) return;
    if (state.session) {
      setConnection(false);
      toast(error.message);
    }
  } finally {
    if (refreshEpoch === state.epoch && refreshToken === accessToken()) {
      state.refreshing = false;
      setBusy(byId("refreshButton"), false);
    }
  }
}

function renderAll() {
  const active = state.advice.filter(item => item.status === "active");
  byId("activeAdviceCount").textContent = active.length;
  byId("immediateAdviceCount").textContent = active.filter(item => item.delivery === "immediate").length;
  byId("goalCount").textContent = state.goals.length;
  byId("modelProvider").textContent = state.health?.model_provider
    ? tr(`模型通道：${state.health.model_provider}`, `Model channel: ${state.health.model_provider}`)
    : tr("模型通道未知", "Model channel unknown");
  renderOverview(active);
  renderAdvice();
  renderGoals();
  renderCapabilities();
}

function renderOverview(active) {
  byId("overviewAdvice").innerHTML = active.length
    ? active.slice(0, 4).map(item => `
      <article class="focus-card">
        <div>
          <h3>${escapeHtml(item.action)}</h3>
          <p>${tr("依据你的目标", "Based on your goal")}: “${escapeHtml(item.goal_quote)}”</p>
        </div>
        <div class="focus-meta">
          <span class="pill active">${escapeHtml(formatLevel(item.effective_level))}</span><br><br>
          ${item.delivery === "immediate" ? tr("即时提醒", "Immediate alert") : tr("建言简报", "Counsel brief")}<br>
          ${formatDate(item.prediction?.deadline)}
        </div>
      </article>`).join("")
    : `<div class="empty-state">${tr(
      "当前没有待处理建言。AI替身会继续对照目标观察局势。",
      "There is no active counsel right now. My AI Twin will keep watching the situation against your goals.",
    )}</div>`;

  byId("overviewGoals").innerHTML = state.goals.length
    ? state.goals.slice(0, 6).map(goalCard).join("")
    : `<div class="empty-state">${tr(
      "尚未建立目标。请先写下你判断成功与否的原话。",
      "No goals yet. Start by writing, in your own words, how you will judge success.",
    )}</div>`;
}

function goalCard(goal) {
  const weeklyHours = Number(goal.target?.weekly_hours);
  return `<article class="goal-card">
    <span class="goal-domain">${escapeHtml(goal.domain)}</span>
    <h3>${escapeHtml(goal.title)}</h3>
    <p>“${escapeHtml(goal.quote)}”</p>
    <small>${tr("版本", "Version")} v${escapeHtml(goal.version)}${weeklyHours > 0
      ? tr(` · 每周 ${weeklyHours} 小时`, ` · ${weeklyHours} hours/week`)
      : ""}</small>
  </article>`;
}

function renderGoals() {
  byId("goalLedger").innerHTML = state.goals.length
    ? state.goals.map(goalCard).join("")
    : `<div class="empty-state">${tr("尚未建立目标。", "No goals yet.")}</div>`;
}

function evidenceText(item) {
  const evidence = Array.isArray(item.evidence) ? item.evidence : [];
  return evidence.map(entry => entry.fact).filter(Boolean).slice(0, 3).join(tr("；", "; "))
    || tr("证据待补充", "Evidence pending");
}

function adviceActions(item) {
  if (item.status !== "active") return "";
  const id = escapeHtml(item.id);
  return `<div class="feedback-actions">
    <button type="button" data-feedback="adopted" data-advice-id="${id}">${tr("采纳", "Adopt")}</button>
    <button type="button" data-feedback="irrelevant" data-advice-id="${id}">${tr("无关", "Irrelevant")}</button>
    <button type="button" data-feedback="fact_error" data-advice-id="${id}">${tr("事实有误", "Factually wrong")}</button>
    <button type="button" data-feedback="later" data-advice-id="${id}">${tr("稍后处理", "Later")}</button>
    <button type="button" data-guidance-id="${id}">${tr("给方向", "Give direction")}</button>
    <button type="button" data-feedback="stop_topic" data-advice-id="${id}">${tr("不再提醒此事", "Stop this topic")}</button>
  </div>`;
}

function renderAdvice() {
  const filter = byId("adviceFilter").value;
  const items = state.advice.filter(item => {
    if (filter === "all") return true;
    if (filter === "dismissed") return ["dismissed", "withdrawn", "verified"].includes(item.status);
    return item.status === filter;
  });
  byId("adviceLedger").innerHTML = items.length ? items.map(item => {
    const status = item.status || "active";
    const delivery = item.delivery === "immediate" ? "immediate" : "brief";
    return `<article class="advice-card ${delivery}">
      <div class="advice-topline"></div>
      <div class="advice-body">
        <div class="level-badge">${escapeHtml(formatLevel(item.effective_level))}</div>
        <div>
          <h3>${escapeHtml(item.action)}</h3>
          <p class="goal-quote">${tr("目标", "Goal")}: “${escapeHtml(item.goal_quote)}”</p>
          <p class="evidence-block"><strong>${tr("依据：", "Evidence: ")}</strong>${escapeHtml(evidenceText(item))}</p>
          <div class="first-step"><strong>${tr("第一步：", "First step: ")}</strong>${escapeHtml(item.first_step)}</div>
          ${item.alternative
            ? `<p class="alternative"><strong>${tr("替代路径：", "Alternative: ")}</strong>${escapeHtml(item.alternative)}</p>`
            : ""}
        </div>
        <div class="advice-side">
          <strong>${delivery === "immediate" ? tr("即时提醒", "Immediate alert") : tr("建言简报", "Counsel brief")}</strong>
          <span class="pill ${escapeHtml(status)}">${escapeHtml(STATUS_LABELS[status]
            ? bilingual(STATUS_LABELS[status])
            : status)}</span><br><br>
          ${tr("置信度", "Confidence")} ${Math.round((item.prediction?.confidence || 0) * 100)}%<br>
          ${tr("核验时间", "Verify by")} ${formatDate(item.prediction?.deadline)}
          ${adviceActions(item)}
        </div>
      </div>
    </article>`;
  }).join("") : `<div class="empty-state">${tr(
    "这个筛选条件下没有建言。",
    "There is no counsel matching this filter.",
  )}</div>`;
}

function renderCapabilities() {
  const boundaries = state.capabilities?.hardBoundaries || [];
  byId("boundaryList").innerHTML = boundaries.length
    ? boundaries.map(item => `<li>${escapeHtml(BOUNDARY_LABELS[item]
      ? bilingual(BOUNDARY_LABELS[item])
      : item)}</li>`).join("")
    : `<li>${tr("服务器暂未提供能力声明。", "The server has not provided capability boundaries.")}</li>`;
}

async function sendFeedback(adviceId, kind, note = null) {
  const body = {feedback_id: newId(), kind};
  if (note) body.note = note;
  try {
    await apiRequest(`/v1/advice/${encodeURIComponent(adviceId)}/feedback`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    toast(kind === "guidance"
      ? tr("方向已记录", "Direction recorded")
      : tr("反馈已记录", "Feedback recorded"));
    await refreshAll();
  } catch (error) {
    toast(error.message);
  }
}

async function createGoal(event) {
  event.preventDefault();
  const hours = byId("goalHours").value.trim();
  const body = {
    title: byId("goalTitle").value.trim(),
    domain: byId("goalDomain").value.trim(),
    quote: byId("goalQuote").value.trim(),
    target: hours ? {weekly_hours: Number(hours)} : {},
    is_redline: false,
  };
  try {
    await apiRequest("/v1/goals", {method: "POST", body: JSON.stringify(body)});
    byId("goalForm").reset();
    toast(tr("目标版本已保存", "Goal version saved"));
    await refreshAll();
  } catch (error) {
    toast(error.message);
  }
}

async function runReview() {
  const button = byId("reviewNowButton");
  setBusy(button, true, tr("复盘中……", "Reviewing…"));
  try {
    await apiRequest("/v1/reviews/run", {
      method: "POST",
      cloudAction: true,
    });
    toast(state.consent.cloud
      ? tr("复盘已完成，云分析已按当前授权使用", "Review complete. Cloud analysis followed the current permission.")
      : tr("复盘已完成，未批准云分析", "Review complete without approving cloud analysis."));
    await refreshAll();
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(button, false);
  }
}

function resetConsentControls() {
  const cloud = byId("cloudAnalysisConsent");
  const raw = byId("rawContextConsent");
  if (!cloud || !raw) return;
  cloud.checked = state.consent.cloud;
  raw.checked = state.consent.raw;
  raw.disabled = !state.consent.cloud;
  byId("rawContextRow").classList.toggle("disabled", !state.consent.cloud);
  byId("cloudConsentNotice").hidden = true;
  byId("rawConsentNotice").hidden = true;
  byId("rawConsentAcknowledge").checked = false;
  byId("confirmRawConsent").disabled = true;
}

function requestCloudConsent() {
  if (!byId("cloudAnalysisConsent").checked) {
    state.consent.cloud = false;
    state.consent.raw = false;
    resetConsentControls();
    toast(tr("云分析授权已关闭", "Cloud analysis permission turned off"));
    return;
  }
  byId("cloudAnalysisConsent").checked = false;
  byId("cloudConsentNotice").hidden = false;
}

function confirmCloudConsent() {
  state.consent.cloud = true;
  resetConsentControls();
  toast(tr("本次会话已允许云模型分析", "Cloud model analysis allowed for this session"));
}

function requestRawConsent() {
  if (!byId("rawContextConsent").checked) {
    state.consent.raw = false;
    resetConsentControls();
    toast(tr("完整原始上下文授权已关闭", "Full original context permission turned off"));
    return;
  }
  byId("rawContextConsent").checked = false;
  byId("rawConsentNotice").hidden = false;
}

function confirmRawConsent() {
  if (!state.consent.cloud || !byId("rawConsentAcknowledge").checked) return;
  state.consent.raw = true;
  resetConsentControls();
  toast(tr(
    "本次会话已允许发送完整原始上下文",
    "Full original context sharing allowed for this session",
  ));
}

function openGuidance(adviceId) {
  state.guidanceAdviceId = adviceId;
  byId("guidanceText").value = "";
  byId("guidanceDialog").showModal();
  byId("guidanceText").focus();
}

async function submitGuidance(event) {
  event.preventDefault();
  const note = byId("guidanceText").value.trim();
  if (!state.guidanceAdviceId || !note) return;
  byId("guidanceDialog").close();
  const adviceId = state.guidanceAdviceId;
  state.guidanceAdviceId = null;
  await sendFeedback(adviceId, "guidance", note);
}

function bindEvents() {
  byId("authLanguageSelect").addEventListener("change", event => {
    applyLocale(event.currentTarget.value, {rerender: false});
    showAuth();
  });
  byId("accountLanguageSelect").addEventListener("change", changeAccountLocale);
  byId("loginTab").addEventListener("click", () => switchAuthTab("login"));
  byId("registerTab").addEventListener("click", () => switchAuthTab("register"));
  byId("loginForm").addEventListener("submit", handleLogin);
  byId("registerForm").addEventListener("submit", handleRegister);
  document.querySelectorAll("[data-reveal]").forEach(button => {
    button.addEventListener("click", () => {
      const input = byId(button.dataset.reveal);
      const reveal = input.type === "password";
      input.type = reveal ? "text" : "password";
      button.textContent = reveal ? tr("隐藏", "Hide") : tr("显示", "Show");
      button.setAttribute(
        "aria-label",
        reveal ? tr("隐藏密码", "Hide password") : tr("显示密码", "Show password"),
      );
    });
  });

  document.querySelectorAll("[data-view]").forEach(button => {
    button.addEventListener("click", () => showView(button.dataset.view));
  });
  window.addEventListener("hashchange", () => {
    if (state.session) showView(location.hash.slice(1), false);
  });
  byId("refreshButton").addEventListener("click", refreshAll);
  byId("logoutButton").addEventListener("click", logout);
  byId("settingsLogoutButton").addEventListener("click", logout);
  byId("changePasswordForm").addEventListener("submit", changePassword);
  byId("exportAccountButton").addEventListener("click", exportAccount);
  byId("deleteAccountForm").addEventListener("submit", deleteAccount);
  byId("reviewNowButton").addEventListener("click", runReview);
  byId("goalForm").addEventListener("submit", createGoal);
  byId("adviceFilter").addEventListener("change", renderAdvice);
  byId("adviceLedger").addEventListener("click", event => {
    const feedback = event.target.closest("[data-feedback]");
    if (feedback) {
      sendFeedback(feedback.dataset.adviceId, feedback.dataset.feedback);
      return;
    }
    const guidance = event.target.closest("[data-guidance-id]");
    if (guidance) openGuidance(guidance.dataset.guidanceId);
  });

  byId("cloudAnalysisConsent").addEventListener("change", requestCloudConsent);
  byId("cancelCloudConsent").addEventListener("click", resetConsentControls);
  byId("confirmCloudConsent").addEventListener("click", confirmCloudConsent);
  byId("rawContextConsent").addEventListener("change", requestRawConsent);
  byId("cancelRawConsent").addEventListener("click", resetConsentControls);
  byId("rawConsentAcknowledge").addEventListener("change", event => {
    byId("confirmRawConsent").disabled = !event.target.checked;
  });
  byId("confirmRawConsent").addEventListener("click", confirmRawConsent);
  byId("guidanceForm").addEventListener("submit", submitGuidance);
  byId("cancelGuidance").addEventListener("click", () => byId("guidanceDialog").close());
}

async function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;
  if (location.protocol !== "https:" && location.hostname !== "localhost" && location.hostname !== "127.0.0.1") return;
  try {
    await navigator.serviceWorker.register("/static/sw.js");
  } catch (_error) {
    // PWA installation is optional; an unavailable worker must not block login.
  }
}

async function boot() {
  applyLocale(state.locale, {rerender: false});
  bindEvents();
  resetConsentControls();
  registerServiceWorker();
  if (await validateSession()) {
    showApp();
    await refreshAll();
  } else {
    showAuth();
  }
}

document.addEventListener("DOMContentLoaded", boot);
