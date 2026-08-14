/**
 * HTTP-клиент к Anonymizer Proxy (эндпоинты ручного ревью).
 */
export interface PendingReview {
  request_id: string;
  session_id: string;
  anonymized_file_path: string;
  created_at: string;
}

export interface PendingResponse {
  pending: PendingReview[];
  count: number;
}

export class ReviewClientError extends Error {}

export class ReviewClient {
  constructor(
    private readonly baseUrl: string,
    private readonly apiToken: string,
  ) {}

  private headers(): Record<string, string> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };
    if (this.apiToken) {
      headers['X-API-Key'] = this.apiToken;
    }
    return headers;
  }

  async getPending(): Promise<PendingReview[]> {
    const res = await this.request(`${this.baseUrl}/api/review/pending`);
    const data = (await res.json()) as PendingResponse;
    return data.pending ?? [];
  }

  async approve(requestId: string, editedContent?: string): Promise<void> {
    const body: { request_id: string; edited_content?: string } = {
      request_id: requestId,
    };
    if (editedContent !== undefined) {
      body.edited_content = editedContent;
    }
    await this.request(`${this.baseUrl}/api/review/approve`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
  }

  async reject(requestId: string, reason?: string): Promise<void> {
    await this.request(`${this.baseUrl}/api/review/reject`, {
      method: 'POST',
      body: JSON.stringify({ request_id: requestId, reason }),
    });
  }

  private async request(url: string, init?: RequestInit): Promise<Response> {
    let res: Response;
    try {
      res = await fetch(url, {
        ...init,
        headers: { ...this.headers(), ...(init?.headers ?? {}) },
      });
    } catch (e) {
      throw new ReviewClientError(
        `Не удалось соединиться с прокси (${url}): ${(e as Error).message}`,
      );
    }

    if (!res.ok) {
      let detail = '';
      try {
        const data = (await res.json()) as { detail?: unknown };
        detail = data.detail ? String(data.detail) : JSON.stringify(data);
      } catch {
        detail = await res.text().catch(() => '');
      }
      throw new ReviewClientError(`Ошибка прокси ${res.status}: ${detail}`);
    }
    return res;
  }
}
