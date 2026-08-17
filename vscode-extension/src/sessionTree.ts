import * as vscode from "vscode";
import { SessionInfo } from "./proxyClient";

function formatTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString();
}

export class SessionTreeItem extends vscode.TreeItem {
  constructor(public readonly session: SessionInfo) {
    super(session.session_id, vscode.TreeItemCollapsibleState.None);
    this.contextValue = "session";
    this.description = `${formatTime(session.created_at)} | маппингов: ${session.mappings_count}`;
    this.iconPath = new vscode.ThemeIcon("database");
  }
}

export class SessionTreeDataProvider implements vscode.TreeDataProvider<SessionTreeItem> {
  private _onDidChangeTreeData = new vscode.EventEmitter<SessionTreeItem | undefined | null | void>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  private items: SessionTreeItem[] = [];

  setSessions(sessions: SessionInfo[]): void {
    this.items = sessions.map((s) => new SessionTreeItem(s));
    this._onDidChangeTreeData.fire();
  }

  getAllSessions(): SessionInfo[] {
    return this.items.map((i) => i.session);
  }

  getTreeItem(element: SessionTreeItem): vscode.TreeItem {
    return element;
  }

  getChildren(element?: SessionTreeItem): vscode.ProviderResult<SessionTreeItem[]> {
    return element ? [] : this.items;
  }
}
