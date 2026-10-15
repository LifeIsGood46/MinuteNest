/* Type-to-search dropdowns (combobox over a native <select>). Loaded before app.js. */
(() => {
  // ---------------- type-to-search dropdowns ----------------
  // A native <select> with 400 OpenRouter models (or 99 languages) can't be
  // searched. This wraps a select in a combobox: type to filter in real time,
  // arrows/Enter/click to pick. The <select> stays in the DOM as the single
  // source of truth (value, options, change events), so every existing
  // read/write of it keeps working. `allowCustom` also lets the user type a
  // value that isn't in the list (a model released five minutes ago).

  // Options matching the search: exact match first, then "starts with", then
  // "contains" -- so typing a full name always puts that entry on top.
  function searchHits(options, q) {
    if (!q) return options;
    const rank = (o) => {
      const t = o.text.toLowerCase(), v = o.value.toLowerCase();
      if (v === q || t === q) return 0;
      if (v.startsWith(q) || t.startsWith(q)) return 1;
      return 2;
    };
    return options
      .filter((o) => o.text.toLowerCase().includes(q) || o.value.toLowerCase().includes(q))
      .map((o, i) => [rank(o), i, o])
      .sort((a, b) => a[0] - b[0] || a[1] - b[1])
      .map((r) => r[2]);
  }

  function div(className, text) {
    const d = document.createElement("div");
    d.className = className;
    d.textContent = text;
    return d;
  }

  class Combobox {
    constructor(sel, { allowCustom, maxShown }) {
      this.sel = sel;
      this.allowCustom = allowCustom;
      this.maxShown = maxShown;
      this.items = [];   // currently listed [{value, text, custom}]
      this.active = -1;
      this.build();
      this.wire();
      this.watchSelect();
    }

    build() {
      const sel = this.sel;
      this.wrap = div("cbx", "");
      this.wrap.dataset.for = sel.id || "";
      const input = this.input = document.createElement("input");
      input.type = "text";
      input.className = "cbx-input";
      input.autocomplete = "off";
      input.spellcheck = false;
      input.setAttribute("role", "combobox");
      input.setAttribute("aria-autocomplete", "list");
      input.setAttribute("aria-expanded", "false");
      input.setAttribute("aria-label", sel.getAttribute("aria-label") || sel.id || "Search");
      // the hint under the control describes the box the user actually focuses
      if (sel.hasAttribute("aria-describedby")) input.setAttribute("aria-describedby", sel.getAttribute("aria-describedby"));
      this.caret = document.createElement("span");
      this.caret.className = "cbx-caret";
      this.caret.setAttribute("aria-hidden", "true");
      this.list = div("cbx-list", "");
      this.list.setAttribute("role", "listbox");
      this.list.hidden = true;
      sel.parentNode.insertBefore(this.wrap, sel);
      this.wrap.append(input, this.caret, this.list, sel);
      sel.classList.add("cbx-native");
      sel.tabIndex = -1;
      sel.setAttribute("aria-hidden", "true");
    }

    realOptions() {
      return [...this.sel.options].filter((o) => o.value !== "__custom__");
    }

    selectedText() {
      const o = this.sel.selectedOptions && this.sel.selectedOptions[0];
      return o && o.value !== "__custom__" ? o.textContent : "";
    }

    sync() {
      this.input.disabled = this.sel.disabled;
      this.wrap.classList.toggle("disabled", this.sel.disabled);
      // While the user is searching, a list rebuild (new options arriving,
      // a value restored by code) must not overwrite what they're typing:
      // the selected name used to be written back, and the search got
      // appended to it ("aion-labs/aion-2.0openrouter/fr"). Refresh the hits.
      if (!this.list.hidden && document.activeElement === this.input) {
        this.input.placeholder = this.selectedText();
        this.render(this.input.value);
        return;
      }
      this.input.value = this.selectedText();
    }

    close() {
      this.list.hidden = true;
      this.input.setAttribute("aria-expanded", "false");
      this.active = -1;
      if (!document.querySelector(".cbx-list:not([hidden])")) document.body.classList.remove("cbx-open");
    }

    setActive(i) {
      const nodes = this.list.querySelectorAll(".cbx-option");
      nodes.forEach((n) => n.classList.remove("active"));
      this.active = Math.max(-1, Math.min(nodes.length - 1, i));
      if (this.active >= 0) {
        nodes[this.active].classList.add("active");
        nodes[this.active].scrollIntoView({ block: "nearest" });
      }
    }

    render(query) {
      const q = (query || "").trim().toLowerCase();
      const hits = searchHits(this.realOptions().map((o) => ({ value: o.value, text: o.textContent })), q);
      this.items = hits.slice(0, this.maxShown);
      const typed = (query || "").trim();
      // a name that matches nothing in the list can still be used as typed
      if (this.allowCustom && typed && !hits.length) {
        this.items.push({ value: typed, text: typed, custom: true });
      }
      this.drawRows(hits.length);
      this.list.hidden = false;
      document.body.classList.add("cbx-open");
      this.input.setAttribute("aria-expanded", "true");
      const cur = this.items.findIndex((it) => it.value === this.sel.value);
      this.setActive(q ? 0 : cur);
    }

    drawRows(hitCount) {
      const list = this.list;
      list.textContent = "";
      this.items.forEach((it, i) => {
        const row = div("cbx-option" + (it.value === this.sel.value ? " selected" : ""),
                        it.custom ? `Use “${it.text}”` : it.text);
        row.setAttribute("role", "option");
        row.title = it.text;
        // mousedown (not click): the input must keep focus, or blur closes the list first
        row.addEventListener("mousedown", (e) => { e.preventDefault(); this.pick(i); });
        row.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); });
        list.appendChild(row);
      });
      if (hitCount > this.items.length) {
        list.appendChild(div("cbx-more", `${hitCount - this.items.length} more — keep typing to narrow down`));
      }
      if (!this.items.length) list.appendChild(div("cbx-more", "No match"));
    }

    pick(i) {
      const it = this.items[i];
      if (!it) return;
      const sel = this.sel;
      if (![...sel.options].some((o) => o.value === it.value)) {
        const opt = document.createElement("option");
        opt.value = it.value;
        opt.textContent = it.text;
        sel.insertBefore(opt, sel.querySelector('option[value="__custom__"]'));
      }
      // Close first: while the list is open, sync() keeps what the user typed
      // (it is a search), so the box went on showing "eng" instead of the pick.
      this.close();
      const changed = sel.value !== it.value;
      sel.value = it.value;
      this.sync();
      if (changed) {
        sel.dispatchEvent(new Event("input", { bubbles: true }));
        sel.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }

    // Focusing empties the field (the current choice stays visible as the
    // placeholder) so you can type a search straight away; leaving without
    // picking restores it.
    open() {
      this.input.placeholder = this.selectedText();
      this.input.value = "";
      this.render("");
    }

    onKey(e) {
      const isOpen = !this.list.hidden;
      const keys = {
        ArrowDown: () => {
          e.preventDefault();
          if (isOpen) this.setActive(this.active + 1); else this.render(this.input.value);
        },
        ArrowUp: () => { e.preventDefault(); this.setActive(this.active - 1); },
        Enter: () => {
          e.preventDefault(); e.stopPropagation();
          if (isOpen) this.pick(this.active >= 0 ? this.active : 0);
        },
        Escape: () => {
          if (isOpen) { e.stopPropagation(); this.close(); this.sync(); }
        },
      };
      if (keys[e.key]) keys[e.key]();
    }

    onBlur() {
      if (this.list.hidden) return;
      const typed = this.input.value.trim();
      this.close();
      if (this.allowCustom && typed && typed !== this.selectedText()) {
        this.items = [{ value: typed, text: typed, custom: true }];
        this.pick(0);
      } else {
        this.sync();
      }
    }

    wire() {
      const input = this.input;
      input.addEventListener("focus", () => this.open());
      input.addEventListener("click", () => { if (this.list.hidden) this.open(); });
      input.addEventListener("input", (e) => { e.stopPropagation(); this.render(input.value); });
      input.addEventListener("keydown", (e) => this.onKey(e));
      input.addEventListener("blur", () => this.onBlur());
      this.caret.addEventListener("mousedown", (e) => {
        e.preventDefault();
        if (!this.list.hidden) { this.close(); return; }
        input.focus();
        if (this.list.hidden) this.open();
      });
    }

    // programmatic `select.value = x` (loadSettings etc.) must refresh the text
    watchSelect() {
      const sel = this.sel;
      const sync = () => this.sync();
      const desc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
      Object.defineProperty(sel, "value", {
        configurable: true,
        get() { return desc.get.call(sel); },
        set(v) { desc.set.call(sel, v); sync(); },
      });
      new MutationObserver(sync).observe(sel, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"] });
    }
  }

  function enhanceSelect(sel, { allowCustom = false, maxShown = 200 } = {}) {
    if (!sel || sel._cbx) return sel && sel._cbx;
    const box = new Combobox(sel, { allowCustom, maxShown });
    sel._cbx = { sync: () => box.sync() };
    box.sync();
    return sel._cbx;
  }

  window.enhanceSelect = enhanceSelect;
})();
