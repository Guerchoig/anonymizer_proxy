/**
 * HTTP-клиент к Anonymizer Proxy (эндпоинты ручного управления).
 */
export interface SessionInfo {
  session_id: string;
  created_at: string;
  expires_at: string;
  mappings_count: number;
  review_files: string[];
}

export interface SessionsResponse {
  sessions: SessionInfo[];
  count: number;
}

export class ProxyClientError extends Error {}

export class ProxyClient {
  constructor(
    private readonly baseUrl: string,
    private readonly apiToken: string,
  ) {}

  private headers(): Record<string, string> {
    const h: Record<string, string> = { "Content-Type": "application/json" };
    if (this.apiToken) {
      h["X-API-Key"] = this.apiToken;
    }
    return h;
  }

  async getSessions(): Promise<SessionInfo[]> {
    const res = await this.request(`${this.baseUrl}/api/sessions`);
    const data = (await res.json()) as SessionsResponse;
    return data.sessions ?? [];
  }

  async send(content: string, sessionId: string): Promise<Record<string, unknown>> {
    const res = await this.request(`${this.baseUrl}/api/send`, {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, content }),
    });
    return (await res.json()) as Record<string, unknown>;
  }

  async deanonymizeFile(sessionId: string, filePath: string): Promise<Record<string, unknown>> {
    const res = await this.request(`${this.baseUrl}/api/deanonymize_file`, {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, file_path: filePath }),
    });
    return (await res.json()) as Record<string, unknown>;
  }

  private async request(url: string, init?: RequestInit): Promise<Response> {
    let res: Response;
    try {
      res = await fetch(url, {
        ...init,
        headers: { ...this.headers(), ...(init?.headers ?? {}) },
      });
    } catch (e) {
      throw new ProxyClientError(`Не удалось соединиться с прокси (${url}): ${(e as Error).message}`);
    }
    if (!res.ok) {
      let detail = "";
      try {
        const data = (await res.json()) as { detail?: unknown };
        detail = data.detail ? String(data.detail) : JSON.stringify(data);
      } catch {
        detail = await res.text().catch(() => "");
      }
      throw new ProxyClientError(`Ошибка прокси ${res.status}: ${detail}`);
    }
    return res;
  }
}
