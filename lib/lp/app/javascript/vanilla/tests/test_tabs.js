/**
 * Tests for lib/lp/app/javascript/vanilla/tabs.js
 */

// -- Helpers --

function makeTablist(tabData) {
  const container = document.createElement("div");
  const tablist = document.createElement("div");
  tablist.setAttribute("role", "tablist");
  tablist.setAttribute("data-js", "tabs");

  tabData.forEach((t, i) => {
    const tab = document.createElement("a");
    tab.setAttribute("role", "tab");
    tab.setAttribute("tabindex", "0");
    tab.textContent = `Tab ${i}`;
    tablist.appendChild(tab);
  });

  container.appendChild(tablist);
  document.body.appendChild(container);

  // Sanity check: the tablist must match the module's SELECTOR.
  if (!tablist.matches(SELECTOR)) {
    throw new Error("makeTablist: tablist does not match SELECTOR");
  }

  return {
    container,
    tablist,
    tabs: Array.from(tablist.querySelectorAll('[role="tab"]')),
  };
}

// -- Tests --

VanillaTest.suite("tabs", (T) => {
  T.suite("switchTabOnArrowPress", (T) => {
    T.test("ArrowRight moves focus forward", () => {
      const ctx = makeTablist([{}, {}, {}]);
      ctx.tabs.forEach((tab, i) => { tab.index = i; });

      switchTabOnArrowPress({ code: "ArrowRight", target: ctx.tabs[0] }, ctx.tabs);
      T.equal(document.activeElement, ctx.tabs[1], "focus on tab 1");
    });

    T.test("ArrowLeft moves focus backward", () => {
      const ctx = makeTablist([{}, {}, {}]);
      ctx.tabs.forEach((tab, i) => { tab.index = i; });

      switchTabOnArrowPress({ code: "ArrowLeft", target: ctx.tabs[1] }, ctx.tabs);
      T.equal(document.activeElement, ctx.tabs[0], "focus on tab 0");
    });

    T.test("ArrowRight wraps from last to first", () => {
      const ctx = makeTablist([{}, {}, {}]);
      ctx.tabs.forEach((tab, i) => { tab.index = i; });

      switchTabOnArrowPress({ code: "ArrowRight", target: ctx.tabs[2] }, ctx.tabs);
      T.equal(document.activeElement, ctx.tabs[0], "focus wraps to tab 0");
    });

    T.test("ArrowLeft wraps from first to last", () => {
      const ctx = makeTablist([{}, {}, {}]);
      ctx.tabs.forEach((tab, i) => { tab.index = i; });

      switchTabOnArrowPress({ code: "ArrowLeft", target: ctx.tabs[0] }, ctx.tabs);
      T.equal(document.activeElement, ctx.tabs[2], "focus wraps to last tab");
    });

    T.test("ignores non-arrow keys", () => {
      const ctx = makeTablist([{}, {}]);
      ctx.tabs.forEach((tab, i) => { tab.index = i; });
      ctx.tabs[0].focus();

      switchTabOnArrowPress({ code: "Enter", target: ctx.tabs[0] }, ctx.tabs);
      T.equal(document.activeElement, ctx.tabs[0], "focus unchanged");
    });

    T.test("ignores target without index", () => {
      const ctx = makeTablist([{}, {}]);
      ctx.tabs[0].focus();

      switchTabOnArrowPress({ code: "ArrowRight", target: ctx.tabs[0] }, ctx.tabs);
      T.equal(document.activeElement, ctx.tabs[0], "focus unchanged");
    });
  });

  T.suite("findTablists", (T) => {
    T.test("finds tablists among descendants", () => {
      const ctx = makeTablist([{}]);
      const result = findTablists(ctx.container);
      T.equal(result.length, 1, "finds one tablist");
      T.equal(result[0], ctx.tablist, "returns the tablist element");
    });

    T.test("includes root when root itself is a tablist", () => {
      const ctx = makeTablist([{}]);
      const result = findTablists(ctx.tablist);
      T.equal(result.length, 1, "finds root tablist");
      T.equal(result[0], ctx.tablist, "returns the root element");
    });

    T.test("ignores tablists that do not match SELECTOR", () => {
      const container = document.createElement("div");
      const tablist = document.createElement("div");
      tablist.setAttribute("role", "tablist");
      container.appendChild(tablist);
      document.body.appendChild(container);

      T.ok(!tablist.matches(SELECTOR), "tablist does not match SELECTOR");
      T.equal(findTablists(container).length, 0, "no tablists found");
    });
  });

  T.suite("initTabsWithin", (T) => {
    T.test("assigns indices to tabs", () => {
      const ctx = makeTablist([{}, {}, {}]);
      initTabsWithin(ctx.container);

      T.equal(ctx.tabs[0].index, 0, "first tab index");
      T.equal(ctx.tabs[1].index, 1, "second tab index");
      T.equal(ctx.tabs[2].index, 2, "third tab index");
    });

    T.test("marks tablist as initialised", () => {
      const ctx = makeTablist([{}]);
      initTabsWithin(ctx.container);

      T.ok(ctx.tablist.hasAttribute("data-tabs-initialized"), "marker set");
    });

    T.test("is idempotent — does not re-attach on second call", () => {
      const ctx = makeTablist([{}, {}]);
      initTabsWithin(ctx.container);

      const origIndex0 = ctx.tabs[0].index;
      const origIndex1 = ctx.tabs[1].index;

      initTabsWithin(ctx.container);

      T.equal(ctx.tabs[0].index, origIndex0, "index unchanged");
      T.equal(ctx.tabs[1].index, origIndex1, "index unchanged");
    });

    T.test("arrow keys work after init", () => {
      const ctx = makeTablist([{}, {}, {}]);
      initTabsWithin(ctx.container);

      const event = new KeyboardEvent("keyup", { code: "ArrowRight" });
      ctx.tabs[0].focus();
      ctx.tabs[0].dispatchEvent(event);

      T.equal(document.activeElement, ctx.tabs[1], "focus moved to tab 1");
    });
  });
});
