/**
 * Minimal test runner for vanilla JS modules.
 *
 * Produces results in the format expected by
 * {@link AbstractYUITestCase.checkResults} (see lib/lp/testing/__init__.py)
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
  /** @type {Record<string, Record<string, {type: string, result?: string, message?: string}>>} */
  #results = {};

  /** @type {string[]} */
  #suiteStack = [];

  /** @type {string | null} */
  #current = null;

  /**
   * Define a test suite. All {@link test} calls inside `fn` are grouped
   * under this suite name in the results. Suites can be nested — the
   * names are joined with `"."` (e.g. `"parent.child"`).
   * @param {string} name - Human-readable suite name.
   * @param {(t: VanillaTestRunner) => void} fn - Suite body containing test definitions.
   */
  suite(name, fn) {
    this.#suiteStack.push(name);
    const fullName = this.#suiteStack.join(".");
    this.#current = fullName;
    this.#results[fullName] = { type: "testcase" };
    fn(this);
    this.#suiteStack.pop();
    this.#current = this.#suiteStack.length
      ? this.#suiteStack.join(".")
      : null;

    if (!this.#suiteStack.length) {
      this.done();
    }
  }

  /**
   * Run a single test case. If `fn` throws, the test is marked as failed
   * with the error message; otherwise it passes. Any DOM nodes appended to
   * `document.body` during the test are removed automatically.
   * @param {string} name - Test case name (must be unique within a suite).
   * @param {() => void} fn - Test function to execute.
   */
  test(name, fn) {
    const before = new Set(document.body.childNodes);
    try {
      fn();
      this.#results[this.#current][name] = {
        type: "test",
        result: "pass",
        message: "Test passed.",
      };
    } catch (e) {
      this.#results[this.#current][name] = {
        type: "test",
        result: "fail",
        message: e.message,
      };
    } finally {
      for (const node of document.body.childNodes) {
        if (!before.has(node)) node.remove();
      }
    }
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
    if (value === undefined) return "undefined";
    if (value === null) return "null";
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
    if (typeof value === "string") return JSON.stringify(value);
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
   * Called automatically when the last top-level suite completes.
   */
  done() {
    window.test_results = JSON.stringify({
      type: "complete",
      results: this.#results,
    });
  }
}

window.VanillaTest = new VanillaTestRunner();
