const API_BASE = import.meta.env.VITE_API_BASE || "/api/v1";
const TOKEN_KEY = "skillgo_access_token";

export function getToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number
  ) {
    super(message);
  }
}

type ValidationIssue = {
  loc?: unknown[];
  msg?: string;
  type?: string;
  ctx?: Record<string, unknown>;
};

const FIELD_LABELS: Record<string, string> = {
  slug: "唯一标识",
  name: "Skill 名称",
  summary: "一句话简介",
  description: "详细说明",
  category: "分类",
  visibility: "可见性",
  package: "Skill 包",
};

function validationIssueMessage(issue: ValidationIssue): string {
  const field = [...(issue.loc || [])].reverse().find((item): item is string => typeof item === "string" && item !== "body");
  const label = field ? FIELD_LABELS[field] || field : "提交内容";
  if (field === "slug") return "唯一标识只能使用小写英文字母、数字和连字符，且不能以连字符开头或结尾";
  if (issue.type === "string_too_short" && typeof issue.ctx?.min_length === "number") {
    return `${label}至少需要 ${issue.ctx.min_length} 个字符`;
  }
  if (issue.type === "string_too_long" && typeof issue.ctx?.max_length === "number") {
    return `${label}不能超过 ${issue.ctx.max_length} 个字符`;
  }
  if (issue.type === "missing") return `请填写${label}`;
  return issue.msg ? `${label}：${issue.msg}` : `${label}填写不正确`;
}

function errorMessage(body: unknown, fallback: string): string {
  if (!body || typeof body !== "object" || !("detail" in body)) return fallback;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .filter((item): item is ValidationIssue => Boolean(item) && typeof item === "object")
      .map(validationIssueMessage);
    if (messages.length) return [...new Set(messages)].slice(0, 3).join("；");
  }
  if (detail && typeof detail === "object" && "message" in detail && typeof detail.message === "string") {
    return detail.message;
  }
  return fallback;
}

function responseError(xhr: XMLHttpRequest, fallback = "请求失败"): ApiError {
  let message = `${fallback} (${xhr.status || 0})`;
  try {
    message = errorMessage(JSON.parse(xhr.responseText), message);
  } catch {
    // Keep the safe generic message.
  }
  return new ApiError(message, xhr.status || 0);
}

function openUploadRequest(path: string, accept: string): XMLHttpRequest {
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `${API_BASE}${path}`);
  xhr.setRequestHeader("Accept", accept);
  const token = getToken();
  if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
  return xhr;
}

export function apiUpload<T>(
  path: string,
  body: FormData,
  onProgress: (loaded: number, total: number) => void,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = openUploadRequest(path, "application/json");
    xhr.upload.onprogress = (event) => onProgress(event.loaded, event.lengthComputable ? event.total : 0);
    xhr.upload.onload = () => onProgress(1, 1);
    xhr.onerror = () => reject(new ApiError("网络连接失败，附件未能上传", 0));
    xhr.onload = () => {
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(responseError(xhr));
        return;
      }
      try {
        resolve(JSON.parse(xhr.responseText) as T);
      } catch {
        reject(new ApiError("服务器返回了无效响应", 502));
      }
    };
    xhr.send(body);
  });
}

export function apiNdjsonUpload<T>(
  path: string,
  body: FormData,
  onProgress: (loaded: number, total: number) => void,
  onEvent: (event: T) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = openUploadRequest(path, "application/x-ndjson");
    let consumed = 0;
    let buffer = "";
    let settled = false;
    const consume = (final: boolean) => {
      if (xhr.status < 200 || xhr.status >= 300) return;
      buffer += xhr.responseText.slice(consumed);
      consumed = xhr.responseText.length;
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      if (final && buffer.trim()) {
        lines.push(buffer);
        buffer = "";
      }
      for (const line of lines) {
        if (line.trim()) onEvent(JSON.parse(line) as T);
      }
    };
    xhr.upload.onprogress = (event) => onProgress(event.loaded, event.lengthComputable ? event.total : 0);
    xhr.upload.onload = () => onProgress(1, 1);
    xhr.onprogress = () => {
      try {
        consume(false);
      } catch {
        if (!settled) {
          settled = true;
          xhr.abort();
          reject(new ApiError("服务器返回了无效的流式响应", 502));
        }
      }
    };
    xhr.onerror = () => {
      if (!settled) reject(new ApiError("网络连接失败，附件未能上传", 0));
      settled = true;
    };
    xhr.onload = () => {
      if (settled) return;
      if (xhr.status < 200 || xhr.status >= 300) {
        settled = true;
        reject(responseError(xhr));
        return;
      }
      try {
        consume(true);
        settled = true;
        resolve();
      } catch {
        settled = true;
        reject(new ApiError("服务器返回了无效的流式响应", 502));
      }
    };
    xhr.send(body);
  });
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      message = errorMessage(await response.json(), message);
    } catch {
      // Keep the safe generic message.
    }
    throw new ApiError(message, response.status);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export async function apiBlob(path: string): Promise<Blob> {
  const token = getToken();
  const headers = new Headers();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${API_BASE}${path}`, { headers });
  if (!response.ok) {
    let message = `下载失败 (${response.status})`;
    try {
      message = errorMessage(await response.json(), message);
    } catch {
      // Keep the safe generic message.
    }
    throw new ApiError(message, response.status);
  }
  return response.blob();
}

export async function apiNdjson<T>(
  path: string,
  init: RequestInit,
  onEvent: (event: T) => void,
): Promise<void> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  headers.set("Accept", "application/x-ndjson");
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      message = errorMessage(await response.json(), message);
    } catch {
      // Keep the safe generic message.
    }
    throw new ApiError(message, response.status);
  }
  if (!response.body) throw new ApiError("服务器没有返回可读取的响应流", 502);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (line.trim()) onEvent(JSON.parse(line) as T);
    }
    if (done) break;
  }
  if (buffer.trim()) onEvent(JSON.parse(buffer) as T);
}
