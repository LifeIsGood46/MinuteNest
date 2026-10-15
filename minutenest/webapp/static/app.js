/* MinuteNest UI — client state, SSE updates, settings, drag/drop queue */

(() => {
  const $ = (sel) => document.querySelector(sel);
  const jobs = new Map(); // id -> job DOM element

  // ---------------- settings dialog ----------------
  // Re-place the guide immediately when settings opens/closes. Without this,
  // closing settings mid-step left the coach-mark pointing at a vanished
  // control until the next poll tick — and the dimming shades used to swallow
  // clicks, so the UI felt stuck until you clicked again.
  function wizardRetarget() {
    if (!wizard.active) return;
    const step = wizard.steps[wizard.idx];
    if (step) delete step.__scrolledFor;  // allow a fresh scroll to the new target
    requestPlace();
  }
  function openSettings() {
    $("#settings-overlay").hidden = false;
    wizardRetarget();
    // A job may have downloaded/loaded a model since the form was filled.
    if (typeof refreshModelStatus === "function" && settingsLoaded) refreshModelStatus();
  }
  function closeSettings() {
    if (autoSaveTimer) flushAutoSave(); // never lose a change made just before closing
    $("#settings-overlay").hidden = true;
    wizardRetarget();
  }
  $("#settings-btn").addEventListener("click", openSettings);
  $("#settings-close-btn").addEventListener("click", closeSettings);
  // Click-off closes, but only when the press STARTED on the backdrop: selecting
  // text in a field and releasing the mouse outside the dialog must not close it.
  let settingsPressOnBackdrop = false;
  $("#settings-overlay").addEventListener("mousedown", (e) => {
    settingsPressOnBackdrop = e.target.id === "settings-overlay";
  });
  $("#settings-overlay").addEventListener("click", (e) => {
    if (e.target.id === "settings-overlay" && settingsPressOnBackdrop) closeSettings();
    settingsPressOnBackdrop = false;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#settings-overlay").hidden) closeSettings();
  });

  // The green "Saved" confirmation. Every settings control goes through
  // here; a failed save says so instead of staying silent.
  function showSaved(ok = true) {
    const saved = $("#settings-saved");
    if (!saved) return;
    saved.classList.toggle("settings-save-failed", !ok);
    saved.querySelector(".settings-saved-text").textContent = ok ? "Saved" : "Not saved — try again";
    saved.querySelector(".settings-saved-icon").setAttribute("href", ok ? "#i-check" : "#i-x");
    saved.hidden = false;
    clearTimeout(saved._t);
    saved._t = setTimeout(() => (saved.hidden = true), ok ? 1500 : 4000);
  }

  // Visual effects mode (local to this PC): auto / full / light.
  (() => {
    const sel = $("#fx-mode");
    let mode = "auto";
    try { mode = localStorage.getItem("mn_fx") || "auto"; } catch (_) { /* storage blocked */ }
    sel.value = mode;
    sel.addEventListener("change", () => {
      let stored = true;
      try { localStorage.setItem("mn_fx", sel.value); } catch (_) { stored = false; }
      const lite = sel.value === "lite" || (sel.value === "auto" && (navigator.hardwareConcurrency || 8) <= 4);
      document.documentElement.classList.toggle("lite", lite);
      showSaved(stored);
    });
  })();

  // Searchable fields: 99 languages and hundreds of OpenRouter models.
  enhanceSelect($("#language"));
  enhanceSelect($("#document-language"));
  enhanceSelect($("#llm-model"), { allowCustom: true });

  // ---------------- onboarding: interactive setup wizard ----------------
  // Hello CTA reuses the guarded dropzone opener.
  $("#hello-add-btn").addEventListener("click", () => $("#dropzone").click());
  $("#hello-sample-btn").addEventListener("click", async () => {
    const btn = $("#hello-sample-btn");
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      const r = await fetch("/api/jobs/sample", { method: "POST" });
      if (!r.ok) {
        const text = await r.text().catch(() => "");
        let msg = "unknown";
        try { msg = JSON.parse(text).detail || text || msg; } catch (_) { msg = text || msg; }
        await modalAlert("Could not add the sample", `HTTP ${r.status}: ${msg}`);
        return;
      }
      await refreshJobs();
    } finally {
      btn.disabled = false;
    }
  });

  // ---------------- interactive setup wizard ----------------
  // Coach-marks over the real UI: download a model -> add a recording ->
  // press Start -> open the folder. Every step reacts to real app state,
  // so slow downloads or a different order still work. Skippable.
  const wizard = {
    idx: 0,
    steps: [],
    active: false,
    poll: null,
  };

  function wizardEls() {
    return {
      root: $("#wizard-root"),
      top: $("#wiz-shade-top"),
      left: $("#wiz-shade-left"),
      right: $("#wiz-shade-right"),
      bottom: $("#wiz-shade-bottom"),
      outline: $("#wiz-outline"),
      tip: $("#wizard-tip"),
      title: $("#wizard-title"),
      text: $("#wizard-text"),
      next: $("#wizard-next"),
      back: $("#wizard-back"),
      skip: $("#wizard-skip"),
    };
  }

  function wizardTargets() {
    return {
      settingsBtn: $("#settings-btn"),
      modelCard: $("#card-model"),
      outputCard: $("#card-output"),
      llmCard: $("#card-llm"),
      // Whole cards, not individual controls: the steps' copy talks about the
      // download buttons, the Ollama host/key fields and the save folder, so
      // the spotlight must cover the entire card. The tip docks beside the
      // modal, so a large highlight can't be covered by it.
      modelControls: () => [$("#card-model")],
      outputControls: () => [$("#card-output")],
      llmControls: () => [$("#card-llm")],
      dropzone: $("#dropzone"),
      sampleBtn: $("#hello-sample-btn"),
      startBtn: () => document.querySelector(".job[data-status='staged'] .start-btn"),
      stagedCard: () => {
        const b = document.querySelector(".job[data-status='staged'] .start-btn");
        return b ? b.closest(".job") : null;
      },
      activeJob: () => document.querySelector(".job[data-status='running'], .job[data-status='queued']"),
      openFolderBtn: () => document.querySelector(".job .open-folder-btn"),
    };
  }

  // The clip box a target can actually be seen through: the nearest inner
  // scroll container (the settings modal) or the viewport. A highlight that
  // uses the raw layout rect paints over the dimmed page when the target is
  // scrolled partly out of its container ("the highlight goes off screen").
  function scrollClipBox(node) {
    let el = node.parentElement;
    while (el && el !== document.body) {
      const oy = getComputedStyle(el).overflowY;
      if ((oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight + 1) {
        return el.getBoundingClientRect();
      }
      el = el.parentElement;
    }
    return { left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight };
  }

  // The visible part of a node: raw rect ∩ clip box. Null when nothing of it
  // is on screen (fully scrolled out of the modal or below the fold).
  function visibleRect(node) {
    if (!node) return null;
    const r = node.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return null;
    const clip = scrollClipBox(node);
    const left = Math.max(r.left, clip.left), top = Math.max(r.top, clip.top);
    const right = Math.min(r.right, clip.right), bottom = Math.min(r.bottom, clip.bottom);
    if (right - left <= 0 || bottom - top <= 0) return null;
    return { left, top, right, bottom, clip };
  }

  // Scroll ONLY the nearest scrollable ancestor (usually the settings modal)
  // to bring a target into view. scrollIntoView bubbles to the document too,
  // so it also scrolled the page — the whole UI appeared to "shift" when the
  // wizard advanced or the user scrolled. Minimal movement, no page scroll.
  function scrollTargetIntoView(node) {
    const margin = 24;
    let el = node.parentElement;
    let scroller = null;
    while (el && el !== document.body) {
      const oy = getComputedStyle(el).overflowY;
      if ((oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight + 1) {
        scroller = el;
        break;
      }
      el = el.parentElement;
    }
    const r = node.getBoundingClientRect();
    if (scroller) {
      const sr = scroller.getBoundingClientRect();
      // A target taller than the scroller can never be fully shown; align
      // its top so the user reads it from the beginning (the highlight then
      // covers the visible part and is re-measured on every scroll).
      if (r.height + 2 * margin > sr.height) {
        scroller.scrollTop += r.top - sr.top - margin;
      } else if (r.top < sr.top + margin) {
        scroller.scrollTop += r.top - sr.top - margin;
      } else if (r.bottom > sr.bottom - margin) {
        scroller.scrollTop += r.bottom - sr.bottom + margin;
      }
      return;
    }
    if (r.top < margin) {
      window.scrollBy({ top: r.top - margin, behavior: "auto" });
    } else if (r.bottom > window.innerHeight - margin) {
      window.scrollBy({ top: r.bottom - window.innerHeight + margin, behavior: "auto" });
    }
  }

  // Targets grow AFTER placement: the LLM hint text arrives from the network
  // a second later, error lines wrap, model lists rebuild. The outline used
  // to be measured once, so the card's lower half stayed outside the ring
  // ("the LLM block is not fully highlighted again"). Re-place whenever a
  // spotlighted node (or the modal itself) changes size.
  const measuredSizes = new WeakMap();
  let observedNodes = [];
  const targetObserver = new ResizeObserver(() => {
    if (!wizard.active) return;
    for (const n of observedNodes) {
      const r = n.getBoundingClientRect();
      const prev = measuredSizes.get(n);
      if (!prev || Math.abs(prev.w - r.width) > 0.5 || Math.abs(prev.h - r.height) > 0.5) {
        // The target grew (hint text arrived, an error wrapped): allow a
        // fresh fit-scroll so a card that outgrew the modal is pulled back
        // into view together with its ring.
        const step = wizard.steps[wizard.idx];
        if (!step) return;
        delete step.__scrolledFor;
        // Place SYNCHRONOUSLY: ResizeObserver runs after layout but before
        // paint, so the ring is correct in the same frame the card grows.
        // Deferring to rAF left the ring one frame behind (a visible 1-frame
        // flash of the card sticking out of it).
        wizardPlace(step);
        return;
      }
    }
  });
  function observeTargets(nodes) {
    const same = nodes.length === observedNodes.length
      && nodes.every((n, i) => n === observedNodes[i]);
    if (!same) {
      targetObserver.disconnect();
      observedNodes = nodes.slice();
      observedNodes.forEach((n) => targetObserver.observe(n));
    }
    nodes.forEach((n) => {
      const r = n.getBoundingClientRect();
      measuredSizes.set(n, { w: r.width, h: r.height });
    });
  }

  // Steps: {title, text, target()|targets(), action(), onLeave(), done(), optional, final}
  function buildWizardSteps() {
    const T = wizardTargets();
    return [...wizardSetupSteps(T), addRecordingStep(T), runJobStep(T)];
  }

  function wizardSetupSteps(T) {
    const inSettings = () => !$("#settings-overlay").hidden;
    // Point at the right Settings controls when the modal is open, otherwise
    // at the Settings rail icon (clicking it opens the modal, the poll
    // retargets). Narrow control sets keep the tip clear of the controls.
    const settingsParts = (parts) => () => (inSettings() ? parts() : [T.settingsBtn]);
    return [
      {
        title: "Welcome to MinuteNest",
        text: "This quick walkthrough sets the app up on your machine — it takes about two minutes. You can skip it at any time.",
        target: () => null,
        lockScroll: true, // nothing to point at yet — don't let the page move
        done: () => true,
      },
      {
        title: "Step 1 — get a model",
        text: "Settings is open on the model section. Whisper models trade speed for accuracy: tiny is a 75 MB download and fine for trying things out, large-v3 is the most accurate (about 3 GB). Pick one and press Download — it's saved as your default. Press Next to fetch it later instead; the first job will download whatever it needs.",
        optional: true, // don't trap users who want to skip the 3 GB models
        targets: settingsParts(T.modelControls),
        action: () => openSettings(),
        onLeave: () => saveSettings(),
        done: () => !!document.querySelector("#model-status .badge-done"),
      },
      {
        title: "Step 2 — language & profile",
        text: "Tell the app what your recordings sound like. Pick the language that is actually spoken: Whisper listens for it, and the wrong one gives a garbled or translated transcript. The profile is a dictionary of domain terms it fixes in the transcript (generic is fine to start). Both are just defaults: every job can override them.",
        optional: true,
        targets: settingsParts(T.outputControls),
        action: () => openSettings(),
        // Persist what the user picked so the first job actually uses it —
        // the "changed the dropdown but the job ran the old default" bug.
        onLeave: () => saveSettings(),
        done: () => true,
      },
      {
        title: "Step 3 — formatted output (optional)",
        text: "A plain transcript doesn't need an LLM. If you also want notes, action items or summaries, pick an LLM here: Ollama and LM Studio run on your own PC for free, while cloud providers (Ollama Cloud, OpenRouter, OpenAI, Anthropic) need an API key. Not sure? Skip — you can set this up any time in Settings.",
        optional: true,
        targets: settingsParts(T.llmControls),
        action: () => openSettings(),
        onLeave: () => saveSettings(),
        done: () => true,
      },
    ];
  }

  function anyJob() {
    return document.querySelectorAll(".job").length > 0;
  }

  function jobWith(...statuses) {
    return document.querySelector(statuses.map((s) => `.job[data-status='${s}']`).join(", "));
  }

  function addRecordingStep(T) {
    return {
      title: () => (anyJob() ? "Step 4 — recording added" : "Step 4 — add a recording"),
      // When a job already exists (replaying from Help, or the user moved
      // ahead), don't repeat the "add one" instructions or point at empty
      // space — describe what's next instead.
      text: () => {
        if (!anyJob()) {
          return "Drop any audio or video file into the upload box — or click “Try with a sample” if you have nothing handy. A card appears when the file is ready.";
        }
        if (jobWith("running", "queued")) {
          return "There's already a job on the list and it's running. Press Next to watch it finish.";
        }
        if (jobWith("done")) {
          return "There's already a finished job on the list. Press Next to see where its files went.";
        }
        return "Recording added. Press Next to set it up and run it.";
      },
      // The whole hello pane, not just the sample button: the step's copy
      // talks about the pane, and a single-control hole left the strip
      // under the buttons dark ("there is a bit of a box below the buttons
      // thats not highlighted"). Dropzone stays part of the hole so files
      // can still be dropped straight into it.
      targets: () => {
        if (anyJob()) return [$("#jobs-pane")];
        const hello = $("#landing-hello");
        return hello ? [hello, T.dropzone] : [T.dropzone];
      },
      action: () => {
        finishOnboardSilently();
        if (!$("#settings-overlay").hidden) closeSettings();
      },
      done: anyJob,
      // Adding the recording is this step's whole job — glide on by itself.
      autoAdvance: true,
    };
  }

  // This step is about the CARD's controls and starting the job — not a
  // repeat of "add a recording" (the old copy duplicated step 4).
  function runJobStep(T) {
    return {
      title: () => {
        if (jobWith("running", "queued")) return "Step 5 — running";
        if (jobWith("done")) return "Step 5 — done";
        if (jobWith("failed", "cancelled")) return "Step 5 — that run didn't finish";
        return "Step 5 — run it your way";
      },
      text: () => {
        if (jobWith("running", "queued")) {
          return "Transcription is running. Watch the progress bar on the card, or press Stop to abort. When it finishes, “Open folder” appears there and takes you to the transcript and the timestamped segments file. That's everything — have fun!";
        }
        if (jobWith("done")) {
          return "Finished. The card's “Open folder” button takes you to the transcript, the timestamped segments and the document. That's everything — have fun!";
        }
        if (jobWith("failed", "cancelled")) {
          return "That run didn't finish — press Retry on the card to start it again; your settings are kept.";
        }
        return "Before starting, the card lets you adjust this file's profile, audio language, output type and model — these only affect this recording. Press Start when it looks right (or just press Start now).";
      },
      targets: () => {
        // Point at the whole staged card so profile/language/output/model
        // controls are all reachable while the wizard explains them.
        if (T.startBtn()) return [T.startBtn().closest(".job") || T.startBtn()];
        // the whole card, finished ones too: aimed at the small "Open folder"
        // button, the tip landed on the card's name and status
        const finished = T.openFolderBtn() && T.openFolderBtn().closest(".job");
        return [T.activeJob() || finished || document.querySelector(".job")];
      },
      // Nothing staged to start (done/failed/cancelled/empty) — don't trap.
      done: () => !!document.querySelector(".job .badge-done, .job .badge-running") || !T.startBtn(),
      // Final step: a separate "step 6 — when it finishes" made the user
      // press Next for nothing while a long job ran, and Back from it
      // landed on stale copy ("step number not updating properly").
      // The running/done copy above already explains the finish.
      final: true,
    };
  }

  async function finishOnboardSilently() {
    try {
      await fetch("/api/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ onboarded: true }),
      });
    } catch (e) {
      // the guide carries on; the flag is set again when it finishes
      console.warn("could not save the onboarding flag", e);
    }
  }

  function wizardFinish(markDone) {
    wizard.active = false;
    if (wizard.poll) { clearInterval(wizard.poll); wizard.poll = null; }
    const els = wizardEls();
    els.root.hidden = true;
    els.outline.hidden = true;
    els.tip.hidden = true;
    document.body.classList.remove("wizard-settings");
    document.body.classList.remove("wizard-stack");
    const modalEl = document.querySelector("#settings-overlay .modal-large");
    if (modalEl) {
      modalEl.style.maxWidth = "";   // undo the fit shrink
      modalEl.style.maxHeight = "";  // undo the stacked-mode height
    }
    // The tip's fit sizing is per-step; clear it so a reopened wizard starts
    // from its natural size again.
    els.tip.style.maxWidth = "";
    els.tip.style.maxHeight = "";
    wizardLockScroll(false);
    if (markDone) {
      // One request: two parallel POSTs here raced on the server and could
      // reset the LLM setup (fixed server-side too, see settings.py).
      fetch("/api/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ onboarded: true, wizard_done: true }),
      }).catch(() => {});
    }
  }

  function wizardShow() {
    const els = wizardEls();
    const step = wizard.steps[wizard.idx];
    delete step.__side;  // fresh step: choose the tip's side anew
    delete step.__scrolledFor;
    els.title.textContent = typeof step.title === "function" ? step.title() : step.title;
    els.text.textContent = typeof step.text === "function" ? step.text() : step.text;
    els.next.textContent = step.final ? "Finish" : "Next";
    els.back.hidden = wizard.idx === 0;
    els.root.hidden = false;
    els.tip.hidden = false;
    wizardLockScroll(!!step.lockScroll); // only steps with no target freeze the page
    if (step.action) step.action();
    wizardPlace(step);
    els.next.disabled = !step.done() && !step.optional;
    if (wizard.poll) clearInterval(wizard.poll);
    wizard.poll = setInterval(() => {
      const s = wizard.steps[wizard.idx];
      if (!wizard.active) return;
      const wasDone = !els.next.disabled;
      const isDone = !!s.done();
      // Once this step's action is complete (a job appeared, the model
      // finished downloading) move on by itself: the user shouldn't have to
      // press Next twice for work the guide told them to do.
      if (s.autoAdvance && isDone && !wasDone) {
        wizardNext();
        return;
      }
      els.next.disabled = !isDone && !s.optional;
      // NOTE: no wizardPlace() on every tick. It measures every target with
      // getBoundingClientRect, which forces a layout — doing that every tick
      // forever was the main "buttons take forever" cost on software-rendered
      // VMs. The highlight is re-placed on real events instead: scroll,
      // resize, settings open/close, job-list refreshes, and the tip's own
      // text changing (a longer text makes the tip taller: placed for the
      // old size, it grew over the card it talks about).
      if (refreshStepText(els, s)) requestPlace();
    }, 700);
  }

  // The step's title and text that depend on the jobs; true when either changed.
  function refreshStepText(els, step) {
    let changed = false;
    [[step.text, els.text], [step.title, els.title]].forEach(([source, el]) => {
      if (typeof source !== "function") return;
      const value = source();
      if (el.textContent !== value) {
        el.textContent = value;
        changed = true;
      }
    });
    return changed;
  }

  const WIZ_GAP = 14, WIZ_MARGIN = 12;
  const MODAL_MAX_W = 760, MIN_MODAL = 420, MIN_TIP = 200;

  function wizardPlace(step) {
    const els = wizardEls();
    const raw = stepTargets(step);
    const inSettings = raw.some((n) => n && n.closest && n.closest("#settings-overlay"));
    const stackTipBelow = fitSettingsModal(els.tip, inSettings);
    document.body.classList.toggle("wizard-settings", inSettings);
    document.body.classList.toggle("wizard-stack", inSettings && stackTipBelow);
    scrollStepTargetOnce(step, raw);
    const nodes = raw.filter((n) => !!visibleRect(n));
    observeTargets(nodes);
    const view = { W: window.innerWidth, H: window.innerHeight,
                   tipW: els.tip.offsetWidth || 380, tipH: els.tip.offsetHeight || 160 };
    if (!nodes.length) {
      placeTipWithoutTarget(els, inSettings, view);
      return;
    }
    const box = spotlightBox(nodes, view);
    shadeAround(els, box, view);
    els.tip.style.right = "auto"; els.tip.style.bottom = "auto";
    if (inSettings) placeTipBesideModal(els.tip, stackTipBelow, view);
    else placeTipNearBox(step, els.tip, box, view);
  }

  function stepTargets(step) {
    if (typeof step.targets === "function") return step.targets();
    return step.target ? [step.target()] : [];
  }

  function wizardRect(node, l, t, w, h) {
    node.style.left = `${l}px`; node.style.top = `${t}px`;
    node.style.width = `${Math.max(0, w)}px`; node.style.height = `${Math.max(0, h)}px`;
  }

  // A tip coordinate kept on screen: WIZ_MARGIN <= v <= room - WIZ_MARGIN
  // (room = window size minus the tip's size).
  function onScreen(v, room) {
    return Math.min(Math.max(WIZ_MARGIN, v), Math.max(WIZ_MARGIN, room - WIZ_MARGIN));
  }

  // The Settings modal NEVER moves during the walkthrough (it used to be
  // shoved left by a reserved column, which read as the UI "shifting").
  // Sizing rule, computed from the window only (no feedback loops):
  //   * side-by-side: modal = min(760, W - 2*(tip + gap + margin)), tip
  //     narrowed down to a readable floor if the window is tight;
  //   * when even that leaves the modal too narrow, the tip stacks BELOW
  //     the modal (the overlay switches to top alignment and the modal
  //     reserves the strip) — it must never sit on the card it explains.
  // Returns whether the tip stacks below the modal.
  function fitSettingsModal(tip, inSettings) {
    const modalEl = document.querySelector("#settings-overlay .modal-large");
    if (!modalEl) return false;
    tip.style.maxWidth = "";  // measure the tip's natural width first
    const tipNat = tip.offsetWidth || 380;
    tip.style.maxHeight = "";  // per-step only; cleared unless it stacks
    let wantW = "", wantH = "", stack = false;
    if (inSettings) {
      const H = window.innerHeight;
      const split = settingsSplit(window.innerWidth, tipNat);
      stack = split.stack;
      if (split.tipW < tipNat) tip.style.maxWidth = `${split.tipW}px`;
      wantW = split.modalW < MODAL_MAX_W ? `${split.modalW}px` : "";
      if (stack) {
        // The tip must share the window with the modal: cap its height; the
        // CSS makes only the text scroll, so the buttons stay reachable.
        tip.style.maxHeight = `${Math.round(H * 0.45)}px`;
        const overlayPad = 16, tipH = tip.offsetHeight || 160;
        wantH = `${Math.max(160, H - 2 * overlayPad - WIZ_GAP - tipH - WIZ_MARGIN)}px`;
      }
    }
    if (modalEl.style.maxWidth !== wantW) modalEl.style.maxWidth = wantW;
    if (modalEl.style.maxHeight !== wantH) modalEl.style.maxHeight = wantH;
    return stack;
  }

  // Widths for the modal and the tip side by side in a W-wide window.
  function settingsSplit(W, tipNat) {
    const modalFor = (tipW) => Math.min(MODAL_MAX_W, W - 2 * (tipW + WIZ_GAP + WIZ_MARGIN));
    let tipW = tipNat;
    let modalW = modalFor(tipW);
    if (modalW < MIN_MODAL) {
      // Shrink the tip toward its floor to buy the modal width.
      tipW = Math.min(tipNat, Math.max(MIN_TIP, (W - MIN_MODAL) / 2 - WIZ_GAP - WIZ_MARGIN));
      modalW = modalFor(tipW);
    }
    if (modalW >= MIN_MODAL) return { tipW, modalW, stack: false };
    // no side room at all: stack vertically
    return { tipW: tipNat, modalW: Math.min(MODAL_MAX_W, W - 2 * WIZ_MARGIN), stack: true };
  }

  // A step may target several controls (e.g. dropzone + sample button);
  // spotlights and the outline cover the union so everything stays reachable.
  // Scroll first (once per step, not every poll tick — that would fight the
  // user), then measure: rects captured before the scroll are stale and used
  // to leave the ring hanging over a scrolled-away card ("the highlight
  // goes off screen"). Only the closest scroll container moves, never the
  // page.
  function scrollStepTargetOnce(step, raw) {
    if (!raw.length || step.__scrolledFor === wizard.idx) return;
    step.__scrolledFor = wizard.idx;
    const n0 = raw.find((n) => n && n.getBoundingClientRect);
    const clip = n0 ? scrollClipBox(n0) : null;
    if (!clip) return;
    const r0 = n0.getBoundingClientRect();
    const fit0 = 24; // keep the ring's padding clear of the container edge
    const tooTall = r0.height + 2 * fit0 > clip.bottom - clip.top;
    const hidden = tooTall ? r0.top < clip.top + fit0
                           : (r0.top < clip.top + fit0 || r0.bottom > clip.bottom - fit0);
    if (hidden) scrollTargetIntoView(n0);
  }

  // The union of the targets' VISIBLE rects (8px padding). The visible rect
  // is clipped by the scroll container, so the dim never reaches over a card
  // scrolled halfway out of the settings modal.
  function spotlightBox(nodes, view) {
    const { W, H } = view;
    const pad = 8;
    let x1 = W, y1 = H, x2 = 0, y2 = 0;
    nodes.forEach((n) => {
      const vr = visibleRect(n);
      if (!vr) return;
      x1 = Math.min(x1, vr.left - pad); y1 = Math.min(y1, vr.top - pad);
      x2 = Math.max(x2, vr.right + pad); y2 = Math.max(y2, vr.bottom + pad);
    });
    // Clamp to the box the target is actually visible through: the settings
    // modal's scroll viewport, or the window. Without this clamp the padded
    // ring paints over the dimmed page when a card is scrolled partly out
    // of the modal ("the highlight goes off screen").
    const clip = scrollClipBox(nodes[0]);
    if (clip) {
      x1 = Math.max(x1, clip.left); y1 = Math.max(y1, clip.top);
      x2 = Math.min(x2, clip.right); y2 = Math.min(y2, clip.bottom);
    }
    // Clamp to the viewport and keep x2>=x1 / y2>=y1 even when the target
    // has been scrolled out of view — otherwise the rects invert and the
    // four shades end up covering the whole screen with no visible hole.
    x1 = Math.min(Math.max(0, x1), W);
    y1 = Math.min(Math.max(0, y1), H);
    return { x1, y1, x2: Math.min(Math.max(x1, x2), W), y2: Math.min(Math.max(y1, y2), H) };
  }

  function shadeAround(els, box, view) {
    const { x1, y1, x2, y2 } = box;
    const { W, H } = view;
    wizardRect(els.top, 0, 0, W, y1);
    wizardRect(els.bottom, 0, y2, W, H - y2);
    wizardRect(els.left, 0, y1, x1, y2 - y1);
    wizardRect(els.right, x2, y1, W - x2, y2 - y1);
    els.outline.style.left = `${x1}px`; els.outline.style.top = `${y1}px`;
    els.outline.style.width = `${x2 - x1}px`; els.outline.style.height = `${y2 - y1}px`;
    els.outline.hidden = false;
  }

  // Dock the tip beside the Settings modal, computed from its real on-screen
  // rect. Prefer right, then left; when the layout stacked (narrow window)
  // the modal reserved a bottom strip and the tip goes there, never over the
  // card the step explains.
  function placeTipBesideModal(tip, stackTipBelow, view) {
    const { W, H, tipW, tipH } = view;
    const mr = document.querySelector("#settings-overlay .modal-large").getBoundingClientRect();
    const midY = Math.max(WIZ_MARGIN, Math.min(mr.top + (mr.height - tipH) / 2, H - tipH - WIZ_MARGIN));
    const midX = mr.left + (mr.width - tipW) / 2;
    const fits = (l, t) => l >= WIZ_MARGIN && t >= WIZ_MARGIN
      && l + tipW <= W - WIZ_MARGIN && t + tipH <= H - WIZ_MARGIN;
    const hitsModal = (l, t) => l < mr.right + 2 && l + tipW > mr.left - 2
      && t < mr.bottom + 2 && t + tipH > mr.top - 2;
    let placed;
    if (stackTipBelow) {
      // The modal reserved the strip under itself; the tip docks there,
      // centered under the modal.
      placed = [onScreen(midX, W - tipW), mr.bottom + WIZ_GAP];
    } else {
      placed = [
        [mr.right + WIZ_GAP, midY],          // right of the modal (preferred)
        [mr.left - tipW - WIZ_GAP, midY],    // left
      ].find(([l, t]) => fits(l, t) && !hitsModal(l, t));
    }
    placed = placed || lastResortSpot(mr, view, midX, midY);
    // Hard clamp: whatever happened above, the tip ends fully on screen.
    tip.style.left = `${onScreen(placed[0], W - tipW)}px`;
    tip.style.top = `${onScreen(placed[1], H - tipH)}px`;
  }

  // Keep the tip on screen in the largest free strip, never over the modal.
  function lastResortSpot(mr, view, midX, midY) {
    const { W, H, tipW, tipH } = view;
    const x = onScreen(midX, W - tipW);
    if (H - mr.bottom - WIZ_GAP - WIZ_MARGIN >= 100) return [x, mr.bottom + WIZ_GAP];
    if (mr.top - WIZ_GAP - WIZ_MARGIN >= 100) return [x, mr.top - tipH - WIZ_GAP];
    return [onScreen(mr.left - tipW - WIZ_GAP, W - tipW), midY];
  }

  // Tip placement: prefer the side chosen earlier for this step, so the tip
  // does not hop between corners as the target moves during scrolling ("the
  // UI shifts halfway after scroll"). Falls back to the usual below -> above
  // -> right -> left order when the chosen side no longer fits at all.
  function placeTipNearBox(step, tip, box, view) {
    const { x1, y1, x2, y2 } = box;
    const { W, H, tipW, tipH } = view;
    const fits = (l, t) => l >= WIZ_MARGIN && t >= WIZ_MARGIN
      && l + tipW <= W - WIZ_MARGIN && t + tipH <= H - WIZ_MARGIN;
    const overlaps = (l, t) => l < x2 + 4 && l + tipW > x1 - 4 && t < y2 + 4 && t + tipH > y1 - 4;
    const bySide = {
      below: [onScreen(x1, W - tipW), y2 + WIZ_GAP],
      above: [onScreen(x1, W - tipW), y1 - tipH - WIZ_GAP],
      right: [x2 + WIZ_GAP, onScreen(y1, H - tipH)],
      left: [x1 - tipW - WIZ_GAP, onScreen(y1, H - tipH)],
    };
    const priority = ["below", "above", "right", "left"];
    const order = (step.__side ? [step.__side, ...priority.filter((s) => s !== step.__side)] : priority)
      .map((side) => [side, bySide[side]]);
    // overlap tolerated before losing the step's side
    const placed = order.find(([, [l, t]]) => fits(l, t) && !overlaps(l, t))
      || order.find(([, [l, t]]) => fits(l, t));
    if (placed) {
      step.__side = placed[0];
      tip.style.left = `${placed[1][0]}px`; tip.style.top = `${placed[1][1]}px`;
    } else {
      step.__side = step.__side || "below";
      tip.style.left = `${onScreen(x1, W - tipW)}px`;
      tip.style.top = `${onScreen(y2 + WIZ_GAP, H - tipH)}px`;
    }
  }

  // No visible target: full shade. Dock the tip bottom-center so it doesn't
  // jump between corners while the UI re-renders underneath — but never over
  // the Settings modal: when the step lives inside it (its card scrolled out
  // of view), sit in the free space beside or below the modal instead.
  function placeTipWithoutTarget(els, inSettings, view) {
    const { W, H, tipW, tipH } = view;
    const tip = els.tip;
    wizardRect(els.top, 0, 0, W, H);
    wizardRect(els.bottom, 0, H, W, 0);
    wizardRect(els.left, 0, H, 0, 0);
    wizardRect(els.right, W, H, 0, 0);
    els.outline.hidden = true;
    tip.style.right = "auto"; tip.style.bottom = "auto";
    if (!(inSettings && !$("#settings-overlay").hidden)) {
      tip.style.left = `${Math.max(12, (W - tipW) / 2)}px`;
      tip.style.top = "auto"; tip.style.bottom = "24px";
      return;
    }
    const mr = document.querySelector("#settings-overlay .modal-large").getBoundingClientRect();
    let spot;
    if (H - mr.bottom - WIZ_GAP - WIZ_MARGIN >= Math.min(120, tipH)) {
      spot = [mr.left + (mr.width - tipW) / 2, mr.bottom + WIZ_GAP];
    } else if (W - mr.right - WIZ_GAP - WIZ_MARGIN >= tipW || W - mr.right >= mr.left) {
      spot = [mr.right + WIZ_GAP, mr.top + 20];
    } else {
      spot = [mr.left - tipW - WIZ_GAP, mr.top + 20];
    }
    tip.style.left = `${onScreen(spot[0], W - tipW)}px`;
    tip.style.top = `${onScreen(spot[1], H - tipH)}px`;
  }

  // The walkthrough must not fight the page: while it is active the document
  // is locked (no scrolling behind the shades, no accidental navigation),
  // and we re-place the highlight on scroll so it never drifts from its target.
  function wizardLockScroll(lock) {
    document.documentElement.style.overflow = lock ? "hidden" : "";
  }
  // Re-place at most once per animation frame: scroll fires in bursts and
  // each placement forces layout + style writes (very costly on software
  // rendering, where it made the whole UI feel laggy).
  let placeQueued = false;
  function requestPlace() {
    if (placeQueued || !wizard.active) return;
    placeQueued = true;
    requestAnimationFrame(() => {
      placeQueued = false;
      if (wizard.active) wizardPlace(wizard.steps[wizard.idx]);
    });
  }
  window.addEventListener("scroll", requestPlace, true);

  // The tip floats over the app, so a wheel gesture on it would normally be
  // swallowed. Forward the delta to the scrollable container underneath
  // (the settings modal, else the page) so users can scroll without moving
  // the mouse off the guide box.
  document.addEventListener("wheel", (e) => {
    if (!wizard.active) return;
    if (!e.target.closest || !e.target.closest("#wizard-tip")) return;
    const scroller = !$("#settings-overlay").hidden
      ? document.querySelector("#settings-overlay .modal-large") : null;
    if (scroller) {
      scroller.scrollTop += e.deltaY;
      e.preventDefault();
    } else if (document.documentElement.style.overflow !== "hidden") {
      window.scrollBy({ top: e.deltaY });
      e.preventDefault();
    }
  }, { passive: false });

  // Steps may opt out at runtime (skipIf): e.g. the "when it finishes" step
  // is pointless once the job is already done — step 5 says everything.
  function nextVisible(from, dir) {
    let i = from + dir;
    while (i >= 0 && i < wizard.steps.length) {
      const s = wizard.steps[i];
      if (!(typeof s.skipIf === "function" && s.skipIf())) return i;
      i += dir;
    }
    return -1;
  }

  async function wizardNext() {
    const step = wizard.steps[wizard.idx];
    if (step.onLeave) await step.onLeave();
    if (step.final) return wizardFinish(true);
    const n = nextVisible(wizard.idx, 1);
    if (n === -1) return wizardFinish(true);
    wizard.idx = n;
    wizardShow();
  }

  function wizardBack() {
    const n = nextVisible(wizard.idx, -1);
    if (n !== -1) {
      wizard.idx = n;
      wizardShow();
    }
  }

  function openWizard() {
    wizard.steps = buildWizardSteps();
    wizard.idx = 0;
    wizard.active = true;
    wizardShow();
  }

  // A window resize invalidates the previous fit-scroll: the modal may now be
  // too short for a card that was fully in view at the old size. Allow a
  // fresh scroll-to-fit, then re-place. Without this the ring tracked the
  // card's clipped slice and sat ~145px above its bottom after shrinking the
  // window (real report: "doesnt adjust to new window").
  function handleResize() {
    if (!wizard.active) return;
    const step = wizard.steps[wizard.idx];
    if (step) delete step.__scrolledFor;
    requestPlace();
  }

  $("#wizard-next").addEventListener("click", wizardNext);
  $("#wizard-back").addEventListener("click", wizardBack);
  $("#wizard-skip").addEventListener("click", () => wizardFinish(true));
  window.addEventListener("resize", handleResize);

  // ---------------- help overlay ----------------
  function openHelp() {
    $("#help-overlay").hidden = false;
    const folderEl = $("#help-folders-paths");
    if (folderEl && !folderEl.textContent.trim()) updateFoldersPaths();
    fetch("/api/log?tail=0").then((r) => r.json()).then((d) => {
      const el = $("#help-log-path");
      if (el) el.textContent = d.exists ? `Current log: ${d.path}` : `Log will appear here: ${d.path}`;
    }).catch(() => {});
  }
  function closeHelp() { $("#help-overlay").hidden = true; }
  $("#help-btn").addEventListener("click", openHelp);
  $("#help-close-btn").addEventListener("click", closeHelp);
  $("#help-overlay").addEventListener("click", (e) => {
    if (e.target.id === "help-overlay") closeHelp();
  });
  $("#help-guide-btn").addEventListener("click", () => {
    closeHelp();
    openWizard();
  });
  $("#help-log-btn").addEventListener("click", async () => {
    try {
      const r = await fetch("/api/log/open-folder", { method: "POST" });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        await modalAlert("Could not open log folder", d.detail || `HTTP ${r.status}`);
      }
    } catch (e) {
      await modalAlert("Could not open log folder", String(e));
    }
  });

  // Any uncaught UI error goes to the backend log too, so one file tells the
  // whole story. Errors already surfaced via modals are reported at the catch
  // site; this covers the rest.
  function reportUiError(where, message) {
    fetch("/api/log/ui-error", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ where, message }),
    }).catch(() => {
      // the server is gone (shut down or restarting): there is no log to write
      // to, and reporting this failure would only fail the same way
    });
  }
  window.addEventListener("error", (e) => reportUiError(`${e.filename}:${e.lineno}`, String(e.message)));
  window.addEventListener("unhandledrejection", (e) => reportUiError("unhandledrejection", String(e.reason)));

  // ---------------- dropdown population ----------------
  let taskDescriptions = {};
  let profileList = [];   // [{name, description, model, corrections}]
  // Short names for the card's Output dropdown; the description goes in the hint
  // under it (name + description in a quarter-width dropdown got cut mid-word).
  const TASK_LABELS = {
    raw: "Transcript", clean: "Clean transcript", notes: "Meeting notes", actions: "Action items",
    testplan: "Test plan", summary: "Summary", custom: "Custom prompt",
  };
  let taskList = [];      // ["raw", "clean", ...]
  // What an output does: the hint under its dropdown, on a card and in Settings.
  function taskHint(task) {
    return `${taskDescriptions[task] || ""}${task === "raw" ? "" : " Written by the LLM."}`;
  }
  let languageList = [];  // [{code, name}] sorted by display name

  async function loadProfiles() {
    const r = await fetch("/api/profiles").then((res) => res.json());
    taskDescriptions = r.task_descriptions || {};
    profileList = r.profiles || [];
    taskList = r.tasks || [];

    // Settings: default profile / default task selectors
    const defProf = $("#default-profile");
    defProf.innerHTML = "";
    profileList.forEach((p) =>
      defProf.insertAdjacentHTML("beforeend", `<option value="${p.name}">${p.name}</option>`)
    );
    const defTask = $("#default-task");
    defTask.innerHTML = "";
    // Short names, the description under it (as on a card): "clean — Transcript
    // with punctuation and paragraphs fixed." did not fit the dropdown
    defTask.innerHTML = optionsHtml(taskList.map((t) => [t, TASK_LABELS[t] || t]), "");
    // Help overlay: output type reference
    const types = $("#help-output-types");
    if (types) {
      types.innerHTML = taskList.map((t) =>
        `<div class="help-type"><code>${escapeHtml(t)}</code> ${escapeHtml(taskDescriptions[t] || "")}</div>`
      ).join("");
    }
    // Language choices for per-job selects (same source as Settings).
    try {
      const langs = await fetch("/api/languages").then((res) => res.json());
      languageList = [...(langs.languages || [])].sort((a, b) => a.name.localeCompare(b.name));
    } catch (e) {
      console.warn("language list unavailable; job cards offer the default only", e);
      languageList = [];
    }
  }

  // Helpers for LLM model select + custom
  function getLlmModelValue() {
    const sel = $("#llm-model");
    if (sel.value === "__custom__") {
      return $("#llm-model-custom").value.trim();
    }
    return sel.value;
  }
  function setLlmModelValue(model) {
    const sel = $("#llm-model");
    const custom = $("#llm-model-custom");
    // If model exists in options, select it
    const hasOption = [...sel.options].some((o) => o.value === model);
    if (model && !hasOption && model !== "__custom__") {
      // Add it as an option (for previously saved custom values)
      const opt = document.createElement("option");
      opt.value = model;
      opt.textContent = model;
      sel.insertBefore(opt, sel.querySelector('option[value="__custom__"]'));
    }
    if (hasOption || [...sel.options].some((o) => o.value === model)) {
      sel.value = model;
      custom.style.display = "none";
    } else if (!model) {
      sel.value = sel.options[0] ? sel.options[0].value : "";
      custom.style.display = "none";
    } else {
      sel.value = "__custom__";
      custom.value = model;
      custom.style.display = "block";
    }
  }
  // Show/hide custom input when select changes
  document.addEventListener("change", (e) => {
    if (e.target.id === "llm-model") {
      const custom = $("#llm-model-custom");
      custom.style.display = e.target.value === "__custom__" ? "block" : "none";
      if (e.target.value === "__custom__") custom.focus();
    }
  });

  // ---------------- settings ----------------
  let settingsCache = {}; // last loaded settings (job cards show effective defaults)
  let settingsLoaded = false; // the form has been filled from the server
  async function loadSettings(prefetched) {
    const s = prefetched || await fetch("/api/settings").then((r) => r.json());
    settingsCache = s;
    await fillModelControls(s);
    await fillLanguageControls(s);
    $("#output-dir").value = s.output_dir || "";
    $("#default-profile").value = s.default_profile || "generic";
    $("#default-task").value = s.default_task || "raw";
    showDefaultTaskHint();
    $("#output-format").value = s.output_format || "both";
    fillLlmControls(s.llm);
    toggleLlmFields();
    updateFoldersPaths();
    refreshModelStatus();
    refreshFfmpegStatus();
    settingsLoaded = true;
    return s;
  }

  async function fillModelControls(s) {
    // The options come from /api/models; when that fetch lost the race the
    // select is still empty and setting .value to "tiny" silently fell back
    // to the first option — then a wizard save persisted whatever happened
    // to be selected. Populate before assigning (real report: "I set tiny but
    // the job ran large-v3 / I had to update models to fix it").
    const sizeSel = $("#model-size");
    if (!sizeSel.options.length) {
      const ms = await fetch("/api/models").then((r) => r.json()).catch(() => null);
      if (ms && ms.models) {
        ms.models.forEach((m) =>
          sizeSel.insertAdjacentHTML("beforeend", `<option value="${m.size}">${m.size}</option>`));
      }
    }
    sizeSel.value = s.model_size;
    if (!sizeSel.value && sizeSel.options.length) sizeSel.selectedIndex = 0;
    $("#model-device").value = s.device === "cuda" ? "cuda" : "cpu";
    $("#model-compute").value = s.compute_type;
    syncComputeOptions(false);
  }

  async function fillLanguageControls(s) {
    // Language dropdown is populated from the model build itself,
    // so only valid codes can ever be selected.
    const langs = await fetch("/api/languages").then((r) => r.json()).catch(() => ({ languages: [] }));
    const sorted = [...langs.languages].sort((a, b) => a.name.localeCompare(b.name));
    const options = sorted.map((l) =>
      `<option value="${escapeHtml(l.code)}">${escapeHtml(l.name)} (${escapeHtml(l.code)})</option>`).join("");
    const langSel = $("#language");
    langSel.innerHTML = options;
    langSel.value = s.language || "ru";
    if (!langSel.value && langSel.options.length) langSel.selectedIndex = 0;
    const docSel = $("#document-language");
    docSel.innerHTML = `<option value="">Same as the audio</option>${options}`;
    docSel.value = s.document_language || "";
  }

  // Which saved key the shared "API key" box shows, per cloud provider.
  const CLOUD_KEY_FIELD = { anthropic: "anthropic_api_key", openrouter: "openrouter_api_key" };

  function fillLlmControls(llm) {
    $("#llm-provider").value = llm.provider;
    // Defer setting LLM model until after model list is populated
    // Store it for refreshLlmModels to pick up
    window._pendingLlmModel = llm.model || "";
    window._pendingLlmProvider = llm.provider; // the saved model belongs to this provider only
    $("#ollama-host").value = llm.ollama_host || "";
    $("#ollama-key").value = llm.ollama_api_key || "";
    $("#compat-url").value = llm.compat_base_url || "";
    $("#compat-key").value = llm.compat_api_key || "";
    $("#cloud-key").value = llm[CLOUD_KEY_FIELD[llm.provider] || "openai_api_key"] || "";
  }

  // float16 / int8_float16 need a GPU; on CPU they crash the job ("Requested
  // float16 compute type, but the target device or backend do not support
  // efficient float16 computation"). Grey them out and move to int8, and go
  // back to float16 when the user picks the GPU again.
  const GPU_ONLY_COMPUTE = new Set(["float16", "int8_float16"]);
  function syncComputeOptions(deviceJustChanged) {
    const cpu = $("#model-device").value === "cpu";
    const comp = $("#model-compute");
    [...comp.options].forEach((o) => { o.disabled = cpu && GPU_ONLY_COMPUTE.has(o.value); });
    if (cpu && GPU_ONLY_COMPUTE.has(comp.value)) comp.value = "int8";
    else if (!cpu && deviceJustChanged && comp.value === "int8") comp.value = "float16";
  }
  $("#model-device").addEventListener("change", () => syncComputeOptions(true));

  function updateModelPill(size, installed, loaded) {
    const pill = $("#model-pill");
    if (!pill) return;
    const state = !installed ? "not downloaded" : loaded ? "in memory" : "installed";
    pill.textContent = `${size} · ${state}`;
  }

  function updateFoldersPaths() {
    const el = $("#folders-paths");
    const out = ($("#output-dir").value.trim() || "results").replace(/[/\\]+$/, "");
    const html = `Models: <code>models/</code><br>Results: <code>${escapeHtml(out)}/&lt;name&gt;/</code><br>State: <code>data/</code> &nbsp; Temp: <code>data/work/</code>`;
    if (el) el.innerHTML = html;
    const helpEl = $("#help-folders-paths");
    if (helpEl) helpEl.innerHTML = html;
  }

  async function refreshFfmpegStatus() {
    const el = $("#ffmpeg-status");
    if (!el) return;
    try {
      const s = await fetch("/api/ffmpeg").then((r) => r.json());
      if (s.ffmpeg && s.ffprobe) {
        el.textContent = s.bundled
          ? "ffmpeg: bundled with the app"
          : "ffmpeg: found on PATH";
        el.style.color = "";
      } else {
        el.textContent = s.frozen
          ? "ffmpeg: MISSING from the app package — re-download the release zip"
          : "ffmpeg: not found on PATH (packaged builds bundle it)";
        el.style.color = "var(--fail)";
      }
    } catch (_) {
      el.textContent = "";
    }
  }
  function toggleLlmFields() {
    const prov2 = $("#llm-provider").value;
    $("#llm-ollama-fields").hidden = prov2 !== "ollama";
    $("#llm-compat-fields").hidden = prov2 !== "compat";
    $("#llm-cloud-fields").hidden = !(prov2 === "anthropic" || prov2 === "openai" || prov2 === "openrouter");
    // No fields at all for "none" — that's the point of it. The model box
    // too: it used to say "No models found. Check the host and key." for a
    // setup that needs neither.
    $("#llm-model-ctl").hidden = prov2 === "none";
  }
  document.addEventListener("change", (e) => {
    if (e.target.id === "llm-provider") {
      toggleLlmFields();
      refreshLlmModels(); // list belongs to the provider; never show stale text
    }
  });

  // ---- instant-apply: every settings control saves itself ----
  // No Save button. Selects/checkboxes save on change; text and key fields save
  // after a short typing pause. Host/URL/key edits also re-list the models so
  // the user sees at once whether the credentials work.
  let autoSaveTimer = null;
  let autoSaveRefresh = false;
  const PATCHED_IDS = new Set(["model-size", "language", "document-language", "default-profile", "default-task", "output-format"]);
  const LLM_CONN_IDS = new Set(["ollama-host", "ollama-key", "compat-url", "compat-key", "cloud-key"]);
  function scheduleAutoSave({ refresh = false, delay = 0 } = {}) {
    if (!settingsLoaded) return; // the form isn't filled yet; saving would wipe settings
    if (refresh) autoSaveRefresh = true;
    clearTimeout(autoSaveTimer);
    autoSaveTimer = setTimeout(flushAutoSave, delay);
  }
  async function flushAutoSave() {
    clearTimeout(autoSaveTimer);
    autoSaveTimer = null;
    const refresh = autoSaveRefresh;
    autoSaveRefresh = false;
    await saveSettings({ refreshModels: refresh });
  }
  function onSettingsEdit(e, delay) {
    const t = e.target;
    if (!t || !t.id || PATCHED_IDS.has(t.id) || t.id === "fx-mode") return; // these save themselves / are local only
    if (t.id === "llm-provider") { scheduleAutoSave({ delay }); return; } // list refresh is handled by its own listener
    scheduleAutoSave({ refresh: LLM_CONN_IDS.has(t.id), delay });
  }
  $("#settings-overlay").addEventListener("change", (e) => onSettingsEdit(e, 0));
  $("#settings-overlay").addEventListener("input", (e) => {
    if (e.target && e.target.tagName === "INPUT") onSettingsEdit(e, 800);
  });

  async function saveSettings({ refreshModels = true } = {}) {
    // NEVER save before the form was populated from the server. The wizard
    // opens before settings finish loading; saving then wrote the still-empty
    // form over the saved config — provider "none", blank host/key — which
    // surfaced later as "No LLM is configured" while the UI showed a
    // configured provider (real report, reproduced).
    if (!settingsLoaded) {
      await loadSettings();
    }
    const body = settingsFormBody();
    let r;
    try {
      r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch (_) {
      showSaved(false);
      return false;
    }
    if (!r.ok) {
      showSaved(false);
      return false;
    }
    const llmDefined = Object.fromEntries(Object.entries(body.llm).filter(([, v]) => v !== undefined));
    Object.assign(settingsCache, { ...body, llm: { ...settingsCache.llm, ...llmDefined } });
    showSaved();
    updateFoldersPaths();
    refreshJobs(); // waiting cards show the saved defaults
    if (refreshModels) refreshLlmModels(); // new key/host saved -> re-list immediately
    return true;
  }

  // A dropdown's value, or the saved one while its list has not loaded.
  function formOrSaved(id, key, fallback) {
    return $(id).value || settingsCache[key] || fallback;
  }

  function settingsFormBody() {
    const docSel = $("#document-language");
    return {
      model_size: formOrSaved("#model-size", "model_size", "large-v3"),
      device: $("#model-device").value,
      compute_type: $("#model-compute").value,
      language: formOrSaved("#language", "language", "ru"),
      // only "Same as the audio" listed = the language list failed to load:
      // keep the saved choice instead of writing "" over it
      document_language: docSel.options.length > 1 ? docSel.value : (settingsCache.document_language || ""),
      default_profile: formOrSaved("#default-profile", "default_profile", "generic"),
      default_task: formOrSaved("#default-task", "default_task", "raw"),
      output_format: formOrSaved("#output-format", "output_format", "both"),
      output_dir: $("#output-dir").value.trim(),
      llm: llmFormBody($("#llm-provider").value),
    };
  }

  // The LLM section; the shared key box belongs to the chosen cloud provider
  // only (undefined = keep the other providers' saved keys).
  function llmFormBody(prov) {
    const key = $("#cloud-key").value;
    return {
      provider: prov,
      model: prov === "none" ? "" : getLlmModelValue(),
      ollama_host: $("#ollama-host").value,
      ollama_api_key: $("#ollama-key").value,
      compat_base_url: $("#compat-url").value,
      compat_api_key: $("#compat-key").value,
      anthropic_api_key: prov === "anthropic" ? key : undefined,
      openai_api_key: prov === "openai" ? key : undefined,
      openrouter_api_key: prov === "openrouter" ? key : undefined,
    };
  }

  // One-key patch, no form-wide side effects (used for dropdowns whose value
  // a new job must honor immediately — "I set tiny but the job ran large-v3").
  async function saveSettingPatch(patch) {
    Object.assign(settingsCache, patch); // job cards show the effective default
    try {
      const r = await fetch("/api/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (r.ok) showSaved(); else showSaved(false);
    } catch (_) {
      showSaved(false);
    }
    refreshJobs(); // waiting cards say "default (<what Settings now says>)"
  }
  $("#model-size").addEventListener("change", () =>
    saveSettingPatch({ model_size: $("#model-size").value }));
  $("#language").addEventListener("change", () =>
    saveSettingPatch({ language: $("#language").value }));
  $("#document-language").addEventListener("change", () =>
    saveSettingPatch({ document_language: $("#document-language").value }));
  $("#default-profile").addEventListener("change", () =>
    saveSettingPatch({ default_profile: $("#default-profile").value }));
  $("#default-task").addEventListener("change", () => {
    showDefaultTaskHint();
    saveSettingPatch({ default_task: $("#default-task").value });
  });
  function showDefaultTaskHint() {
    $("#default-task-hint").textContent = taskHint($("#default-task").value);
  }
  $("#output-format").addEventListener("change", () =>
    saveSettingPatch({ output_format: $("#output-format").value }));
  $("#output-dir").addEventListener("input", updateFoldersPaths);

  // ---------------- model management ----------------
  const modelBtns = ["download-model-btn", "load-model-btn", "unload-model-btn", "delete-model-btn"];
  function hideModelBtns() {
    modelBtns.forEach((id) => { $("#" + id).hidden = true; });
  }

  async function refreshModelStatus() {
    const size = $("#model-size").value;
    const s = await fetch(`/api/models/${size}/status`).then((r) => r.json());
    updateModelPill(size, s.installed, s.loaded);
    const el = $("#model-status");
    hideModelBtns();
    $("#model-progress").hidden = true;
    const sizeTxt = s.size_mb ? ` · ${s.size_mb >= 1000 ? (s.size_mb / 1000).toFixed(1) + " GB" : s.size_mb + " MB"}` : "";
    const loadedTxt = s.loaded ? ` <span class="badge badge-running">in memory</span>` : "";
    if (s.installed) {
      el.innerHTML = `<span class="badge badge-done">installed</span> ${size}${sizeTxt}${loadedTxt}`;
      $("#model-progress-text").textContent = s.error
        ? `Load failed: ${s.error.replace(/^error:\s*/, "")}`
        : "";
      if (s.loaded) {
        $("#unload-model-btn").hidden = false;
      } else {
        $("#load-model-btn").hidden = false;
      }
      $("#delete-model-btn").hidden = false;
    } else if (s.downloading) {
      el.innerHTML = `<span class="badge badge-running">downloading</span> ${size}`;
      $("#model-progress").hidden = false;
    } else if (s.loading) {
      el.innerHTML = `<span class="badge badge-running">loading</span> ${size}${sizeTxt}`;
      $("#model-progress").hidden = false;
      $("#model-progress-fill").style.width = `100%`;
      $("#model-progress-text").textContent = `Loading ${size} into memory...`;
    } else {
      el.innerHTML = `<span class="badge badge-queued">not installed</span> ${size}`;
      $("#model-progress-text").textContent = s.error ? `Download failed: ${s.error}` : "";
      const dlBtn = $("#download-model-btn");
      dlBtn.hidden = false;
      dlBtn.innerHTML = `<svg class="ic ic-sm" width="15" height="15"><use href="#i-download"/></svg> ${s.error ? "Retry download" : "Download"}`;
    }
  }

  $("#model-size").addEventListener("change", refreshModelStatus);
  $("#download-model-btn").addEventListener("click", async () => {
    const size = $("#model-size").value;
    // Persist the choice BEFORE downloading: the wizard says "pick a size,
    // press Download", and users reasonably expect new jobs to use exactly
    // that model — not the previously saved default (which used to sneak
    // large-v3 in and download 3 GB on a "tiny" setup).
    await saveSettingPatch({ model_size: size });
    await fetch(`/api/models/${size}/download`, { method: "POST" });
    refreshModelStatus();
  });
  $("#load-model-btn").addEventListener("click", async () => {
    const size = $("#model-size").value;
    await fetch(`/api/models/${size}/load`, { method: "POST" });
    refreshModelStatus();
  });
  $("#unload-model-btn").addEventListener("click", async () => {
    const size = $("#model-size").value;
    await fetch(`/api/models/${size}/unload`, { method: "POST" });
    refreshModelStatus();
  });
  $("#delete-model-btn").addEventListener("click", async () => {
    const size = $("#model-size").value;
    if (!await modalConfirm({ title: "Delete model?", message: `Delete the downloaded "${size}" model from disk?\n\nIt will download again automatically if a job needs it.`, okText: "Delete", danger: true })) return;
    const r = await fetch(`/api/models/${size}/delete`, { method: "POST" });
    const data = await r.json().catch(() => ({}));
    if (!data.deleted) await modalAlert("Delete failed", "Could not delete the model folder.");
    refreshModelStatus();
  });
  $("#models-folder-btn").addEventListener("click", async () => {
    const r = await fetch(`/api/models/open-folder`, { method: "POST" });
    if (!r.ok) await modalAlert("No model folder", "Model folder does not exist yet — download a model first.");
  });

  // ---------------- queue ----------------
  // Click-to-browse: explicit programmatic click on the input. The previous
  // "input stretched over the dropzone" pattern queued a chooser per click
  // and stole click targets from drag handlers. One chooser at a time: extra
  // clicks while it is open are ignored, and the guard is released when the
  // dialog closes (a file picked, or cancelled). The timer only covers engines
  // that don't report a cancelled dialog.
  let chooserPending = false;
  const fileInput = $("#file-input");
  fileInput.addEventListener("change", (e) => {
    chooserPending = false;
    uploadFiles([...e.target.files]);
    e.target.value = ""; // allow picking the same file again
  });
  fileInput.addEventListener("cancel", () => (chooserPending = false));

  const dz = $("#dropzone");
  dz.addEventListener("click", (e) => {
    if (e.target === fileInput || chooserPending) return; // the input's own click bubbles here too
    chooserPending = true;
    fileInput.click();
    setTimeout(() => (chooserPending = false), 300);
  });
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.add("dragover");
    })
  );
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.remove("dragover");
    })
  );
  dz.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));

  const modelCount = (n) => `${n} model${n === 1 ? "" : "s"}`;

  // This page's id: cards it creates belong to it, so the server can drop the
  // ones still waiting for their bytes when the page is reloaded or closed.
  const pageId = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2));

  async function uploadFiles(fileList) {
    if (!fileList.length) return;
    $("#file-input").value = ""; // allow re-adding the same file
    // Every file gets its card at once ("Loading… N%"), then the bytes follow one
    // file at a time, streamed straight into the job folder. A 1-2 GB video
    // used to show nothing until the whole upload had been copied twice.
    const created = [];
    for (const f of fileList) {
      const r = await fetch("/api/jobs/new", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: f.name, size: f.size, page: pageId }),
      });
      if (r.ok) created.push([f, (await r.json()).id]);
    }
    for (const [f, id] of created) {
      try {
        await fetch(`/api/jobs/${id}/file`, { method: "PUT", body: f });
      } catch (_) {
        // cancelled from its card, or the app went away: the server cleans up
      }
    }
  }

  // ---------------- jobs rendering ----------------
  // Coalesce refreshes: SSE ticks arrive in bursts, and clicking Delete/Remove
  // used to queue several full refreshes behind the click on a slow VM (the
  // "buttons take forever" feel). At most one refresh is in flight; callers
  // asking while one runs get folded into a single follow-up.
  let refreshRunning = false;
  let refreshQueued = false;
  async function refreshJobs() {
    if (refreshRunning) {
      refreshQueued = true;
      return;
    }
    refreshRunning = true;
    try {
      do {
        refreshQueued = false;
        await refreshJobsOnce();
      } while (refreshQueued);
    } finally {
      refreshRunning = false;
    }
  }

  async function refreshJobsOnce() {
    const data = await fetch("/api/jobs").then((r) => r.json());
    const staged = data.jobs.filter((j) => j.status === "staged").length;
    const other = data.jobs.length - staged;
    $("#jobs-count").textContent = staged
      ? `${staged} to start, ${other} processed`
      : `${other} job(s)`;
    const list = $("#jobs-list");
    // Empty queue shows a plain hello instead of a tutorial.
    const empty = data.jobs.length === 0;
    $("#landing-hello").hidden = !empty;
    $("#jobs-pane").hidden = empty;

    // Rebuild only the cards whose status changed. A full innerHTML wipe on
    // every refresh threw away the DOM node under the pointer, so clicks
    // landed on detached elements (buttons "needed a second click") and the
    // rebuild itself was expensive on software-rendered VMs.
    const wanted = new Map(data.jobs.map((j) => [j.id, j]));
    const order = data.jobs.map((j) => j.id);

    // Remove cards that no longer exist; drop stale node references.
    [...list.children].forEach((el) => {
      const id = el.id.replace(/^job-/, "");
      if (!wanted.has(id)) {
        el.remove();
        jobs.delete(id);
      }
    });

    // Rebuild a card when its status OR any staged setting changed. The card
    // DOM embeds task/profile/model/language/prompt (and the server fills the
    // task's preset prompt on a task switch); rebuilding only on status
    // changes left the old card in place, so the prompt editor never showed.
    data.jobs.forEach((j) => {
      const existing = jobs.get(j.id);
      if (!existing) return;
      const statusChanged = existing.dataset.status !== j.status;
      const stagedChanged = j.status === "staged" && existing.dataset.sig !== stagedSignature(j);
      if (statusChanged || stagedChanged) {
        existing.remove();
        jobs.delete(j.id);
      }
    });

    // Create missing cards, then fix ordering (moving nodes is cheap and
    // keeps the existing ones alive).
    order.forEach((id, i) => {
      let el = jobs.get(id);
      if (!el) {
        el = jobCard(wanted.get(id));
      }
      const at = list.children[i];
      if (at !== el) list.insertBefore(el, at || null);
    });
    // Cards may have appeared/moved; keep the guide pinned to the new layout.
    requestPlace();
  }

  function progressBarHtml(fraction) {
    // Determinate when the backend reports 0..1, indeterminate otherwise.
    if (typeof fraction === "number" && isFinite(fraction)) {
      const pct = Math.max(0, Math.min(100, fraction * 100)).toFixed(1);
      return `<div class="progress-bar job-progress"><div class="progress-fill" style="width:${pct}%"></div></div>`;
    }
    return `<div class="progress-bar job-progress"><div class="progress-fill indeterminate"></div></div>`;
  }

  // Whole-job time left, e.g. "42% · about 3 min left".
  function formatEta(seconds) {
    const s = Math.max(0, Math.round(seconds));
    if (s < 45) return "less than a minute";
    if (s < 3600) return `about ${Math.max(1, Math.round(s / 60))} min`;
    const h = Math.floor(s / 3600);
    const m = Math.round((s % 3600) / 60);
    return `about ${h} h${m ? ` ${m} min` : ""}`;
  }
  // scope "transcript": the estimate covers the transcript only; how long the
  // LLM takes is unknown until it answers (a guessed 15 s per request showed
  // "less than a minute" for jobs that then waited minutes).
  function etaText(fraction, eta, scope) {
    const pct = typeof fraction === "number" && isFinite(fraction) ? `${Math.round(fraction * 100)}% · ` : "";
    if (typeof eta === "number" && isFinite(eta)) {
      return scope === "transcript"
        ? `${pct}${formatEta(eta)} until the transcript, then the document`
        : `${pct}${formatEta(eta)} left`;
    }
    return `${pct}estimating time left…`;
  }
  function setEta(el, fraction, eta, scope) {
    const box = el.querySelector(".job-eta");
    if (box) box.textContent = etaText(fraction, eta, scope);
  }

  function setProgressFill(el, fraction) {
    const fill = el.querySelector(".job-progress .progress-fill");
    if (!fill || typeof fraction !== "number" || !isFinite(fraction)) return;
    fill.classList.remove("indeterminate");
    fill.style.width = `${Math.max(0, Math.min(100, fraction * 100)).toFixed(1)}%`;
  }

  // Everything a staged card's DOM depends on. Compared against the card's
  // stored signature so a field change (task -> prompt preset) rebuilds it.
  function stagedSignature(j) {
    return [j.profile, j.task, j.model_size || "", j.language || "",
            j.document_language || "", j.custom_prompt || "",
            // The saved defaults the card spells out as "default (…)": a card
            // that ignored them kept a stale label until some other setting
            // changed, which then looked like that setting switching it.
            settingsCache.language || "", settingsCache.model_size || "",
            settingsCache.document_language || "",
            // Effective format (override, else the saved default) — the card
            // displays it, so a changed default must rebuild the card.
            j.output_format || settingsCache.output_format || ""].join("\u0000");
  }

  function jobCard(j) {
    const el = document.createElement("article");
    el.className = "job card";
    el.dataset.status = j.status; // status accent border via CSS
    el.id = `job-${j.id}`;
    if (j.status === "staged") {
      // ---- staged: editable settings + start/remove ----
      el.innerHTML = stagedCardHtml(j);
      wireStagedCard(el, j);
      el.dataset.sig = stagedSignature(j);
    } else {
      // ---- processed (uploading/queued/running/done/failed/cancelled) ----
      el.innerHTML = processedCardHtml(j);
      wireProcessedCard(el, j);
    }
    jobs.set(j.id, el);
    return el;
  }

  // <option>s from [value, label] pairs, ``selected`` marked.
  function optionsHtml(pairs, selected) {
    return pairs.map(([value, label]) =>
      `<option value="${escapeHtml(value)}" ${value === selected ? "selected" : ""}>${escapeHtml(label)}</option>`
    ).join("");
  }

  // Blank value = the saved default, spelled out as "default (…)".
  function languageOptionsHtml(defaultLabel, selected) {
    return optionsHtml([["", defaultLabel],
                        ...languageList.map((l) => [l.code, `${l.name} (${l.code})`])], selected || "");
  }

  function languageName(code, fallback) {
    const lang = languageList.find((l) => l.code === code);
    return lang ? lang.name : fallback;
  }

  const MODEL_SIZES = ["tiny", "base", "small", "medium", "large-v1", "large-v2", "large-v3"];
  const FORMAT_LABELS = [["md", "Markdown (.md)"], ["txt", "Plain text (.txt)"], ["both", "Markdown + plain text"]];

  // One labelled dropdown of a staged card, with what it does under it (and
  // as its accessible description).
  function ctlHtml(j, { label, cls, hint, options, text }) {
    return `
          <label class="ctl">
            <span class="muted">${label}</span>
            <select class="${cls}" aria-label="${label}" aria-describedby="${hint}-${j.id}">${options}</select>
            <span class="ctl-hint" id="${hint}-${j.id}">${escapeHtml(text)}</span>
          </label>`;
  }

  function stagedControls(j) {
    const prof = profileList.find((p) => p.name === j.profile) || {};
    // The "default" option spells out the model that will actually run —
    // users were blindsided by a hidden large-v3 default.
    const effectiveModel = prof.model || settingsCache.model_size || "large-v3";
    const controls = [
      { label: "Profile", cls: "job-profile", hint: "hint-profile",
        options: optionsHtml(profileList.map((p) => [p.name, p.name]), j.profile),
        text: prof.corrections ? `${prof.description} Fixes its ${prof.corrections} terms in the transcript.`
                               : "No domain dictionary: words stay as heard." },
      { label: "Audio language", cls: "job-language", hint: "hint-language",
        options: languageOptionsHtml(`default (${languageName(settingsCache.language || "ru", settingsCache.language || "ru")})`,
                                     j.language),
        text: "Language spoken in the recording." },
      { label: "Output", cls: "job-task", hint: "hint-task",
        options: optionsHtml(taskList.map((t) => [t, TASK_LABELS[t] || t]), j.task),
        text: taskHint(j.task) },
      { label: "Model", cls: "job-model", hint: "hint-model",
        options: optionsHtml([["", `default (${effectiveModel})`], ...MODEL_SIZES.map((m) => [m, m])],
                             j.model_size || prof.model || ""),
        text: "Bigger is more accurate, but slower." },
      // Output file format: the card shows the effective choice (per-job
      // override, else the saved default). No separate "default" option —
      // it just duplicated whichever real option was already selected.
      { label: "Output file format", cls: "job-format", hint: "hint-format",
        options: optionsHtml(FORMAT_LABELS, j.output_format || settingsCache.output_format || "both"),
        text: "Files the result is saved as." },
    ];
    if (j.task !== "raw") {
      // What the LLM writes in; the saved default may be "same as the audio".
      controls.push({ label: "Document language", cls: "job-doc-language", hint: "hint-doclang",
        // Settings' own words for "no document language": "default (same as
        // the audio)" did not fit a card's column
        options: languageOptionsHtml(settingsCache.document_language
          ? `default (${languageName(settingsCache.document_language, settingsCache.document_language)})`
          : "Same as the audio",
                                     j.document_language),
        text: "Language the document is written in." });
    }
    return controls.map((c) => ctlHtml(j, c)).join("");
  }

  function stagedCardHtml(j) {
    // Open the prompt by default for tasks that run an LLM instruction —
    // it was a collapsed <details> and users could not find the editor at
    // all ("prompt edit is just gone").
    const promptOpen = j.task === "custom" || !!j.custom_prompt;
    const prompt = j.task === "raw" ? "" : `
        <details class="prompt-details" ${promptOpen ? "open" : ""}>
          <summary>Prompt (editable preset — or type your own)</summary>
          <label class="ctl" style="margin-top:0.5rem">
            <textarea class="job-prompt" rows="3" placeholder="Describe what you want from the transcript">${escapeHtml(j.custom_prompt || "")}</textarea>
          </label>
        </details>`;
    return `
        <div class="job-head">
          <strong class="job-name">${escapeHtml(j.filename)}</strong>
          <span class="badge badge-staged">ready to start</span>
        </div>
        <div class="staged-controls">${stagedControls(j)}
        </div>${prompt}
        <div class="job-actions">
          <button class="btn btn-small start-btn" data-id="${j.id}"><svg class="ic ic-sm" width="15" height="15"><use href="#i-play"/></svg> Start</button>
          <button class="btn btn-ghost btn-small remove-btn" data-id="${j.id}"><svg class="ic ic-sm" width="15" height="15"><use href="#i-trash"/></svg> Remove</button>
        </div>
      `;
  }

  // An error response's message: FastAPI's {"detail": ...} or the raw text.
  async function responseDetail(r) {
    const text = await r.text().catch(() => "");
    try {
      return JSON.parse(text).detail || text || "unknown";
    } catch (_) {
      return text || "unknown";
    }
  }

  // What a refused request tells the user: the server's own explanation of
  // a refusal (4xx), with the HTTP status when the server failed (5xx).
  async function failureText(r) {
    const detail = await responseDetail(r);
    return r.status < 500 ? detail : `HTTP ${r.status}: ${detail}`;
  }

  // One card's requests go out one after another, in the order the user made
  // them: a prompt typed and started straight away is saved (its change event
  // fires as the click leaves the box) before the start arrives.
  function cardRequest(el, url, init) {
    const send = () => fetch(url, init);
    const sent = (el._requests || Promise.resolve()).then(send, send);
    // the chain only orders requests; each caller handles its own result
    el._requests = sent.then(() => undefined, () => undefined);
    return sent;
  }

  // POST /api/jobs/<id>/<action>: a refusal is shown, then the list shows
  // the job as it really is.
  // Returns whether the app took it.
  async function jobAction(el, j, action, failTitle) {
    const r = await cardRequest(el, `/api/jobs/${j.id}/${action}`, { method: "POST" });
    if (!r.ok) await modalAlert(failTitle, await failureText(r));
    refreshJobs();
    return r.ok;
  }

  // A staged card's control saves itself when changed. ``rerender``: the
  // server changes more than this field (the model preset follows the
  // profile; a new task brings its own prompt), so the card is rebuilt.
  function onJobField(el, j, { selector, field, failTitle, rerender = false }) {
    const ctl = el.querySelector(selector);
    if (!ctl) return;
    ctl.addEventListener("change", async (e) => {
      const r = await cardRequest(el, `/api/jobs/${j.id}/update`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [field]: e.target.value }),
      });
      if (!r.ok) await modalAlert(failTitle, await failureText(r));
      if (!r.ok || rerender) await refreshJobs();
    });
  }

  const JOB_FIELDS = [
    { selector: ".job-profile", field: "profile", failTitle: "Could not set the profile", rerender: true },
    { selector: ".job-language", field: "language", failTitle: "Could not set language" },
    { selector: ".job-task", field: "task", failTitle: "Could not set the output", rerender: true },
    { selector: ".job-model", field: "model_size", failTitle: "Could not set the model" },
    { selector: ".job-format", field: "output_format", failTitle: "Could not set the file format" },
    { selector: ".job-doc-language", field: "document_language", failTitle: "Could not set the document language" },
    { selector: ".job-prompt", field: "custom_prompt", failTitle: "Could not save the prompt" },
  ];

  function wireStagedCard(el, j) {
    enhanceSelect(el.querySelector(".job-language"));
    const docLangSel = el.querySelector(".job-doc-language");
    if (docLangSel) enhanceSelect(docLangSel);
    JOB_FIELDS.forEach((f) => onJobField(el, j, f));
    const startBtn = el.querySelector(".start-btn");
    startBtn.addEventListener("click", async () => {
      startBtn.disabled = true; // a double click starts the job once, without an error
      // refused (no prompt yet, ...): the card stays as it is, so Start must work again
      if (!await jobAction(el, j, "start", "Could not start job")) startBtn.disabled = false;
    });
    el.querySelector(".remove-btn").addEventListener("click", async () => {
      if (!await modalConfirm({ title: "Remove job?", message: "Remove this staged job from the list? The uploaded file is discarded.", okText: "Remove" })) return;
      await fetch(`/api/jobs/${j.id}/delete?delete_artifacts=false`, { method: "POST" });
      refreshJobs();
    });
  }

  function processedCardHtml(j) {
    const moving = j.status === "running" || j.status === "uploading";
    const eta = j.status === "running"
      ? `<div class="job-eta muted small">${etaText(j.fraction, j.eta_s, j.eta_scope)}</div>` : "";
    return `
      <div class="job-head">
        <strong class="job-name">${escapeHtml(j.filename)}</strong>
        <span class="badge badge-${j.status}">${statusLabel(j.status)}</span>
        <span class="muted small job-meta">${escapeHtml(j.profile)} &middot; ${escapeHtml(j.task)}</span>
      </div>
      <div class="job-stage muted">${escapeHtml(j.stage || "")}</div>
      ${moving ? progressBarHtml(j.fraction) : ""}
      ${eta}
      ${j.error ? `<pre class="error">${escapeHtml(j.error)}</pre>` : ""}
      ${(j.warnings || []).map((w) => `<div class="job-warning" role="note">${escapeHtml(w)}</div>`).join("")}
      ${j.out_dir ? `<div class="saved-path">Saved to: <code>${escapeHtml(j.out_dir)}</code></div>` : ""}
      <div class="job-actions">
        ${jobButtonsHtml(j)}
      </div>
    `;
  }

  function jobButtonsHtml(j) {
    const btn = (cls, icon, label) =>
      `<button class="btn btn-small ${cls}" data-id="${j.id}"><svg class="ic ic-sm" width="15" height="15"><use href="#${icon}"/></svg> ${label}</button>`;
    const ended = j.status === "failed" || j.status === "cancelled";
    const finished = ended || j.status === "done";
    return [
      j.out_dir && btn("open-folder-btn", "i-folder", '<span class="btn-label">Open folder</span>'),
      ended && btn("retry-btn", "i-play", "Retry"),
      (j.status === "queued" || j.status === "running") && btn("btn-ghost stop-btn", "i-stop", "Stop"),
      j.status === "uploading" && btn("btn-ghost cancel-upload-btn", "i-stop", "Cancel"),
      finished && btn("btn-ghost rm-btn", "i-trash", "Remove"),
      finished && j.out_dir && btn("btn-ghost btn-danger del-btn", "i-trash", "Delete + files"),
    ].filter(Boolean).join("\n        ");
  }

  function wireProcessedCard(el, j) {
    const onClick = (selector, handler) => {
      const b = el.querySelector(selector);
      if (b) b.addEventListener("click", () => handler(b));
    };
    onClick(".open-folder-btn", (b) => openJobFolder(b, j));
    onClick(".retry-btn", () => jobAction(el, j, "retry", "Could not restart job"));
    onClick(".stop-btn", (b) => {
      b.disabled = true; // one press is enough; the card shows "Stopping…" until the worker ends
      return jobAction(el, j, "cancel", "Could not stop job");
    });
    onClick(".cancel-upload-btn", async (b) => {
      // Nothing to confirm: the user's own file is never touched, only the copy being added.
      b.disabled = true;
      await fetch(`/api/jobs/${j.id}/delete?delete_artifacts=false`, { method: "POST" });
      refreshJobs();
    });
    onClick(".rm-btn", async () => {
      if (!await modalConfirm({ title: "Remove job?", message: "Remove this job from the list? Files stay on disk.", okText: "Remove" })) return;
      await fetch(`/api/jobs/${j.id}/delete?delete_artifacts=false`, { method: "POST" });
      refreshJobs();
    });
    onClick(".del-btn", async () => {
      if (!await modalConfirm({ title: "Delete job and files?", message: `Delete this job AND its output folder?\n\n${j.out_dir}\n\nOnly the folder created by minutenest is deleted.`, okText: "Delete", danger: true })) return;
      await fetch(`/api/jobs/${j.id}/delete?delete_artifacts=true`, { method: "POST" });
      refreshJobs();
    });
  }

  async function openJobFolder(btn, j) {
    // Explorer takes seconds to cold-start on a VM; acknowledge the click
    // immediately so it doesn't read as a dead button. Only the label span
    // changes — swapping the whole innerHTML dropped the icon and shrank the
    // button for a frame ("the button flickers").
    const labelEl = btn.querySelector(".btn-label");
    const label = labelEl.textContent;
    btn.disabled = true;
    // Pin the current width: swapping in the shorter "Opening…" label
    // would otherwise shrink the button for a frame.
    btn.style.minWidth = `${btn.getBoundingClientRect().width}px`;
    labelEl.textContent = "Opening…";
    try {
      const r = await fetch(`/api/jobs/${j.id}/open-folder`, { method: "POST" });
      // FastAPI wraps unhandled 500s as {"detail":"Internal Server Error"};
      // responseDetail asks the text body for anything more specific.
      if (!r.ok) await modalAlert("Could not open folder", `HTTP ${r.status}: ${await responseDetail(r)}`);
    } catch (e) {
      await modalAlert("Could not open folder", String(e));
    } finally {
      btn.disabled = false;
      labelEl.textContent = label;
    }
  }

  // What the badge says. "uploading" reads like sending somewhere else; the
  // file is only being loaded into this app on this PC.
  function statusLabel(status) {
    return status === "uploading" ? "loading" : status;
  }

  function updateJobInPlace(payload) {
    const el = jobs.get(payload.id);
    if (!el) return refreshJobs();
    if (payload.status) showJobStatus(el, payload.status);
    setProgressFill(el, payload.fraction);
    if (payload.status === "running") {
      const fillEl = el.querySelector(".job-progress .progress-fill");
      const shown = fillEl && fillEl.style.width ? parseFloat(fillEl.style.width) / 100 : payload.fraction;
      setEta(el, shown, payload.eta_s, payload.eta_scope);
    }
    const stageEl = el.querySelector(".job-stage");
    if (payload.stage && stageEl) stageEl.textContent = payload.stage;
    if (payload.out_dir) showSavedPath(el, payload.out_dir);
    if (payload.error) {
      el.querySelectorAll(".error").forEach((e) => e.remove());
      el.insertAdjacentHTML("beforeend", `<pre class="error">${escapeHtml(payload.error)}</pre>`);
    }
  }

  function showJobStatus(el, status) {
    el.dataset.status = status;
    const badge = el.querySelector(".badge");
    if (badge) {
      badge.className = `badge badge-${status}`;
      badge.textContent = statusLabel(status);
    }
    const bar = el.querySelector(".job-progress");
    if (bar) bar.hidden = !(status === "running" || status === "uploading");
  }

  function showSavedPath(el, outDir) {
    const pathEl = el.querySelector(".saved-path");
    if (pathEl) {
      pathEl.querySelector("code").textContent = outDir;
      return;
    }
    const anchor = el.querySelector(".job-actions");
    if (anchor) {
      anchor.insertAdjacentHTML("beforebegin",
        `<div class="saved-path">Saved to: <code>${escapeHtml(outDir)}</code></div>`);
    }
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s === null || s === undefined ? "" : String(s);
    // quotes too: the result also goes inside attribute values (value="…")
    return d.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ---------------- in-page modal (no origin prefix like native dialogs) ----------------
  function modalPrompt({ title, message, okText = "OK", danger = false, hideCancel = false }) {
    return new Promise((resolve) => {
      const overlay = $("#modal-overlay");
      const titleEl = $("#modal-title");
      const msgEl = $("#modal-message");
      const okBtn = $("#modal-ok");
      const cancelBtn = $("#modal-cancel");
      titleEl.textContent = title;
      msgEl.textContent = message;
      okBtn.textContent = okText;
      okBtn.classList.toggle("btn-danger-solid", danger);
      cancelBtn.hidden = hideCancel;
      overlay.hidden = false;
      const done = (value) => {
        overlay.hidden = true;
        okBtn.onclick = cancelBtn.onclick = overlay.onclick = null;
        document.removeEventListener("keydown", onKey);
        resolve(value);
      };
      const onKey = (e) => { if (e.key === "Escape" && !hideCancel) done(false); };
      okBtn.onclick = () => done(true);
      cancelBtn.onclick = () => done(false);
      overlay.onclick = (e) => { if (e.target === overlay && !hideCancel) done(false); };
      document.addEventListener("keydown", onKey);
      (hideCancel ? okBtn : cancelBtn).focus();
    });
  }
  const modalConfirm = (opts) => modalPrompt(opts);
  const modalAlert = (title, message) =>
    modalPrompt({ title, message, okText: "OK", hideCancel: true });

  // ---------------- events (SSE) ----------------
  // Single live stream per tab. The old code spawned a new EventSource on
  // every error WITHOUT closing the failed one: each zombie kept a socket
  // in CONNECTING state (plus its own native auto-retry). exe restarts /
  // sleep-wake therefore accumulated streams until the browser's ~6
  // connections-per-host pool was exhausted and pages stopped loading.
  let eventStream = null;
  let eventRetryMs = 1000;
  function subscribeEvents() {
    if (eventStream) {
      try { eventStream.close(); } catch (_) { /* already dead */ }
      eventStream = null;
    }
    const es = new EventSource(`/api/events?page=${pageId}`);
    eventStream = es;
    es.onopen = () => {
      eventRetryMs = 1000; // healthy again: reset backoff
      // Visible readiness marker: UI automation (and a user pressing F12)
      // can tell that live updates are actually flowing.
      document.body.dataset.events = "on";
      // Anything that changed while the stream was not connected (between
      // the first job fetch and now, or during a reconnect) was never
      // pushed: re-read the list so no card stays stale until a reload.
      refreshJobs();
    };
    es.addEventListener("job", (e) => {
      const data = JSON.parse(e.data);
      if (data.status === "deleted") return refreshJobs();
      const card = jobs.get(data.id);
      // Same-state progress ticks (stage/ETA text) update in place — smooth,
      // no rebuild. A lifecycle change re-renders the card for its new UI.
      if (card && data.status && card.dataset.status === data.status) {
        return updateJobInPlace(data);
      }
      return refreshJobs();
    });
    es.addEventListener("model", (e) => {
      const data = JSON.parse(e.data);
      const size = $("#model-size").value;
      if (data.size !== size) return;
      // Terminal states re-render fully (buttons change); otherwise live progress.
      if (data.installed || (!data.downloading && !data.loading)) return refreshModelStatus();
      if (data.downloading) {
        $("#model-progress").hidden = false;
        $("#model-progress-fill").style.width = `${(data.progress * 100).toFixed(1)}%`;
        $("#model-progress-text").textContent =
          `Downloading ${size}: ${(data.progress * 100).toFixed(0)}%`;
      } else if (data.loading) {
        $("#model-progress").hidden = false;
        $("#model-progress-fill").style.width = `100%`;
        $("#model-progress-text").textContent = `Loading ${size} into memory...`;
      }
    });
    // Close the failed stream BEFORE resubscribing so a flapping server
    // can never stack zombie connections. Backoff caps at 30 s.
    es.onerror = () => {
      if (eventStream === es) {
        try { es.close(); } catch (_) { /* already dead */ }
        eventStream = null;
      }
      const wait = eventRetryMs + Math.random() * 500;
      eventRetryMs = Math.min(eventRetryMs * 2, 30000);
      setTimeout(subscribeEvents, wait);
    };
  }
  // Release the socket on tab close / navigation (bfcache included) and tell
  // the server the tab is gone. In browser-fallback mode the launcher stops
  // the server on this signal; without it, closing the tab left the python
  // process in Task Manager forever.
  window.addEventListener("pagehide", () => {
    if (eventStream) {
      try { eventStream.close(); } catch (_) { /* already dead */ }
      eventStream = null;
    }
    try {
      navigator.sendBeacon(`/api/client-bye?page=${pageId}`);
    } catch (_) { /* older engines: SSE disconnect is the fallback signal */ }
  });

  // ---------------- LLM model list (Ollama) ----------------
  // Latest request wins. Switching provider (or pasting a key) while a slow
  // listing is in flight must re-list for the NEW provider, and the old
  // answer must never land on top of it (real report: "Could not reach
  // http://localhost:1234/v1 — is LM Studio running?" shown with OpenRouter
  // selected, after a misclick on LM Studio). The old guard skipped the new
  // request instead.
  let llmListSeq = 0;
  async function refreshLlmModels({ fresh = false } = {}) {
    const hint = $("#llm-model-hint");
    const btn = $("#refresh-llm-models");
    const seq = ++llmListSeq;
    const prov = $("#llm-provider").value;
    if (prov === "none") {   // nothing to list, nothing to explain
      hint.textContent = "";
      btn.disabled = false;
      return;
    }
    btn.disabled = true;
    hint.textContent = "Loading...";
    hint.style.color = "";
    try {
      const r = await fetch("/api/llm/models?" + llmListQuery(prov, fresh)).then((res) => res.json());
      if (seq !== llmListSeq || $("#llm-provider").value !== prov) return; // a newer list owns the UI
      showLlmModels(r, prov);
    } catch (e) {
      console.warn("could not list the LLM models", e);
      if (seq === llmListSeq) hint.textContent = "Failed to fetch models";
    } finally {
      if (seq === llmListSeq) btn.disabled = false;
    }
  }

  // Query with the CURRENT form values so switching provider/host/key
  // refreshes immediately without requiring Save first.
  function llmListQuery(prov, fresh) {
    const params = new URLSearchParams({ provider: prov });
    if (fresh) params.set("fresh", "1"); // the manual button must re-ask the provider, not replay a cached answer
    if (prov === "ollama") {
      params.set("host", $("#ollama-host").value.trim());
      params.set("key", $("#ollama-key").value);
    } else if (prov === "compat") {
      params.set("host", $("#compat-url").value.trim());
      params.set("key", $("#compat-key").value);
    } else {
      params.set("key", $("#cloud-key").value);
    }
    return params.toString();
  }

  function showLlmModels(r, prov) {
    // The model saved in settings belongs to the saved provider. If the user
    // switched provider before that provider's list arrived, drop it: it
    // was carried over to OpenRouter and even saved there (Windows run).
    if (window._pendingLlmModel !== undefined && window._pendingLlmProvider !== prov) {
      delete window._pendingLlmModel;
    }
    const sel = $("#llm-model");
    // Keep the user's pick across rebuilds of the SAME provider's list. A
    // model id from another provider (an LM Studio model under OpenRouter)
    // means nothing there and must not be carried over.
    const current = sel.dataset.provider === prov ? getLlmModelValue() : "";
    sel.dataset.provider = prov;
    sel.innerHTML = "";
    if (!r.key_invalid && r.models && r.models.length) listLlmModels(sel, r, prov, current);
    else showNoLlmModels(sel, r);
  }

  function appendOption(sel, value, label) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = label;
    sel.appendChild(opt);
  }

  // Bad key, or nothing listed: keep the saved model and Custom, say why.
  function showNoLlmModels(sel, r) {
    const hint = $("#llm-model-hint");
    const pending = window._pendingLlmModel || "";
    if (pending) {
      appendOption(sel, pending, pending);
      sel.value = pending;
    }
    appendOption(sel, "__custom__", "Custom...");
    if (r.key_invalid) {
      hint.textContent = r.error;
      hint.style.color = "var(--fail)";
    } else {
      hint.textContent = r.error || "No models found. Check the host and key.";
    }
    delete window._pendingLlmModel;
  }

  function listLlmModels(sel, r, prov, current) {
    r.models.forEach((m) => appendOption(sel, m, m));
    appendOption(sel, "__custom__", "Custom...");
    // Restore selection: explicit pending from settings wins; otherwise
    // keep whatever was selected before the rebuild (never reset to option 0).
    // Nothing chosen yet: the server's recommendation (a free model on
    // OpenRouter), never "the first one alphabetically", which silently
    // picked a PAID model (real report: aion-labs/aion-2.0).
    if (window._pendingLlmModel !== undefined) {
      setLlmModelValue(window._pendingLlmModel || r.recommended || "");
      delete window._pendingLlmModel;
    } else {
      setLlmModelValue(current || r.recommended || "");
    }
    // What the box shows is what jobs must use: persist a preselection
    // the user hasn't saved yet (otherwise jobs fell back to a different
    // provider default than the one on screen).
    const saved = settingsCache.llm || {};
    if (settingsLoaded && (getLlmModelValue() !== (saved.model || "") || (saved.provider || "") !== prov)) {
      scheduleAutoSave({ delay: 0 });
    }
    const hint = $("#llm-model-hint");
    hint.textContent = llmListHint(r, prov);
    if (r.key_missing) hint.style.color = "var(--warn)";
  }

  function llmListHint(r, prov) {
    const count = modelCount(r.models.length);
    // The catalog is public, the models are not: "N models available"
    // above an empty key box read as "ready to use" (VM report).
    if (r.key_missing) return `${count} — paste your OpenRouter API key below to use them`;
    // Transient issue (e.g. rate limit): list is shown, key may still be fine.
    let text = r.warning ? `${count} available. Note: ${r.warning}` : `${count} available`;
    // A cloud key against a local daemon is silently unused — say so.
    if (prov === "ollama" && $("#ollama-key").value.trim()
        && /localhost|127\.0\.0\.1/.test($("#ollama-host").value.trim())) {
      text += " — key is unused against a local daemon; use the Cloud host for cloud models.";
    }
    return text;
  }

  $("#refresh-llm-models").addEventListener("click", () => refreshLlmModels({ fresh: true }));

  $("#browse-folder-btn").addEventListener("click", async () => {
    const btn = $("#browse-folder-btn");
    if (btn.disabled) return; // picker already open
    btn.disabled = true;
    try {
      const r = await fetch("/api/settings/browse-folder", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current: $("#output-dir").value.trim() }),
      });
      if (!r.ok) {
        const text = await r.text().catch(() => "");
        let msg = "unknown";
        try { msg = JSON.parse(text).detail || text || msg; } catch (_) { msg = text || msg; }
        await modalAlert("Folder picker failed", `HTTP ${r.status}: ${msg}`);
        return;
      }
      const data = await r.json();
      if (data.path) {
        $("#output-dir").value = data.path;
        updateFoldersPaths();
        scheduleAutoSave();
      }
    } catch (e) {
      await modalAlert("Folder picker failed", String(e));
    } finally {
      btn.disabled = false;
    }
  });

  // Host preset chips (Ollama Local/Cloud, LM Studio/llama.cpp/vLLM):
  // one click fills the host and re-lists immediately.
  document.addEventListener("click", (e) => {
    const chip = e.target.closest(".chip[data-host]");
    if (!chip) return;
    const input = document.getElementById(chip.dataset.target);
    if (!input) return;
    input.value = chip.dataset.host;
    // re-list once the address is saved: a saved key goes only to the saved address
    scheduleAutoSave({ refresh: true });
  });

  // ---------------- init ----------------
  $("#quit-btn").addEventListener("click", async () => {
    if (!await modalConfirm({ title: "Shut down MinuteNest?", message: "The server stops and the window can be closed.", okText: "Shut down", danger: true })) return;
    try {
      await fetch("/api/shutdown", { method: "POST" });
    } catch (e) {
      // expected: the server may drop the connection while it stops
      console.info("shutdown request ended with", e);
    }
    document.body.innerHTML = "<main style='padding:2rem;font-family:sans-serif;color:#8b93a3'>Shutting down&hellip; you can close this tab.</main>";
  });

  (async () => {
    // Kick off every independent fetch in parallel, then wire up UI as each
    // resolves. The old sequential chain delayed the first paint of the
    // walkthrough by ~6 round-trips, so it appeared late ("had to click
    // before it showed up").
    const versionP = fetch("/api/version").then((r) => r.json()).catch(() => null);
    const settingsP = fetch("/api/settings").then((r) => r.json()).catch(() => null);
    const modelsP = fetch("/api/models").then((r) => r.json()).catch(() => null);

    // Version + model list are cosmetic; fill them whenever they land.
    versionP.then((v) => {
      if (v) $("#app-version").textContent = `v${v.version} · started ${v.started_at}`;
    });
    const settings = await settingsP;

    // First launch ever: show the walkthrough as soon as settings are known.
    // It must not wait for profiles/languages/jobs to finish loading. Saving
    // is safe even before the form is populated: saveSettings() awaits
    // loadSettings() first (see the guard there).
    if (settings && !settings.wizard_done) openWizard();

    const models = await modelsP;
    if (models) {
      const sel = $("#model-size");
      sel.innerHTML = "";
      models.models.forEach((m) =>
        sel.insertAdjacentHTML("beforeend", `<option value="${m.size}">${m.size}</option>`));
    }
    await loadProfiles();
    await loadSettings(settings);
    // Fire-and-forget: the LLM model list must NEVER block the queue UI,
    // even if the cloud endpoint is slow or rate-limiting us.
    refreshLlmModels();
    await refreshJobs();
    subscribeEvents();
  })();
})();
