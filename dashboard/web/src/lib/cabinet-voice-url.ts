import { chatId as dashboardChatId, dashboardToken } from '@/lib/api';

const FALLBACK_CABINET_CHAT_ID = 'cabinet-browser';

export function cabinetVoiceUrl(
  meetingId: number,
  chatId = dashboardChatId || FALLBACK_CABINET_CHAT_ID,
  token = dashboardToken,
): string {
  const qs = new URLSearchParams({
    meetingId: String(meetingId),
    chatId,
    token,
  });
  return `/api/cabinet/voice/ui?${qs.toString()}`;
}

export function openCabinetVoiceUrl(url: string): void {
  window.open(url, '_blank', 'noopener,noreferrer');
}
