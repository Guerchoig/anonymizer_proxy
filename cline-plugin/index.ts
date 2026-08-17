import type { AgentPlugin } from "@cline/core";
import { createTool } from "@cline/core";

const BASE_URL = process.env.ANONYMIZER_PROXY_URL ?? "http://127.0.0.1:8081";
const API_TOKEN = process.env.ANONYMIZER_PROXY_TOKEN ?? "";

async function apiRequest(path: string, body: unknown): Promise<unknown> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (API_TOKEN) {
    headers["X-API-Key"] = API_TOKEN;
  }
  const res = await fetch(`${BASE_URL}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Proxy ${res.status}: ${text}`);
  }
  return res.json();
}

const plugin: AgentPlugin = {
  name: "anonymizer-proxy",
  manifest: {
    capabilities: ["tools"],
  },
  setup(api) {
    api.registerTool(
      createTool<unknown, unknown>({
        name: "anonymize_text",
        description:
          "Анонимизировать текст через Anonymizer Proxy (NER + плейсхолдеры). " +
          "Возвращает анонимизированный текст, session_id и путь к .md для ревью.",
        inputSchema: {
          type: "object",
          properties: {
            text: { type: "string", description: "Текст для анонимизации" },
          },
          required: ["text"],
        },
        async execute(input) {
          const { text } = input as { text: string };
          return await apiRequest("/api/anonymize", { text });
        },
      }),
    );

    api.registerTool(
      createTool<unknown, unknown>({
        name: "anonymize_file",
        description:
          "Анонимизировать локальный файл (DOCX/XLSX/...). Создаёт копию " +
          "<name>.anonymized.<ext> с плейсхолдерами рядом с оригиналом и .md для ревью.",
        inputSchema: {
          type: "object",
          properties: {
            file_path: {
              type: "string",
              description: "Абсолютный путь к файлу",
            },
          },
          required: ["file_path"],
        },
        async execute(input) {
          const { file_path } = input as { file_path: string };
          return await apiRequest("/api/anonymize_file", { file_path });
        },
      }),
    );

    api.registerTool(
      createTool<unknown, unknown>({
        name: "send_prompt",
        description:
          "Отправить анонимизированный промпт в облако через прокси и получить " +
          "де-анонимизированный ответ.",
        inputSchema: {
          type: "object",
          properties: {
            session_id: { type: "string", description: "ID сессии с маппингами" },
            content: {
              type: "string",
              description: "Анонимизированный контент (markdown или текст)",
            },
          },
          required: ["session_id", "content"],
        },
        async execute(input) {
          const { session_id, content } = input as { session_id: string; content: string };
          return await apiRequest("/api/send", { session_id, content });
        },
      }),
    );

    api.registerTool(
      createTool<unknown, unknown>({
        name: "deanonymize_file",
        description:
          "Заменить плейсхолдеры в файле на реальные значения (по session_id). " +
          "Финальный шаг после правок модели.",
        inputSchema: {
          type: "object",
          properties: {
            file_path: { type: "string", description: "Путь к файлу с плейсхолдерами" },
            session_id: { type: "string", description: "ID сессии с маппингами" },
          },
          required: ["file_path", "session_id"],
        },
        async execute(input) {
          const { file_path, session_id } = input as { file_path: string; session_id: string };
          return await apiRequest("/api/deanonymize_file", { file_path, session_id });
        },
      }),
    );
  },
};

export default plugin;
