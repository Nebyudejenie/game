import { api, escapeHtml, fmtDate } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Rounds";

export async function render(container) {
  container.innerHTML = `
    <h1>Rounds</h1>
    <form id="round-filter-form" class="inline-form">
      <input type="number" id="room-id-input" placeholder="Filter by room ID (optional)" />
      <button type="submit" class="btn">Filter</button>
    </form>
    <div id="rounds-list"><p class="loading">Loading…</p></div>
    <div id="round-detail"></div>
  `;

  const form = container.querySelector("#round-filter-form");
  const roomInput = container.querySelector("#room-id-input");
  const listEl = container.querySelector("#rounds-list");
  const detailEl = container.querySelector("#round-detail");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    detailEl.innerHTML = "";
    await loadList();
  });

  async function loadList() {
    listEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const roomId = roomInput.value.trim();
      const path = roomId ? `/rounds?room_id=${encodeURIComponent(roomId)}` : "/rounds";
      const rounds = await api(path);
      renderList(rounds);
    } catch (err) {
      renderError(listEl, err);
    }
  }

  function renderList(rounds) {
    if (rounds.length === 0) {
      listEl.innerHTML = `<p class="empty">No rounds found.</p>`;
      return;
    }
    listEl.innerHTML = `
      <table class="data-table">
        <thead>
          <tr><th>ID</th><th>Room</th><th>Seq</th><th>Status</th><th>Stake</th><th>Pot</th><th>Derash</th><th>Players</th><th>Ended</th></tr>
        </thead>
        <tbody>
          ${rounds.map((r) => `
            <tr class="clickable-row" data-round-id="${r.id}">
              <td>${r.id}</td><td>${r.room_id}</td><td>${r.seq}</td>
              <td><span class="badge badge-${escapeHtml(r.status)}">${escapeHtml(r.status)}</span></td>
              <td>${r.stake}</td><td>${r.pot}</td><td>${r.derash}</td>
              <td>${r.player_count}</td><td>${fmtDate(r.ended_at)}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
    for (const row of listEl.querySelectorAll(".clickable-row")) {
      row.addEventListener("click", () => loadDetail(Number(row.dataset.roundId)));
    }
  }

  async function loadDetail(roundId) {
    detailEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const detail = await api(`/rounds/${roundId}`);
      detailEl.innerHTML = renderDetail(roundId, detail);
      wireDetail(roundId);
    } catch (err) {
      renderError(detailEl, err);
    }
  }

  function renderDetail(roundId, detail) {
    const round = detail.round;
    return `
      <div class="detail-panel">
        <h2 style="margin-top:0">Round #${round.id} <span class="badge badge-${escapeHtml(round.status)}">${escapeHtml(round.status)}</span></h2>
        <div class="detail-grid">
          <div><div class="field-label">Room</div><div class="field-value">${round.room_id}</div></div>
          <div><div class="field-label">Stake</div><div class="field-value">${round.stake} ETB</div></div>
          <div><div class="field-label">Pot</div><div class="field-value">${round.pot} ETB</div></div>
          <div><div class="field-label">Derash</div><div class="field-value">${round.derash} ETB</div></div>
          <div><div class="field-label">Started</div><div class="field-value">${fmtDate(round.started_at)}</div></div>
          <div><div class="field-label">Ended</div><div class="field-value">${fmtDate(round.ended_at)}</div></div>
        </div>

        <h2>Winners</h2>
        ${detail.winners.length === 0 ? '<p class="empty">No winners recorded.</p>' : `
          <table class="data-table">
            <thead><tr><th>User</th><th>Card</th><th>Pattern</th><th>Won on call</th><th>Amount</th></tr></thead>
            <tbody>
              ${detail.winners.map((w) => `
                <tr><td>${w.user_id}</td><td>${w.card_no}</td><td>${escapeHtml(w.pattern)}</td><td>${w.won_on_call}</td><td>${w.amount} ETB</td></tr>
              `).join("")}
            </tbody>
          </table>
        `}

        <h2>Entries (${detail.entries.length})</h2>
        <p class="empty">${detail.entries.map((e) => `#${e.card_no}: user ${e.user_id}`).join(", ") || "None"}</p>

        <div class="action-row">
          <button class="btn" id="fairness-btn">Verify fairness</button>
          <label>Void reason
            <input type="text" id="void-reason" placeholder="required" />
          </label>
          <button class="btn btn-danger" id="void-btn">Void &amp; refund</button>
        </div>
        <div id="fairness-result"></div>
      </div>
    `;
  }

  function wireDetail(roundId) {
    const panel = detailEl.querySelector(".detail-panel");

    panel.querySelector("#fairness-btn").addEventListener("click", async () => {
      const resultEl = panel.querySelector("#fairness-result");
      resultEl.innerHTML = `<p class="loading">Checking…</p>`;
      try {
        const fairness = await api(`/rounds/${roundId}/fairness`);
        resultEl.innerHTML = `<pre class="code-block">${escapeHtml(JSON.stringify(fairness, null, 2))}</pre>`;
      } catch (err) {
        renderError(resultEl, err);
      }
    });

    panel.querySelector("#void-btn").addEventListener("click", async () => {
      const reason = panel.querySelector("#void-reason").value.trim();
      if (!reason) {
        toast("A reason is required.", true);
        return;
      }
      if (!window.confirm(`Void round #${roundId} and refund all players?`)) return;
      try {
        const result = await api(`/rounds/${roundId}/void`, { method: "POST", body: { reason } });
        toast(result.refunded ? "Round voided and refunded." : "Round was already terminal; nothing to refund.");
        loadDetail(roundId);
      } catch (err) {
        toast(err.detail || err.message, true);
      }
    });
  }

  await loadList();
}
