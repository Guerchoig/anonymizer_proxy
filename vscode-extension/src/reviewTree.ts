import * as vscode from 'vscode';
import { PendingReview } from './reviewClient';

export class ReviewTreeItem extends vscode.TreeItem {
  constructor(public readonly review: PendingReview) {
    super(review.session_id, vscode.TreeItemCollapsibleState.None);
    this.contextValue = 'pendingReview';
    this.tooltip = `Файл: ${review.anonymized_file_path}`;
    this.description = formatTime(review.created_at);
    this.iconPath = new vscode.ThemeIcon('shield');
  }
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) {
    return iso;
  }
  return d.toLocaleTimeString();
}

export class ReviewTreeDataProvider
  implements vscode.TreeDataProvider<ReviewTreeItem>
{
  private _onDidChangeTreeData = new vscode.EventEmitter<
    ReviewTreeItem | undefined | null | void
  >();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  private items: ReviewTreeItem[] = [];

  setReviews(reviews: PendingReview[]): void {
    this.items = reviews.map((r) => new ReviewTreeItem(r));
    this._onDidChangeTreeData.fire();
  }

  getAllReviews(): PendingReview[] {
    return this.items.map((i) => i.review);
  }

  getTreeItem(element: ReviewTreeItem): vscode.TreeItem {
    return element;
  }

  getChildren(element?: ReviewTreeItem): vscode.ProviderResult<ReviewTreeItem[]> {
    if (element) {
      return [];
    }
    return this.items;
  }
}
