/**
 * Content swap utility. Fetches HTML and swaps it into a target element.
 *
 * Trigger attributes:
 *   swap-url         – clean base URL to fetch (no query params)
 *   swap-target      – CSS selector for the target element
 *   swap-style       – "outerHTML" (default) or "innerHTML"
 *   swap-param-key   – query-param key this trigger owns (e.g. "packages-chart")
 *   swap-param-value – query-param value (e.g. "binary")
 *   swap-default     – boolean; present on triggers whose value is the
 *                      section default (their param is omitted from URLs)
 *   swap-current     – boolean; present on the currently active trigger
 *   href             – no-JS fallback URL (server-baked params)
 *
 * When swap-param-key is set, JS collects params from the clicked
 * trigger and all active tabs ([swap-current]) in the DOM, building
 * a query string appended to the clean swap-url.  Tabs marked
 * swap-default are omitted for clean URLs.
 *
 * Dispatches `swap:afterSwap` on the target (or the newly inserted root
 * for outerHTML swaps).
 */

export const ATTRIBUTES = {
  /**
   * The clean base URL to fetch (no query params).
   */
  swapUrl: "swap-url",
  /**
   * The CSS selector for the target element to swap into.
   */
  swapTarget: "swap-target",
  /**
   * The key of the parameter to collect from the trigger.
   */
  swapParamKey: "swap-param-key",
  /**
   * The value of the parameter to collect from the trigger.
   */
  swapParamValue: "swap-param-value",
  /**
   * Whether the trigger is the default value.
   */
  isSwapDefault: "swap-default",
  /**
   * Whether the trigger is the active tab.
   */
  isSwapCurrent: "swap-current",
  /**
   * The style of the swap (innerHTML or outerHTML).
   */
  swapStyle: "swap-style",
};

export const SELECTORS = {
  anchorElement: `a[${ATTRIBUTES.swapUrl}]`,
};

export const EVENTS = {
  afterSwap: "swap:afterSwap",
};

export const CLASSES = {
  swapLoading: "swap-loading",
};

/** @type {Map<HTMLElement, AbortController>} */
const pendingSwaps = new Map();

/**
 * Returns the current page search params with the trigger's swap param
 * applied. If the trigger carries `swap-default`, its key is removed;
 * otherwise its key/value is set.
 * @param {HTMLElement} trigger the clicked trigger element
 * @returns {URLSearchParams} the merged parameters
 */
export function getSwapSearchParams(trigger) {
  const params = new URLSearchParams(location.search);
  const triggerParamKey = trigger.getAttribute(ATTRIBUTES.swapParamKey);
  const triggerParamValue = trigger.getAttribute(ATTRIBUTES.swapParamValue);
  const triggerIsDefaultValue = trigger.hasAttribute(ATTRIBUTES.isSwapDefault);

  if (triggerParamKey) {
    if (triggerIsDefaultValue) {
      params.delete(triggerParamKey);
    } else {
      params.set(triggerParamKey, triggerParamValue);
    }
  }

  return params;
}

/**
 * Fetches HTML and swaps it into `target`.
 * Aborts any in-flight request for the same target.
 * @param {string} url
 * @param {HTMLElement} target
 * @param {"innerHTML" | "outerHTML"} swapStyle
 */
export async function performSwap(url, target, swapStyle) {
  const prev = pendingSwaps.get(target);
  if (prev) {
    prev.abort();
  }
  const controller = new AbortController();
  pendingSwaps.set(target, controller);

  target.classList.add(CLASSES.swapLoading);
  target.setAttribute("aria-busy", "true");

  try {
    const res = await fetch(url, { signal: controller.signal });
    if (!res.ok) throw new Error(`swap: ${res.status}`);

    const html = await res.text();
    target[swapStyle] = html;

    let eventTarget = target;
    if (swapStyle === "outerHTML") {
      eventTarget = document.getElementById(target.id) ?? document;
    }
    eventTarget.dispatchEvent(
      new CustomEvent(EVENTS.afterSwap, { bubbles: true }),
    );
  } finally {
    if (pendingSwaps.get(target) === controller) {
      pendingSwaps.delete(target);
      target.classList.remove(CLASSES.swapLoading);
      target.removeAttribute("aria-busy");
    }
  }
}

/**
 * Reads swap config from a trigger's attributes, fetches content,
 * swaps the target, and updates browser history.
 * @param {HTMLElement} trigger
 */
export async function handleSwapTrigger(trigger) {
  const url = trigger.getAttribute(ATTRIBUTES.swapUrl);
  const targetSelector = trigger.getAttribute(ATTRIBUTES.swapTarget);
  if (!url || !targetSelector) {
    return;
  }

  const target = document.querySelector(targetSelector);
  if (!target) {
    return;
  }

  const swapStyle = trigger.getAttribute(ATTRIBUTES.swapStyle) ?? "outerHTML";
  const swapParams = getSwapSearchParams(trigger);

  const parsed = new URL(url, location.href);
  parsed.search = swapParams.toString();
  const fetchUrl = parsed.pathname + parsed.search;
  await performSwap(fetchUrl, target, swapStyle);
  // recollect swap params after swap to take into account potential other
  // changes in the URL since the perform swap call started
  const swapParamsAfterSwap = getSwapSearchParams(trigger);
  const pageUrl = new URL(location.href);
  pageUrl.search = swapParamsAfterSwap.toString();
  history.replaceState(null, "", pageUrl);
}

// -- Event adapters --

// Anchor: click
document.addEventListener("click", async (e) => {
  const trigger = e.target.closest(SELECTORS.anchorElement);
  if (!trigger) return;
  e.preventDefault();
  if (trigger.hasAttribute(ATTRIBUTES.isSwapCurrent)) return;
  try {
    await handleSwapTrigger(trigger);
  } catch (err) {
    if (err.name !== "AbortError") console.error(err);
  }
});
