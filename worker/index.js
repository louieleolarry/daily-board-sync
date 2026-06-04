export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') {
      return new Response('ok', { status: 200 });
    }

    if (request.method !== 'POST') {
      return Response.json({ error: 'POST only' }, { status: 405 });
    }

    try {
      const body = await request.json();
      const eventType = body.type || 'card.updated';

      // Skip bot-originated events to avoid infinite loops
      if (body.origin === 'bot-api' || body.actorType === 'bot') {
        return Response.json({ skipped: true, reason: 'bot event' });
      }

      // Trigger GitHub Actions workflow
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
              boardId: body.boardId || '',
              eventType,
              recordId: body.recordId || '',
            },
          }),
        }
      );

      if (res.status === 204) {
        return Response.json({ ok: true, dispatched: eventType });
      }

      const text = await res.text();
      return Response.json({ ok: false, status: res.status, body: text }, { status: 502 });
    } catch (e) {
      return Response.json({ error: e.message }, { status: 500 });
    }
  },
};
