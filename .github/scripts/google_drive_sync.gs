/**
 * Last30Days → Google Drive 自动同步脚本（Google Apps Script）
 *
 * 功能：
 * 1. 每天北京时间 09:30 自动拉取 GitHub Pages 生成的中文研报清单（drive-sync/manifest.json）
 * 2. 自动把新研报转为格式整齐的中文 Google Docs，存入「Last30Days 研报库」文件夹
 * 3. 自动把双语交互 HTML、首页 index.html 和 feed.xml 备份到「HTML 归档」子文件夹
 * 4. 自动去重，绝不重复创建同一份研报
 * 5. 可选：每次同步出新研报后，自动向你的 Gmail 发送一封中文早报通知邮件
 *
 * 首次启用只需 3 步（全程免费、无需 Google Cloud 控制台、永不过期）：
 * 1. 打开 https://script.google.com → 新建项目 → 完整粘贴本脚本并保存
 * 2. 顶部函数下拉框选择 `setupAndSyncNow`，点击「运行」并按提示授予 Google Drive 权限
 * 3. 完成！之后每天早上 09:30（北京时间）全自动同步
 */

const CONFIG = {
  MANIFEST_URL: "https://zhouguanfang66-crypto.github.io/last30days-skill/drive-sync/manifest.json",
  ROOT_FOLDER_NAME: "Last30Days 研报库",
  HTML_ARCHIVE_FOLDER_NAME: "HTML 归档",
  TIMEZONE: "Asia/Shanghai",
  DAILY_HOUR: 9,
  DAILY_MINUTE: 30,
  // 设为 true：每次同步到新研报时自动发中文早报邮件；不需要可改为 false
  SEND_EMAIL_DIGEST: true,
  // 收件邮箱：留空发给当前 Gmail；填其他邮箱（如 QQ 邮箱）则直接发往该地址，多个可用英文逗号分隔
  EMAIL_TO: "",
};

/**
 * 首次一键初始化：创建每天 09:30（北京时间）的定时触发器，并立刻同步一次。
 */
function setupAndSyncNow() {
  const existingTriggers = ScriptApp.getProjectTriggers();
  for (const trigger of existingTriggers) {
    if (trigger.getHandlerFunction() === "syncLast30DaysToDrive") {
      ScriptApp.deleteTrigger(trigger);
    }
  }

  ScriptApp.newTrigger("syncLast30DaysToDrive")
    .timeBased()
    .everyDays(1)
    .atHour(CONFIG.DAILY_HOUR)
    .nearMinute(CONFIG.DAILY_MINUTE)
    .inTimezone(CONFIG.TIMEZONE)
    .create();

  Logger.log(
    "已创建每日定时任务：每天 %s:%s (%s) 自动同步到 Google Drive 并发送邮件。",
    String(CONFIG.DAILY_HOUR).padStart(2, "0"),
    String(CONFIG.DAILY_MINUTE).padStart(2, "0"),
    CONFIG.TIMEZONE
  );

  const result = syncLast30DaysToDrive();
  // 首次初始化时若今日文档已存在导致未触发新邮件，也发一封确认邮件方便验收邮箱是否通畅
  if (CONFIG.SEND_EMAIL_DIGEST && result.createdCount === 0 && result.latestDocs.length > 0) {
    sendDigestEmail_(
      result.latestDocs,
      result.folderUrl,
      result.siteUrl,
      "（首次配置验证：定时同步与早报推送已就绪）"
    );
  }
}

/**
 * 可随时手动运行：立刻发送一封包含最新研报链接的测试邮件。
 */
function sendTestEmailNow() {
  const manifest = fetchManifest_();
  const rootFolder = getOrCreateFolder_(DriveApp.getRootFolder(), CONFIG.ROOT_FOLDER_NAME);
  const latestDocs = (manifest.briefs || []).slice(0, 5).map(function (b) {
    return {
      name: String(b.doc_name || b.slug || "研报"),
      docUrl: rootFolder.getUrl(),
      webUrl: b.html_url || manifest.site_url || "",
    };
  });
  sendDigestEmail_(latestDocs, rootFolder.getUrl(), manifest.site_url || "", "（测试邮件）");
}

/**
 * 核心同步函数（由每日定时触发器自动调用，也可随时手动运行）。
 */
function syncLast30DaysToDrive() {
  const manifest = fetchManifest_();
  const briefs = Array.isArray(manifest.briefs) ? manifest.briefs : [];

  const rootFolder = getOrCreateFolder_(DriveApp.getRootFolder(), CONFIG.ROOT_FOLDER_NAME);
  const htmlFolder = getOrCreateFolder_(rootFolder, CONFIG.HTML_ARCHIVE_FOLDER_NAME);

  const existingDocMap = listFilesByName_(rootFolder);
  const existingHtmlMap = listFilesByName_(htmlFolder);

  const props = PropertiesService.getScriptProperties();
  const syncedSlugs = loadSyncedSet_(props, "SYNCED_SLUGS");
  const newlyCreatedDocs = [];
  const latestDocs = [];

  for (const brief of briefs) {
    const slug = String(brief.slug || "");
    const docName = String(brief.doc_name || slug);
    const filename = String(brief.filename || (slug + ".html"));

    // 1. 同步中文版 Google Doc 到「Last30Days 研报库」
    if (!syncedSlugs.has(slug) && !existingDocMap.has(docName) && brief.zh_doc_html_url) {
      const zhResp = UrlFetchApp.fetch(brief.zh_doc_html_url, { muteHttpExceptions: true });
      if (zhResp.getResponseCode() >= 200 && zhResp.getResponseCode() < 300) {
        const zhHtml = zhResp.getContentText("UTF-8");
        const created = createGoogleDocFromHtml_(rootFolder.getId(), docName, zhHtml);
        const docUrl = "https://docs.google.com/document/d/" + created.id + "/edit";
        existingDocMap.set(docName, docUrl);
        newlyCreatedDocs.push({
          name: docName,
          docUrl: docUrl,
          webUrl: brief.html_url || manifest.site_url || "",
        });
        Logger.log("已创建 Google Doc：%s", docName);
      } else {
        Logger.log("跳过 %s（获取中文 HTML 失败 HTTP %s）", docName, zhResp.getResponseCode());
      }
    }

    if (latestDocs.length < 5) {
      latestDocs.push({
        name: docName,
        docUrl: existingDocMap.get(docName) || rootFolder.getUrl(),
        webUrl: brief.html_url || manifest.site_url || "",
      });
    }

    // 2. 同步双语交互 HTML 到「HTML 归档」
    if (!existingHtmlMap.has(filename) && brief.html_url) {
      const htmlResp = UrlFetchApp.fetch(brief.html_url, { muteHttpExceptions: true });
      if (htmlResp.getResponseCode() >= 200 && htmlResp.getResponseCode() < 300) {
        const createdHtml = htmlFolder.createFile(
          Utilities.newBlob(htmlResp.getContentText("UTF-8"), "text/html", filename)
        );
        existingHtmlMap.set(filename, createdHtml.getUrl());
      }
    }

    if (slug) {
      syncedSlugs.add(slug);
    }
  }

  saveSyncedSet_(props, "SYNCED_SLUGS", syncedSlugs);

  // 3. 覆盖更新最新 index.html 与 feed.xml 到「HTML 归档」
  if (manifest.index_html_url) {
    upsertTextFile_(htmlFolder, "index.html", manifest.index_html_url, "text/html");
  }
  if (manifest.feed_xml_url) {
    upsertTextFile_(htmlFolder, "feed.xml", manifest.feed_xml_url, "application/atom+xml");
  }

  Logger.log("同步完成：本次新增 %s 篇中文 Google Docs。", newlyCreatedDocs.length);

  // 4. 如有新研报且开启邮件提醒，自动发早报邮件
  if (CONFIG.SEND_EMAIL_DIGEST && newlyCreatedDocs.length > 0) {
    sendDigestEmail_(newlyCreatedDocs, rootFolder.getUrl(), manifest.site_url || "", "");
  }

  return {
    createdCount: newlyCreatedDocs.length,
    latestDocs: latestDocs,
    folderUrl: rootFolder.getUrl(),
    siteUrl: manifest.site_url || "",
  };
}

function fetchManifest_() {
  const manifestResp = UrlFetchApp.fetch(CONFIG.MANIFEST_URL, { muteHttpExceptions: true });
  const status = manifestResp.getResponseCode();
  if (status < 200 || status >= 300) {
    throw new Error("无法读取 manifest.json（HTTP " + status + "）：" + CONFIG.MANIFEST_URL);
  }
  return JSON.parse(manifestResp.getContentText("UTF-8"));
}

function getOrCreateFolder_(parentFolder, name) {
  const iter = parentFolder.getFoldersByName(name);
  if (iter.hasNext()) {
    return iter.next();
  }
  return parentFolder.createFolder(name);
}

function listFilesByName_(folder) {
  const map = new Map();
  const files = folder.getFiles();
  while (files.hasNext()) {
    const file = files.next();
    map.set(file.getName(), file.getUrl());
  }
  return map;
}

function loadSyncedSet_(props, key) {
  try {
    const raw = props.getProperty(key);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return new Set(Array.isArray(arr) ? arr : []);
  } catch (err) {
    return new Set();
  }
}

function saveSyncedSet_(props, key, setObj) {
  const list = Array.from(setObj).slice(-500);
  props.setProperty(key, JSON.stringify(list));
}

function createGoogleDocFromHtml_(folderId, docName, htmlText) {
  const boundary = "l30d_sync_" + Utilities.getUuid().replace(/-/g, "");
  const metadata = JSON.stringify({
    name: docName,
    mimeType: "application/vnd.google-apps.document",
    parents: [folderId],
  });
  const multipartBody =
    "--" + boundary + "\r\n" +
    "Content-Type: application/json; charset=UTF-8\r\n\r\n" +
    metadata + "\r\n" +
    "--" + boundary + "\r\n" +
    "Content-Type: text/html; charset=UTF-8\r\n\r\n" +
    htmlText + "\r\n" +
    "--" + boundary + "--";

  const resp = UrlFetchApp.fetch(
    "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
    {
      method: "post",
      contentType: "multipart/related; boundary=" + boundary,
      headers: { Authorization: "Bearer " + ScriptApp.getOAuthToken() },
      payload: Utilities.newBlob(multipartBody, "multipart/related; boundary=" + boundary).getBytes(),
      muteHttpExceptions: true,
    }
  );
  const code = resp.getResponseCode();
  if (code < 200 || code >= 300) {
    throw new Error("Drive API 创建 Google Doc 失败（HTTP " + code + "）：" + resp.getContentText());
  }
  return JSON.parse(resp.getContentText("UTF-8"));
}

function upsertTextFile_(folder, filename, sourceUrl, mimeType) {
  const resp = UrlFetchApp.fetch(sourceUrl, { muteHttpExceptions: true });
  if (resp.getResponseCode() < 200 || resp.getResponseCode() >= 300) {
    return;
  }
  const text = resp.getContentText("UTF-8");
  const existing = folder.getFilesByName(filename);
  if (existing.hasNext()) {
    existing.next().setContent(text);
  } else {
    folder.createFile(Utilities.newBlob(text, mimeType, filename));
  }
}

function escapeHtml_(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function sendDigestEmail_(docs, folderUrl, siteUrl, suffix) {
  const recipient = (CONFIG.EMAIL_TO || "").trim() || Session.getActiveUser().getEmail();
  if (!recipient) return;

  const today = Utilities.formatDate(new Date(), CONFIG.TIMEZONE, "yyyy-MM-dd");
  const subject = "【Last30Days 每日研报】" + today + " 中文研报已同步" + (suffix || "");

  const textLines = [
    "你好，今日自动研报已同步到 Google Drive「" + CONFIG.ROOT_FOLDER_NAME + "」：",
    "",
  ];
  const htmlItems = [];
  for (const doc of docs) {
    textLines.push("• " + doc.name);
    textLines.push("  Google Doc：" + doc.docUrl);
    if (doc.webUrl) {
      textLines.push("  网页版：" + doc.webUrl);
    }
    textLines.push("");

    htmlItems.push(
      '<li style="margin:8px 0;">' +
        "<strong>" + escapeHtml_(doc.name) + "</strong><br>" +
        '<a href="' + escapeHtml_(doc.docUrl) + '">打开 Google Docs 中文版</a>' +
        (doc.webUrl ? ' · <a href="' + escapeHtml_(doc.webUrl) + '">打开双语网页版</a>' : "") +
      "</li>"
    );
  }
  textLines.push("Google Drive 文件夹：" + folderUrl);
  if (siteUrl) {
    textLines.push("在线研报站：" + siteUrl);
  }

  const htmlBody =
    '<div style="font-family:-apple-system,BlinkMacSystemFont,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;line-height:1.7;color:#1f2328;max-width:640px;">' +
      '<h2 style="margin:0 0 12px;color:#111827;">Last30Days 每日研报（' + escapeHtml_(today) + "）</h2>" +
      "<p>最新中文研报已自动同步至你的 Google Drive「" + escapeHtml_(CONFIG.ROOT_FOLDER_NAME) + "」：</p>" +
      '<ul style="padding-left:20px;">' + htmlItems.join("") + "</ul>" +
      '<p style="margin-top:16px;">' +
        '<a href="' + escapeHtml_(folderUrl) + '" style="display:inline-block;padding:8px 14px;background:#1a73e8;color:#ffffff;text-decoration:none;border-radius:6px;margin-right:8px;">打开 Google Drive 研报库</a>' +
        (siteUrl ? '<a href="' + escapeHtml_(siteUrl) + '" style="display:inline-block;padding:8px 14px;background:#f3f4f6;color:#111827;text-decoration:none;border-radius:6px;">打开在线研报站</a>' : "") +
      "</p>" +
    "</div>";

  MailApp.sendEmail({
    to: recipient,
    subject: subject,
    body: textLines.join("\n"),
    htmlBody: htmlBody,
  });
  Logger.log("已发送早报邮件至：%s", recipient);
}
