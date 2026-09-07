import { api, escapeHtml, parseMaybeJson } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Contacts";

export async function render(container) {
  container.innerHTML = `
    <h1>Contacts</h1>
    <form id="create-contact-form" class="inline-form">
      <input type="text" name="phone_e164" placeholder="+251911000000" required />
      <input type="text" name="display_name" placeholder="Display name" />
      <input type="text" name="attribute_key" placeholder="Attribute key (optional)" />
      <input type="text" name="attribute_value" placeholder="Attribute value" />
      <button type="submit" class="btn">Add / update</button>
    </form>
    <div id="contacts-error"></div>
    <table class="data-table">
      <thead><tr><th>Phone</th><th>Name</th><th>Attributes</th><th>Opted out</th></tr></thead>
      <tbody id="contacts-body"><tr><td colspan="4" class="loading">Loading…</td></tr></tbody>
    </table>
  `;

  const errorEl = container.querySelector("#contacts-error");
  const bodyEl = container.querySelector("#contacts-body");

  async function refresh() {
    const contacts = await api("/contacts");
    bodyEl.innerHTML = contacts.length === 0
      ? `<tr><td colspan="4" class="empty">No contacts yet.</td></tr>`
      : contacts.map((c) => `
        <tr>
          <td>${escapeHtml(c.phone_e164)}</td>
          <td>${escapeHtml(c.display_name || "—")}</td>
          <td>${escapeHtml(JSON.stringify(parseMaybeJson(c.attributes)))}</td>
          <td>${c.opted_out ? "yes" : "no"}</td>
        </tr>
      `).join("");
  }

  container.querySelector("#create-contact-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    errorEl.innerHTML = "";
    const data = new FormData(event.target);
    const attributes = {};
    if (data.get("attribute_key")) attributes[data.get("attribute_key")] = data.get("attribute_value");
    try {
      await api("/contacts", {
        method: "POST",
        body: { phone_e164: data.get("phone_e164"), display_name: data.get("display_name") || null, attributes },
      });
      event.target.reset();
      toast("Contact saved");
      await refresh();
    } catch (err) {
      renderError(errorEl, err);
    }
  });

  await refresh();
}
