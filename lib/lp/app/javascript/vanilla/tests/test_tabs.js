/**
 * Tests for lib/lp/app/javascript/vanilla/tabs.js
 */
import {
  SELECTOR,
  handleKeydown,
  findTablists,
  initTabsWithin,
} from "../tabs.js";

// -- Helpers --

/**
 * Build a tablist container and append it to the document.
 * Simulates server-rendered markup (no ARIA tab roles).
 * @param {number} count - Number of tabs to create.
 * @param {{ activeIndex?: number, withPanels?: boolean }} [options]
 * @returns {{
 *   container: HTMLDivElement,
 *   tablist: HTMLDivElement,
 *   tabs: HTMLAnchorElement[],
 *   panels: HTMLDivElement[],
 * }}
 */
function makeTablist(count, { activeIndex = 0, withPanels = false } = {}) {
  const container = document.createElement("div");
  const tablist = document.createElement("div");
  tablist.setAttribute("data-js", "tabs");
  tablist.setAttribute("aria-label", "Test tabs");

  /** @type {HTMLDivElement[]} */
  const panels = [];

  for (let i = 0; i < count; i++) {
    const panelId = `test-panel-${i}`;
    const tab = document.createElement("a");
    tab.className = "p-tabs__link";
    tab.setAttribute("href", `?tab=${i}`);
    tab.setAttribute("data-controls", panelId);
    tab.setAttribute("tabindex", "0");
    tab.textContent = `Tab ${i}`;

    if (i === activeIndex) {
      tab.setAttribute("aria-current", "page");
    }
    tablist.appendChild(tab);

    if (withPanels) {
      const panel = document.createElement("div");
      panel.id = panelId;
      panel.textContent = `Panel ${i} content`;
      panels.push(panel);
    }
  }

  container.appendChild(tablist);
  panels.forEach((p) => container.appendChild(p));
  document.body.appendChild(container);

  // Sanity check: the tablist must match the module's SELECTOR.
  if (!tablist.matches(SELECTOR)) {
    throw new Error("makeTablist: tablist does not match SELECTOR");
  }

  return {
    container,
    tablist,
    tabs: /** @type {HTMLAnchorElement[]} */ (
      [...tablist.querySelectorAll(".p-tabs__link")]
    ),
    panels,
  };
}

/**
 * Dispatch a keydown event on an element.
 * @param {HTMLElement} element
 * @param {string} key
 */
function pressKey(element, key) {
  element.dispatchEvent(
    new KeyboardEvent("keydown", { key, bubbles: true })
  );
}

// -- Tests --

VanillaTest.suite("tabs", (T) => {
  T.suite("handleKeydown", (T) => {
    T.test("ArrowRight moves focus forward", () => {
      const ctx = makeTablist(3);
      initTabsWithin(ctx.container);

      ctx.tabs[0].focus();
      pressKey(ctx.tabs[0], "ArrowRight");
      T.equal(document.activeElement, ctx.tabs[1], "focus on tab 1");
    });

    T.test("ArrowLeft moves focus backward", () => {
      const ctx = makeTablist(3);
      initTabsWithin(ctx.container);

      ctx.tabs[1].focus();
      pressKey(ctx.tabs[1], "ArrowLeft");
      T.equal(document.activeElement, ctx.tabs[0], "focus on tab 0");
    });

    T.test("ArrowRight wraps from last to first", () => {
      const ctx = makeTablist(3);
      initTabsWithin(ctx.container);

      ctx.tabs[2].focus();
      pressKey(ctx.tabs[2], "ArrowRight");
      T.equal(document.activeElement, ctx.tabs[0], "focus wraps to tab 0");
    });

    T.test("ArrowLeft wraps from first to last", () => {
      const ctx = makeTablist(3);
      initTabsWithin(ctx.container);

      ctx.tabs[0].focus();
      pressKey(ctx.tabs[0], "ArrowLeft");
      T.equal(document.activeElement, ctx.tabs[2], "focus wraps to last tab");
    });

    T.test("Home moves focus to first tab", () => {
      const ctx = makeTablist(3);
      initTabsWithin(ctx.container);

      ctx.tabs[2].focus();
      pressKey(ctx.tabs[2], "Home");
      T.equal(document.activeElement, ctx.tabs[0], "focus on first tab");
    });

    T.test("End moves focus to last tab", () => {
      const ctx = makeTablist(3);
      initTabsWithin(ctx.container);

      ctx.tabs[0].focus();
      pressKey(ctx.tabs[0], "End");
      T.equal(document.activeElement, ctx.tabs[2], "focus on last tab");
    });

    T.test("Space activates (clicks) the focused tab", () => {
      const ctx = makeTablist(3);
      initTabsWithin(ctx.container);

      let clicked = false;
      ctx.tabs[1].addEventListener("click", () => { clicked = true; });

      ctx.tabs[1].focus();
      pressKey(ctx.tabs[1], " ");
      T.ok(clicked, "tab was clicked");
    });

    T.test("ignores non-navigation keys", () => {
      const ctx = makeTablist(2);
      initTabsWithin(ctx.container);
      ctx.tabs[0].focus();

      pressKey(ctx.tabs[0], "a");
      T.equal(document.activeElement, ctx.tabs[0], "focus unchanged");
    });

    T.test("ignores events from non-tab targets", () => {
      const ctx = makeTablist(2);
      initTabsWithin(ctx.container);
      ctx.tabs[0].focus();

      const spy = document.createElement("span");
      handleKeydown(
        /** @type {any} */ ({
          key: "ArrowRight",
          target: spy,
          preventDefault() {},
        }),
        ctx.tabs,
      );
      T.equal(document.activeElement, ctx.tabs[0], "focus unchanged");
    });
  });

  T.suite("findTablists", (T) => {
    T.test("finds tablists among descendants", () => {
      const ctx = makeTablist(1);
      const result = findTablists(ctx.container);
      T.equal(result.length, 1, "finds one tablist");
      T.equal(result[0], ctx.tablist, "returns the tablist element");
    });

    T.test("includes root when root itself is a tablist", () => {
      const ctx = makeTablist(1);
      const result = findTablists(ctx.tablist);
      T.equal(result.length, 1, "finds root tablist");
      T.equal(result[0], ctx.tablist, "returns the root element");
    });

    T.test("ignores elements that do not match SELECTOR", () => {
      const container = document.createElement("div");
      const inner = document.createElement("div");
      container.appendChild(inner);
      document.body.appendChild(container);

      T.equal(findTablists(container).length, 0, "no tablists found");
    });
  });

  T.suite("initTabsWithin (hydration)", (T) => {
    T.test("adds role='tablist' to container", () => {
      const ctx = makeTablist(2);
      initTabsWithin(ctx.container);

      T.equal(
        ctx.tablist.getAttribute("role"), "tablist", "tablist role set"
      );
    });

    T.test("adds role='tab' and aria-selected to tabs", () => {
      const ctx = makeTablist(2);
      initTabsWithin(ctx.container);

      T.equal(ctx.tabs[0].getAttribute("role"), "tab", "active tab role");
      T.equal(
        ctx.tabs[0].getAttribute("aria-selected"), "true",
        "active tab selected",
      );
      T.equal(ctx.tabs[1].getAttribute("role"), "tab", "inactive tab role");
      T.equal(
        ctx.tabs[1].getAttribute("aria-selected"), "false",
        "inactive tab not selected",
      );
    });

    T.test("sets roving tabindex", () => {
      const ctx = makeTablist(2);
      initTabsWithin(ctx.container);

      T.equal(
        ctx.tabs[0].getAttribute("tabindex"), "0", "active tab tabindex"
      );
      T.equal(
        ctx.tabs[1].getAttribute("tabindex"), "-1", "inactive tab tabindex"
      );
    });

    T.test("removes aria-current after hydration", () => {
      const ctx = makeTablist(2);
      initTabsWithin(ctx.container);

      T.ok(
        !ctx.tabs[0].hasAttribute("aria-current"),
        "aria-current removed from active tab",
      );
    });

    T.test("sets aria-controls and hydrates panels when present", () => {
      const ctx = makeTablist(2, { withPanels: true });
      initTabsWithin(ctx.container);

      const panelId0 = ctx.tabs[0].getAttribute("data-controls");
      const panelId1 = ctx.tabs[1].getAttribute("data-controls");

      T.equal(
        ctx.tabs[0].getAttribute("aria-controls"), panelId0,
        "active tab aria-controls",
      );
      T.equal(
        ctx.tabs[1].getAttribute("aria-controls"), panelId1,
        "inactive tab aria-controls",
      );

      T.equal(
        ctx.panels[0].getAttribute("role"), "tabpanel", "panel 0 role"
      );
      T.equal(
        ctx.panels[0].getAttribute("aria-labelledby"), ctx.tabs[0].id,
        "panel 0 labelledby",
      );
      T.equal(
        ctx.panels[1].getAttribute("role"), "tabpanel", "panel 1 role"
      );
      T.equal(
        ctx.panels[1].getAttribute("aria-labelledby"), ctx.tabs[1].id,
        "panel 1 labelledby",
      );
    });

    T.test("sets aria-controls even without panels in DOM", () => {
      const ctx = makeTablist(2, { withPanels: false });
      initTabsWithin(ctx.container);

      T.equal(
        ctx.tabs[0].getAttribute("aria-controls"),
        ctx.tabs[0].getAttribute("data-controls"),
        "aria-controls set from data-controls",
      );
    });

    T.test("marks tablist as initialised", () => {
      const ctx = makeTablist(1);
      initTabsWithin(ctx.container);

      T.ok(
        ctx.tablist.hasAttribute("data-tabs-initialized"), "marker set"
      );
    });

    T.test("is idempotent — does not re-initialise on second call", () => {
      const ctx = makeTablist(2);
      initTabsWithin(ctx.container);

      const selected0 = ctx.tabs[0].getAttribute("aria-selected");
      const selected1 = ctx.tabs[1].getAttribute("aria-selected");

      initTabsWithin(ctx.container);

      T.equal(
        ctx.tabs[0].getAttribute("aria-selected"), selected0,
        "selection unchanged",
      );
      T.equal(
        ctx.tabs[1].getAttribute("aria-selected"), selected1,
        "selection unchanged",
      );
    });

    T.test("arrow keys work after init", () => {
      const ctx = makeTablist(3);
      initTabsWithin(ctx.container);

      ctx.tabs[0].focus();
      pressKey(ctx.tabs[0], "ArrowRight");
      T.equal(document.activeElement, ctx.tabs[1], "focus moved to tab 1");
    });
  });
});
