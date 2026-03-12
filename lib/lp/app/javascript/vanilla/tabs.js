/**
 * Tabs with manual activation for the Vanilla tabs pattern.
 *
 * Server-rendered markup uses plain links with {@link aria-current}="page"
 * on the active tab and no ARIA tab roles.  This module hydrates the markup
 * on load:
 *
 *   1. Adds `role="tablist"` to the container.
 *   2. Adds `role="tab"`, `aria-selected`, and roving `tabindex` to links.
 *   3. If a panel referenced by `data-controls` exists in the DOM, adds
 *      `role="tabpanel"` and `aria-labelledby`.
 *   4. Keyboard: Arrow keys move focus, Home/End jump to first/last,
 *      Space activates (clicks) the focused tab.
 *
 * @see https://vanillaframework.io/docs/patterns/tabs
 * @see https://www.w3.org/WAI/ARIA/apg/patterns/tabs/examples/tabs-manual/
 *
 * Server-rendered (no-JS) markup:
 *
 *   <div class="p-tabs__list" data-js="tabs" aria-label="…">
 *     <a class="p-tabs__link" aria-current="page"
 *        data-controls="p-1" href="…">Tab 1</a>
 *     <a class="p-tabs__link" data-controls="p-2" href="…">Tab 2</a>
 *   </div>
 *   <div id="p-1">…</div>
 *
 * After hydration, ARIA roles, aria-selected, aria-controls,
 * roving tabindex, and role="tabpanel" are applied by this module.
 */

export const SELECTOR = '[data-js="tabs"]';

/**
 * Handles keyboard interaction within a tablist (manual activation).
 *
 * - ArrowRight / ArrowLeft move focus between tabs (wrapping).
 * - Home / End jump to the first / last tab.
 * - Space activates (clicks) the focused tab.
 *
 * @param {KeyboardEvent} event
 * @param {HTMLElement[]} tabs
 */
export function handleKeydown(event, tabs) {
  const currentIndex = tabs.indexOf(/** @type {HTMLElement} */ (event.target));
  if (currentIndex === -1) return;

  /** @type {number|undefined} */
  let nextIndex;

  switch (event.key) {
    case "ArrowRight":
      nextIndex = (currentIndex + 1) % tabs.length;
      break;
    case "ArrowLeft":
      nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
      break;
    case "Home":
      nextIndex = 0;
      break;
    case "End":
      nextIndex = tabs.length - 1;
      break;
    case " ":
      event.preventDefault();
      event.target.click();
      return;
    default:
      return;
  }

  event.preventDefault();
  tabs[nextIndex].focus();
}

/**
 * Returns elements matching the tabs selector within a root element.
 * Includes the root itself if it matches.
 * @param {HTMLElement|Document} root
 * @returns {HTMLElement[]}
 */
export function findTablists(root) {
  const tablists = [...root.querySelectorAll(SELECTOR)];
  if (root.matches?.(SELECTOR)) {
    tablists.unshift(root);
  }
  return tablists;
}

/**
 * Hydrates a single tablist: adds ARIA roles, roving tabindex,
 * and keyboard event listeners.
 * @param {HTMLElement} tablist
 */
function hydrateTablist(tablist) {
  tablist.setAttribute("role", "tablist");

  const tabs = [...tablist.querySelectorAll(".p-tabs__link")];

  tabs.forEach((tab) => {
    const isActive = tab.hasAttribute("aria-current");
    const panelId = tab.getAttribute("data-controls");

    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", isActive ? "true" : "false");
    tab.setAttribute("tabindex", isActive ? "0" : "-1");
    tab.removeAttribute("aria-current");

    if (panelId) {
      const tabId = "tab-" + panelId;
      tab.setAttribute("id", tabId);
      tab.setAttribute("aria-controls", panelId);

      const panel = document.getElementById(panelId);
      if (panel) {
        panel.setAttribute("role", "tabpanel");
        panel.setAttribute("aria-labelledby", tabId);
      }
    }
  });

  tablist.addEventListener("keydown", (e) => handleKeydown(e, tabs));
}

/**
 * Initializes tabs within a root element.
 * Skips tablists that have already been initialized (idempotent).
 * @param {HTMLElement|Document} root
 */
export function initTabsWithin(root) {
  for (const tablist of findTablists(root)) {
    if (tablist.hasAttribute("data-tabs-initialized")) continue;
    tablist.setAttribute("data-tabs-initialized", "");
    hydrateTablist(tablist);
  }
}

initTabsWithin(document);
