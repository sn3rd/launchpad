/**
 * Minimal test runner for vanilla JS modules.
 *
 * Produces results in the format expected by
 * {@link AbstractYUITestCase.checkResults} (see [lib/lp/testing/__init__.py](../../../../testing/__init__.py))
 * so that tests are auto-discovered and run by the existing Selenium-based
 * `bin/test` infrastructure.
 *
 * A singleton instance is exposed as `window.VanillaTest`.
 *
 * @example
 *   VanillaTest.suite("MyModule", (T) => {
 *     T.test("does something", () => { ... });
 *   });
 *
 * @see README.md for full documentation on writing and running tests.
 */
class VanillaTestRunner {
  /**
   * Nested results object keyed by suite name → test name.
   *
   * @example
   *   {
   *     "MySuite": {
   *       type: "testcase",              // suite marker
   *       "does something": {
   *         type: "test",                // test marker
   *         result: "pass",              // "pass" | "fail"
   *         message: "Test passed.",     // human-readable status or error
   *       },
   *     },
   *   }
   *
   * @typedef {{type: "test", result: "pass" | "fail", message: string}} TestResult
   * @typedef {{type: "testcase"} & Record<string, TestResult>} SuiteResult — suite marker plus test name to result.
   * @type {Record<string, SuiteResult>} Map of suite name to suite result.
   */
  #results = {};

  /** @type {string[]} */
  #suiteStack = [];

  /** @type {string | null} */
  get #current() {
    return this.#suiteStack.length ? this.#suiteStack.join(".") : null;
  }

  /** @type {Array<{name: string, suiteName: string, fn: Function}>} */
  #queue = [];

  /**
   * Define a test suite. All {@link test} calls inside `fn` are grouped
   * under this suite name in the results. Suites can be nested — the
   * names are joined with `"."` (e.g. `"parent.child"`).
   *
   * When the outermost suite finishes registering tests, the queued
   * tests are executed sequentially (awaiting async tests) so that
   * each test gets a clean DOM snapshot.
   *
   * @param {string} name - Human-readable suite name.
   * @param {(t: VanillaTestRunner) => void} fn - Suite body containing test definitions.
   */
  suite(name, fn) {
    this.#suiteStack.push(name);
    this.#results[this.#current] = { type: "testcase" };
    fn(this);
    this.#suiteStack.pop();

    if (!this.#suiteStack.length) {
      this.#runQueue();
    }
  }

  /**
   * Register a single test case. The test function is queued and
   * executed later (when the outermost suite completes) so that
   * async tests run sequentially with proper DOM isolation.
   *
   * The `document.body` is saved before each test and fully restored
   * afterward, so any DOM additions, modifications, or removals are
   * reverted.
   *
   * Supports async test functions — if `fn` returns a promise, the
   * runner awaits it before proceeding to the next test.
   *
   * @param {string} name - Test case name (must be unique within a suite).
   * @param {() => void | Promise<void>} fn - Test function to execute.
   */
  test(name, fn) {
    const suiteName = this.#current;
    if (this.#queue.some((t) => t.suiteName === suiteName && t.name === name)) {
      throw new Error(
        `Duplicate test name "${name}" in suite "${suiteName}".`
      );
    }
    this.#queue.push({ name, suiteName, fn });
  }

  /**
   * Process queued tests sequentially. Each test's DOM is saved
   * before execution and restored afterward, ensuring full isolation.
   */
  async #runQueue() {
    for (const { name, suiteName, fn } of this.#queue) {
      const snapshot = document.body.innerHTML;
      const savedUrl = location.pathname;
      try {
        await fn();
        this.#results[suiteName][name] = {
          type: "test",
          result: "pass",
          message: "Test passed.",
        };
      } catch (e) {
        this.#results[suiteName][name] = {
          type: "test",
          result: "fail",
          message: e.message,
        };
      } finally {
        document.body.innerHTML = snapshot;
        history.replaceState(null, "", savedUrl);
      }
    }
    this.done();
  }

  /**
   * Assert strict equality (`===`). Throws if `actual !== expected`.
   * @param {*} actual - The value produced by the code under test.
   * @param {*} expected - The expected value.
   * @param {string | undefined} [msg] - Optional label included in the error on failure.
   */
  equal(actual, expected, msg) {
    if (actual !== expected) {
      throw new Error(
        `${msg || "equal"}: expected ${this.#fmt(expected)}, got ${this.#fmt(actual)}`
      );
    }
  }

  #fmt(value) {
    if (value instanceof Element) {
      const attrs = Array.from(value.attributes)
        .map((a) => ` ${a.name}="${a.value}"`)
        .join("");
      const tag = value.tagName.toLowerCase();
      const text = value.textContent.trim();
      const body = text ? text.slice(0, 40) : "";
      return body
        ? `<${tag}${attrs}>${body}</${tag}>`
        : `<${tag}${attrs}>`;
    }
    return String(value);
  }

  /**
   * Assert that `val` is truthy. Throws if `!val`.
   * @param {*} val - The value to check.
   * @param {string | undefined} [msg] - Optional message shown on failure.
   */
  ok(val, msg) {
    if (!val) {
      throw new Error(msg || `Expected truthy value, got ${val}`);
    }
  }

  /**
   * Create a container element from an HTML string and append it to
   * `document.body`. The container is removed automatically after the
   * test completes.
   * @param {string} html - HTML markup to inject.
   * @returns {HTMLDivElement} The wrapper element containing the parsed HTML.
   */
  dom(html) {
    const container = document.createElement("div");
    container.innerHTML = html;
    document.body.appendChild(container);
    return container;
  }

  /**
   * Finalize and report results to the Selenium test harness.
   * Writes a JSON string to `window.test_results` in the format expected
   * by `AbstractYUITestCase.checkResults()`.
   *
   * Called automatically when the test queue finishes processing.
   */
  done() {
    window.test_results = JSON.stringify({
      type: "complete",
      results: this.#results,
    });
  }
}

window.VanillaTest = new VanillaTestRunner();
