/**
 * Tests for lib/lp/app/javascript/vanilla/swap.js
 */
import {
  SELECTORS,
  CLASSES,
  getSwapSearchParams,
  performSwap,
  handleSwapTrigger,
  ATTRIBUTES,
} from "../swap.js";

// -- Helpers --

function makeSwapFixture(opts) {
  history.replaceState(null, "", location.pathname);
  for (const el of document.querySelectorAll(`[${ATTRIBUTES.isSwapCurrent}]`)) {
    el.removeAttribute(ATTRIBUTES.isSwapCurrent);
  }

  opts = opts || {};
  const swapUrl = opts.swapUrl || "/swap";

  const container = document.createElement("div");

  const target = document.createElement("div");
  target.id = "target";
  target.innerHTML = opts.initialHTML || "<p>old</p>";
  container.appendChild(target);

  const trigger = document.createElement("a");
  trigger.setAttribute(ATTRIBUTES.swapUrl, swapUrl);
  trigger.setAttribute(ATTRIBUTES.swapTarget, "#target");
  if (opts.swapStyle) {
    trigger.setAttribute(ATTRIBUTES.swapStyle, opts.swapStyle);
  }
  if (opts.href) {
    trigger.setAttribute("href", opts.href);
  }
  if (opts.swapParam) {
    const [key, value] = opts.swapParam.split("=");
    trigger.setAttribute(ATTRIBUTES.swapParamKey, key);
    trigger.setAttribute(ATTRIBUTES.swapParamValue, value);
  }
  if (opts.swapDefault) {
    trigger.setAttribute(ATTRIBUTES.isSwapDefault, "");
  }
  trigger.textContent = "click";
  container.appendChild(trigger);

  document.body.appendChild(container);

  return { container, target, trigger };
}

function mockFetch(html) {
  const orig = window.fetch;
  window.fetch = () =>
    Promise.resolve({
      ok: true,
      text: () => Promise.resolve(html),
    });
  return () => { window.fetch = orig; };
}

/**
 * Creates a fake active tab element in the DOM with the given swap-param
 * (as "key=value") and optionally swap-default.
 */
function makeActiveTab(swapParam, isDefault) {
  const tab = document.createElement("a");
  tab.setAttribute(ATTRIBUTES.isSwapCurrent, "");
  const [key, value] = swapParam.split("=");
  tab.setAttribute(ATTRIBUTES.swapParamKey, key);
  tab.setAttribute(ATTRIBUTES.swapParamValue, value);
  if (isDefault) {
    tab.setAttribute(ATTRIBUTES.isSwapDefault, "");
  }
  document.body.appendChild(tab);
  return tab;
}

// -- Tests --

VanillaTest.suite("swap", (T) => {
  T.suite("SELECTORS.anchorElement", (T) => {
    T.test("matches anchor with swap-url", () => {
      const ctx = makeSwapFixture();
      T.ok(ctx.trigger.matches(SELECTORS.anchorElement), "trigger matches selector");
    });

    T.test("does not match elements without swap-url", () => {
      const el = document.createElement("a");
      T.ok(!el.matches(SELECTORS.anchorElement), "plain element does not match");
    });
  });

  T.suite("getSwapSearchParams", (T) => {
    T.test("returns clicked param when no active tabs in DOM", () => {
      const trigger = document.createElement("a");
      trigger.setAttribute(ATTRIBUTES.swapParamKey, "packages-chart");
      trigger.setAttribute(ATTRIBUTES.swapParamValue, "binary");
      document.body.appendChild(trigger);

      const params = getSwapSearchParams(trigger);
      T.equal(params.get("packages-chart"), "binary", "clicked param set");
      T.equal(params.toString(), "packages-chart=binary", "only one param");
    });

    T.test("preserves other params already in the URL", () => {
      history.replaceState(null, "", location.pathname + "?packages-list=my-uploads");

      const trigger = document.createElement("a");
      trigger.setAttribute(ATTRIBUTES.swapParamKey, "packages-chart");
      trigger.setAttribute(ATTRIBUTES.swapParamValue, "binary");
      document.body.appendChild(trigger);

      const params = getSwapSearchParams(trigger);
      T.equal(params.get("packages-chart"), "binary", "clicked param");
      T.equal(params.get("packages-list"), "my-uploads", "other section param");
    });

    T.test("preserves multiple params already in the URL", () => {
      history.replaceState(null, "", location.pathname + "?packages-list=my-uploads&builds-view=failed");

      const trigger = document.createElement("a");
      trigger.setAttribute(ATTRIBUTES.swapParamKey, "packages-chart");
      trigger.setAttribute(ATTRIBUTES.swapParamValue, "binary");
      document.body.appendChild(trigger);

      const params = getSwapSearchParams(trigger);
      T.equal(params.get("packages-chart"), "binary", "clicked param");
      T.equal(params.get("packages-list"), "my-uploads", "section 2");
      T.equal(params.get("builds-view"), "failed", "section 3");
    });

    T.test("clicked param overrides same-key active tab", () => {
      // Active tab in same section shows "source", user clicks "binary"
      makeActiveTab("packages-chart=source");

      const trigger = document.createElement("a");
      trigger.setAttribute(ATTRIBUTES.swapParamKey, "packages-chart");
      trigger.setAttribute(ATTRIBUTES.swapParamValue, "binary");
      document.body.appendChild(trigger);

      const params = getSwapSearchParams(trigger);
      T.equal(
        params.get("packages-chart"), "binary",
        "clicked value overrides active tab"
      );
    });

    T.test("ignores inactive tabs", () => {
      // Tab without swap-current should be ignored
      const inactive = document.createElement("a");
      inactive.setAttribute(ATTRIBUTES.swapParamKey, "packages-list");
      inactive.setAttribute(ATTRIBUTES.swapParamValue, "my-uploads");
      document.body.appendChild(inactive);

      const trigger = document.createElement("a");
      trigger.setAttribute(ATTRIBUTES.swapParamKey, "packages-chart");
      trigger.setAttribute(ATTRIBUTES.swapParamValue, "binary");
      document.body.appendChild(trigger);

      const params = getSwapSearchParams(trigger);
      T.equal(params.get("packages-chart"), "binary", "clicked param set");
      T.ok(!params.has("packages-list"), "inactive tab ignored");
    });

    T.test("omits default active tab from params", () => {
      makeActiveTab("packages-list=latest", true);

      const trigger = document.createElement("a");
      trigger.setAttribute(ATTRIBUTES.swapParamKey, "packages-chart");
      trigger.setAttribute(ATTRIBUTES.swapParamValue, "binary");
      document.body.appendChild(trigger);

      const params = getSwapSearchParams(trigger);
      T.equal(params.get("packages-chart"), "binary", "clicked param set");
      T.ok(!params.has("packages-list"), "default active tab omitted");
    });

    T.test("omits clicked param when trigger has swap-default", () => {
      const trigger = document.createElement("a");
      trigger.setAttribute(ATTRIBUTES.swapParamKey, "packages-chart");
      trigger.setAttribute(ATTRIBUTES.swapParamValue, "source");
      trigger.setAttribute(ATTRIBUTES.isSwapDefault, "");
      document.body.appendChild(trigger);

      const params = getSwapSearchParams(trigger);
      T.ok(!params.has("packages-chart"), "default clicked param omitted");
      T.equal(params.toString(), "", "no params");
    });

    T.test("preserves other URL params and omits default clicked", () => {
      // "my-uploads" is already in the URL from another section
      history.replaceState(null, "", location.pathname + "?packages-list=my-uploads");

      const trigger = document.createElement("a");
      trigger.setAttribute(ATTRIBUTES.swapParamKey, "packages-chart");
      trigger.setAttribute(ATTRIBUTES.swapParamValue, "source");
      trigger.setAttribute(ATTRIBUTES.isSwapDefault, "");
      document.body.appendChild(trigger);

      const params = getSwapSearchParams(trigger);
      T.ok(!params.has("packages-chart"), "default clicked omitted");
      T.equal(params.get("packages-list"), "my-uploads", "non-default kept");
    });

    T.test("all defaults results in empty params", () => {
      // Both sections at their defaults
      makeActiveTab("packages-list=latest", true);

      const trigger = document.createElement("a");
      trigger.setAttribute(ATTRIBUTES.swapParamKey, "packages-chart");
      trigger.setAttribute(ATTRIBUTES.swapParamValue, "source");
      trigger.setAttribute(ATTRIBUTES.isSwapDefault, "");
      document.body.appendChild(trigger);

      const params = getSwapSearchParams(trigger);
      T.equal(params.toString(), "", "no params when all are defaults");
    });

    T.test("returns empty params when trigger has no swap-param-key", () => {
      const trigger = document.createElement("a");
      document.body.appendChild(trigger);

      const params = getSwapSearchParams(trigger);
      T.equal(params.toString(), "", "no params without key");
    });

    T.test("handles trigger with swap-param-key but no swap-param-value", () => {
      const trigger = document.createElement("a");
      trigger.setAttribute(ATTRIBUTES.swapParamKey, "packages-chart");
      // no swap-param-value set
      document.body.appendChild(trigger);

      const params = getSwapSearchParams(trigger);
      T.ok(params.has("packages-chart"), "key is present");
      T.equal(params.get("packages-chart"), "null", "missing value becomes null string");
    });

    T.test("skips active tab with swap-current but no swap-param-key", () => {
      // Active tab missing its param key — should not pollute params
      const broken = document.createElement("a");
      broken.setAttribute(ATTRIBUTES.isSwapCurrent, "");
      // no swap-param-key
      document.body.appendChild(broken);

      const trigger = document.createElement("a");
      trigger.setAttribute(ATTRIBUTES.swapParamKey, "packages-chart");
      trigger.setAttribute(ATTRIBUTES.swapParamValue, "binary");
      document.body.appendChild(trigger);

      const params = getSwapSearchParams(trigger);
      T.equal(params.toString(), "packages-chart=binary", "broken tab skipped");
    });

    T.test("preserves URL param with empty value", () => {
      history.replaceState(null, "", location.pathname + "?packages-list=");

      const trigger = document.createElement("a");
      trigger.setAttribute(ATTRIBUTES.swapParamKey, "packages-chart");
      trigger.setAttribute(ATTRIBUTES.swapParamValue, "binary");
      document.body.appendChild(trigger);

      const params = getSwapSearchParams(trigger);
      T.equal(params.get("packages-chart"), "binary", "clicked param set");
      T.ok(params.has("packages-list"), "URL key is present");
      T.equal(params.get("packages-list"), "", "empty value preserved");
    });

    T.test("trigger with swap-current does not double-add its param", () => {
      // Trigger is itself marked as active (swap-current) — its param
      // should appear once from the trigger block, not again from the loop.
      const trigger = document.createElement("a");
      trigger.setAttribute(ATTRIBUTES.isSwapCurrent, "");
      trigger.setAttribute(ATTRIBUTES.swapParamKey, "packages-chart");
      trigger.setAttribute(ATTRIBUTES.swapParamValue, "binary");
      document.body.appendChild(trigger);

      const params = getSwapSearchParams(trigger);
      T.equal(params.toString(), "packages-chart=binary", "param appears once");
    });
  });

  T.suite("handleSwapTrigger", (T) => {
    T.test("swaps content into target", async () => {
      const ctx = makeSwapFixture();
      const restore = mockFetch('<div id="target"><p>new</p></div>');

      try {
        await handleSwapTrigger(ctx.trigger);
        const newTarget = document.getElementById("target");
        T.equal(newTarget.innerHTML, "<p>new</p>", "content swapped");
      } finally {
        restore();
      }
    });

    T.test("does nothing when target selector is missing", async () => {
      const ctx = makeSwapFixture();
      ctx.trigger.removeAttribute(ATTRIBUTES.swapTarget);
      const restore = mockFetch("<p>new</p>");

      try {
        await handleSwapTrigger(ctx.trigger);
        T.equal(ctx.target.innerHTML, "<p>old</p>", "content unchanged");
      } finally {
        restore();
      }
    });

    T.test("does nothing when target element not found", async () => {
      const ctx = makeSwapFixture();
      ctx.trigger.setAttribute(ATTRIBUTES.swapTarget, "#nope");
      const restore = mockFetch("<p>new</p>");

      try {
        await handleSwapTrigger(ctx.trigger);
        T.equal(ctx.target.innerHTML, "<p>old</p>", "content unchanged");
      } finally {
        restore();
      }
    });

    T.test("builds fetch URL from swap-url and existing URL params", async () => {
      const ctx = makeSwapFixture({
        swapUrl: "/fetch-base",
        swapParam: "packages-chart=binary",
      });
      history.replaceState(null, "", location.pathname + "?packages-list=my-uploads");

      let capturedUrl;
      const orig = window.fetch;
      window.fetch = (url) => {
        capturedUrl = url;
        return Promise.resolve({
          ok: true,
          text: () => Promise.resolve('<div id="target"><p>new</p></div>'),
        });
      };

      try {
        await handleSwapTrigger(ctx.trigger);
        T.ok(
          capturedUrl.startsWith("/fetch-base?"),
          "uses clean base path"
        );
        T.ok(
          capturedUrl.includes("packages-chart=binary"),
          "has clicked param"
        );
        T.ok(
          capturedUrl.includes("packages-list=my-uploads"),
          "has other section param"
        );
      } finally {
        window.fetch = orig;
      }
    });

    T.test("omits default params from fetch URL", async () => {
      const ctx = makeSwapFixture({
        swapUrl: "/fetch-base",
        swapParam: "packages-chart=binary",
      });
      makeActiveTab("packages-list=latest", true);

      let capturedUrl;
      const orig = window.fetch;
      window.fetch = (url) => {
        capturedUrl = url;
        return Promise.resolve({
          ok: true,
          text: () => Promise.resolve('<div id="target"><p>new</p></div>'),
        });
      };

      try {
        await handleSwapTrigger(ctx.trigger);
        T.ok(
          capturedUrl.includes("packages-chart=binary"),
          "has non-default param"
        );
        T.ok(
          !capturedUrl.includes("packages-list"),
          "default param omitted"
        );
      } finally {
        window.fetch = orig;
      }
    });

    T.test("uses swap-url directly when no swap-param", async () => {
      const ctx = makeSwapFixture({ swapUrl: "/plain-url" });
      // No swap-param-key set on trigger

      let capturedUrl;
      const orig = window.fetch;
      window.fetch = (url) => {
        capturedUrl = url;
        return Promise.resolve({
          ok: true,
          text: () => Promise.resolve('<div id="target"><p>new</p></div>'),
        });
      };

      try {
        await handleSwapTrigger(ctx.trigger);
        T.equal(capturedUrl, "/plain-url", "swap-url used as-is");
      } finally {
        window.fetch = orig;
      }
    });

    T.test("does nothing when swap-url is missing", async () => {
      const ctx = makeSwapFixture();
      ctx.trigger.removeAttribute(ATTRIBUTES.swapUrl);
      const restore = mockFetch("<p>new</p>");

      try {
        await handleSwapTrigger(ctx.trigger);
        T.equal(ctx.target.innerHTML, "<p>old</p>", "content unchanged");
      } finally {
        restore();
      }
    });

    T.test("clean URL when all params are defaults", async () => {
      const ctx = makeSwapFixture({
        swapUrl: "/fetch-base",
        swapParam: "packages-chart=source",
        swapDefault: true,
      });
      makeActiveTab("packages-list=latest", true);

      let capturedUrl;
      const orig = window.fetch;
      window.fetch = (url) => {
        capturedUrl = url;
        return Promise.resolve({
          ok: true,
          text: () => Promise.resolve('<div id="target"><p>new</p></div>'),
        });
      };

      try {
        await handleSwapTrigger(ctx.trigger);
        T.equal(capturedUrl, "/fetch-base", "no query string appended");
      } finally {
        window.fetch = orig;
      }
    });

    T.test("preserves non-swap query params in URL", async () => {
      const ctx = makeSwapFixture({
        swapUrl: "/fetch-base",
        swapParam: "packages-chart=binary",
      });

      // Simulate existing non-swap query params in the page URL.
      history.replaceState(null, "", location.pathname + "?extra=keep");

      let capturedUrl;
      const orig = window.fetch;
      window.fetch = (url) => {
        capturedUrl = url;
        return Promise.resolve({
          ok: true,
          text: () => Promise.resolve('<div id="target"><p>new</p></div>'),
        });
      };

      try {
        await handleSwapTrigger(ctx.trigger);
        T.ok(
          capturedUrl.includes("packages-chart=binary"),
          "has swap param"
        );
        T.ok(
          capturedUrl.includes("extra=keep"),
          "preserved non-swap param in fetch URL"
        );
      } finally {
        window.fetch = orig;
        history.replaceState(null, "", location.pathname);
      }
    });

    T.test("concurrent swaps to different targets preserve both params in URL", async () => {
      // Two swap sections with separate targets.
      history.replaceState(null, "", location.pathname);

      const container = document.createElement("div");

      const target1 = document.createElement("div");
      target1.id = "target-a";
      target1.innerHTML = "<p>old-a</p>";
      container.appendChild(target1);

      const target2 = document.createElement("div");
      target2.id = "target-b";
      target2.innerHTML = "<p>old-b</p>";
      container.appendChild(target2);

      const trigger1 = document.createElement("a");
      trigger1.setAttribute(ATTRIBUTES.swapUrl, "/fetch-a");
      trigger1.setAttribute(ATTRIBUTES.swapTarget, "#target-a");
      trigger1.setAttribute(ATTRIBUTES.swapParamKey, "packages-chart");
      trigger1.setAttribute(ATTRIBUTES.swapParamValue, "binary");
      container.appendChild(trigger1);

      const trigger2 = document.createElement("a");
      trigger2.setAttribute(ATTRIBUTES.swapUrl, "/fetch-b");
      trigger2.setAttribute(ATTRIBUTES.swapTarget, "#target-b");
      trigger2.setAttribute(ATTRIBUTES.swapParamKey, "packages-list");
      trigger2.setAttribute(ATTRIBUTES.swapParamValue, "my-uploads");
      container.appendChild(trigger2);

      document.body.appendChild(container);

      // Control when each fetch resolves.
      let resolveFetch1;
      let resolveFetch2;
      const orig = window.fetch;
      window.fetch = (url) => {
        if (url.startsWith("/fetch-a")) {
          return new Promise((resolve) => {
            resolveFetch1 = () =>
              resolve({
                ok: true,
                text: () =>
                  Promise.resolve('<div id="target-a"><p>new-a</p></div>'),
              });
          });
        }
        return new Promise((resolve) => {
          resolveFetch2 = () =>
            resolve({
              ok: true,
              text: () =>
                Promise.resolve('<div id="target-b"><p>new-b</p></div>'),
            });
        });
      };

      try {
        // Request 1 starts (packages-chart=binary), stays pending.
        const swap1 = handleSwapTrigger(trigger1);
        // Yield so fetch1 is called and resolveFetch1 is assigned.
        await new Promise((r) => setTimeout(r, 0));

        // Request 2 starts (packages-list=my-uploads) and finishes.
        const swap2 = handleSwapTrigger(trigger2);
        await new Promise((r) => setTimeout(r, 0));
        resolveFetch2();
        await swap2;

        // Request 1 finishes after request 2.
        resolveFetch1();
        await swap1;

        const search = new URLSearchParams(location.search);
        T.equal(
          search.get("packages-chart"), "binary",
          "request 1 param in URL"
        );
        T.equal(
          search.get("packages-list"), "my-uploads",
          "request 2 param in URL"
        );
      } finally {
        window.fetch = orig;
        history.replaceState(null, "", location.pathname);
      }
    });

    T.test("updates URL via replaceState", async () => {
      const ctx = makeSwapFixture({
        swapUrl: "/fetch-base",
        swapParam: "packages-chart=binary",
      });
      const restore = mockFetch('<div id="target"><p>new</p></div>');

      try {
        await handleSwapTrigger(ctx.trigger);
        const search = new URLSearchParams(location.search);
        T.equal(search.get("packages-chart"), "binary", "param written to URL");
      } finally {
        restore();
      }
    });
  });

  T.suite("click handler", (T) => {
    T.test("skips swap when trigger has swap-current", async () => {
      const ctx = makeSwapFixture();
      ctx.trigger.setAttribute(ATTRIBUTES.isSwapCurrent, "");

      let fetched = false;
      const orig = window.fetch;
      window.fetch = () => {
        fetched = true;
        return Promise.resolve({
          ok: true,
          text: () => Promise.resolve('<div id="target"><p>new</p></div>'),
        });
      };

      try {
        ctx.trigger.click();
        // Allow any microtask from the click handler to settle.
        await new Promise((r) => setTimeout(r, 0));
        T.ok(!fetched, "no fetch when trigger is already active");
        T.equal(ctx.target.innerHTML, "<p>old</p>", "content unchanged");
      } finally {
        window.fetch = orig;
      }
    });

    T.test("still prevents default navigation on swap-current trigger", async () => {
      const ctx = makeSwapFixture({
        href: "/some-page",
      });
      ctx.trigger.setAttribute(ATTRIBUTES.isSwapCurrent, "");

      let defaultPrevented = false;
      // Listen on document so we run after swap.js's handler has
      // already called preventDefault().
      document.addEventListener("click", (e) => {
        defaultPrevented = e.defaultPrevented;
      }, { once: true });

      ctx.trigger.click();
      await new Promise((r) => setTimeout(r, 0));
      T.ok(defaultPrevented, "default navigation prevented");
    });
  });

  T.suite("performSwap", (T) => {
    T.test("swaps innerHTML", async () => {
      const ctx = makeSwapFixture();
      const restore = mockFetch("<p>new</p>");

      try {
        await performSwap("/test", ctx.target, "innerHTML");
        T.equal(ctx.target.innerHTML, "<p>new</p>", "content swapped");
      } finally {
        restore();
      }
    });

    T.test("swaps outerHTML", async () => {
      const ctx = makeSwapFixture();
      const restore = mockFetch('<div id="target"><p>replaced</p></div>');

      try {
        await performSwap("/test", ctx.target, "outerHTML");
        const newTarget = document.getElementById("target");
        T.ok(newTarget, "replacement element exists");
        T.equal(newTarget.innerHTML, "<p>replaced</p>", "content replaced");
      } finally {
        restore();
      }
    });

    T.test("dispatches swap:afterSwap event on target for innerHTML", async () => {
      const ctx = makeSwapFixture();
      const restore = mockFetch("<p>new</p>");

      let eventTarget = null;
      ctx.target.addEventListener("swap:afterSwap", (e) => {
        eventTarget = e.target;
      });

      try {
        await performSwap("/test", ctx.target, "innerHTML");
        T.equal(eventTarget, ctx.target, "event fired on target");
      } finally {
        restore();
      }
    });

    T.test("dispatches swap:afterSwap on new root for outerHTML", async () => {
      const ctx = makeSwapFixture();
      const restore = mockFetch('<div id="target"><p>new</p></div>');

      let eventTarget = null;
      document.addEventListener("swap:afterSwap", (e) => {
        eventTarget = e.target;
      }, { once: true });

      try {
        await performSwap("/test", ctx.target, "outerHTML");
        T.ok(eventTarget, "event fired");
        T.equal(eventTarget.id, "target", "event target is the new root");
        T.equal(
          eventTarget, document.getElementById("target"),
          "event target is the swapped-in element"
        );
      } finally {
        restore();
      }
    });

    T.test("does not modify history", async () => {
      const ctx = makeSwapFixture();
      const restore = mockFetch("<p>new</p>");
      const before = history.length;

      try {
        await performSwap("/test", ctx.target, "innerHTML");
        T.equal(history.length, before, "no history entry added");
      } finally {
        restore();
      }
    });

    T.test("sets aria-busy and swap-loading during fetch", async () => {
      const ctx = makeSwapFixture();
      let busyDuringFetch = false;
      let loadingDuringFetch = false;

      const orig = window.fetch;
      window.fetch = () => {
        busyDuringFetch = ctx.target.getAttribute("aria-busy") === "true";
        loadingDuringFetch = ctx.target.classList.contains(CLASSES.swapLoading);
        return Promise.resolve({
          ok: true,
          text: () => Promise.resolve(""),
        });
      };

      try {
        await performSwap("/test", ctx.target, "innerHTML");
        T.ok(busyDuringFetch, "aria-busy was set during fetch");
        T.ok(loadingDuringFetch, "swap-loading was set during fetch");
      } finally {
        window.fetch = orig;
      }
    });

    T.test("removes aria-busy and swap-loading after fetch", async () => {
      const ctx = makeSwapFixture();
      const restore = mockFetch("");

      try {
        await performSwap("/test", ctx.target, "innerHTML");
        T.ok(!ctx.target.hasAttribute("aria-busy"), "aria-busy removed");
        T.ok(
          !ctx.target.classList.contains(CLASSES.swapLoading),
          "swap-loading removed"
        );
      } finally {
        restore();
      }
    });

    T.test("aborts previous request for the same target", async () => {
      const ctx = makeSwapFixture();
      let aborted = false;
      let callCount = 0;

      const orig = window.fetch;
      window.fetch = (url, opts) => {
        callCount++;
        if (callCount === 1) {
          return new Promise((resolve, reject) => {
            opts.signal.addEventListener("abort", () => {
              aborted = true;
              reject(new DOMException("Aborted", "AbortError"));
            });
          });
        }
        return Promise.resolve({
          ok: true,
          text: () => Promise.resolve("<p>second</p>"),
        });
      };

      try {
        const first = performSwap("/first", ctx.target, "innerHTML")
          .catch(() => {});
        const second = performSwap("/second", ctx.target, "innerHTML");
        await Promise.all([first, second]);
        T.ok(aborted, "first request was aborted");
        T.equal(ctx.target.innerHTML, "<p>second</p>", "second swap applied");
      } finally {
        window.fetch = orig;
      }
    });
  });

});
