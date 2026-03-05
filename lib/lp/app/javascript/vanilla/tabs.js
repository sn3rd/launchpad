/**
 * Arrow-key navigation for the Vanilla tabs pattern.
 *
 * @see https://vanillaframework.io/docs/patterns/tabs
 * @see https://www.w3.org/WAI/ARIA/apg/patterns/tabs/examples/tabs-manual/
 *
 * Expected markup (server-rendered):
 *
 *   <div role="tablist" data-js="tabs" aria-label="…">
 *     <a role="tab" aria-selected="true"  aria-controls="panel-1" href="…">…</a>
 *     <a role="tab" aria-selected="false" aria-controls="panel-2" href="…">…</a>
 *   </div>
 *   <div role="tabpanel" id="panel-1">…</div>
 *   <div role="tabpanel" id="panel-2" hidden>…</div>
 *
 * Behaviour:
 *   - ArrowLeft / ArrowRight move focus between tabs (wrapping).
 *   - Click and focus are left to the browser default (link navigation).
 *   - Requires `data-js="tabs"` on the tablist to opt in; plain
 *     `[role="tablist"]` elements without the attribute are ignored.
 *   - Runs on DOMContentLoaded for the full document.
 */

const SELECTOR = '[role="tablist"][data-js="tabs"]';

/**
 * Moves focus to the next/previous tab on arrow key press, wrapping around.
 * @param {KeyboardEvent} event
 * @param {HTMLElement[]} tabs
 */
function switchTabOnArrowPress(event, tabs) {
  const keysDirection = {
    ArrowLeft: -1,
    ArrowRight: 1,
  };
  const step = keysDirection[event.code];
  if (!step || event.target.index === undefined) return;

  const nextIndex = (event.target.index + step + tabs.length) % tabs.length;
  tabs[nextIndex].focus();
}

/**
 * Attaches arrow-key navigation events to a set of tabs.
 * @param {HTMLElement[]} tabs
 */
function attachEvents(tabs) {
  tabs.forEach((tab, index) => {
    tab.index = index;

    tab.addEventListener("keyup", (e) => {
      switchTabOnArrowPress(e, tabs);
    });
  });
}

/**
 * Returns tablist elements to initialise within a root element.
 * Includes the root itself if it matches the selector.
 * @param {HTMLElement|Document} root
 * @returns {HTMLElement[]}
 */
function findTablists(root) {
  const tablists = [...root.querySelectorAll(SELECTOR)];
  if (root.matches && root.matches(SELECTOR)) {
    tablists.unshift(root);
  }
  return tablists;
}

/**
 * Initialises tabs within a root element.
 * Skips tablists that have already been initialised (idempotent).
 * @param {HTMLElement|Document} root
 */
function initTabsWithin(root) {
  for (const tablist of findTablists(root)) {
    if (tablist.hasAttribute("data-tabs-initialized")) continue;
    tablist.setAttribute("data-tabs-initialized", "");

    const tabs = [...tablist.querySelectorAll('[role="tab"]')];
    attachEvents(tabs);
  }
}

document.addEventListener("DOMContentLoaded", () => initTabsWithin(document));
