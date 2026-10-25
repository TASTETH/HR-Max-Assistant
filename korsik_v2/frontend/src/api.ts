let csrf = "";
export function setCsrf(v: string) {
  csrf = v;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, msg: string) {
    super(msg);
    this.status = status;
  }
}

async function handle(r: Response) {
  if (r.status === 401) {
    window.dispatchEvent(new Event("korsik:logout"));
  }
  const text = await r.text();
  let data: any = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!r.ok) {
    const d = data && data.detail;
    const msg = typeof d === "string" ? d : Array.isArray(d) ? d.map((x: any) => x.msg).join("; ") : `Ошибка ${r.status}`;
    throw new ApiError(r.status, msg);
  }
  return data;
}

export const api = {
  get: (url: string) => fetch(url, { credentials: "same-origin" }).then(handle),
  post: (url: string, body?: unknown) =>
    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF": csrf },
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then(handle),
  patch: (url: string, body: unknown) =>
    fetch(url, {
      method: "PATCH",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF": csrf },
      body: JSON.stringify(body),
    }).then(handle),
  del: (url: string) =>
    fetch(url, { method: "DELETE", credentials: "same-origin", headers: { "X-CSRF": csrf } }).then(handle),
  upload: (url: string, form: FormData) =>
    fetch(url, { method: "POST", credentials: "same-origin", headers: { "X-CSRF": csrf }, body: form }).then(handle),
};

export const fmtDT = (s?: string | null) =>
  s ? new Date(s).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
export const fmtD = (s?: string | null) => (s ? new Date(s).toLocaleDateString("ru-RU") : "—");

export const MODE: Record<string, string> = { auto: "Корсик", waiting_hr: "Ждёт HR", hr_active: "HR в диалоге" };
export const PRIO: Record<string, string> = { low: "низкий", medium: "средний", high: "высокий" };
export const CASE_ST: Record<string, string> = { new: "новое", in_progress: "в работе", resolved: "решено" };
export const EV_ST: Record<string, string> = {
  scheduled: "запланировано",
  sending: "отправляется",
  sent: "отправлено",
  waiting_reply: "ждёт ответа",
  done: "завершено",
  skipped: "пропущено",
  cancelled: "отменено",
  failed: "ошибка",
};
export const EMP_ST: Record<string, string> = {
  draft: "черновик",
  active: "сопровождение",
  paused: "пауза",
  completed: "завершено",
  deactivated: "отключён",
};
export const SENT: Record<string, string> = { positive: "позитив", neutral: "нейтрально", negative: "негатив", unknown: "не определено" };
export const AUTHOR: Record<string, string> = {
  employee: "Сотрудник",
  bot_script: "Корсик · сценарий",
  bot_llm: "Корсик · ответ по регламентам",
  bot_system: "Корсик",
  hr: "HR",
};
export const DELIV: Record<string, string> = {
  pending: "в очереди",
  sending: "отправляется",
  sent: "принято MAX",
  uncertain: "неизвестно",
  failed: "ошибка",
  cancelled: "отменено",
};
