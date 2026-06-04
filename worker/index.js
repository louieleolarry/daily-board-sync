const KV_KEY = 'pending-dispatch';

export default {
  // Webhook handler — store event timestamp, don't dispatch yet
  async fetch(request, env) {
    if (request.method === 'OPTIONS') {
      return new Response('ok', { status: 200 });
    }

    if (request.method !== 'POST') {
      return Response.json({ error: 'POST only' }, { status: 405 });
    }

    try {
      const body = await request.json();

      if (body.origin === 'bot-api' || body.actorType === 'bot') {
        return Response.json({ skipped: true, reason: 'bot event' });
      }

      // Store the latest event timestamp — resets the debounce window
      await env.SYNC_STATE.put(KV_KEY, JSON.stringify({
        timestamp: Date.now(),
        boardId: body.boardId || '',
        eventType: body.type || 'card.updated',
      }));

      return Response.json({ ok: true, queued: true, debounce: `${env.DEBOUNCE_SECONDS}s` });
    } catch (e) {
      return Response.json({ error: e.message }, { status: 500 });
    }
  },

  // Cron trigger — check if debounce window has passed, then dispatch
  async scheduled(event, env, ctx) {
    const raw = await env.SYNC_STATE.get(KV_KEY);
    if (!raw) return;

    const pending = JSON.parse(raw);
    const elapsed = (Date.now() - pending.timestamp) / 1000;
    const debounce = parseInt(env.DEBOUNCE_SECONDS) || 60;

    if (elapsed < debounce) {
      return; // Still in debounce window — user may still be editing
    }

    // Debounce passed — dispatch and clear
    await env.SYNC_STATE.delete(KV_KEY);

    const res = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`,
      {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${env.GITHUB_PAT}`,
          'Accept': 'application/vnd.github+json',
          'User-Agent': 'wfs-sync-relay',
        },
        body: JSON.stringify({
          event_type: 'wfs-board-change',
          client_payload: {
            boardId: pending.boardId,
            eventType: pending.eventType,
            debounced: true,
          },
        }),
      }
    );

    if (res.status === 204) {
      console.log(`Dispatched sync after ${Math.round(elapsed)}s debounce`);
    } else {
      console.log(`Dispatch failed: ${res.status}`);
    }
  },
};
