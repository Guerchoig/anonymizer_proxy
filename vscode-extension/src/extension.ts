import * as vscode from 'vscode';
import { promises as fs } from 'fs';
import { ReviewClient, PendingReview } from './reviewClient';
import { ReviewTreeDataProvider, ReviewTreeItem } from './reviewTree';

let client: ReviewClient;
let treeProvider: ReviewTreeDataProvider;
let statusBar: vscode.StatusBarItem;
let view: vscode.TreeView<ReviewTreeItem>;
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
  client = new ReviewClient(config.baseUrl, config.apiToken);
  treeProvider = new ReviewTreeDataProvider();

  statusBar = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Left,
    100,
  );
  statusBar.command = 'anonymizerProxy.refreshReviews';
  statusBar.text = '$(shield) Ревью: …';
  statusBar.show();
  context.subscriptions.push(statusBar);

  view = vscode.window.createTreeView('anonymizerProxy.reviewView', {
    treeDataProvider: treeProvider,
    showCollapseAll: false,
  });
  view.message = 'Нет запросов на ручное ревью';
  context.subscriptions.push(view);

  registerCommands(context);

  void refreshReviews();
  pollTimer = setInterval(() => {
    void refreshReviews({ silent: true });
  }, config.pollIntervalMs);
  context.subscriptions.push({
    dispose: () => {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = undefined;
      }
    },
  });
}

function registerCommands(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.commands.registerCommand(
      'anonymizerProxy.refreshReviews',
      () => refreshReviews(),
    ),
    vscode.commands.registerCommand(
      'anonymizerProxy.openFile',
      async (item?: ReviewTreeItem) => {
        const review = resolveItem(item);
        if (review) {
          await openFile(review);
        }
      },
    ),
    vscode.commands.registerCommand(
      'anonymizerProxy.approveWithEdits',
      async (item?: ReviewTreeItem) => {
        const review = resolveItem(item);
        if (review) {
          await approveWithEdits(review);
        }
      },
    ),
    vscode.commands.registerCommand(
      'anonymizerProxy.approveNoEdits',
      async (item?: ReviewTreeItem) => {
        const review = resolveItem(item);
        if (review) {
          await approveNoEdits(review);
        }
      },
    ),
    vscode.commands.registerCommand(
      'anonymizerProxy.reject',
      async (item?: ReviewTreeItem) => {
        const review = resolveItem(item);
        if (review) {
          await rejectReview(review);
        }
      },
    ),
  );
}

function resolveItem(item?: ReviewTreeItem): PendingReview | undefined {
  if (item?.review) {
    return item.review;
  }
  // Команда вызвана из палитры — берём первый ожидающий запрос.
  return treeProvider.getAllReviews()[0];
}

async function refreshReviews(
  options: { silent?: boolean } = {},
): Promise<void> {
  try {
    const pending = await client.getPending();
    treeProvider.setReviews(pending);
    view.message = pending.length
      ? undefined
      : 'Нет запросов на ручное ревью. Отправьте запрос в Cline с mode="review".';
    statusBar.text = pending.length
      ? `$(shield) Ревью: ${pending.length}`
      : '$(shield) Ревью: нет';
    statusBar.tooltip = pending.length
      ? 'Запросы, ожидающие ручного ревью'
      : 'Очередь ревью пуста';
  } catch (e) {
    view.message =
      'Прокси недоступен. Проверьте, что Anonymizer Proxy запущен (anonymizerProxy.baseUrl).';
    if (!options.silent) {
      void vscode.window.showErrorMessage(
        `Anonymizer Proxy: ${(e as Error).message}`,
      );
    }
    statusBar.text = '$(shield) Ревью: офлайн';
  }
}

async function openFile(review: PendingReview): Promise<void> {
  const uri = vscode.Uri.file(review.anonymized_file_path);
  try {
    const doc = await vscode.workspace.openTextDocument(uri);
    await vscode.window.showTextDocument(doc);
  } catch (e) {
    void vscode.window.showErrorMessage(
      `Не удалось открыть файл: ${(e as Error).message}`,
    );
  }
}

async function approveWithEdits(review: PendingReview): Promise<void> {
  try {
    const content = await fs.readFile(review.anonymized_file_path, 'utf-8');
    await client.approve(review.request_id, content);
    void vscode.window.showInformationMessage(
      `Запрос ${review.request_id} одобрен с правками`,
    );
    void refreshReviews({ silent: true });
  } catch (e) {
    void vscode.window.showErrorMessage(
      `Не удалось одобрить: ${(e as Error).message}`,
    );
  }
}

async function approveNoEdits(review: PendingReview): Promise<void> {
  try {
    await client.approve(review.request_id);
    void vscode.window.showInformationMessage(
      `Запрос ${review.request_id} одобрен без правок`,
    );
    void refreshReviews({ silent: true });
  } catch (e) {
    void vscode.window.showErrorMessage(
      `Не удалось одобрить: ${(e as Error).message}`,
    );
  }
}

async function rejectReview(review: PendingReview): Promise<void> {
  const reason = await vscode.window.showInputBox({
    prompt: 'Причина отклонения (необязательно)',
    placeHolder: 'например: содержит лишние данные',
  });
  if (reason === undefined) {
    return; // пользователь отменил
  }
  try {
    await client.reject(review.request_id, reason || undefined);
    void vscode.window.showInformationMessage(
      `Запрос ${review.request_id} отклонён`,
    );
    void refreshReviews({ silent: true });
  } catch (e) {
    void vscode.window.showErrorMessage(
      `Не удалось отклонить: ${(e as Error).message}`,
    );
  }
}

export function deactivate(): void {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = undefined;
  }
}
