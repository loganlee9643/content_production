export type Reference = {
  shortcode: string;
  url: string;
  category: string;
  notes: string;
};

export async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch("/api" + path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options?.headers },
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(typeof data?.detail === "string" ? data.detail : "요청을 처리하지 못했습니다.");
  }
  return data as T;
}
