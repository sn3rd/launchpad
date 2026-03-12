# Vanilla JS Tests

Unit tests for the vanilla JS modules in `lib/lp/app/javascript/vanilla/`.

Tests run in **headless Firefox via Selenium**, providing a real browser DOM.
They are auto-discovered by the existing `bin/test` infrastructure, no
Python boilerplate is required.

## Running tests

```bash
# Run only vanilla JS tests
bin/test -vvct lp.app.tests.test_yuitests -t vanilla

# Run all JS tests (includes vanilla + YUI)
bin/test -vvc lp.app.tests.test_yuitests
```

## Writing a new test

Each test consists of two files: an HTML runner and a JS test script.

### 1. Create the HTML runner

Create `test_<module>.html` in this directory. It loads `runner.js`,
the module under test, and the test script:

```html
<!DOCTYPE html>
<html>
<head>
  <title>My Module Tests</title>
</head>
<body>
  <script type="text/javascript" src="runner.js"></script>
  <script type="text/javascript" src="../my_module.js"></script>
  <script type="text/javascript" src="test_my_module.js"></script>
</body>
</html>
```

The file name **must** match the `test_*.html` pattern to be auto-discovered.

### 2. Write the test script

Create `test_<module>.js`. Use the `VanillaTest` singleton exposed by
`runner.js`:

```js
VanillaTest.suite("myModule", (T) => {
  T.test("does something", () => {
    const container = T.dom(`<div>hello</div>`);

    T.equal(container.textContent, "hello", "text content matches");
    T.ok(container.parentNode, "element is in the DOM");
  });

  // Suites can be nested — names are joined with "."
  T.suite("subFeature", (T) => {
    T.test("works", () => {
      T.ok(true, "nested suite test");
    });
  });
});
```

## How it works

`runner.js` collects test results into a structure matching the format
expected by `AbstractYUITestCase.checkResults()` in
`lib/lp/testing/__init__.py`. Results are automatically written to
`window.test_results` as a JSON string when the last top-level suite
completes. The Selenium driver polls this variable and reports pass/fail
back to `bin/test`. No YUI code is involved.

## Tips

- **DOM cleanup is automatic** — any nodes appended to `document.body`
  during a test are removed after it completes (pass or fail).
- **Elements must be focusable** to test `focus()` — add `tabindex="0"` to
  elements that aren't natively focusable (e.g. `<div>`, `<a>` without
  `href`).
- **`file://` limitations** — `window.location.origin` is `null` when tests
  run as local files, so functions that depend on it (e.g. `new URL(path,
  window.location.origin)`) cannot be tested directly.
