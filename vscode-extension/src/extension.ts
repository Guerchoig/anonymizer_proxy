import * as vscode from "vscode";
import { promises as fs } from "fs";
import { ProxyClient, SessionInfo } from "./proxyClient";
import { SessionTreeDataProvider, SessionTreeItem } from "./sessionTree";

let client: ProxyClient;
let treeProvider: SessionTreeDataProvider;
let statusBar: vscode.StatusBarItem;
let pollTimer: NodeJS.Timeout | undefined;

interface ExtensionConfig {
  baseUrl: string;
  apiToken: string;
  pollIntervalMs: number;
}

function getConfig(): ExtensionConfig {
  const c = vscode.workspace.getConfiguration('anonymizerProxy');
  return {
    baseUrl: c.get<string>('baseUrl', 'http://127.0.0.1:8081'),
    apiToken: c.get<string>('apiToken', ''),
    pollIntervalMs: c.get<number>('pollIntervalMs', 2000),
  };
}

export function activate(context: vscode.ExtensionContext): void {
  const config = getConfig();
  client = new ProxyClient(config.baseUrl, config.apiToken);
  treeProvider = new SessionTreeDataProvider();

  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBar.command = "anonymizerProxy.refreshSessions";
  statusBar.text = "$(database) Сессий: …";
  statusBar.show();
  context.subscriptions.push(statusBar);

  const treeView = vscode.window.createTreeView("anonymizerProxy.sessionsView", {
    treeDataProvider: treeProvider,
    showCollapseAll: false,
  });
  context.subscriptions.push(treeView);

  registerCommands(context);

  void refreshSessions();
  pollTimer = setInterval(() => {
    void refreshSessions({ silent: true });
  }, config.pollIntervalMs);
  context.subscriptions.push({
    dispose: () => {
      if (pollTimer) clearInterval(pollTimer);
    },
  });
}

function registerCommands(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("anonymizerProxy.refreshSessions", () => refreshSessions()),
    vscode.commands.registerCommand("anonymizerProxy.openMd", async (item?: SessionTreeItem) => {
      const session = resolveItem(item);
      if (session) await openMd(session);
    }),
    vscode.commands.registerCommand("anonymizerProxy.send", async (item?: SessionTreeItem) => {
      const session = resolveItem(item);
      if (session) await sendPrompt(session);
    }),
    vscode.commands.registerCommand("anonymizerProxy.deanonymizeFile", async (item?: SessionTreeItem) => {
      const session = resolveItem(item);
      if (session) await deanonymizeFile(session);
    }),
  );
}

function resolveItem(item?: SessionTreeItem): SessionInfo | undefined {
  if (item?.session) return item.session;
  return treeProvider.getAllSessions()[0];
}

async function refreshSessions(options: { silent?: boolean } = {}): Promise<void> {
  try {
    const sessions = await client.getSessions();
    treeProvider.setSessions(sessions);
    statusBar.text = sessions.length
      ? `$(database) Сессий: ${sessions.length}`
      : "$(database) Сессий: нет";
  } catch (e) {
    if (!options.silent) {
      void vscode.window.showErrorMessage(`Anonymizer Proxy: ${(e as Error).message}`);
    }
    statusBar.text = "$(database) Сессий: офлайн";
  }
}

async function openMd(session: SessionInfo): Promise<void> {
  const files = session.review_files;
  if (!files || files.length === 0) {
    void vscode.window.showInformationMessage("У сессии нет файлов ревью (.md).");
    return;
  }
  let filePath: string;
  if (files.length === 1) {
    filePath = files[0];
  } else {
    const picked = await vscode.window.showQuickPick(files, { placeHolder: "Выберите файл ревью" });
    if (!picked) return;
    filePath = picked;
  }
  try {
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(filePath));
    await vscode.window.showTextDocument(doc);
  } catch (e) {
    void vscode.window.showErrorMessage(`Не удалось открыть файл: ${(e as Error).message}`);
  }
}

async function sendPrompt(session: SessionInfo): Promise<void> {
  let content: string | undefined;
  if (session.review_files && session.review_files.length > 0) {
    try {
      content = await fs.readFile(session.review_files[0], "utf-8");
    } catch { /* файл не читается */ }
  }
  if (!content) {
    content = await vscode.window.showInputBox({
      prompt: "Введите анонимизированный контент для отправки (markdown или текст)",
    });
    if (!content) return;
  }
  try {
    const result = await client.send(content, session.session_id);
    void vscode.window.showInformationMessage(`Запрос отправлен (сессия ${session.session_id}).`);
    void refreshSessions({ silent: true });
  } catch (e) {
    void vscode.window.showErrorMessage(`Не удалось отправить: ${(e as Error).message}`);
  }
}

async function deanonymizeFile(session: SessionInfo): Promise<void> {
  const filePath = await vscode.window.showInputBox({
    prompt: "Путь к файлу с плейсхолдерами (абсолютный)",
    placeHolder: "C:\\...\\file.docx",
  });
  if (!filePath) return;
  try {
    const result = await client.deanonymizeFile(session.session_id, filePath);
    void vscode.window.showInformationMessage(`Файл де-анонимизирован (сессия ${session.session_id}).`);
  } catch (e) {
    void vscode.window.showErrorMessage(`Не удалось де-анонимизировать: ${(e as Error).message}`);
  }
}

export function deactivate(): void {
  if (pollTimer) clearInterval(pollTimer);
}
